import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import livenote_worker_dashboard as dashboard


class WorkerDashboardSourceTests(unittest.TestCase):
    def test_aggregate_status_reports_each_server_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            common = {'service': 'worker', 'phase': 'waiting', 'message': '等待任务', 'updatedAt': 9999999999999, 'task': None}
            local = {**common, 'sourceId': 'local', 'sourceLabel': '本地 Server'}
            ecs = {**common, 'sourceId': 'ecs', 'sourceLabel': 'ECS 云端'}
            (runtime / 'worker-local-status.json').write_text(json.dumps(local), encoding='utf-8')
            (runtime / 'worker-ecs-status.json').write_text(json.dumps(ecs), encoding='utf-8')
            with patch.object(dashboard, 'RUNTIME_DIR', runtime):
                aggregate = dashboard.aggregate_service_status('worker')

        self.assertTrue(aggregate['online'])
        self.assertEqual({source['label'] for source in aggregate['sources']}, {'本地 Server', 'ECS 云端'})

    def test_events_keep_same_task_ids_separate_by_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            events = [
                {
                    'service': 'worker', 'sourceId': 'local', 'sourceLabel': '本地 Server',
                    'phase': 'completed', 'message': '本地完成', 'updatedAt': 1000,
                    'task': {'id': 'same-task', 'title': '本地录音', 'status': 'COMPLETED'},
                },
                {
                    'service': 'worker', 'sourceId': 'ecs', 'sourceLabel': 'ECS 云端',
                    'phase': 'failed', 'message': '云端失败', 'updatedAt': 2000,
                    'task': {'id': 'same-task', 'title': '云端录音', 'status': 'FAILED'},
                },
            ]
            (runtime / 'worker-events-local.jsonl').write_text(
                json.dumps(events[0], ensure_ascii=False) + '\n', encoding='utf-8'
            )
            (runtime / 'worker-events-ecs.jsonl').write_text(
                json.dumps(events[1], ensure_ascii=False) + '\n', encoding='utf-8'
            )
            with patch.object(dashboard, 'RUNTIME_DIR', runtime):
                records = dashboard.recent_events()

        self.assertEqual(len(records), 2)
        self.assertEqual({record['sourceLabel'] for record in records}, {'本地 Server', 'ECS 云端'})


if __name__ == '__main__':
    unittest.main()
