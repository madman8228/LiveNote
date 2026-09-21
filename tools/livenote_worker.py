"""LiveNote local PC task bridge.

This tool only moves tasks and files between the LiveNote server and a local
processing directory. It does not call OpenAI, run a local ASR model, or
decide how the audio should be summarized.

Examples:
  python tools/livenote_worker.py list
  python tools/livenote_worker.py pull --limit 3
  python tools/livenote_worker.py upload-result task-... inbox/task-.../knowledge.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_SERVER = os.environ.get('LIVENOTE_SERVER_URL', 'http://127.0.0.1:8000/api/v1').rstrip('/')
DEFAULT_INBOX = Path(os.environ.get('LIVENOTE_WORKER_INBOX', 'worker-inbox'))
API_KEY = os.environ.get('LIVENOTE_API_KEY', '')
WORKER_TOKEN = os.environ.get('LIVENOTE_WORKER_TOKEN', '')


def request_json(server: str, path: str, method: str = 'GET', payload: dict[str, Any] | None = None, extra_headers: dict[str, str] | None = None) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    headers = {'Accept': 'application/json'}
    if body is not None:
        headers['Content-Type'] = 'application/json'
    if API_KEY:
        headers['X-API-Key'] = API_KEY
    if WORKER_TOKEN:
        headers['X-Worker-Token'] = WORKER_TOKEN
    headers.update(extra_headers or {})
    request = urllib.request.Request(f'{server}{path}', data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as error:
        detail = error.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'服务器请求失败 {error.code}: {detail}') from error
    except urllib.error.URLError as error:
        raise RuntimeError(f'无法连接服务器：{error.reason}') from error


def request_bytes(server: str, path: str, destination: Path, extra_headers: dict[str, str] | None = None) -> str | None:
    headers = {'Accept': 'audio/webm'}
    if API_KEY:
        headers['X-API-Key'] = API_KEY
    if WORKER_TOKEN:
        headers['X-Worker-Token'] = WORKER_TOKEN
    headers.update(extra_headers or {})
    request = urllib.request.Request(f'{server}{path}', headers=headers)
    try:
        temporary = destination.with_suffix(destination.suffix + '.part')
        digest = hashlib.sha256()
        with urllib.request.urlopen(request, timeout=300) as response, temporary.open('wb') as output:
            expected_length = response.headers.get('Content-Length')
            size = 0
            while block := response.read(1024 * 1024):
                output.write(block)
                digest.update(block)
                size += len(block)
            if expected_length and int(expected_length) != size:
                temporary.unlink(missing_ok=True)
                raise RuntimeError('音频 Content-Length 校验失败')
        temporary.replace(destination)
        return digest.hexdigest()
    except urllib.error.HTTPError as error:
        detail = error.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'下载音频失败 {error.code}: {detail}') from error
    except urllib.error.URLError as error:
        raise RuntimeError(f'无法下载音频：{error.reason}') from error


def file_sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def list_tasks(args: argparse.Namespace) -> int:
    response = request_json(args.server, f'/tasks?status={urllib.parse.quote(args.status)}&limit={args.limit}')
    tasks = response.get('tasks', [])
    if not tasks:
        print('没有可处理任务。')
        return 0
    for task in tasks:
        duration = (task.get('durationMs') or 0) / 1000
        print(f"{task['id']} | {task.get('status')} | {task.get('title') or '未命名'} | {duration:.0f}s | {task['sessionId']}")
    return 0


def pull_tasks(args: argparse.Namespace) -> int:
    response = request_json(args.server, f'/tasks?status=ALL&limit={args.limit * 3}')
    tasks = [task for task in response.get('tasks', []) if task.get('status') == 'READY' or (task.get('claimedBy') == args.worker_id and task.get('status') in {'CLAIMED', 'LOCAL_READY'})][:args.limit]
    if not tasks:
        print('没有 READY 任务。')
        return 0
    args.inbox.mkdir(parents=True, exist_ok=True)
    pulled = 0
    for task in tasks:
        task_id = task['id']
        task_dir = args.inbox / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        lease: dict[str, Any] = {}
        try:
            lease_path = task_dir / 'lease.json'
            if task.get('claimedBy') == args.worker_id and lease_path.is_file():
                lease = json.loads(lease_path.read_text(encoding='utf-8'))
                claimed = task
            else:
                response = request_json(args.server, f'/tasks/{urllib.parse.quote(task_id)}/claim', 'POST', {'workerId': args.worker_id})
                claimed = response['task']
                lease = {'workerId': args.worker_id, 'leaseToken': response['leaseToken']}
                lease_path.write_text(json.dumps(lease, ensure_ascii=False, indent=2), encoding='utf-8')
            (task_dir / 'task.json').write_text(json.dumps(claimed, ensure_ascii=False, indent=2), encoding='utf-8')
            headers = {'X-Worker-Id': args.worker_id, 'X-Task-Lease': lease['leaseToken']}
            audio_path = task_dir / 'audio.webm'
            manifest_path = task_dir / 'manifest.json'
            manifest: dict[str, Any] = {}
            if manifest_path.is_file():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
                except json.JSONDecodeError:
                    manifest = {}
            reusable = audio_path.is_file() and manifest.get('taskId') == task_id and manifest.get('sessionId') == claimed.get('sessionId') and manifest.get('workerId') == args.worker_id
            if reusable:
                digest, size = file_sha256(audio_path)
                reusable = digest == manifest.get('sha256') and size == manifest.get('size')
            if not reusable:
                digest = request_bytes(args.server, f'/tasks/{urllib.parse.quote(task_id)}/audio', audio_path, headers)
                if not digest:
                    raise RuntimeError('音频下载没有返回校验值')
                _, size = file_sha256(audio_path)
                manifest = {'taskId': task_id, 'sessionId': claimed.get('sessionId'), 'workerId': args.worker_id, 'source': f'/tasks/{task_id}/audio', 'sha256': digest, 'size': size, 'downloadedAt': int(time.time() * 1000)}
                manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
            if claimed.get('status') != 'LOCAL_READY':
                response = request_json(args.server, f'/tasks/{urllib.parse.quote(task_id)}/status', 'POST', {'status': 'LOCAL_READY', 'workerId': args.worker_id, 'leaseToken': lease['leaseToken']}, headers)
                claimed = response.get('task', claimed)
                (task_dir / 'task.json').write_text(json.dumps(claimed, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f"已就绪：{task_id} -> {task_dir} ({'复用已校验文件' if reusable else '重新下载'})")
            pulled += 1
        except Exception as error:
            print(f'拉取失败 {task_id}: {error}', file=sys.stderr)
            try:
                if 'lease' in locals() and lease.get('leaseToken'):
                    headers = {'X-Worker-Id': args.worker_id, 'X-Task-Lease': lease['leaseToken']}
                    request_json(args.server, f'/tasks/{urllib.parse.quote(task_id)}/status', 'POST', {'status': 'FAILED', 'workerId': args.worker_id, 'leaseToken': lease['leaseToken'], 'errorMessage': str(error)}, headers)
            except Exception:
                pass
    return 0 if pulled else 1


def upload_result(args: argparse.Namespace) -> int:
    result_path = Path(args.result)
    document = json.loads(result_path.read_text(encoding='utf-8'))
    version = int(document.get('version', 1))
    result = document.get('result', document)
    if not isinstance(result, dict):
        raise RuntimeError('knowledge.json 必须是 JSON 对象，或包含 result 对象。')
    lease_path = result_path.parent / 'lease.json'
    lease = json.loads(lease_path.read_text(encoding='utf-8')) if lease_path.is_file() else {}
    headers = {'X-Worker-Id': args.worker_id, 'X-Task-Lease': lease.get('leaseToken', '')}
    response = request_json(
        args.server,
        f'/tasks/{urllib.parse.quote(args.task_id)}/result',
        'POST',
        {'version': version, 'result': result},
        headers,
    )
    print(f"已回传：{response.get('taskId')}，状态 {response.get('status')}")
    return 0


def start_processing(args: argparse.Namespace) -> int:
    task_dir = args.inbox / args.task_id
    lease = json.loads((task_dir / 'lease.json').read_text(encoding='utf-8'))
    headers = {'X-Worker-Id': args.worker_id, 'X-Task-Lease': lease['leaseToken']}
    response = request_json(args.server, f'/tasks/{urllib.parse.quote(args.task_id)}/status', 'POST', {'status': 'PROCESSING', 'workerId': args.worker_id, 'leaseToken': lease['leaseToken']}, headers)
    (task_dir / 'task.json').write_text(json.dumps(response.get('task', {'id': args.task_id, 'status': 'PROCESSING'}), ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"已开始处理：{response['task']['id']}")
    return 0


def watch_tasks(args: argparse.Namespace) -> int:
    print(f'Worker {args.worker_id} 已启动轮询；不会自动开始人工加工。')
    try:
        while True:
            pull_tasks(args)
            if args.inbox.is_dir():
                for task_dir in args.inbox.iterdir():
                    lease_path = task_dir / 'lease.json'
                    task_path = task_dir / 'task.json'
                    if not task_dir.is_dir() or not lease_path.is_file() or not task_path.is_file():
                        continue
                    lease = json.loads(lease_path.read_text(encoding='utf-8'))
                    task = json.loads(task_path.read_text(encoding='utf-8'))
                    if task.get('status') not in {'CLAIMED', 'LOCAL_READY', 'PROCESSING'}:
                        continue
                    headers = {'X-Worker-Id': args.worker_id, 'X-Task-Lease': lease['leaseToken']}
                    try:
                        refreshed = request_json(args.server, f'/tasks/{urllib.parse.quote(task["id"])}/heartbeat', 'POST', {'workerId': args.worker_id, 'leaseToken': lease['leaseToken']}, headers)
                        if refreshed.get('task'):
                            task_path.write_text(json.dumps(refreshed['task'], ensure_ascii=False, indent=2), encoding='utf-8')
                    except RuntimeError as error:
                        print(f'租约续期失败 {task["id"]}: {error}', file=sys.stderr)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print('Worker 已停止。')
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='LiveNote 本地电脑任务桥接工具')
    parser.add_argument('--server', default=DEFAULT_SERVER, help='LiveNote API 地址')
    parser.add_argument('--inbox', type=Path, default=DEFAULT_INBOX, help='本地任务目录')
    parser.add_argument('--worker-id', default=os.environ.get('LIVENOTE_WORKER_ID', 'local-pc'), help='电脑端 Worker 名称')
    subparsers = parser.add_subparsers(dest='command', required=True)

    list_parser = subparsers.add_parser('list', help='列出任务')
    list_parser.add_argument('--status', default='READY', help='READY/CLAIMED/PROCESSING/COMPLETED/ALL')
    list_parser.add_argument('--limit', type=int, default=50)
    list_parser.set_defaults(handler=list_tasks)

    pull_parser = subparsers.add_parser('pull', help='领取 READY 任务并下载到本地')
    pull_parser.add_argument('--limit', type=int, default=5)
    pull_parser.set_defaults(handler=pull_tasks)

    result_parser = subparsers.add_parser('upload-result', aliases=['submit-result'], help='把本地 knowledge.json 回传服务器草稿')
    result_parser.add_argument('task_id')
    result_parser.add_argument('result')
    result_parser.set_defaults(handler=upload_result)

    processing_parser = subparsers.add_parser('start-processing', help='把本地就绪任务标记为处理中')
    processing_parser.add_argument('task_id')
    processing_parser.set_defaults(handler=start_processing)
    watch_parser = subparsers.add_parser('watch', help='轮询指定任务、恢复下载并维持租约')
    watch_parser.add_argument('--limit', type=int, default=5)
    watch_parser.add_argument('--interval', type=int, default=20)
    watch_parser.set_defaults(handler=watch_tasks)
    return parser


if __name__ == '__main__':
    arguments = build_parser().parse_args()
    try:
        raise SystemExit(arguments.handler(arguments))
    except (OSError, RuntimeError, json.JSONDecodeError) as error:
        print(f'错误：{error}', file=sys.stderr)
        raise SystemExit(1)
