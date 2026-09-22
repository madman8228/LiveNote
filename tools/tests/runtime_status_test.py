"""Regression test for silent polling status updates."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import tools.livenote_runtime_status as runtime_status  # noqa: E402


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        runtime_status.RUNTIME_DIR = Path(temporary_directory)

        runtime_status.update_status('worker', 'starting', '本地 Worker 已启动。', record_event=True)
        runtime_status.update_status('worker', 'checking', '正在查看 ECS 上的新任务。', record_event=False)
        runtime_status.update_status('worker', 'waiting', '等待新的录音任务。', record_event=False)

        events_path = runtime_status.RUNTIME_DIR / 'worker-events.jsonl'
        assert not events_path.exists(), 'silent polling must not append activity events'

        runtime_status.update_status(
            'worker',
            'claiming',
            '正在领取任务。',
            task={'id': 'task-1', 'title': '第一次录音'},
            record_event=True,
        )
        events = [json.loads(line) for line in events_path.read_text(encoding='utf-8').splitlines()]
        assert len(events) == 1
        assert events[0]['phase'] == 'claiming'


if __name__ == '__main__':
    main()
