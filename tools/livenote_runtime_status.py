"""Small local-only status store shared by the Worker, Codex Bridge and dashboard."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any


RUNTIME_DIR = Path(os.environ.get('LIVENOTE_RUNTIME_LOG_DIR', '.runtime-logs'))
STATUS_SCOPE = os.environ.get('LIVENOTE_RUNTIME_SCOPE', '').strip()
SOURCE_ID = os.environ.get('LIVENOTE_SOURCE_ID', '').strip()
SOURCE_LABEL = os.environ.get('LIVENOTE_SOURCE_LABEL', '').strip()
_lock = threading.Lock()


def _status_path(service: str) -> Path:
    suffix = f'-{STATUS_SCOPE}' if STATUS_SCOPE else ''
    return RUNTIME_DIR / f'{service}{suffix}-status.json'


def _events_path() -> Path:
    suffix = f'-{STATUS_SCOPE}' if STATUS_SCOPE else ''
    return RUNTIME_DIR / f'worker-events{suffix}.jsonl'


def update_status(service: str, phase: str, message: str, *, task: dict[str, Any] | None = None, progress: dict[str, Any] | None = None, error: str = '', summary: dict[str, Any] | None = None, next_poll_at: int | None = None, record_event: bool = True, update_snapshot: bool = True) -> None:
    """Optionally update the service snapshot and append a task event."""
    now = int(time.time() * 1000)
    snapshot: dict[str, Any] = {
        'service': service,
        'phase': phase,
        'message': message,
        'updatedAt': now,
        'pid': os.getpid(),
        'task': task or None,
        'progress': progress or None,
        'error': error,
    }
    if summary:
        snapshot['summary'] = summary
    if next_poll_at is not None:
        snapshot['nextPollAt'] = int(next_poll_at)
    if SOURCE_ID:
        snapshot['sourceId'] = SOURCE_ID
    if SOURCE_LABEL:
        snapshot['sourceLabel'] = SOURCE_LABEL
    try:
        with _lock:
            RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            if update_snapshot:
                target = _status_path(service)
                temporary = target.with_suffix(target.suffix + '.part')
                temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
                temporary.replace(target)
            if record_event and task and (task.get('id') or task.get('taskId')):
                with _events_path().open('a', encoding='utf-8') as output:
                    output.write(json.dumps(snapshot, ensure_ascii=False) + '\n')
                _trim_events()
    except OSError:
        # Status is diagnostic; a locked or unavailable log directory must not
        # stop recording processing.
        pass


def refresh_status(service: str) -> None:
    """Touch an existing status snapshot without changing its visible state."""
    try:
        with _lock:
            target = _status_path(service)
            snapshot = json.loads(target.read_text(encoding='utf-8'))
            snapshot['updatedAt'] = int(time.time() * 1000)
            snapshot['pid'] = os.getpid()
            temporary = target.with_suffix(target.suffix + '.part')
            temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(target)
    except (OSError, json.JSONDecodeError):
        # Status is diagnostic; a locked, unavailable, or not-yet-created
        # snapshot must not stop processing.
        pass


def _trim_events() -> None:
    path = _events_path()
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
        if len(lines) > 300:
            path.write_text('\n'.join(lines[-300:]) + '\n', encoding='utf-8')
    except OSError:
        pass
