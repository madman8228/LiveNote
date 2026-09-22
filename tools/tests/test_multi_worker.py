import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.livenote_multi_automation import child_command
from tools.livenote_multi_config import load_targets, public_target


class MultiWorkerConfigTests(unittest.TestCase):
    def _write_config(self, directory: str) -> Path:
        path = Path(directory) / 'multi.json'
        path.write_text(json.dumps({
            'targets': [
                {
                    'id': 'local', 'label': '本地 Server',
                    'server': 'http://127.0.0.1:8000/api/v1',
                    'workerId': 'local-pc-local', 'inbox': 'worker-inbox/local',
                    'workerTokenEnv': 'LOCAL_TOKEN',
                },
                {
                    'id': 'ecs', 'label': 'ECS 云端',
                    'server': 'https://example.test/api/v1',
                    'workerId': 'local-pc-ecs', 'inbox': 'worker-inbox/ecs',
                    'workerTokenEnv': 'ECS_TOKEN',
                },
            ],
        }), encoding='utf-8')
        return path

    def test_loads_two_isolated_targets_without_exposing_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'LOCAL_TOKEN': 'local-secret', 'ECS_TOKEN': 'ecs-secret'}):
            targets = load_targets(self._write_config(directory))

        self.assertEqual([target.id for target in targets], ['local', 'ecs'])
        self.assertNotEqual(targets[0].inbox, targets[1].inbox)
        self.assertEqual(targets[1].worker_token, 'ecs-secret')
        self.assertNotIn('worker_token', public_target(targets[1]))

    def test_child_commands_route_each_target_to_its_own_server_and_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'LOCAL_TOKEN': 'local-secret', 'ECS_TOKEN': 'ecs-secret'}):
            targets = load_targets(self._write_config(directory))

        local_command = child_command('worker', targets[0], 'python-test')
        ecs_command = child_command('worker', targets[1], 'python-test')
        self.assertIn('http://127.0.0.1:8000/api/v1', local_command)
        self.assertIn('https://example.test/api/v1', ecs_command)
        self.assertIn(str(targets[0].inbox), local_command)
        self.assertIn(str(targets[1].inbox), ecs_command)

    def test_duplicate_worker_identity_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_config(directory)
            document = json.loads(path.read_text(encoding='utf-8'))
            document['targets'][1]['workerId'] = document['targets'][0]['workerId']
            path.write_text(json.dumps(document), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'workerId 重复'):
                load_targets(path)


if __name__ == '__main__':
    unittest.main()
