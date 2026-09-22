"""Local-only dashboard for the LiveNote Worker and Codex Bridge."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = Path(os.environ.get('LIVENOTE_RUNTIME_LOG_DIR', ROOT / '.runtime-logs'))
HTML_PATH = Path(__file__).with_name('livenote_worker_dashboard.html')
FAVICON_PATH = Path(__file__).with_name('livenote_worker_favicon.svg')
SERVER_URL = os.environ.get('LIVENOTE_SERVER_URL', '').rstrip('/')
API_KEY = os.environ.get('LIVENOTE_API_KEY', '')
STALE_AFTER_MS = 45_000
SERVER_CACHE_MS = max(60_000, int(os.environ.get('LIVENOTE_DASHBOARD_SERVER_CACHE_SECONDS', '300')) * 1000)
_server_cache_lock = threading.Lock()
_server_cache_updated_at = 0
_server_cache_value: dict = {}


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}


def service_status(name: str) -> dict:
    data = read_json(RUNTIME_DIR / f'{name}-status.json')
    updated_at = int(data.get('updatedAt') or 0)
    data['online'] = bool(updated_at and int(time.time() * 1000) - updated_at <= STALE_AFTER_MS and data.get('phase') != 'stopped')
    return data


def server_status() -> dict:
    global _server_cache_updated_at, _server_cache_value
    now = int(time.time() * 1000)
    with _server_cache_lock:
        if _server_cache_updated_at and now - _server_cache_updated_at < SERVER_CACHE_MS:
            return dict(_server_cache_value)
    if not SERVER_URL:
        value = {'online': False, 'message': '未配置 ECS 地址'}
        with _server_cache_lock:
            _server_cache_updated_at, _server_cache_value = now, value
        return dict(value)
    request = urllib.request.Request(f'{SERVER_URL}/health', headers={'Accept': 'application/json'})
    if API_KEY:
        request.add_header('X-API-Key', API_KEY)
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            payload = json.loads(response.read().decode('utf-8'))
        capabilities = payload.get('capabilities') or {}
        value = {
            'online': True,
            'message': 'ECS 连接正常',
            'processingMode': capabilities.get('processingMode'),
            'storageOnly': capabilities.get('storageOnly') is True,
        }
    except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError) as error:
        value = {'online': False, 'message': f'ECS 暂时无法连接：{error}'}
    with _server_cache_lock:
        _server_cache_updated_at, _server_cache_value = now, value
    return dict(value)


def recent_events(limit: int = 40) -> list[dict]:
    path = RUNTIME_DIR / 'worker-events.jsonl'
    try:
        lines = path.read_text(encoding='utf-8').splitlines()[-limit:]
    except OSError:
        return []
    events: list[dict] = []
    for line in reversed(lines):
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def tail_log(name: str, limit: int = 80) -> list[str]:
    safe_name = name if name in {'storage-worker.stdout.log', 'storage-worker.stderr.log', 'codex-bridge.stdout.log', 'codex-bridge.stderr.log'} else 'storage-worker.stdout.log'
    try:
        return (RUNTIME_DIR / safe_name).read_text(encoding='utf-8', errors='replace').splitlines()[-limit:]
    except OSError:
        return []


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = 'LiveNoteWorkerDashboard/1.0'

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in {'/', '/worker'}:
            self.send_file()
            return
        if parsed.path == '/worker-favicon.svg':
            self.send_favicon()
            return
        if parsed.path == '/api/status':
            self.send_json({
                'worker': service_status('worker'),
                'codex': service_status('codex'),
                'server': server_status(),
                'events': recent_events(),
                'updatedAt': int(time.time() * 1000),
            })
            return
        if parsed.path == '/api/logs':
            query = parse_qs(parsed.query)
            name = query.get('name', ['storage-worker.stdout.log'])[0]
            self.send_json({'name': name, 'lines': tail_log(name)})
            return
        self.send_error(404)

    def send_file(self) -> None:
        try:
            body = HTML_PATH.read_bytes()
        except OSError:
            self.send_error(500, 'dashboard file unavailable')
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_favicon(self) -> None:
        try:
            body = FAVICON_PATH.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'image/svg+xml')
        self.send_header('Cache-Control', 'public, max-age=86400')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    host = os.environ.get('LIVENOTE_WORKER_DASHBOARD_HOST', '127.0.0.1')
    port = int(os.environ.get('LIVENOTE_WORKER_DASHBOARD_PORT', '8765'))
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f'LiveNote Worker 页面已启动：http://{host}:{port}/worker', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
