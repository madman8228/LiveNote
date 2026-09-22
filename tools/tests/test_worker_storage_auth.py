import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import livenote_worker


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size: int = -1) -> bytes:
        if not self.body:
            return b''
        body, self.body = self.body, b''
        return body


class WorkerStorageAuthTests(unittest.TestCase):
    def test_chunk_download_carries_server_and_worker_credentials(self) -> None:
        body = b'chunk-data'
        captured = {}

        def fake_urlopen(request, timeout):
            captured['headers'] = dict(request.header_items())
            return _Response(body)

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'chunk.bin'
            with patch.object(livenote_worker, 'API_KEY', 'api-key'), \
                 patch.object(livenote_worker, 'WORKER_TOKEN', 'worker-token'), \
                 patch.object(livenote_worker.urllib.request, 'urlopen', fake_urlopen):
                livenote_worker.download_file(
                    'https://example.test/api/v1',
                    '/tasks/task-1/chunks/segment-1/0',
                    destination,
                    {'X-Worker-Id': 'local-pc', 'X-Task-Lease': 'lease-token'},
                    hashlib.sha256(body).hexdigest(),
                    len(body),
                )

            self.assertEqual(captured['headers']['X-api-key'], 'api-key')
            self.assertEqual(captured['headers']['X-worker-token'], 'worker-token')
            self.assertEqual(captured['headers']['X-worker-id'], 'local-pc')
            self.assertEqual(captured['headers']['X-task-lease'], 'lease-token')
            self.assertEqual(destination.read_bytes(), body)


if __name__ == '__main__':
    unittest.main()
