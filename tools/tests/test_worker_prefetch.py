from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from server import asr, reconstruction
from tools import livenote_runtime_status, livenote_worker


_CHUNK = b"test-audio-chunk"


class _StorageServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.tasks = {
            f"task-{index}": {
                "id": f"task-{index}",
                "sessionId": f"session-{index}",
                "ownerName": "test01",
                "title": f"录音 {index}",
                "durationMs": 1000,
                "createdAt": index,
                "status": "READY",
            }
            for index in (1, 2)
        }
        self.second_manifest_requested = threading.Event()
        self.second_manifest_overlapped_asr = False
        self.active_status_preserved = False
        self.first_asr_active = threading.Event()
        self.status_path: Path | None = None
        super().__init__(("127.0.0.1", 0), _StorageHandler)


class _StorageHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, value: dict):
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        server: _StorageServer = self.server
        parsed = urlsplit(self.path)
        if parsed.path == "/tasks":
            query = parse_qs(parsed.query)
            status = query.get("status", ["READY"])[0]
            limit = int(query.get("limit", ["1"])[0])
            tasks = [task for task in server.tasks.values() if task["status"] == status]
            tasks.sort(key=lambda task: task["createdAt"])
            self._send_json({"tasks": tasks[:limit]})
            return

        if parsed.path.startswith("/chunks/"):
            self.send_response(200)
            self.send_header("Content-Length", str(len(_CHUNK)))
            self.end_headers()
            self.wfile.write(_CHUNK)
            return

        task_id = parsed.path.removeprefix("/tasks/").split("/", 1)[0]
        task = server.tasks[task_id]
        if parsed.path.endswith("/manifest"):
            if task_id == "task-2":
                server.second_manifest_overlapped_asr = server.first_asr_active.is_set()
                if server.status_path and server.status_path.is_file():
                    active_status = json.loads(server.status_path.read_text(encoding="utf-8"))
                    server.active_status_preserved = (
                        active_status.get("phase") == "transcribing"
                        and (active_status.get("task") or {}).get("id") == "task-1"
                    )
                server.second_manifest_requested.set()
            self._send_json({
                "task": task,
                "taskId": task_id,
                "sessionId": task["sessionId"],
                "segments": [{
                    "id": f"segment-{task_id}",
                    "index": 1,
                    "chunks": [{
                        "index": 0,
                        "sha256": hashlib.sha256(_CHUNK).hexdigest(),
                        "size": len(_CHUNK),
                        "downloadPath": f"/chunks/{task_id}",
                    }],
                }],
            })
            return
        self._send_json({"task": task})

    def do_POST(self):
        server: _StorageServer = self.server
        parsed = urlsplit(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        task_id = parsed.path.removeprefix("/tasks/").split("/", 1)[0]
        task = server.tasks[task_id]
        if parsed.path.endswith("/claim"):
            task["status"] = "CLAIMED"
            self._send_json({"task": task, "leaseToken": f"lease-{task_id}"})
            return
        if parsed.path.endswith("/status"):
            payload = json.loads(body)
            task["status"] = payload["status"]
            self._send_json({"task": task})
            return
        if parsed.path.endswith("/transcript"):
            task["status"] = "TRANSCRIBED"
            self._send_json({"taskId": task_id, "status": "TRANSCRIBED"})
            return
        self._send_json({})


class WorkerPrefetchTests(unittest.TestCase):
    def test_next_task_download_overlaps_asr_without_clobbering_active_task(self) -> None:
        server = _StorageServer()
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                args = argparse.Namespace(
                    inbox=root / "inbox",
                    server=f"http://127.0.0.1:{server.server_port}",
                    worker_id="local-pc",
                    limit=1,
                    model="test-model",
                    language="zh",
                )
                server.status_path = root / "runtime" / "worker-status.json"
                active_asr_count = 0
                maximum_asr_count = 0
                count_lock = threading.Lock()

                def fake_transcribe(*_args, **_kwargs):
                    nonlocal active_asr_count, maximum_asr_count
                    with count_lock:
                        active_asr_count += 1
                        maximum_asr_count = max(maximum_asr_count, active_asr_count)
                    try:
                        if server.first_asr_active.is_set():
                            pass
                        else:
                            server.first_asr_active.set()
                            server.second_manifest_requested.wait(timeout=2)
                        return {"text": "已识别", "segments": [], "model": "test-model", "language": "zh"}
                    finally:
                        with count_lock:
                            active_asr_count -= 1

                def fake_reconstruct_segment(_data_dir, _session_id, index, _chunk_paths):
                    return root / f"segment-{index}.webm"

                def fake_reconstruct_session(data_dir, session_id, _segment_paths):
                    audio_path = data_dir / "reconstructed" / "sessions" / session_id / "session.webm"
                    audio_path.parent.mkdir(parents=True, exist_ok=True)
                    audio_path.write_bytes(b"assembled-audio")
                    return audio_path

                with (
                    patch.object(livenote_runtime_status, "RUNTIME_DIR", root / "runtime"),
                    patch.object(asr, "transcribe", side_effect=fake_transcribe),
                    patch.object(reconstruction, "reconstruct_segment", side_effect=fake_reconstruct_segment),
                    patch.object(reconstruction, "reconstruct_session", side_effect=fake_reconstruct_session),
                ):
                    processed = livenote_worker.process_storage_tasks(args)
                    status = json.loads((root / "runtime" / "worker-status.json").read_text(encoding="utf-8"))

            self.assertTrue(server.second_manifest_requested.is_set(), "the next task should be downloaded during the first task's ASR")
            self.assertTrue(server.second_manifest_overlapped_asr)
            self.assertTrue(server.active_status_preserved, "prefetch must not replace the active task's progress")
            self.assertEqual(maximum_asr_count, 1, "Whisper recognition must remain serial")
            self.assertGreaterEqual(processed, 2)
            self.assertEqual(status["task"]["id"], "task-2")
            self.assertEqual(server.tasks["task-1"]["status"], "TRANSCRIBED")
            self.assertEqual(server.tasks["task-2"]["status"], "TRANSCRIBED")
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
