import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import livenote_worker


class WorkerOrphanTaskTests(unittest.TestCase):
    def test_missing_remote_task_is_quarantined_instead_of_retried(self) -> None:
        task_id = 'task-missing-on-server'
        with tempfile.TemporaryDirectory() as directory:
            inbox = Path(directory) / 'worker-inbox'
            task_dir = inbox / task_id
            task_dir.mkdir(parents=True)
            (task_dir / 'task.json').write_text(json.dumps({
                'id': task_id,
                'status': 'LOCAL_READY',
                'title': '本地遗留任务',
            }), encoding='utf-8')
            (task_dir / 'manifest.json').write_text('{}', encoding='utf-8')
            (task_dir / 'lease.json').write_text(json.dumps({
                'workerId': 'local-pc',
                'leaseToken': 'lease-token',
            }), encoding='utf-8')
            (task_dir / 'audio.webm').write_bytes(b'keep-me')

            args = argparse.Namespace(
                inbox=inbox,
                server='https://example.test/api/v1',
                worker_id='local-pc',
            )
            missing = RuntimeError('服务器请求失败 404: {"detail":"任务不存在"}')
            with patch.object(livenote_worker, 'pull_storage_tasks', return_value=0), \
                 patch.object(livenote_worker, 'lease_heartbeat_loop'), \
                 patch.object(livenote_worker, 'request_json', side_effect=missing), \
                 patch.object(livenote_worker, 'report_worker'):
                livenote_worker.process_storage_tasks(args)

            self.assertFalse(task_dir.exists())
            orphaned = list((inbox / 'orphaned').glob(f'{task_id}-*'))
            self.assertEqual(len(orphaned), 1)
            self.assertEqual((orphaned[0] / 'audio.webm').read_bytes(), b'keep-me')


if __name__ == '__main__':
    unittest.main()
