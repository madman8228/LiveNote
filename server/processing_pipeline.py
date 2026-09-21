"""Restartable local transcription pipeline for completed LiveNote sessions."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:
    from asr import ASR_CHUNK_OVERLAP_SECONDS, ASR_CHUNK_SECONDS, AsrError, _probe_duration_seconds, _validate_audio_input, transcribe_range
    from reconstruction import ReconstructionError, reconstruct_segment, reconstruct_session
    from asr import save_transcript
    from live_processing import finalize_live_run, live_processing_has_pending_windows, merge_transcript_segments
except ImportError:
    from .asr import ASR_CHUNK_OVERLAP_SECONDS, ASR_CHUNK_SECONDS, AsrError, _probe_duration_seconds, _validate_audio_input, transcribe_range
    from .reconstruction import ReconstructionError, reconstruct_segment, reconstruct_session
    from .asr import save_transcript
    from .live_processing import finalize_live_run, live_processing_has_pending_windows, merge_transcript_segments


class ProcessingError(RuntimeError):
    pass


TRANSCRIBING = 'TRANSCRIBING'
TRANSCRIBED = 'TRANSCRIBED'
STALE_MS = 10 * 60 * 1000


def now_ms() -> int:
    return int(time.time() * 1000)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


@contextmanager
def _connect(db_path: Path):
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys = ON')
    connection.execute('PRAGMA busy_timeout = 5000')
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
    finally:
        connection.close()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def _segment_chunk_paths(data_dir: Path, connection: sqlite3.Connection, session_id: str, segment_id: str) -> tuple[int, list[Path]]:
    segment = connection.execute('SELECT segment_index FROM segments WHERE id = ? AND session_id = ?', (segment_id, session_id)).fetchone()
    rows = connection.execute('SELECT chunk_index, local_path FROM chunks WHERE segment_id = ? ORDER BY chunk_index', (segment_id,)).fetchall()
    if segment is None or not rows:
        raise ProcessingError('录音分段或音频块不存在。')
    indexes = [int(row['chunk_index']) for row in rows]
    if indexes != list(range(len(indexes))):
        raise ProcessingError(f'录音块不连续：{indexes}')
    return int(segment['segment_index']), [data_dir / row['local_path'] for row in rows]


def _rebuild_audio(data_dir: Path, db_path: Path, session_id: str) -> Path:
    with _connect(db_path) as connection:
        segments = connection.execute('SELECT id FROM segments WHERE session_id = ? ORDER BY segment_index', (session_id,)).fetchall()
        if not segments:
            raise ProcessingError('Session 没有录音分段。')
        paths = [_segment_chunk_paths(data_dir, connection, session_id, row['id']) for row in segments]
    rebuilt = [(index, reconstruct_segment(data_dir, session_id, index, chunks)) for index, chunks in paths]
    try:
        return reconstruct_session(data_dir, session_id, rebuilt)
    except ReconstructionError as error:
        raise ProcessingError(f'整场录音重建失败：{error}') from error


def _claim_task(db_path: Path, worker_id: str) -> sqlite3.Row | None:
    timestamp = now_ms()
    with _connect(db_path) as connection:
        connection.execute(
            """UPDATE processing_tasks SET status='READY', claimed_by=NULL, claimed_at=NULL,
                   lease_token_hash=NULL, lease_expires_at=NULL, error_message='', error_stage='RECOVER'
               WHERE status='PROCESSING' AND updated_at < ?
                 AND NOT EXISTS (SELECT 1 FROM processing_runs WHERE processing_runs.task_id=processing_tasks.id AND processing_runs.status='RUNNING')""",
            (timestamp - STALE_MS,),
        )
        stale = connection.execute(
            """SELECT id, task_id FROM processing_runs
               WHERE status = 'RUNNING' AND heartbeat_at < ?""",
            (timestamp - STALE_MS,),
        ).fetchall()
        for run in stale:
            connection.execute(
                "UPDATE processing_runs SET status = 'INTERRUPTED', error_message = '处理进程超时，已允许恢复。' WHERE id = ?",
                (run['id'],),
            )
            connection.execute(
                """UPDATE processing_tasks SET status='READY', claimed_by=NULL, claimed_at=NULL,
                       error_message='上次转写进程已停止，正在恢复。', error_stage='RECOVER', updated_at=?
                   WHERE id=? AND status='TRANSCRIBING'""",
                (timestamp, run['task_id']),
            )
        candidates = connection.execute(
            """SELECT id, session_id FROM processing_tasks
               WHERE status IN ('READY', 'LOCAL_READY', 'FAILED')
               ORDER BY updated_at, created_at LIMIT 50""",
        ).fetchall()
        row = next(
            (candidate for candidate in candidates if not live_processing_has_pending_windows(connection, str(candidate['session_id']))),
            None,
        )
        if row is None:
            return None
        changed = connection.execute(
            """UPDATE processing_tasks SET status = ?, claimed_by = ?, claimed_at = ?,
                   attempts = attempts + 1, error_message = '', error_stage = 'TRANSCRIBE', updated_at = ?
               WHERE id = ? AND status IN ('READY', 'LOCAL_READY', 'FAILED')""",
            (TRANSCRIBING, worker_id, timestamp, timestamp, row['id']),
        )
        if changed.rowcount != 1:
            return None
        return connection.execute('SELECT * FROM processing_tasks WHERE id = ?', (row['id'],)).fetchone()


def _new_run(connection: sqlite3.Connection, task_id: str, session_id: str, source_hash: str, model: str, language: str) -> sqlite3.Row:
    latest = connection.execute('SELECT COALESCE(MAX(generation), 0) AS generation FROM processing_runs WHERE task_id = ?', (task_id,)).fetchone()['generation']
    run_id = f'run-{uuid.uuid4()}'
    timestamp = now_ms()
    connection.execute(
        '''INSERT INTO processing_runs(id, task_id, session_id, generation, source_hash, model, language, status, started_at, heartbeat_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'RUNNING', ?, ?)''',
        (run_id, task_id, session_id, int(latest) + 1, source_hash, model, language, timestamp, timestamp),
    )
    return connection.execute('SELECT * FROM processing_runs WHERE id = ?', (run_id,)).fetchone()


def process_task(data_dir: Path, db_path: Path, task_id: str, model: str, language: str, worker_id: str) -> dict[str, Any]:
    with _connect(db_path) as connection:
        task = connection.execute('SELECT * FROM processing_tasks WHERE id = ?', (task_id,)).fetchone()
        if task is None:
            raise ProcessingError('处理任务不存在。')
        session = connection.execute('SELECT * FROM sessions WHERE id = ?', (task['session_id'],)).fetchone()
        if session is None:
            raise ProcessingError('Session 不存在。')
    audio_path = _rebuild_audio(data_dir, db_path, session['id'])
    source_hash = file_sha256(audio_path)
    duration_seconds = _probe_duration_seconds(audio_path)
    audio_quality = _validate_audio_input(audio_path)
    step_seconds = max(1, ASR_CHUNK_SECONDS - ASR_CHUNK_OVERLAP_SECONDS)
    part_count = max(1, math.ceil(max(0.001, duration_seconds - ASR_CHUNK_OVERLAP_SECONDS) / step_seconds))
    output_dir = data_dir / 'processed' / 'sessions' / session['id'] / 'transcript-parts'
    with _connect(db_path) as connection:
        run = connection.execute(
            '''SELECT * FROM processing_runs WHERE task_id = ? AND source_hash = ? AND model = ? AND language = ?
               AND status = 'COMPLETED' ORDER BY generation DESC LIMIT 1''',
            (task_id, source_hash, model, language),
        ).fetchone()
        if run is None:
            run = _new_run(connection, task_id, session['id'], source_hash, model, language)
        run_id = run['id']

    all_segments: list[dict[str, Any]] = []
    detected_languages: set[str] = set()
    device = 'unknown'
    for part_index in range(part_count):
        start_seconds = part_index * step_seconds
        if start_seconds >= duration_seconds:
            break
        duration_part = min(ASR_CHUNK_SECONDS, duration_seconds - start_seconds)
        trim_before_ms = round((start_seconds + ASR_CHUNK_OVERLAP_SECONDS) * 1000) if part_index else 0
        with _connect(db_path) as connection:
            existing = connection.execute(
                'SELECT * FROM processing_parts WHERE run_id = ? AND part_index = ?', (run_id, part_index)
            ).fetchone()
            if existing and existing['status'] == 'COMPLETED' and existing['source_hash'] == source_hash and existing['model'] == model and existing['language'] == language and existing['transcript_json']:
                part_result = json.loads(existing['transcript_json'])
            else:
                part_id = existing['id'] if existing else f'part-{uuid.uuid4()}'
                connection.execute(
                    '''INSERT INTO processing_parts(id, run_id, task_id, part_index, start_ms, end_ms, source_hash, model, language, status, started_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'RUNNING', ?)
                       ON CONFLICT(run_id, part_index) DO UPDATE SET status='RUNNING', error_message='', started_at=excluded.started_at''',
                    (part_id, run_id, task_id, part_index, round(start_seconds * 1000), round((start_seconds + duration_part) * 1000), source_hash, model, language, now_ms()),
                )
                connection.commit()
                try:
                    part_result = transcribe_range(audio_path, start_seconds, duration_part, model_name=model, language=language)
                    segments = [segment for segment in part_result.get('segments', []) if int(segment.get('startMs', 0)) >= trim_before_ms]
                    part_result['segments'] = segments
                    part_result['text'] = ' '.join(str(segment.get('text', '')).strip() for segment in segments).strip()
                    _atomic_json(output_dir / f'part-{part_index:05d}.json', part_result)
                    connection.execute(
                        '''UPDATE processing_parts SET status='COMPLETED', transcript_json=?, completed_at=?, error_message=''
                           WHERE run_id=? AND part_index=?''',
                        (json.dumps(part_result, ensure_ascii=False), now_ms(), run_id, part_index),
                    )
                except Exception as error:
                    connection.execute(
                        "UPDATE processing_parts SET status='FAILED', error_message=? WHERE run_id=? AND part_index=?",
                        (str(error), run_id, part_index),
                    )
                    raise
            connection.execute('UPDATE processing_runs SET heartbeat_at = ? WHERE id = ?', (now_ms(), run_id))
        if part_result.get('language'):
            detected_languages.add(str(part_result['language']))
        device = part_result.get('device') or device
        all_segments.extend(part_result.get('segments', []))

    all_segments = merge_transcript_segments(all_segments)
    transcript = {
        'model': model,
        'device': device,
        'language': next(iter(detected_languages)) if len(detected_languages) == 1 else (language if language not in {'', 'auto'} else 'auto'),
        'audioQuality': audio_quality,
        'text': ' '.join(str(segment.get('text', '')).strip() for segment in all_segments).strip(),
        'segments': all_segments,
        'chunked': part_count > 1,
        'chunkDurationSeconds': ASR_CHUNK_SECONDS,
        'chunkOverlapSeconds': ASR_CHUNK_OVERLAP_SECONDS,
        'sourceHash': source_hash,
        'runId': run_id,
        'generation': int(run['generation']),
    }
    save_transcript(data_dir, session['id'], transcript)
    finalize_live_run(db_path, session['id'], source_hash)
    with _connect(db_path) as connection:
        connection.execute("UPDATE processing_runs SET status='COMPLETED', completed_at=?, heartbeat_at=? WHERE id=?", (now_ms(), now_ms(), run_id))
        connection.execute(
            """UPDATE processing_tasks SET status=?, claimed_by=NULL, claimed_at=NULL, error_message='', error_stage='', updated_at=?
               WHERE id=? AND status=? AND claimed_by=?""",
            (TRANSCRIBED, now_ms(), task_id, TRANSCRIBING, worker_id),
        )
    return {'taskId': task_id, 'sessionId': session['id'], 'status': TRANSCRIBED, 'runId': run_id, 'generation': int(run['generation']), 'sourceHash': source_hash, 'segments': len(all_segments), 'durationSeconds': duration_seconds, 'device': device}


def run_once(data_dir: Path, db_path: Path, model: str, language: str, worker_id: str) -> dict[str, Any] | None:
    task = _claim_task(db_path, worker_id)
    if task is None:
        return None
    try:
        return process_task(data_dir, db_path, task['id'], model, language, worker_id)
    except Exception as error:
        with _connect(db_path) as connection:
            connection.execute(
                """UPDATE processing_tasks SET status='FAILED', claimed_by=NULL, claimed_at=NULL, error_message=?, error_stage='TRANSCRIBE', updated_at=?
                   WHERE id=? AND status=? AND claimed_by=?""",
                (str(error), now_ms(), task['id'], TRANSCRIBING, worker_id),
            )
            connection.execute(
                "UPDATE processing_runs SET status='FAILED', completed_at=?, error_message=? WHERE task_id=? AND status='RUNNING'",
                (now_ms(), str(error), task['id']),
            )
        raise ProcessingError(str(error)) from error


def watch(data_dir: Path, db_path: Path, model: str, language: str, worker_id: str, interval: int = 5) -> None:
    print(f'LiveNote transcriber {worker_id} 已启动，模型 {model}，语言 {language}。', flush=True)
    while True:
        try:
            result = run_once(data_dir, db_path, model, language, worker_id)
            if result:
                print(json.dumps(result, ensure_ascii=False), flush=True)
            else:
                time.sleep(max(1, interval))
        except KeyboardInterrupt:
            return
        except Exception as error:
            print(f'转写任务失败：{error}', flush=True)
            time.sleep(max(5, interval))
