from __future__ import annotations

import json
import hashlib
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


def _knowledge_result_from_report(report: dict[str, Any], session: sqlite3.Row) -> dict[str, Any]:
    summary = report.get('summary')
    if isinstance(summary, dict) and str(summary.get('title', '')).strip():
        return summary

    draft = report.get('localDraft') if isinstance(report.get('localDraft'), dict) else {}
    key_points = [
        str(item.get('note', '')).strip()
        for item in draft.get('keyPoints', [])
        if isinstance(item, dict) and str(item.get('note', '')).strip()
    ]
    action_items = [
        str(item.get('note', '')).strip()
        for item in draft.get('todos', [])
        if isinstance(item, dict) and str(item.get('note', '')).strip()
    ]
    return {
        'title': str(session['title'] or '未命名会话'),
        'overview': str(draft.get('overviewPreview', '')).strip(),
        'keyPoints': key_points,
        'knowledgeStructure': draft.get('knowledgeStructure', []),
        'questions': [],
        'actionItems': action_items,
        'confidenceNotes': ['当前使用本地自动提取草稿，发布前建议快速复核。'],
    }


def _store_task_report(db_path: Path, task_id: str, session_id: str, report: dict[str, Any], session: sqlite3.Row) -> None:
    result = _knowledge_result_from_report(report, session)
    content_json = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    content_hash = hashlib.sha256(content_json.encode('utf-8')).hexdigest()
    updated_at = _now_ms()
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        task = connection.execute('SELECT status FROM processing_tasks WHERE id = ?', (task_id,)).fetchone()
        if task is None:
            raise JobError('处理任务不存在。')
        existing = connection.execute(
            'SELECT id, version FROM result_revisions WHERE task_id = ? AND content_hash = ?',
            (task_id, content_hash),
        ).fetchone()
        if existing is None:
            latest = connection.execute(
                'SELECT COALESCE(MAX(version), 0) AS version FROM result_revisions WHERE task_id = ?',
                (task_id,),
            ).fetchone()['version']
            revision_id = f'revision-{uuid.uuid4()}'
            version = int(latest) + 1
            connection.execute(
                'INSERT INTO result_revisions(id, task_id, session_id, version, content_hash, content_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (revision_id, task_id, session_id, version, content_hash, content_json, updated_at),
            )
        else:
            version = existing['version']
        connection.execute(
            '''UPDATE processing_tasks SET status = 'REVIEW', result_version = ?,
               claimed_by = NULL, claimed_at = NULL, requested_worker_id = NULL,
               lease_token_hash = NULL, lease_expires_at = NULL,
               error_message = '', error_stage = '', updated_at = ? WHERE id = ?''',
            (version, updated_at, task_id),
        )


def _mark_task_failed(db_path: Path, task_id: str, error: Exception) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            '''UPDATE processing_tasks SET status = 'FAILED',
               claimed_by = NULL, claimed_at = NULL, requested_worker_id = NULL,
               lease_token_hash = NULL, lease_expires_at = NULL,
               error_message = ?, error_stage = 'AUTO_PROCESS', updated_at = ? WHERE id = ?''',
            (str(error), _now_ms(), task_id),
        )


def _run_job(data_dir: Path, db_path: Path, job_id: str, session_id: str, model: str, language: str, task_id: str | None = None) -> None:
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
        if task_id:
            _store_task_report(db_path, task_id, session_id, report, session)
        _update_job(data_dir, job_id, status='COMPLETED', stage='DONE', message='本地 ASR、内容分析和报告已完成。', transcriptSegments=len(transcript.get('segments', [])), markerCount=len(marker_rows), reportStatus=report.get('summaryStatus'))
    except (JobError, ReconstructionError, AsrError, ContentError) as error:
        if task_id:
            _mark_task_failed(db_path, task_id, error)
        _update_job(data_dir, job_id, status='FAILED', stage='FAILED', message=str(error), error=str(error))
    except Exception as error:
        if task_id:
            _mark_task_failed(db_path, task_id, error)
        _update_job(data_dir, job_id, status='FAILED', stage='FAILED', message='本地处理发生未预期错误。', error=str(error))


def _now_ms() -> int:
    import time
    return round(time.time() * 1000)


def create_job(data_dir: Path, db_path: Path, session_id: str, model: str, language: str, task_id: str | None = None) -> dict[str, Any]:
    # The check and creation must be atomic from the app's point of view.
    # Otherwise two rapid clicks can both observe no active job and enqueue
    # duplicate reconstruction/ASR work for the same Session.
    with _lock:
        existing = find_active_job(data_dir, session_id)
        if existing is not None and (task_id is None or existing.get('taskId') == task_id):
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
        if task_id:
            state['taskId'] = task_id
        _write_job(data_dir, state)
        _executor.submit(_run_job, data_dir, db_path, job_id, session_id, model, language, task_id)
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
        task_id = state.get('taskId') if isinstance(state.get('taskId'), str) else None
        if not isinstance(job_id, str) or not isinstance(session_id, str):
            continue
        state.update({
            'status': 'QUEUED',
            'stage': 'QUEUED',
            'message': '服务已恢复，任务重新排队。',
            'updatedAt': _now_ms(),
        })
        _write_job(data_dir, state)
        _executor.submit(_run_job, data_dir, db_path, job_id, session_id, model, language, task_id)
        recovered += 1
    return recovered
