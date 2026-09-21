import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


_ROOT = Path(tempfile.mkdtemp(prefix='livenote-playback-diagnostic-test-'))
with patch.dict(os.environ, {
    'LIVENOTE_DATA_DIR': str(_ROOT / 'data'),
    'LIVENOTE_DB_PATH': str(_ROOT / 'livenote.sqlite3'),
    'LIVENOTE_REQUIRE_DEVICE_AUTH': '0',
}, clear=False):
    from server import main


class PlaybackDiagnosticEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.DATA_DIR = _ROOT / 'data'
        main.DB_PATH = _ROOT / 'livenote.sqlite3'
        main.init_db()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(_ROOT, ignore_errors=True)

    def test_diagnostic_snapshot_is_saved_as_the_explicit_single_report(self) -> None:
        from fastapi.testclient import TestClient

        report = {
            'kind': 'playback-diagnostic',
            'schemaVersion': 1,
            'clientReportId': 'client-test-1',
            'sessionId': 'session-test-1',
            'attemptId': 'attempt-test-1',
            'buildId': 'build-test',
            'events': [{'type': 'media-sample', 'data': {'currentTime': 1.0, 'duration': 2.0}}],
            'preparation': [],
        }
        with TestClient(main.app) as client:
            response = client.post(
                '/api/v1/diagnostics',
                data={'description': 'playback clientReportId=client-test-1 sessionId=session-test-1'},
                files={'snapshot': ('snapshot.json', json.dumps(report).encode('utf-8'), 'application/json')},
            )
        self.assertEqual(response.status_code, 200, response.text)
        diagnostic_id = response.json()['diagnosticId']
        diagnostic_dir = main.DATA_DIR / 'diagnostics' / diagnostic_id
        self.assertEqual(json.loads((diagnostic_dir / 'snapshot.json').read_text(encoding='utf-8')), report)
        metadata = json.loads((diagnostic_dir / 'metadata.json').read_text(encoding='utf-8'))
        self.assertEqual(metadata['description'], 'playback clientReportId=client-test-1 sessionId=session-test-1')
        self.assertEqual(metadata['files'][0]['name'], 'snapshot.json')


if __name__ == '__main__':
    unittest.main()
