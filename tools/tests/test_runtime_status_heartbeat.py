import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import livenote_runtime_status as runtime_status


class RuntimeStatusHeartbeatTests(unittest.TestCase):
    def test_refresh_preserves_status_without_creating_activity_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            with patch.object(runtime_status, 'RUNTIME_DIR', runtime):
                runtime_status.update_status(
                    'codex',
                    'waiting',
                    '等待新的待总结任务。',
                    task={'id': 'task-1', 'title': '保留任务状态'},
                    error='保留诊断信息',
                    record_event=False,
                )
                before = json.loads((runtime / 'codex-status.json').read_text(encoding='utf-8'))
                time.sleep(0.01)
                runtime_status.refresh_status('codex')
                after = json.loads((runtime / 'codex-status.json').read_text(encoding='utf-8'))

            self.assertGreater(after['updatedAt'], before['updatedAt'])
            self.assertEqual(after['phase'], 'waiting')
            self.assertEqual(after['task'], before['task'])
            self.assertEqual(after['error'], '保留诊断信息')
            self.assertFalse((runtime / 'worker-events.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
