"""Regression test for one dashboard activity row per task."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import tools.livenote_worker_dashboard as dashboard  # noqa: E402


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        dashboard.RUNTIME_DIR = Path(temporary_directory)
        events = [
            {'service': 'worker', 'phase': 'waiting', 'message': '等待新的录音任务。', 'updatedAt': 1, 'task': None},
            {
                'service': 'worker',
                'phase': 'claiming',
                'message': '正在领取任务。',
                'updatedAt': 2,
                'task': {
                    'id': 'task-1',
                    'title': '第一次录音',
                    'ownerName': '张三',
                    'startedAt': 1760000000000,
                    'durationMs': 125000,
                    'status': 'READY',
                },
            },
            {
                'service': 'worker',
                'phase': 'transcribing',
                'message': '本地 Whisper 正在识别录音。',
                'updatedAt': 3,
                'task': {'id': 'task-1', 'title': '第一次录音', 'status': 'PROCESSING'},
            },
            {
                'service': 'codex',
                'phase': 'completed',
                'message': '总结已回传 ECS，等待管理员审核。',
                'updatedAt': 4,
                'task': {'id': 'task-1', 'title': '第一次录音', 'status': 'REVIEW'},
            },
        ]
        (dashboard.RUNTIME_DIR / 'worker-events.jsonl').write_text(
            '\n'.join(json.dumps(event, ensure_ascii=False) for event in events) + '\n',
            encoding='utf-8',
        )

        records = dashboard.recent_events()
        assert len(records) == 1
        assert records[0]['taskId'] == 'task-1'
        assert records[0]['phase'] == 'completed'
        assert records[0]['taskStatus'] == 'REVIEW'
        assert records[0]['ownerName'] == '张三'
        assert records[0]['startedAt'] == 1760000000000
        assert records[0]['durationMs'] == 125000
        assert [entry['phase'] for entry in records[0]['history']] == ['claiming', 'transcribing', 'completed']


if __name__ == '__main__':
    main()
