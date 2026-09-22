"""Small local-only status store shared by the Worker, Codex Bridge and dashboard."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any


RUNTIME_DIR = Path(os.environ.get('LIVENOTE_RUNTIME_LOG_DIR', '.runtime-logs'))
_lock = threading.Lock()


def _status_path(service: str) -> Path:
    return RUNTIME_DIR / f'{service}-status.json'


def _events_path() -> Path:
    return RUNTIME_DIR / 'worker-events.jsonl'


def update_status(service: str, phase: str, message: str, *, task: dict[str, Any] | None = None, progress: dict[str, Any] | None = None, error: str = '') -> None:
    """Write a dashboard-safe status snapshot and append one human-readable event."""
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
    try:
        with _lock:
            RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            target = _status_path(service)
            temporary = target.with_suffix(target.suffix + '.part')
            temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(target)
            with _events_path().open('a', encoding='utf-8') as output:
                output.write(json.dumps(snapshot, ensure_ascii=False) + '\n')
            _trim_events()
    except OSError:
        # Status is diagnostic; a locked or unavailable log directory must not
        # stop recording processing.
        pass


def _trim_events() -> None:
    path = _events_path()
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
        if len(lines) > 300:
            path.write_text('\n'.join(lines[-300:]) + '\n', encoding='utf-8')
    except OSError:
        pass
