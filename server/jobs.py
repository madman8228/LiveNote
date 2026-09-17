from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

try:
    from asr import AsrError, save_transcript, transcribe
    from content import ContentError, build_structured_report, load_transcript, save_report
    from reconstruction import ReconstructionError, reconstruct_segment, reconstruct_session
except ImportError:
    from .asr import AsrError, save_transcript, transcribe
    from .content import ContentError, build_structured_report, load_transcript, save_report
    from .reconstruction import ReconstructionError, reconstruct_segment, reconstruct_session


class JobError(RuntimeError):
    pass


_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='livenote-processing')
_lock = threading.Lock()


def _job_path(data_dir: Path, job_id: str) -> Path:
    return data_dir / 'processed' / 'jobs' / f'{job_id}.json'


def _write_job(data_dir: Path, state: dict[str, Any]) -> None:
    path = _job_path(data_dir, state['id'])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f'.{path.name}.tmp')
    temporary_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary_path.replace(path)


def get_job(data_dir: Path, job_id: str) -> dict[str, Any] | None:
    path = _job_path(data_dir, job_id)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except Exception as error:
        raise JobError(f'任务状态读取失败：{error}') from error
    return value if isinstance(value, dict) else None


def find_active_job(data_dir: Path, session_id: str) -> dict[str, Any] | None:
    jobs_dir = data_dir / 'processed' / 'jobs'
    if not jobs_dir.is_dir():
        return None
    active: list[dict[str, Any]] = []
    for path in jobs_dir.glob('job-*.json'):
        try:
            state = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            continue
        if isinstance(state, dict) and state.get('sessionId') == session_id and state.get('status') in {'QUEUED', 'RUNNING'}:
            active.append(state)
    return max(active, key=lambda state: state.get('createdAt', 0)) if active else None


def find_latest_job(data_dir: Path, session_id: str) -> dict[str, Any] | None:
    """Return the newest persisted job for a Session, including finished jobs."""
    jobs_dir = data_dir / 'processed' / 'jobs'
    if not jobs_dir.is_dir():
        return None
    latest: list[dict[str, Any]] = []
    for path in jobs_dir.glob('job-*.json'):
        try:
            state = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            continue
        if isinstance(state, dict) and state.get('sessionId') == session_id:
            latest.append(state)
    return max(latest, key=lambda state: (state.get('createdAt', 0), state.get('updatedAt', 0))) if latest else None


def _update_job(data_dir: Path, job_id: str, **patch: Any) -> None:
    with _lock:
        state = get_job(data_dir, job_id) or {'id': job_id}
        state.update(patch)
        state['updatedAt'] = _now_ms()
        _write_job(data_dir, state)


def _segment_chunk_paths(data_dir: Path, connection: sqlite3.Connection, session_id: str, segment_id: str) -> tuple[int, list[Path]]:
    segment = connection.execute('SELECT segment_index FROM segments WHERE id = ? AND session_id = ?', (segment_id, session_id)).fetchone()
    if segment is None:
        raise JobError('Segment 不存在。')
    rows = connection.execute('SELECT chunk_index, local_path FROM chunks WHERE segment_id = ? ORDER BY chunk_index', (segment_id,)).fetchall()
    indexes = [row[0] for row in rows]
    if not indexes:
        raise JobError('Segment 没有可处理的 Chunk。')
    if indexes != list(range(len(indexes))):
        raise JobError(f'Segment Chunk index 不连续：{indexes}')
    return segment[0], [data_dir / row[1] for row in rows]


def _run_job(data_dir: Path, db_path: Path, job_id: str, session_id: str, model: str, language: str) -> None:
    try:
        _update_job(data_dir, job_id, status='RUNNING', stage='RECONSTRUCTING', message='正在重建 Session 音频。')
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        try:
            session = connection.execute('SELECT * FROM sessions WHERE id = ?', (session_id,)).fetchone()
            if session is None:
                raise JobError('Session 不存在。')
            marker_rows = connection.execute('SELECT * FROM markers WHERE session_id = ? ORDER BY elapsed_ms, created_at', (session_id,)).fetchall()
            segments = connection.execute('SELECT id, segment_index FROM segments WHERE session_id = ? ORDER BY segment_index', (session_id,)).fetchall()
            segment_paths: list[tuple[int, Path]] = []
            for segment in segments:
                segment_index, chunk_paths = _segment_chunk_paths(data_dir, connection, session_id, segment['id'])
                segment_paths.append((segment_index, reconstruct_segment(data_dir, session_id, segment_index, chunk_paths)))
        finally:
            connection.close()

        audio_path = reconstruct_session(data_dir, session_id, segment_paths)
        _update_job(data_dir, job_id, stage='ASR', message=f'正在使用 {model} 模型转写。')
        transcript = transcribe(audio_path, model_name=model, language=language)
        save_transcript(data_dir, session_id, transcript)
        _update_job(data_dir, job_id, stage='REPORT', message='ASR 完成，正在分析内容并生成报告。', transcriptSegments=len(transcript.get('segments', [])))
        report = build_structured_report(dict(session), [dict(row) for row in marker_rows], transcript, _now_ms())
        save_report(data_dir, session_id, report)
        _update_job(data_dir, job_id, status='COMPLETED', stage='DONE', message='本地 ASR、内容分析和报告已完成。', transcriptSegments=len(transcript.get('segments', [])), markerCount=len(marker_rows), reportStatus=report.get('summaryStatus'))
    except (JobError, ReconstructionError, AsrError, ContentError) as error:
        _update_job(data_dir, job_id, status='FAILED', stage='FAILED', message=str(error), error=str(error))
    except Exception as error:
        _update_job(data_dir, job_id, status='FAILED', stage='FAILED', message='本地处理发生未预期错误。', error=str(error))


def _now_ms() -> int:
    import time
    return round(time.time() * 1000)


def create_job(data_dir: Path, db_path: Path, session_id: str, model: str, language: str) -> dict[str, Any]:
    existing = find_active_job(data_dir, session_id)
    if existing is not None:
        return existing
    job_id = f'job-{uuid.uuid4()}'
    now = _now_ms()
    state = {
        'id': job_id,
        'sessionId': session_id,
        'status': 'QUEUED',
        'stage': 'QUEUED',
        'message': '任务已排队。',
        'model': model,
        'language': language,
        'createdAt': now,
        'updatedAt': now,
    }
    _write_job(data_dir, state)
    _executor.submit(_run_job, data_dir, db_path, job_id, session_id, model, language)
    return state


def recover_jobs(data_dir: Path, db_path: Path) -> int:
    jobs_dir = data_dir / 'processed' / 'jobs'
    if not jobs_dir.is_dir():
        return 0
    recovered = 0
    for path in jobs_dir.glob('job-*.json'):
        try:
            state = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            continue
        if not isinstance(state, dict) or state.get('status') not in {'QUEUED', 'RUNNING'}:
            continue
        job_id = state.get('id')
        session_id = state.get('sessionId')
        model = state.get('model') or 'medium'
        language = state.get('language') or 'zh'
        if not isinstance(job_id, str) or not isinstance(session_id, str):
            continue
        state.update({
            'status': 'QUEUED',
            'stage': 'QUEUED',
            'message': '服务已恢复，任务重新排队。',
            'updatedAt': _now_ms(),
        })
        _write_job(data_dir, state)
        _executor.submit(_run_job, data_dir, db_path, job_id, session_id, model, language)
        recovered += 1
    return recovered
