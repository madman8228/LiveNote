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
from urllib.parse import parse_qs, quote, urlparse


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = Path(os.environ.get('LIVENOTE_RUNTIME_LOG_DIR', ROOT / '.runtime-logs'))
HTML_PATH = Path(__file__).with_name('livenote_worker_dashboard.html')
FAVICON_PATH = Path(__file__).with_name('livenote_worker_favicon.svg')
WORKER_INBOX = Path(os.environ.get('LIVENOTE_WORKER_INBOX', ROOT / 'worker-inbox'))
RESTART_SCRIPT = ROOT / 'deploy' / 'windows' / 'Restart-LiveNoteWorker.ps1'
START_LOCAL_SERVER_SCRIPT = ROOT / 'deploy' / 'windows' / 'Start-LiveNoteApi.ps1'
MULTI_CONFIG = os.environ.get('LIVENOTE_MULTI_CONFIG', '').strip()
MULTI_RESTART_SCRIPT = ROOT / 'deploy' / 'windows' / 'Restart-LiveNoteMultiAutomation.ps1'
SERVER_URL = os.environ.get('LIVENOTE_SERVER_URL', '').rstrip('/')
API_KEY = os.environ.get('LIVENOTE_API_KEY', '')
WORKER_TOKEN = os.environ.get('LIVENOTE_WORKER_TOKEN', '')
LOCAL_SERVER_URL = os.environ.get('LIVENOTE_LOCAL_SERVER_URL', 'http://127.0.0.1:8000/api/v1').rstrip('/')
LOCAL_API_KEY = os.environ.get('LIVENOTE_LOCAL_API_KEY', '')
STALE_AFTER_MS = 45_000
SERVER_CACHE_MS = max(60_000, int(os.environ.get('LIVENOTE_DASHBOARD_SERVER_CACHE_SECONDS', '300')) * 1000)
LOCAL_SERVER_CACHE_MS = max(5_000, int(os.environ.get('LIVENOTE_DASHBOARD_LOCAL_SERVER_CACHE_SECONDS', '10')) * 1000)
SUMMARY_CACHE_MS = max(5_000, int(os.environ.get('LIVENOTE_DASHBOARD_SUMMARY_CACHE_SECONDS', '15')) * 1000)
_server_cache_lock = threading.Lock()
_server_cache: dict[str, tuple[int, dict]] = {}
_summary_cache_lock = threading.Lock()
_summary_cache: dict[str, tuple[int, dict]] = {}
_summary_error_cache: dict[str, tuple[int, str]] = {}


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
    default_source_label = '本地 Worker' if name == 'worker' else '本地总结' if name == 'codex' else name
    sources = [{
        'id': item.get('sourceId') or item.get('sourceLabel') or item.get('pid'),
        'label': item.get('sourceLabel') or item.get('sourceId') or default_source_label,
        'online': item.get('online') is True,
        'phase': item.get('phase', ''),
        'message': item.get('message', ''),
        'updatedAt': item.get('updatedAt', 0),
    } for item in statuses]
    aggregate = dict(current)
    aggregate['online'] = any(item['online'] for item in sources)
    aggregate['sources'] = sources
    return aggregate


def _server_status(cache_key: str, url: str, api_key: str, source_id: str, online_message: str, offline_message: str, cache_ms: int) -> dict:
    now = int(time.time() * 1000)
    with _server_cache_lock:
        cached = _server_cache.get(cache_key)
        if cached and now - cached[0] < cache_ms:
            return dict(cached[1])
    if not url:
        value = {'online': False, 'sourceId': source_id, 'message': offline_message}
        with _server_cache_lock:
            _server_cache[cache_key] = (now, value)
        return dict(value)
    request = urllib.request.Request(f'{url}/health', headers={'Accept': 'application/json'})
    if api_key:
        request.add_header('X-API-Key', api_key)
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            payload = json.loads(response.read().decode('utf-8'))
        capabilities = payload.get('capabilities') or {}
        value = {
            'online': True,
            'sourceId': source_id,
            'message': online_message,
            'processingMode': capabilities.get('processingMode'),
            'storageOnly': capabilities.get('storageOnly') is True,
        }
    except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError) as error:
        if source_id == 'local-server' and (
            '10061' in str(error) or 'connection refused' in str(error).lower()
        ):
            message = '本地 Server 未启动，请点击“启动本地 Server”。'
        else:
            message = f'{offline_message}：{error}'
        value = {'online': False, 'sourceId': source_id, 'message': message}
    with _server_cache_lock:
        _server_cache[cache_key] = (now, value)
    return dict(value)


def local_server_status() -> dict:
    value = _server_status(
        'local', LOCAL_SERVER_URL, LOCAL_API_KEY, 'local-server',
        '本地 Server 运行正常', '本地 Server 未运行', LOCAL_SERVER_CACHE_MS,
    )
    address = LOCAL_SERVER_URL
    if address.endswith('/api/v1'):
        address = address[:-len('/api/v1')]
    value['address'] = address
    value['addressLink'] = f'{address}/health' if address else ''
    return value


def server_status() -> dict:
    return _server_status(
        'ecs', SERVER_URL, API_KEY, 'ecs',
        'ECS 连接正常', '未配置 ECS 地址' if not SERVER_URL else 'ECS 暂时无法连接', SERVER_CACHE_MS,
    )


def fetch_remote_summary(task_id: str, source_id: str = '') -> dict:
    """Read a generated draft through the Worker-scoped result endpoint.

    The dashboard deliberately uses the existing Worker credential instead of
    asking the local machine to hold an administrator token. A missing draft
    is a normal state while a task is still being processed, so failures are
    treated as an empty result and retried after a short cache window.
    """
    if not task_id or source_id == 'local' or not SERVER_URL or not WORKER_TOKEN:
        return {}
    now = int(time.time() * 1000)
    with _summary_cache_lock:
        cached = _summary_cache.get(task_id)
        if cached and now - cached[0] < SUMMARY_CACHE_MS:
            return dict(cached[1])
    request = urllib.request.Request(
        f'{SERVER_URL}/tasks/{quote(task_id, safe="")}/summary-result',
        headers={'Accept': 'application/json', 'X-Worker-Token': WORKER_TOKEN},
    )
    if API_KEY:
        request.add_header('X-API-Key', API_KEY)
    value: dict = {}
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            payload = json.loads(response.read().decode('utf-8'))
        items = payload.get('items') if isinstance(payload, dict) else None
        if isinstance(items, list) and items and isinstance(items[0], dict) and isinstance(items[0].get('result'), dict):
            value = items[0]['result']
    except urllib.error.HTTPError as error:
        if error.code == 405:
            _summary_error_cache[task_id] = (now, 'ECS 服务尚未更新结构化结果读取接口。')
        elif error.code == 401:
            _summary_error_cache[task_id] = (now, 'ECS 拒绝读取结构化结果，请检查 Worker 凭据。')
        value = {}
    except (OSError, ValueError, urllib.error.URLError):
        value = {}
    with _summary_cache_lock:
        _summary_cache[task_id] = (now, value)
    return dict(value)


def summary_sync_error(task_id: str) -> str:
    cached = _summary_error_cache.get(task_id)
    if not cached:
        return ''
    timestamp, message = cached
    if int(time.time() * 1000) - timestamp >= SUMMARY_CACHE_MS:
        return ''
    return message


def start_local_server() -> tuple[int, dict[str, str]]:
    if os.name != 'nt':
        return 501, {'ok': 'false', 'message': '当前系统不支持从页面启动本地 Server。'}
    if local_server_status().get('online') is True:
        return 200, {'ok': 'true', 'message': '本地 Server 已经运行，无需重复启动。'}
    if not START_LOCAL_SERVER_SCRIPT.is_file():
        return 500, {'ok': 'false', 'message': '找不到本地 Server 启动脚本。'}
    environment = os.environ.copy()
    environment['LIVENOTE_PROCESSING_MODE'] = 'storage'
    environment['LIVENOTE_LIVE_PROCESSING_ENABLED'] = '0'
    environment['LIVENOTE_INSTANCE_ID'] = 'local'
    environment['LIVENOTE_INSTANCE_LABEL'] = '本地 Server'
    environment['LIVENOTE_API_KEY'] = environment.get('LIVENOTE_LOCAL_API_KEY', '')
    environment['LIVENOTE_WORKER_TOKEN'] = environment.get('LIVENOTE_LOCAL_WORKER_TOKEN', '')
    python = environment.get('PYTHON', 'python')
    try:
        creation_flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        subprocess.Popen(
            [
                'powershell.exe',
                '-NoProfile',
                '-ExecutionPolicy',
                'Bypass',
                '-File',
                str(START_LOCAL_SERVER_SCRIPT),
                '-Port',
                '8000',
                '-Python',
                python,
            ],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
    except OSError as error:
        return 500, {'ok': 'false', 'message': f'启动本地 Server 失败：{error}'}
    return 202, {'ok': 'true', 'message': '本地 Server 启动已开始，页面会自动刷新状态。'}


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
                if not isinstance(value, dict):
                    return {}
                knowledge_path = path.parent / 'knowledge.json'
                if knowledge_path.is_file():
                    try:
                        knowledge = json.loads(knowledge_path.read_text(encoding='utf-8'))
                        value['summary'] = knowledge.get('result', knowledge) if isinstance(knowledge, dict) else None
                    except (OSError, json.JSONDecodeError):
                        pass
                return value
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
            'claimedAt': task.get('claimedAt'),
            'downloadedAt': task.get('downloadedAt'),
            'service': event.get('service', ''),
            'sourceId': source_id,
            'sourceLabel': event.get('sourceLabel', ''),
            'phase': event.get('phase', ''),
            'taskStatus': task.get('status') or event.get('phase', ''),
            'message': event.get('message', ''),
            'error': event.get('error', ''),
            'result': '',
            'summary': event.get('summary') or task.get('summary'),
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
            'summary': event.get('summary'),
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
            'claimedAt': merged_task.get('claimedAt') or record.get('claimedAt'),
            'downloadedAt': merged_task.get('downloadedAt') or record.get('downloadedAt'),
            'service': event.get('service', record['service']),
            'sourceId': event.get('sourceId', record.get('sourceId', '')),
            'sourceLabel': event.get('sourceLabel', record.get('sourceLabel', '')),
            'phase': event.get('phase', record['phase']),
            'taskStatus': task.get('status') or event.get('phase', record['taskStatus']),
            'message': event.get('message', record['message']),
            'error': event.get('error', ''),
            'updatedAt': event.get('updatedAt', record['updatedAt']),
            'summary': event.get('summary') or record.get('summary'),
        })
        if event.get('phase') in {'completed', 'failed', 'orphaned'}:
            record['result'] = event.get('error') or event.get('message', '')
    for record in records.values():
        local_task = _local_task_snapshot(record['taskId'], record.get('sourceId', ''))
        if local_task:
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
                'claimedAt': merged_task.get('claimedAt') or record.get('claimedAt'),
                'downloadedAt': merged_task.get('downloadedAt') or record.get('downloadedAt'),
                'taskStatus': merged_task.get('status') or record['taskStatus'],
                'updatedAt': max(int(record.get('updatedAt') or 0), int(merged_task.get('updatedAt') or 0)),
                'summary': local_task.get('summary') or record.get('summary'),
            })
            if merged_task.get('status') == 'FAILED':
                record['result'] = merged_task.get('errorMessage') or record.get('result') or '处理失败'
        if not record.get('summary'):
            remote_summary = fetch_remote_summary(record['taskId'], record.get('sourceId', ''))
            if remote_summary:
                record['summary'] = remote_summary
                record['task'] = dict(record.get('task') or {})
                record['task']['summary'] = remote_summary
            else:
                sync_error = summary_sync_error(record['taskId'])
                if sync_error:
                    record['summarySyncError'] = sync_error
    def task_created_at(record: dict) -> int:
        task = record.get('task') if isinstance(record.get('task'), dict) else {}
        try:
            return int(record.get('createdAt') or task.get('createdAt') or 0)
        except (TypeError, ValueError):
            return 0

    return sorted(records.values(), key=task_created_at, reverse=True)[:limit]


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
                'localServer': local_server_status(),
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
        if parsed.path == '/api/local-server/start':
            status, payload = start_local_server()
            self.send_json(payload, status)
            return
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
