"""Durable incremental processing for recordings that are still uploading.

The recorder emits MediaRecorder fragments.  A fragment is not assumed to be
independently decodable; this module appends only the contiguous prefix of a
segment to a staging WebM and processes bounded windows from that prefix.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

try:
    from asr import AsrError, DEFAULT_MODEL, transcribe_range
except ImportError:
    from .asr import AsrError, DEFAULT_MODEL, transcribe_range


LIVE_WINDOW_CHUNKS = max(2, min(4, int(os.environ.get('LIVENOTE_LIVE_WINDOW_CHUNKS', '4'))))
LIVE_WINDOW_OVERLAP_SECONDS = max(0.0, min(3.0, float(os.environ.get('LIVENOTE_LIVE_WINDOW_OVERLAP_SECONDS', '2'))))
LIVE_POLL_SECONDS = max(0.5, float(os.environ.get('LIVENOTE_LIVE_POLL_SECONDS', '2')))
LIVE_LEASE_MS = max(30_000, int(float(os.environ.get('LIVENOTE_LIVE_LEASE_SECONDS', '120')) * 1000))
LIVE_PROCESSING_ENABLED = os.environ.get('LIVENOTE_LIVE_PROCESSING_ENABLED', '1').strip().lower() in {'1', 'true', 'yes', 'on'}
LIVE_PIPELINE_VERSION = 'live-prefix-window-v1'

WAITING_FOR_CHUNKS = 'WAITING_FOR_CHUNKS'
READY = 'READY'
CLAIMED = 'CLAIMED'
DECODING = 'DECODING'
TRANSCRIBING = 'TRANSCRIBING'
COMPLETED = 'COMPLETED'
WAITING_FOR_DECODABLE_PREFIX = 'WAITING_FOR_DECODABLE_PREFIX'
FAILED = 'FAILED'
FINALIZED = 'FINALIZED'


def now_ms() -> int:
    return round(time.time() * 1000)


@contextmanager
def _connect(db_path: Path):
    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys = ON')
        connection.execute('PRAGMA busy_timeout = 5000')
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
    finally:
        connection.close()


def ensure_live_run(connection: sqlite3.Connection, session_id: str) -> str:
    run_id = f'live-{uuid.uuid4()}'
    timestamp = now_ms()
    connection.execute(
        '''INSERT OR IGNORE INTO live_processing_runs(
               id, session_id, model, language, pipeline_version, status,
               last_contiguous_chunk, processed_until_ms, source_prefix_hash,
               heartbeat_at, created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, -1, 0, '', ?, ?, ?)''',
        (run_id, session_id, DEFAULT_MODEL, 'zh', LIVE_PIPELINE_VERSION, WAITING_FOR_CHUNKS, timestamp, timestamp, timestamp),
    )
    row = connection.execute('SELECT id FROM live_processing_runs WHERE session_id = ?', (session_id,)).fetchone()
    if row is None:
        raise RuntimeError(f'无法创建增量处理运行：{session_id}')
    connection.execute(
        '''UPDATE live_processing_runs
           SET status = CASE WHEN status IN (?, ?, ?, ?) THEN ? ELSE status END,
               claimed_by = CASE WHEN status IN (?, ?, ?, ?) THEN NULL ELSE claimed_by END,
               lease_expires_at = CASE WHEN status IN (?, ?, ?, ?) THEN NULL ELSE lease_expires_at END,
               error_code = CASE WHEN status IN (?, ?, ?, ?) THEN '' ELSE error_code END,
               error_message = CASE WHEN status IN (?, ?, ?, ?) THEN '' ELSE error_message END,
               updated_at = ?
           WHERE session_id = ?''',
        (FAILED, WAITING_FOR_DECODABLE_PREFIX, WAITING_FOR_CHUNKS, COMPLETED, READY,
         FAILED, WAITING_FOR_DECODABLE_PREFIX, WAITING_FOR_CHUNKS, COMPLETED,
         FAILED, WAITING_FOR_DECODABLE_PREFIX, WAITING_FOR_CHUNKS, COMPLETED,
         FAILED, WAITING_FOR_DECODABLE_PREFIX, WAITING_FOR_CHUNKS, COMPLETED,
         FAILED, WAITING_FOR_DECODABLE_PREFIX, WAITING_FOR_CHUNKS, COMPLETED,
         timestamp, session_id),
    )
    return str(row['id'] if isinstance(row, sqlite3.Row) else row[0])


def retry_live_run(connection: sqlite3.Connection, session_id: str) -> str:
    run_id = ensure_live_run(connection, session_id)
    timestamp = now_ms()
    connection.execute(
        '''UPDATE live_processing_windows
           SET status = ?, error_code = '', error_message = '', completed_at = NULL, updated_at = ?
           WHERE run_id = ? AND status IN (?, ?)''',
        (READY, timestamp, run_id, FAILED, WAITING_FOR_DECODABLE_PREFIX),
    )
    connection.execute(
        '''UPDATE live_processing_runs
           SET status = ?, claimed_by = NULL, lease_expires_at = NULL,
               error_code = '', error_message = '', updated_at = ?
           WHERE id = ?''',
        (READY, timestamp, run_id),
    )
    return run_id


def _safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _atomic_json(path: Path, value: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _directory_size(directory: Path) -> int:
    total = 0
    if not directory.is_dir():
        return total
    for path in directory.iterdir():
        if path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return total


def _append_prefix(data_dir: Path, session_id: str, segment_index: int, rows: list[sqlite3.Row]) -> tuple[Path, str]:
    directory = data_dir / 'processed' / 'live' / session_id / f'segment_{segment_index}'
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / 'prefix.webm'
    temporary = directory / f'.prefix.{uuid.uuid4().hex}.tmp'
    digest = hashlib.sha256()
    try:
        with temporary.open('wb') as target:
            for row in rows:
                source = data_dir / str(row['local_path'])
                if not source.is_file():
                    raise FileNotFoundError(f'Chunk 文件不存在：{source}')
                chunk_digest = hashlib.sha256()
                chunk_size = 0
                with source.open('rb') as chunk:
                    while True:
                        block = chunk.read(1024 * 1024)
                        if not block:
                            break
                        chunk_size += len(block)
                        chunk_digest.update(block)
                        digest.update(block)
                        target.write(block)
                expected_size = int(row['size'])
                expected_sha256 = str(row['sha256']).lower()
                if chunk_size != expected_size or chunk_digest.hexdigest().lower() != expected_sha256:
                    raise ValueError(f'Chunk 校验失败：{row["chunk_index"]}')
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return output, digest.hexdigest()


def _contiguous_rows(connection: sqlite3.Connection, segment_id: str) -> list[sqlite3.Row]:
    rows = connection.execute(
        'SELECT chunk_index, elapsed_ms, local_path, sha256, size FROM chunks WHERE segment_id = ? ORDER BY chunk_index',
        (segment_id,),
    ).fetchall()
    contiguous: list[sqlite3.Row] = []
    for expected, row in enumerate(rows):
        if int(row['chunk_index']) != expected:
            break
        contiguous.append(row)
    return contiguous


def _completed_window_count(connection: sqlite3.Connection, run_id: str, segment_id: str) -> int:
    row = connection.execute(
        '''SELECT COUNT(*) AS count FROM live_processing_windows
           WHERE run_id = ? AND segment_id = ? AND status = ?''',
        (run_id, segment_id, COMPLETED),
    ).fetchone()
    return int(row['count'] if row else 0)


def _window_rows(contiguous: list[sqlite3.Row], window_index: int, session_completed: bool) -> tuple[int, int] | None:
    start = window_index * LIVE_WINDOW_CHUNKS
    if start >= len(contiguous):
        return None
    end = min(len(contiguous), start + LIVE_WINDOW_CHUNKS) - 1
    if end - start + 1 < LIVE_WINDOW_CHUNKS and not session_completed:
        return None
    return start, end


def _has_pending_window(
    connection: sqlite3.Connection,
    run_id: str,
    segments: list[sqlite3.Row],
    session_completed: bool,
) -> bool:
    for segment in segments:
        contiguous = _contiguous_rows(connection, str(segment['id']))
        window_index = _completed_window_count(connection, run_id, str(segment['id']))
        if _window_rows(contiguous, window_index, session_completed) is not None:
            return True
    return False


def live_processing_has_pending_windows(connection: sqlite3.Connection, session_id: str) -> bool:
    """Return whether final transcription must wait for a live window drain."""
    if not LIVE_PROCESSING_ENABLED:
        return False
    run = connection.execute(
        'SELECT id, status FROM live_processing_runs WHERE session_id = ?',
        (session_id,),
    ).fetchone()
    if run is None or run['status'] in {FINALIZED, FAILED, WAITING_FOR_DECODABLE_PREFIX, WAITING_FOR_CHUNKS}:
        return False
    session = connection.execute('SELECT status FROM sessions WHERE id = ?', (session_id,)).fetchone()
    if session is None or session['status'] != 'COMPLETED':
        return False
    segments = connection.execute(
        'SELECT id FROM segments WHERE session_id = ? ORDER BY segment_index',
        (session_id,),
    ).fetchall()
    return _has_pending_window(connection, str(run['id']), segments, True)


def _window_segments(result: dict[str, Any], segment_start_ms: int, core_start_ms: int, core_end_ms: int) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for item in result.get('segments', []):
        if not isinstance(item, dict):
            continue
        start_ms = segment_start_ms + int(item.get('startMs', 0) or 0)
        end_ms = segment_start_ms + int(item.get('endMs', 0) or 0)
        if end_ms <= core_start_ms or start_ms >= core_end_ms:
            continue
        start_ms = max(start_ms, core_start_ms)
        end_ms = min(max(end_ms, start_ms), core_end_ms)
        text = str(item.get('text', '')).strip()
        if not text:
            continue
        output.append({'startMs': start_ms, 'endMs': end_ms, 'text': text})
    return output


def merge_transcript_segments(segments: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge ordered window output without deleting legitimate repeated speech."""
    ordered = sorted(
        (item for item in segments if isinstance(item, dict) and str(item.get('text', '')).strip()),
        key=lambda item: (int(item.get('startMs', 0) or 0), int(item.get('endMs', 0) or 0)),
    )
    merged: list[dict[str, Any]] = []
    for item in ordered:
        start_ms = max(0, int(item.get('startMs', 0) or 0))
        end_ms = max(start_ms, int(item.get('endMs', start_ms) or start_ms))
        text = str(item.get('text', '')).strip()
        if not text:
            continue
        if merged:
            previous = merged[-1]
            same_text = ''.join(str(previous['text']).split()) == ''.join(text.split())
            overlaps = start_ms <= int(previous['endMs'])
            if same_text and overlaps:
                previous['endMs'] = max(int(previous['endMs']), end_ms)
                continue
        merged.append({'startMs': start_ms, 'endMs': end_ms, 'text': text})
    return [{**item, 'index': index} for index, item in enumerate(merged)]


def _write_live_transcript(data_dir: Path, session_id: str, windows: list[sqlite3.Row]) -> None:
    segments: list[dict[str, Any]] = []
    for row in windows:
        try:
            result = json.loads(row['transcript_json'] or '{}')
        except (TypeError, json.JSONDecodeError):
            continue
        for item in result.get('segments', []):
            if isinstance(item, dict):
                segments.append(item)
    normalised = merge_transcript_segments(segments)
    transcript = {
        'model': DEFAULT_MODEL,
        'language': 'zh',
        'text': ' '.join(item['text'] for item in normalised).strip(),
        'segments': normalised,
        'chunked': True,
        'liveProcessing': True,
        'pipelineVersion': LIVE_PIPELINE_VERSION,
        'updatedAt': now_ms(),
    }
    _atomic_json(data_dir / 'processed' / 'sessions' / session_id / 'transcript-live.json', transcript)


def finalize_live_run(db_path: Path, session_id: str, source_hash: str) -> None:
    """Close the provisional run only after the canonical full transcript exists."""
    with _connect(db_path) as connection:
        connection.execute(
            '''UPDATE live_processing_runs
               SET status = ?, source_prefix_hash = ?, claimed_by = NULL,
                   lease_expires_at = NULL, heartbeat_at = ?, updated_at = ?
               WHERE session_id = ?''',
            (FINALIZED, source_hash, now_ms(), now_ms(), session_id),
        )


def _claim_run(connection: sqlite3.Connection, session_id: str, worker_id: str) -> sqlite3.Row | None:
    timestamp = now_ms()
    connection.execute('BEGIN IMMEDIATE')
    row = connection.execute('SELECT * FROM live_processing_runs WHERE session_id = ?', (session_id,)).fetchone()
    if row is None:
        return None
    if row['status'] == FINALIZED:
        return None
    if row['claimed_by'] and row['claimed_by'] != worker_id and int(row['lease_expires_at'] or 0) > timestamp:
        return None
    connection.execute(
        '''UPDATE live_processing_runs SET status = ?, claimed_by = ?, lease_expires_at = ?, heartbeat_at = ?, updated_at = ?
           WHERE id = ?''',
        (CLAIMED, worker_id, timestamp + LIVE_LEASE_MS, timestamp, timestamp, row['id']),
    )
    return connection.execute('SELECT * FROM live_processing_runs WHERE id = ?', (row['id'],)).fetchone()


def _mark_run(connection: sqlite3.Connection, run_id: str, status: str, **values: Any) -> None:
    fields = ['status = ?', 'updated_at = ?']
    parameters: list[Any] = [status, now_ms()]
    for field, value in values.items():
        fields.append(f'{field} = ?')
        parameters.append(value)
    parameters.append(run_id)
    connection.execute(f'UPDATE live_processing_runs SET {", ".join(fields)} WHERE id = ?', parameters)


def _process_window(
    data_dir: Path,
    db_path: Path,
    run_id: str,
    worker_id: str,
    segment: sqlite3.Row,
    rows: list[sqlite3.Row],
    window_index: int,
    session_completed: bool,
    model: str,
    language: str,
) -> bool:
    bounds = _window_rows(rows, window_index, session_completed)
    if bounds is None:
        return False
    start_index, end_index = bounds
    window_started_at = now_ms()
    segment_start_ms = int(segment['start_elapsed_ms'] or 0)
    core_start_ms = segment_start_ms if start_index == 0 else int(rows[start_index]['elapsed_ms'])
    core_end_ms = max(core_start_ms + 1, int(rows[end_index]['elapsed_ms']))
    overlap_start_ms = max(segment_start_ms, core_start_ms - round(LIVE_WINDOW_OVERLAP_SECONDS * 1000))
    prepare_started_at = now_ms()
    audio_path, prefix_hash = _append_prefix(data_dir, str(segment['session_id']), int(segment['segment_index']), rows[:end_index + 1])
    prepare_duration_ms = max(0, now_ms() - prepare_started_at)
    staging_directory = audio_path.parent
    peak_staging_bytes = _directory_size(staging_directory)
    input_hash = hashlib.sha256(f'{prefix_hash}:{start_index}:{end_index}'.encode('utf-8')).hexdigest()
    with _connect(db_path) as connection:
        existing = connection.execute(
            'SELECT * FROM live_processing_windows WHERE run_id = ? AND segment_id = ? AND window_index = ?',
            (run_id, segment['id'], window_index),
        ).fetchone()
        if existing is not None and existing['status'] == COMPLETED and existing['input_hash'] == input_hash:
            return False
        if existing is not None and existing['status'] in {FAILED, WAITING_FOR_DECODABLE_PREFIX} and existing['input_hash'] == input_hash:
            return False
        window_id = existing['id'] if existing else f'live-window-{uuid.uuid4()}'
        connection.execute(
            '''INSERT INTO live_processing_windows(
                   id, run_id, session_id, segment_id, window_index,
                   model, language, pipeline_version,
                   input_start_chunk, input_end_chunk, start_ms, end_ms,
                   core_start_ms, core_end_ms, input_hash, status, attempts, started_at, updated_at,
                   prepare_duration_ms, asr_duration_ms, peak_staging_bytes
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
               ON CONFLICT(run_id, segment_id, window_index) DO UPDATE SET
                   input_start_chunk=excluded.input_start_chunk,
                   input_end_chunk=excluded.input_end_chunk,
                   start_ms=excluded.start_ms, end_ms=excluded.end_ms,
                   core_start_ms=excluded.core_start_ms, core_end_ms=excluded.core_end_ms,
                   input_hash=excluded.input_hash, status=excluded.status,
                   attempts=live_processing_windows.attempts + 1,
                   started_at=excluded.started_at, error_code='', error_message='', updated_at=excluded.updated_at ''',
            (window_id, run_id, segment['session_id'], segment['id'], window_index, model, language, LIVE_PIPELINE_VERSION,
             start_index, end_index, overlap_start_ms, core_end_ms, core_start_ms, core_end_ms, input_hash, TRANSCRIBING,
             window_started_at, now_ms(), prepare_duration_ms, 0, peak_staging_bytes),
        )
        connection.commit()
    try:
        start_seconds = max(0.0, (overlap_start_ms - segment_start_ms) / 1000)
        duration_seconds = max(0.25, (core_end_ms - overlap_start_ms) / 1000)
        asr_started_at = now_ms()
        result = transcribe_range(audio_path, start_seconds, duration_seconds, model_name=model, language=language)
        asr_duration_ms = max(0, now_ms() - asr_started_at)
        segments = _window_segments(result, segment_start_ms, core_start_ms, core_end_ms)
        result = {**result, 'segments': segments, 'text': ' '.join(item['text'] for item in segments).strip()}
        artifact_path = data_dir / 'processed' / 'live' / str(segment['session_id']) / f'segment_{segment["segment_index"]}' / f'window-{window_index:05d}.json'
        artifact_hash = _atomic_json(artifact_path, result)
        peak_staging_bytes = max(peak_staging_bytes, _directory_size(staging_directory))
        with _connect(db_path) as connection:
            lease_timestamp = now_ms()
            lease = connection.execute(
                '''UPDATE live_processing_runs
                   SET lease_expires_at = ?, heartbeat_at = ?, updated_at = ?
                   WHERE id = ? AND claimed_by = ? AND status = ? AND lease_expires_at > ?''',
                (lease_timestamp + LIVE_LEASE_MS, lease_timestamp, lease_timestamp, run_id, worker_id, CLAIMED, lease_timestamp),
            )
            if lease.rowcount != 1:
                artifact_path.unlink(missing_ok=True)
                return False
            connection.execute(
                '''UPDATE live_processing_windows
                   SET status = ?, transcript_json = ?, artifact_path = ?, artifact_hash = ?, completed_at = ?, error_code = '', error_message = ?, updated_at = ?,
                       prepare_duration_ms = ?, asr_duration_ms = ?, peak_staging_bytes = ?
                   WHERE id = ? AND run_id = ?
                     AND EXISTS (SELECT 1 FROM live_processing_runs WHERE id = ? AND status != ? AND claimed_by = ? AND lease_expires_at > ?)''',
                (COMPLETED, _safe_json(result), str(artifact_path.relative_to(data_dir)), artifact_hash, now_ms(), '', now_ms(),
                 prepare_duration_ms, asr_duration_ms, peak_staging_bytes, window_id, run_id, run_id, FINALIZED, worker_id, lease_timestamp),
            )
            if connection.execute('SELECT changes()').fetchone()[0] != 1:
                artifact_path.unlink(missing_ok=True)
                return False
            connection.execute(
                '''UPDATE live_processing_runs SET status = ?, last_contiguous_chunk = MAX(last_contiguous_chunk, ?),
                   processed_until_ms = MAX(processed_until_ms, ?), source_prefix_hash = ?, heartbeat_at = ?, updated_at = ?
                   WHERE id = ? AND claimed_by = ?''',
                (COMPLETED, end_index, core_end_ms, prefix_hash, now_ms(), now_ms(), run_id, worker_id),
            )
            completed = connection.execute(
                'SELECT * FROM live_processing_windows WHERE run_id = ? AND status = ? ORDER BY segment_id, window_index',
                (run_id, COMPLETED),
            ).fetchall()
        _write_live_transcript(data_dir, str(segment['session_id']), completed)
        return True
    except AsrError as error:
        code = 'WAITING_FOR_DECODABLE_PREFIX' if any(token in str(error).lower() for token in ('ffmpeg', '音频预处理', '无法读取', '音频文件')) else 'ASR_FAILED'
        error_timestamp = now_ms()
        with _connect(db_path) as connection:
            connection.execute(
                '''UPDATE live_processing_windows SET status = ?, error_code = ?, error_message = ?, completed_at = NULL, updated_at = ?,
                       prepare_duration_ms = ?, asr_duration_ms = ?, peak_staging_bytes = ?
                   WHERE id = ? AND EXISTS (SELECT 1 FROM live_processing_runs WHERE id = ? AND claimed_by = ? AND lease_expires_at > ?)''',
                (WAITING_FOR_DECODABLE_PREFIX if code == 'WAITING_FOR_DECODABLE_PREFIX' else FAILED, code, str(error)[:1000], error_timestamp,
                 prepare_duration_ms, max(0, now_ms() - asr_started_at) if 'asr_started_at' in locals() else 0,
                 max(peak_staging_bytes, _directory_size(staging_directory)), window_id, run_id, worker_id, error_timestamp),
            )
            connection.execute('''UPDATE live_processing_runs SET status = ?, claimed_by = NULL, lease_expires_at = NULL,
                                  error_code = ?, error_message = ?, updated_at = ?
                                  WHERE id = ? AND claimed_by = ? AND lease_expires_at > ?''',
                               (WAITING_FOR_DECODABLE_PREFIX if code == 'WAITING_FOR_DECODABLE_PREFIX' else FAILED, code, str(error)[:1000], error_timestamp, run_id, worker_id, error_timestamp))
        return False
    except Exception as error:
        error_timestamp = now_ms()
        with _connect(db_path) as connection:
            connection.execute('''UPDATE live_processing_windows SET status = ?, error_code = ?, error_message = ?, completed_at = NULL, updated_at = ?,
                                  prepare_duration_ms = ?, asr_duration_ms = ?, peak_staging_bytes = ?
                                  WHERE id = ? AND EXISTS (SELECT 1 FROM live_processing_runs WHERE id = ? AND claimed_by = ? AND lease_expires_at > ?)''',
                               (FAILED, 'UNEXPECTED', str(error)[:1000], error_timestamp, prepare_duration_ms, max(0, now_ms() - asr_started_at) if 'asr_started_at' in locals() else 0, max(peak_staging_bytes, _directory_size(staging_directory)), window_id, run_id, worker_id, error_timestamp))
            connection.execute('''UPDATE live_processing_runs SET status = ?, claimed_by = NULL, lease_expires_at = NULL,
                                  error_code = ?, error_message = ?, updated_at = ?
                                  WHERE id = ? AND claimed_by = ? AND lease_expires_at > ?''',
                               (FAILED, 'UNEXPECTED', str(error)[:1000], error_timestamp, run_id, worker_id, error_timestamp))
        return False


def process_session_once(data_dir: Path, db_path: Path, session_id: str, worker_id: str = 'live-worker') -> bool:
    with _connect(db_path) as connection:
        run = _claim_run(connection, session_id, worker_id)
        if run is None:
            return False
        session = connection.execute('SELECT status FROM sessions WHERE id = ?', (session_id,)).fetchone()
        segments = connection.execute('SELECT * FROM segments WHERE session_id = ? ORDER BY segment_index', (session_id,)).fetchall()
    if session is None:
        return False
    did_work = False
    try:
        for segment in segments:
            with _connect(db_path) as connection:
                rows = _contiguous_rows(connection, segment['id'])
                window_index = _completed_window_count(connection, run['id'], segment['id'])
            if _process_window(data_dir, db_path, run['id'], worker_id, segment, rows, window_index, session['status'] == 'COMPLETED', run['model'], run['language']):
                did_work = True
                break
        with _connect(db_path) as connection:
            if did_work:
                next_status = READY if _has_pending_window(connection, run['id'], segments, session['status'] == 'COMPLETED') else COMPLETED
                connection.execute('UPDATE live_processing_runs SET status = ?, claimed_by = NULL, lease_expires_at = NULL, heartbeat_at = ?, updated_at = ? WHERE id = ? AND claimed_by = ? AND status = ?', (next_status, now_ms(), now_ms(), run['id'], worker_id, COMPLETED))
            else:
                connection.execute('UPDATE live_processing_runs SET status = ?, claimed_by = NULL, lease_expires_at = NULL, heartbeat_at = ?, updated_at = ? WHERE id = ? AND claimed_by = ? AND status = ?', (WAITING_FOR_CHUNKS, now_ms(), now_ms(), run['id'], worker_id, CLAIMED))
    except Exception as error:
        with _connect(db_path) as connection:
            connection.execute('UPDATE live_processing_runs SET status = ?, claimed_by = NULL, lease_expires_at = NULL, error_code = ?, error_message = ?, updated_at = ? WHERE id = ? AND claimed_by = ? AND status = ?', (FAILED, 'UNEXPECTED', str(error)[:1000], now_ms(), run['id'], worker_id, CLAIMED))
    return did_work


def live_processing_status(db_path: Path, session_id: str) -> dict[str, Any] | None:
    with _connect(db_path) as connection:
        run = connection.execute('SELECT * FROM live_processing_runs WHERE session_id = ?', (session_id,)).fetchone()
        if run is None:
            return None
        session = connection.execute('SELECT duration_ms FROM sessions WHERE id = ?', (session_id,)).fetchone()
        total = connection.execute('SELECT COUNT(*) AS count FROM chunks WHERE session_id = ?', (session_id,)).fetchone()['count']
        segment_counts = connection.execute('SELECT segment_id, COUNT(*) AS count FROM chunks WHERE session_id = ? GROUP BY segment_id', (session_id,)).fetchall()
        windows = connection.execute('SELECT COUNT(*) AS count FROM live_processing_windows WHERE run_id = ? AND status = ?', (run['id'], COMPLETED)).fetchone()['count']
        latest = connection.execute('SELECT error_code, error_message FROM live_processing_windows WHERE run_id = ? AND status IN (?, ?) ORDER BY updated_at DESC LIMIT 1', (run['id'], FAILED, WAITING_FOR_DECODABLE_PREFIX)).fetchone()
        metrics = connection.execute('''SELECT completed_at - started_at AS duration_ms, asr_duration_ms, peak_staging_bytes
                                        FROM live_processing_windows
                                        WHERE run_id = ? AND status = ? ORDER BY updated_at DESC LIMIT 1''', (run['id'], COMPLETED)).fetchone()
    return {
        'runId': run['id'],
        'status': run['status'],
        'completedWindows': int(windows),
        'totalWindows': max(1, sum(max(1, (int(row['count']) + LIVE_WINDOW_CHUNKS - 1) // LIVE_WINDOW_CHUNKS) for row in segment_counts)),
        'processedDurationMs': int(run['processed_until_ms'] or 0),
        'totalDurationMs': int(session['duration_ms'] or 0) if session else 0,
        'uploadedChunks': int(total),
        'lastError': latest['error_message'] if latest else run['error_message'],
        'errorCode': latest['error_code'] if latest else run['error_code'],
        'canRetry': run['status'] in {FAILED, WAITING_FOR_DECODABLE_PREFIX},
        'updatedAt': run['updated_at'],
        'lastContiguousChunk': int(run['last_contiguous_chunk'] or -1),
        'lastWindowDurationMs': int(metrics['duration_ms'] or 0) if metrics else 0,
        'lastAsrDurationMs': int(metrics['asr_duration_ms'] or 0) if metrics else 0,
        'peakStagingBytes': int(metrics['peak_staging_bytes'] or 0) if metrics else 0,
    }


class LiveProcessingWorker:
    def __init__(self, data_dir: Path, db_path: Path, enabled: bool = True, interval_seconds: float = LIVE_POLL_SECONDS):
        self.data_dir = data_dir
        self.db_path = db_path
        self.enabled = enabled
        self.interval_seconds = interval_seconds
        self.worker_id = f'live-worker-{uuid.uuid4().hex[:8]}'
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name='livenote-live-processing', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None

    def wake(self, session_id: str | None = None) -> None:
        # session_id is intentionally not stored in process memory. The DB is
        # the durable queue; this event only shortens the normal poll delay.
        self._wake.set()

    def _resume_deferred_final_jobs(self) -> int:
        """Resume admin jobs deferred until the live windows are drained."""
        try:
            try:
                from jobs import create_job, find_active_job
            except ImportError:
                from .jobs import create_job, find_active_job

            with _connect(self.db_path) as connection:
                tasks = connection.execute(
                    '''SELECT id, session_id FROM processing_tasks
                       WHERE status = 'PROCESSING' AND error_stage = 'LIVE_DRAIN'
                       ORDER BY updated_at LIMIT 16''',
                ).fetchall()

            resumed = 0
            for task in tasks:
                task_id = str(task['id'])
                session_id = str(task['session_id'])
                with _connect(self.db_path) as connection:
                    current = connection.execute(
                        'SELECT status, error_stage FROM processing_tasks WHERE id = ?',
                        (task_id,),
                    ).fetchone()
                    if current is None or current['status'] != 'PROCESSING' or current['error_stage'] != 'LIVE_DRAIN':
                        continue
                    if live_processing_has_pending_windows(connection, session_id):
                        continue

                if find_active_job(self.data_dir, session_id) is not None:
                    continue
                try:
                    create_job(self.data_dir, self.db_path, session_id, DEFAULT_MODEL, 'zh', task_id=task_id)
                except Exception as error:
                    with _connect(self.db_path) as connection:
                        connection.execute(
                            '''UPDATE processing_tasks SET status = 'FAILED',
                               claimed_by = NULL, claimed_at = NULL, requested_worker_id = NULL,
                               lease_token_hash = NULL, lease_expires_at = NULL,
                               error_message = ?, error_stage = 'AUTO_PROCESS', updated_at = ?
                               WHERE id = ? AND status = 'PROCESSING' AND error_stage = 'LIVE_DRAIN' ''',
                            (str(error), now_ms(), task_id),
                        )
                    continue
                with _connect(self.db_path) as connection:
                    connection.execute(
                        '''UPDATE processing_tasks SET error_message = '', error_stage = 'AUTO_PROCESS', updated_at = ?
                           WHERE id = ? AND status = 'PROCESSING' AND error_stage = 'LIVE_DRAIN' ''',
                        (now_ms(), task_id),
                    )
                resumed += 1
            return resumed
        except Exception:
            # Durable task state remains in LIVE_DRAIN and will be retried by
            # the next poll; a transient import or database error must not
            # terminate the live worker.
            return 0

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with _connect(self.db_path) as connection:
                    sessions = connection.execute(
                        '''SELECT session_id FROM live_processing_runs
                           WHERE status IN (?, ?, ?, ?) AND (claimed_by IS NULL OR lease_expires_at < ?)
                           ORDER BY updated_at LIMIT 8''',
                        (WAITING_FOR_CHUNKS, READY, WAITING_FOR_DECODABLE_PREFIX, CLAIMED, now_ms()),
                    ).fetchall()
                progressed = False
                for row in sessions:
                    progressed = process_session_once(self.data_dir, self.db_path, str(row['session_id']), self.worker_id) or progressed
                self._resume_deferred_final_jobs()
                if progressed:
                    continue
            except Exception:
                # The next durable poll retries. Never let the background
                # thread terminate because one malformed session is present.
                pass
            self._wake.wait(self.interval_seconds)
            self._wake.clear()
