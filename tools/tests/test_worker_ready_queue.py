import argparse
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from tools import livenote_worker


class _QueueServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, tasks: list[dict]):
        self.tasks = {task['id']: task for task in tasks}
        self.claimed_ids: list[str] = []
        super().__init__(('127.0.0.1', 0), _QueueHandler)


class _QueueHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send(self, value: dict):
        body = json.dumps(value).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        server: _QueueServer = self.server
        parsed = urlsplit(self.path)
        if parsed.path == '/tasks':
            query = parse_qs(parsed.query)
            status = query.get('status', ['READY'])[0]
            limit = int(query.get('limit', ['50'])[0])
            tasks = list(server.tasks.values())
            if status != 'ALL':
                tasks = [task for task in tasks if task['status'] == status]
            tasks.sort(key=lambda task: task['createdAt'])
            self._send({'tasks': tasks[:limit]})
            return

        task_id = parsed.path.removeprefix('/tasks/').split('/', 1)[0]
        task = server.tasks[task_id]
        if parsed.path.endswith('/manifest'):
            self._send({'task': task, 'segments': []})
        else:
            # Simulate that the task completed on the server before local ASR begins.
            task['status'] = 'REVIEW'
            self._send({'task': task})

    def do_POST(self):
        server: _QueueServer = self.server
        parsed = urlsplit(self.path)
        length = int(self.headers.get('Content-Length', '0'))
        payload = json.loads(self.rfile.read(length) or b'{}')
        task_id = parsed.path.removeprefix('/tasks/').split('/', 1)[0]
        task = server.tasks[task_id]
        if parsed.path.endswith('/claim'):
            task['status'] = 'CLAIMED'
            task['claimedBy'] = payload['workerId']
            server.claimed_ids.append(task_id)
            self._send({'task': task, 'leaseToken': 'test-lease'})
        else:
            task['status'] = payload['status']
            self._send({'task': task})


class WorkerReadyQueueTests(unittest.TestCase):
    def test_ready_task_is_claimed_beyond_oldest_terminal_rows_and_local_task_is_resumed(self) -> None:
        tasks = [
            {'id': 'old-failed', 'status': 'FAILED', 'createdAt': 1},
            {'id': 'old-review-1', 'status': 'REVIEW', 'createdAt': 2},
            {'id': 'old-review-2', 'status': 'REVIEW', 'createdAt': 3},
            {'id': 'ready-1', 'status': 'READY', 'createdAt': 4},
            {'id': 'ready-2', 'status': 'READY', 'createdAt': 5},
            {'id': 'resumable', 'status': 'REVIEW', 'createdAt': 0},
        ]
        server = _QueueServer(tasks)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                inbox = Path(directory) / 'worker-inbox'
                resume_dir = inbox / 'resumable'
                resume_dir.mkdir(parents=True)
                (resume_dir / 'task.json').write_text(json.dumps({
                    'id': 'resumable',
                    'status': 'PROCESSING',
                }), encoding='utf-8')
                (resume_dir / 'manifest.json').write_text('{}', encoding='utf-8')
                (resume_dir / 'lease.json').write_text(json.dumps({
                    'workerId': 'local-pc',
                    'leaseToken': 'resume-lease',
                }), encoding='utf-8')
                args = argparse.Namespace(
                    inbox=inbox,
                    server=f'http://127.0.0.1:{server.server_port}',
                    worker_id='local-pc',
                    limit=1,
                )
                with patch.object(livenote_worker, 'report_worker'):
                    processed = livenote_worker.process_storage_tasks(args)

                self.assertEqual(processed, 1)
                self.assertEqual(server.claimed_ids, ['ready-1'])
                resumed_task = json.loads((resume_dir / 'task.json').read_text(encoding='utf-8'))
                self.assertEqual(resumed_task['status'], 'REVIEW')
                self.assertTrue((inbox / 'ready-1' / 'manifest.json').is_file())
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
