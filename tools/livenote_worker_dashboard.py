"""Local-only dashboard for the LiveNote Worker and Codex Bridge."""

from __future__ import annotations

import json
import os
import subprocess
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
WORKER_INBOX = Path(os.environ.get('LIVENOTE_WORKER_INBOX', ROOT / 'worker-inbox'))
RESTART_SCRIPT = ROOT / 'deploy' / 'windows' / 'Restart-LiveNoteWorker.ps1'
MULTI_CONFIG = os.environ.get('LIVENOTE_MULTI_CONFIG', '').strip()
MULTI_RESTART_SCRIPT = ROOT / 'deploy' / 'windows' / 'Restart-LiveNoteMultiAutomation.ps1'
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


def scoped_service_statuses(name: str) -> list[dict]:
    """Read the single-service status plus all multi-server status files."""
    paths = [RUNTIME_DIR / f'{name}-status.json']
    paths.extend(sorted(RUNTIME_DIR.glob(f'{name}-*-status.json')))
    statuses: list[dict] = []
    seen: set[Path] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        data = read_json(path)
        if not data:
            continue
        updated_at = int(data.get('updatedAt') or 0)
        data['online'] = bool(updated_at and int(time.time() * 1000) - updated_at <= STALE_AFTER_MS and data.get('phase') != 'stopped')
        statuses.append(data)
    return sorted(statuses, key=lambda item: int(item.get('updatedAt') or 0), reverse=True)


def aggregate_service_status(name: str) -> dict:
    statuses = scoped_service_statuses(name)
    if not statuses:
        return service_status(name)
    current = next((item for item in statuses if item.get('online')), statuses[0])
    sources = [{
        'id': item.get('sourceId') or item.get('sourceLabel') or item.get('pid'),
        'label': item.get('sourceLabel') or '未命名来源',
        'online': item.get('online') is True,
        'phase': item.get('phase', ''),
        'message': item.get('message', ''),
        'updatedAt': item.get('updatedAt', 0),
    } for item in statuses]
    aggregate = dict(current)
    aggregate['online'] = any(item['online'] for item in sources)
    aggregate['sources'] = sources
    return aggregate


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


def restart_worker() -> tuple[int, dict[str, str]]:
    if os.name != 'nt':
        return 501, {'ok': 'false', 'message': '当前系统不支持从页面重启 Worker。'}
    worker = aggregate_service_status('worker') if MULTI_CONFIG else service_status('worker')
    if worker.get('task') and worker.get('phase') not in {'waiting', 'failed', 'stopped', 'starting'}:
        return 409, {'ok': 'false', 'message': 'Worker 正在处理任务，请任务结束后再重启。'}
    restart_script = MULTI_RESTART_SCRIPT if MULTI_CONFIG else RESTART_SCRIPT
    if not restart_script.is_file():
        return 500, {'ok': 'false', 'message': '找不到 Worker 重启脚本。'}
    try:
        creation_flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        subprocess.Popen(
            [
                'powershell.exe',
                '-NoProfile',
                '-ExecutionPolicy',
                'Bypass',
                '-File',
                str(restart_script),
                '-ProjectRoot',
                str(ROOT),
                *(['-ConfigPath', MULTI_CONFIG] if MULTI_CONFIG else []),
            ],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
    except OSError as error:
        return 500, {'ok': 'false', 'message': f'启动重启操作失败：{error}'}
    return 202, {'ok': 'true', 'message': 'Worker 重启已开始，页面会自动刷新状态。'}


def _task_id(event: dict) -> str:
    task = event.get('task') if isinstance(event.get('task'), dict) else {}
    return str(event.get('taskId') or task.get('id') or task.get('taskId') or '')


def _local_task_snapshot(task_id: str, source_id: str = '') -> dict:
    candidates = []
    if source_id:
        candidates.append(WORKER_INBOX / source_id / task_id / 'task.json')
    candidates.append(WORKER_INBOX / task_id / 'task.json')
    try:
        for path in candidates:
            if path.is_file():
                value = json.loads(path.read_text(encoding='utf-8'))
                return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def recent_events(limit: int = 40) -> list[dict]:
    lines: list[str] = []
    paths = [RUNTIME_DIR / 'worker-events.jsonl', *sorted(RUNTIME_DIR.glob('worker-events-*.jsonl'))]
    for path in paths:
        try:
            lines.extend(path.read_text(encoding='utf-8').splitlines()[-300:])
        except OSError:
            continue
    if not lines:
        return []
    lines = lines[-600:]
    records: dict[str, dict] = {}
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        task_id = _task_id(event)
        if not task_id:
            continue
        task = event.get('task') if isinstance(event.get('task'), dict) else {}
        source_id = str(event.get('sourceId') or '')
        record_key = f'{source_id}:{task_id}'
        record = records.setdefault(record_key, {
            'taskId': task_id,
            'title': task.get('title') or task_id,
            'task': task or None,
            'ownerId': task.get('ownerId'),
            'ownerName': task.get('ownerName'),
            'createdAt': task.get('createdAt'),
            'startedAt': task.get('startedAt'),
            'durationMs': task.get('durationMs'),
            'service': event.get('service', ''),
            'sourceId': source_id,
            'sourceLabel': event.get('sourceLabel', ''),
            'phase': event.get('phase', ''),
            'taskStatus': task.get('status') or event.get('phase', ''),
            'message': event.get('message', ''),
            'error': event.get('error', ''),
            'result': '',
            'updatedAt': event.get('updatedAt', 0),
            'history': [],
        })
        history_entry = {
            'updatedAt': event.get('updatedAt', 0),
            'service': event.get('service', ''),
            'sourceId': event.get('sourceId', ''),
            'sourceLabel': event.get('sourceLabel', ''),
            'phase': event.get('phase', ''),
            'message': event.get('message', ''),
            'error': event.get('error', ''),
        }
        history = record['history']
        if history and history[-1]['service'] == history_entry['service'] and history[-1]['phase'] == history_entry['phase']:
            history[-1] = history_entry
        else:
            history.append(history_entry)
        merged_task = dict(record.get('task') or {})
        for key, value in task.items():
            if value not in (None, '', '未命名录音'):
                merged_task[key] = value
        record.update({
            'title': merged_task.get('title') or record['title'],
            'task': merged_task or record['task'],
            'ownerId': merged_task.get('ownerId') or record.get('ownerId'),
            'ownerName': merged_task.get('ownerName') or record.get('ownerName'),
            'createdAt': merged_task.get('createdAt') or record.get('createdAt'),
            'startedAt': merged_task.get('startedAt') or record.get('startedAt'),
            'durationMs': merged_task.get('durationMs') or record.get('durationMs'),
            'service': event.get('service', record['service']),
            'sourceId': event.get('sourceId', record.get('sourceId', '')),
            'sourceLabel': event.get('sourceLabel', record.get('sourceLabel', '')),
            'phase': event.get('phase', record['phase']),
            'taskStatus': task.get('status') or event.get('phase', record['taskStatus']),
            'message': event.get('message', record['message']),
            'error': event.get('error', ''),
            'updatedAt': event.get('updatedAt', record['updatedAt']),
        })
        if event.get('phase') in {'completed', 'failed', 'orphaned'}:
            record['result'] = event.get('error') or event.get('message', '')
    for record in records.values():
        local_task = _local_task_snapshot(record['taskId'], record.get('sourceId', ''))
        if not local_task:
            continue
        merged_task = dict(record.get('task') or {})
        for key, value in local_task.items():
            if value not in (None, '', '未命名录音'):
                merged_task[key] = value
        record.update({
            'title': merged_task.get('title') or record['title'],
            'task': merged_task,
            'ownerId': merged_task.get('ownerId') or record.get('ownerId'),
            'ownerName': merged_task.get('ownerName') or record.get('ownerName'),
            'createdAt': merged_task.get('createdAt') or record.get('createdAt'),
            'startedAt': merged_task.get('startedAt') or record.get('startedAt'),
            'durationMs': merged_task.get('durationMs') or record.get('durationMs'),
            'taskStatus': merged_task.get('status') or record['taskStatus'],
            'updatedAt': max(int(record.get('updatedAt') or 0), int(merged_task.get('updatedAt') or 0)),
        })
        if merged_task.get('status') == 'FAILED':
            record['result'] = merged_task.get('errorMessage') or record.get('result') or '处理失败'
    return sorted(records.values(), key=lambda record: record.get('updatedAt', 0), reverse=True)[:limit]


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
                'worker': aggregate_service_status('worker'),
                'workerSources': scoped_service_statuses('worker'),
                'codex': aggregate_service_status('codex'),
                'codexSources': scoped_service_statuses('codex'),
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

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == '/api/worker/restart':
            status, payload = restart_worker()
            self.send_json(payload, status=status)
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

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
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
