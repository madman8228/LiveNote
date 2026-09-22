import json
import tempfile
import unittest
from pathlib import Path

from tools import livenote_worker


class WorkerFailureStateTests(unittest.TestCase):
    def test_local_failure_is_terminal_and_keeps_task_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task_dir = Path(directory) / 'task-1'
            task_dir.mkdir()
            (task_dir / 'task.json').write_text(json.dumps({
                'id': 'task-1',
                'title': '测试录音',
                'ownerName': 'test01',
                'status': 'PROCESSING',
            }), encoding='utf-8')

            failed = livenote_worker.mark_local_task_failed(task_dir, '识别失败')

            self.assertEqual(failed['status'], 'FAILED')
            self.assertEqual(failed['ownerName'], 'test01')
            self.assertEqual(json.loads((task_dir / 'task.json').read_text(encoding='utf-8'))['status'], 'FAILED')


if __name__ == '__main__':
    unittest.main()
