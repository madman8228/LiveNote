import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import livenote_worker_dashboard as dashboard


class WorkerDashboardRestartTests(unittest.TestCase):
    def test_start_local_server_launches_storage_mode_when_offline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'Start-LiveNoteApi.ps1'
            script.write_text('', encoding='utf-8')
            with patch.object(dashboard.os, 'name', 'nt'), \
                 patch.object(dashboard, 'START_LOCAL_SERVER_SCRIPT', script), \
                 patch.object(dashboard, 'local_server_status', return_value={'online': False}), \
                 patch.object(dashboard.subprocess, 'Popen') as popen:
                status, payload = dashboard.start_local_server()

        self.assertEqual(status, 202)
        self.assertEqual(payload['ok'], 'true')
        args, kwargs = popen.call_args
        self.assertIn('-File', args[0])
        self.assertIn(str(script), args[0])
        self.assertEqual(kwargs['env']['LIVENOTE_PROCESSING_MODE'], 'storage')
        self.assertEqual(kwargs['env']['LIVENOTE_LIVE_PROCESSING_ENABLED'], '0')
        self.assertEqual(kwargs['env']['LIVENOTE_INSTANCE_ID'], 'local')
        self.assertEqual(kwargs['env']['LIVENOTE_INSTANCE_LABEL'], '本地 Server')

    def test_start_local_server_is_noop_when_already_online(self) -> None:
        with patch.object(dashboard.os, 'name', 'nt'), \
             patch.object(dashboard, 'local_server_status', return_value={'online': True}), \
             patch.object(dashboard.subprocess, 'Popen') as popen:
            status, payload = dashboard.start_local_server()

        self.assertEqual(status, 200)
        self.assertEqual(payload['ok'], 'true')
        self.assertIn('已经运行', payload['message'])
        popen.assert_not_called()

    def test_restart_is_rejected_while_worker_has_active_task(self) -> None:
        with patch.object(dashboard.os, 'name', 'nt'), \
             patch.object(dashboard, 'service_status', return_value={'phase': 'transcribing', 'task': {'id': 'task-1'}}):
            status, payload = dashboard.restart_worker()

        self.assertEqual(status, 409)
        self.assertIn('正在处理任务', payload['message'])

    def test_restart_starts_windows_script_when_worker_is_idle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'Restart-LiveNoteWorker.ps1'
            script.write_text('', encoding='utf-8')
            with patch.object(dashboard.os, 'name', 'nt'), \
                 patch.object(dashboard, 'RESTART_SCRIPT', script), \
                 patch.object(dashboard, 'service_status', return_value={'phase': 'waiting', 'task': None}), \
                 patch.object(dashboard.subprocess, 'Popen') as popen:
                status, payload = dashboard.restart_worker()

        self.assertEqual(status, 202)
        self.assertEqual(payload['ok'], 'true')
        popen.assert_called_once()


if __name__ == '__main__':
    unittest.main()
