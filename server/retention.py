from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any


class RetentionError(RuntimeError):
    pass


def _safe_data_path(data_dir: Path, relative_path: str) -> Path:
    root = data_dir.resolve()
    candidate = (data_dir / relative_path).resolve()
    if root != candidate and root not in candidate.parents:
        raise RetentionError(f'拒绝删除数据目录之外的文件：{relative_path}')
    return candidate


def _job_belongs_to_session(path: Path, session_id: str) -> bool:
    try:
        value: Any = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and value.get('sessionId') == session_id


def delete_session_data(data_dir: Path, db_path: Path, session_id: str) -> dict[str, int]:
    """Delete one server-side Session and all derived files.

    The database rows are removed in a transaction before filesystem cleanup.
    Files are scoped to the server data directory and are deleted best-effort
    after the transaction commits, so a path mistake can never escape DATA_DIR.
    """
    file_paths: list[Path] = []
    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        session = connection.execute('SELECT id FROM sessions WHERE id = ?', (session_id,)).fetchone()
        if session is None:
            raise KeyError(session_id)

        segments = connection.execute('SELECT id FROM segments WHERE session_id = ?', (session_id,)).fetchall()
        chunks = connection.execute('SELECT local_path FROM chunks WHERE session_id = ?', (session_id,)).fetchall()
        file_paths.extend(_safe_data_path(data_dir, str(row['local_path'])) for row in chunks)

        connection.execute('DELETE FROM chunks WHERE session_id = ?', (session_id,))
        connection.execute('DELETE FROM markers WHERE session_id = ?', (session_id,))
        connection.execute('DELETE FROM segments WHERE session_id = ?', (session_id,))
        connection.execute('DELETE FROM sessions WHERE id = ?', (session_id,))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    session_roots = [
        data_dir / 'sessions' / session_id,
        data_dir / 'reconstructed' / 'sessions' / session_id,
        data_dir / 'processed' / 'sessions' / session_id,
    ]
    for root in session_roots:
        _safe_data_path(data_dir, str(root.relative_to(data_dir)))
        shutil.rmtree(root, ignore_errors=True)

    jobs_removed = 0
    jobs_dir = data_dir / 'processed' / 'jobs'
    if jobs_dir.is_dir():
        for path in jobs_dir.glob('job-*.json'):
            if _job_belongs_to_session(path, session_id):
                _safe_data_path(data_dir, str(path.relative_to(data_dir)))
                path.unlink(missing_ok=True)
                jobs_removed += 1

    for path in file_paths:
        path.unlink(missing_ok=True)

    return {
        'segments': len(segments),
        'chunks': len(chunks),
        'jobs': jobs_removed,
    }
