"""LiveNote local PC task bridge and storage-mode processor.

In storage mode the optional ``process`` command also runs the local FFmpeg /
Whisper pipeline and uploads only the durable transcript and playable audio
artifact. It never sends audio or transcript data to OpenAI.

Examples:
  python tools/livenote_worker.py list
  python tools/livenote_worker.py pull --limit 3
  python tools/livenote_worker.py process --watch
  python tools/livenote_worker.py upload-result task-... inbox/task-.../knowledge.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    from livenote_runtime_status import update_status
except ImportError:
    from tools.livenote_runtime_status import update_status


DEFAULT_SERVER = os.environ.get('LIVENOTE_SERVER_URL', 'http://127.0.0.1:8000/api/v1').rstrip('/')
DEFAULT_INBOX = Path(os.environ.get('LIVENOTE_WORKER_INBOX', 'worker-inbox'))
API_KEY = os.environ.get('LIVENOTE_API_KEY', '')
WORKER_TOKEN = os.environ.get('LIVENOTE_WORKER_TOKEN', '')
POLL_SECONDS = max(60, int(os.environ.get('LIVENOTE_WORKER_POLL_SECONDS', '300')))
HEARTBEAT_SECONDS = max(60, int(os.environ.get('LIVENOTE_WORKER_HEARTBEAT_SECONDS', '300')))


def report_worker(phase: str, message: str, task: dict[str, Any] | None = None, progress: dict[str, Any] | None = None, error: str = '', record_event: bool = True) -> None:
    task_snapshot = None
    if task:
        task_snapshot = {
            'id': task.get('id'),
            'title': task.get('title') or task.get('sessionId') or '未命名录音',
            'sessionId': task.get('sessionId'),
            'ownerId': task.get('ownerId'),
            'ownerName': task.get('ownerName'),
            'createdAt': task.get('createdAt'),
            'startedAt': task.get('startedAt'),
            'durationMs': task.get('durationMs'),
            'status': task.get('status'),
            'durationMs': task.get('durationMs'),
        }
    update_status('worker', phase, message, task=task_snapshot, progress=progress, error=error, record_event=record_event)


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


def request_upload(server: str, path: str, source: Path, extra_headers: dict[str, str] | None = None) -> dict[str, Any]:
    """Upload a file as multipart/form-data without loading it into memory."""
    boundary = f'----LiveNote{os.urandom(12).hex()}'
    digest, size = file_sha256(source)
    prefix = (
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="upload"; filename="{source.name}"\r\n'
        'Content-Type: audio/webm\r\n\r\n'
    ).encode('utf-8')
    suffix = f'\r\n--{boundary}--\r\n'.encode('utf-8')
    headers = {
        'Accept': 'application/json',
        'Content-Type': f'multipart/form-data; boundary={boundary}',
        'Content-Length': str(len(prefix) + size + len(suffix)),
        'X-File-Sha256': digest,
    }
    if API_KEY:
        headers['X-API-Key'] = API_KEY
    if WORKER_TOKEN:
        headers['X-Worker-Token'] = WORKER_TOKEN
    headers.update(extra_headers or {})

    class MultipartBody:
        def __iter__(self):
            yield prefix
            with source.open('rb') as stream:
                while block := stream.read(1024 * 1024):
                    yield block
            yield suffix

    # urllib accepts a bytes-like body or a file-like object. A small file-like
    # adapter keeps the upload streaming while preserving the standard library.
    class BodyReader:
        def __init__(self):
            self.parts = iter(MultipartBody())
            self.current = b''

        def read(self, amount: int = -1) -> bytes:
            if amount < 0:
                return b''.join(self)
            output = bytearray()
            while len(output) < amount:
                if not self.current:
                    try:
                        self.current = next(self.parts)
                    except StopIteration:
                        break
                take = min(amount - len(output), len(self.current))
                output.extend(self.current[:take])
                self.current = self.current[take:]
            return bytes(output)

        def __iter__(self):
            if self.current:
                yield self.current
                self.current = b''
            yield from self.parts

    request = urllib.request.Request(f'{server}{path}', data=BodyReader(), headers=headers, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'文件回传失败 {error.code}: {error.read().decode("utf-8", errors="replace")}') from error
    except urllib.error.URLError as error:
        raise RuntimeError(f'无法回传文件：{error.reason}') from error


def download_file(server: str, path: str, destination: Path, headers: dict[str, str], expected_sha256: str, expected_size: int) -> None:
    """Download one raw Chunk and verify both its declared size and digest."""
    temporary = destination.with_suffix(destination.suffix + '.part')
    digest = hashlib.sha256()
    size = 0
    request_headers = {'Accept': 'application/octet-stream', **headers}
    if API_KEY:
        request_headers['X-API-Key'] = API_KEY
    if WORKER_TOKEN:
        request_headers['X-Worker-Token'] = WORKER_TOKEN
    request = urllib.request.Request(f'{server}{path}', headers=request_headers)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(request, timeout=300) as response, temporary.open('wb') as output:
            while block := response.read(1024 * 1024):
                output.write(block)
                digest.update(block)
                size += len(block)
        if size != int(expected_size) or digest.hexdigest().lower() != expected_sha256.lower():
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f'Chunk 校验失败：期望 {expected_sha256}/{expected_size}，实际 {digest.hexdigest()}/{size}')
        temporary.replace(destination)
    except urllib.error.HTTPError as error:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f'下载 Chunk 失败 {error.code}: {error.read().decode("utf-8", errors="replace")}') from error
    except urllib.error.URLError as error:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f'无法下载 Chunk：{error.reason}') from error


def worker_headers(worker_id: str, lease_token: str) -> dict[str, str]:
    return {'X-Worker-Id': worker_id, 'X-Task-Lease': lease_token}


def load_lease(task_dir: Path) -> dict[str, Any]:
    path = task_dir / 'lease.json'
    if not path.is_file():
        raise RuntimeError(f'任务租约文件不存在：{path}')
    return json.loads(path.read_text(encoding='utf-8'))


def is_missing_remote_task_error(error: Exception) -> bool:
    text = str(error)
    lowered = text.lower()
    return '404' in text and ('任务不存在' in text or 'task not found' in lowered or 'not found' in lowered)


def quarantine_task_dir(task_dir: Path, reason: str) -> Path:
    """Move an orphaned local task aside without deleting its recording data."""
    orphaned_root = task_dir.parent / 'orphaned'
    orphaned_root.mkdir(parents=True, exist_ok=True)
    target = orphaned_root / f'{task_dir.name}-{int(time.time() * 1000)}'
    task_dir.replace(target)
    (target / 'orphaned.json').write_text(json.dumps({
        'taskId': task_dir.name,
        'reason': reason,
        'quarantinedAt': int(time.time() * 1000),
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    return target


def lease_heartbeat_loop(args: argparse.Namespace, task_id: str, lease: dict[str, Any], stop: threading.Event) -> None:
    """Keep a long local Whisper run leased without touching audio data."""
    headers = worker_headers(args.worker_id, lease['leaseToken'])
    while not stop.wait(HEARTBEAT_SECONDS):
        try:
            request_json(
                args.server,
                f'/tasks/{urllib.parse.quote(task_id)}/heartbeat',
                'POST',
                {'workerId': args.worker_id, 'leaseToken': lease['leaseToken']},
                headers,
            )
        except RuntimeError as error:
            print(f'租约续期失败 {task_id}: {error}', file=sys.stderr)


def storage_manifest(args: argparse.Namespace, task_id: str, task_dir: Path, claimed: dict[str, Any], lease: dict[str, Any]) -> dict[str, Any]:
    headers = worker_headers(args.worker_id, lease['leaseToken'])
    manifest = request_json(args.server, f'/tasks/{urllib.parse.quote(task_id)}/manifest', extra_headers=headers)
    (task_dir / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    chunks = [chunk for segment in manifest.get('segments', []) for chunk in segment.get('chunks', [])]
    completed = 0
    report_worker('downloading', f'正在下载 {len(chunks)} 个 Chunk。', claimed, {'current': completed, 'total': len(chunks), 'unit': 'Chunk'})
    for segment in manifest.get('segments', []):
        for chunk in segment.get('chunks', []):
            destination = task_dir / 'chunks' / segment['id'] / f"{int(chunk['index']):06d}.bin"
            reusable = False
            if destination.is_file():
                digest, size = file_sha256(destination)
                reusable = digest.lower() == str(chunk['sha256']).lower() and size == int(chunk['size'])
            if not reusable:
                download_file(args.server, chunk['downloadPath'], destination, headers, chunk['sha256'], int(chunk['size']))
            completed += 1
            report_worker('downloading', f'正在下载 Chunk（{completed}/{len(chunks)}）。', claimed, {'current': completed, 'total': len(chunks), 'unit': 'Chunk'})
    (task_dir / 'task.json').write_text(json.dumps(manifest.get('task', claimed), ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest


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


def pull_storage_tasks(args: argparse.Namespace) -> int:
    """Claim tasks and download raw Chunks for an ECS storage-only server."""
    report_worker('checking', '正在查看 ECS 上的新任务。', record_event=False)
    response = request_json(args.server, f'/tasks?status=ALL&limit={args.limit * 3}')
    candidates = [task for task in response.get('tasks', []) if task.get('status') == 'READY' or (task.get('claimedBy') == args.worker_id and task.get('status') in {'CLAIMED', 'LOCAL_READY', 'PROCESSING'})]
    tasks: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate.get('status') != 'READY':
            local_task_path = args.inbox / candidate['id'] / 'task.json'
            try:
                local_task = json.loads(local_task_path.read_text(encoding='utf-8')) if local_task_path.is_file() else {}
            except (OSError, json.JSONDecodeError):
                local_task = {}
            if local_task.get('status') == 'FAILED':
                continue
        tasks.append(candidate)
        if len(tasks) >= args.limit:
            break
    if not tasks:
        report_worker('waiting', '等待新的录音任务。', record_event=False)
        return 0
    args.inbox.mkdir(parents=True, exist_ok=True)
    processed = 0
    for task in tasks:
        task_id = task['id']
        task_dir = args.inbox / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        lease: dict[str, Any] = {}
        try:
            report_worker('claiming', '正在领取任务。', task)
            lease_path = task_dir / 'lease.json'
            if task.get('claimedBy') == args.worker_id and lease_path.is_file():
                lease = load_lease(task_dir)
                claimed = task
            else:
                claimed_response = request_json(args.server, f'/tasks/{urllib.parse.quote(task_id)}/claim', 'POST', {'workerId': args.worker_id})
                claimed = claimed_response['task']
                lease = {'workerId': args.worker_id, 'leaseToken': claimed_response['leaseToken']}
                lease_path.write_text(json.dumps(lease, ensure_ascii=False, indent=2), encoding='utf-8')
            manifest = storage_manifest(args, task_id, task_dir, claimed, lease)
            total_chunks = sum(len(segment.get('chunks', [])) for segment in manifest.get('segments', []))
            headers = worker_headers(args.worker_id, lease['leaseToken'])
            if claimed.get('status') != 'LOCAL_READY':
                updated = request_json(args.server, f'/tasks/{urllib.parse.quote(task_id)}/status', 'POST', {'status': 'LOCAL_READY', 'workerId': args.worker_id, 'leaseToken': lease['leaseToken']}, headers)
                (task_dir / 'task.json').write_text(json.dumps(updated.get('task', manifest.get('task', claimed)), ensure_ascii=False, indent=2), encoding='utf-8')
                claimed = updated.get('task', claimed)
            report_worker('ready', '录音已下载，准备在本机处理。', claimed, {'current': total_chunks, 'total': total_chunks, 'unit': 'Chunk'})
            print(f'已下载 Chunk：{task_id} -> {task_dir}')
            processed += 1
        except Exception as error:
            report_worker('failed', '领取或下载失败。', task, error=str(error))
            print(f'拉取失败 {task_id}: {error}', file=sys.stderr)
            try:
                if lease.get('leaseToken'):
                    headers = worker_headers(args.worker_id, lease['leaseToken'])
                    request_json(args.server, f'/tasks/{urllib.parse.quote(task_id)}/status', 'POST', {'status': 'FAILED', 'workerId': args.worker_id, 'leaseToken': lease['leaseToken'], 'errorMessage': str(error)}, headers)
            except Exception:
                pass
    return processed


def process_storage_task(args: argparse.Namespace, task_dir: Path) -> int:
    """Rebuild, transcribe, and upload one task entirely from the local PC."""
    task = json.loads((task_dir / 'task.json').read_text(encoding='utf-8'))
    task_id = task['id']
    if task.get('status') == 'FAILED':
        return 0
    report_worker('checking', '正在确认任务状态。', task)
    current_response = request_json(args.server, f'/tasks/{urllib.parse.quote(task_id)}')
    current_task = current_response.get('task', task)
    current_status = current_task.get('status')
    if current_status in {'FAILED', 'TRANSCRIBED', 'SUMMARIZING', 'REVIEW', 'COMPLETED'}:
        previous_status = task.get('status')
        (task_dir / 'task.json').write_text(json.dumps(current_task, ensure_ascii=False, indent=2), encoding='utf-8')
        if previous_status != current_status:
            terminal_messages = {
                'FAILED': ('failed', '任务已标记失败，已停止继续处理。'),
                'TRANSCRIBED': ('completed', '识别已完成，等待生成总结。'),
                'SUMMARIZING': ('summarizing', '任务正在生成总结。'),
                'REVIEW': ('completed', '总结已生成，等待审核。'),
                'COMPLETED': ('completed', '任务已完成。'),
            }
            terminal_phase, terminal_message = terminal_messages[current_status]
            report_worker(
                terminal_phase,
                terminal_message,
                current_task,
                error=current_task.get('errorMessage', '') if current_status == 'FAILED' else '',
            )
        return 0
    task = current_task
    lease = load_lease(task_dir)
    headers = worker_headers(args.worker_id, lease['leaseToken'])
    status = task.get('status')
    if status == 'LOCAL_READY':
        response = request_json(args.server, f'/tasks/{urllib.parse.quote(task_id)}/status', 'POST', {'status': 'PROCESSING', 'workerId': args.worker_id, 'leaseToken': lease['leaseToken']}, headers)
        task = response.get('task', task)
        (task_dir / 'task.json').write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding='utf-8')
    elif status != 'PROCESSING':
        return 0

    # Imports are intentionally lazy: the ECS storage service can use the same
    # repository without installing the heavy local-only dependencies.
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from server.asr import transcribe
    from server.reconstruction import reconstruct_segment, reconstruct_session

    manifest = json.loads((task_dir / 'manifest.json').read_text(encoding='utf-8'))
    session_id = manifest['sessionId']
    runtime_data = task_dir / 'runtime-data'
    segment_paths: list[tuple[int, Path]] = []
    segments = manifest.get('segments', [])
    report_worker('reconstructing', f'正在拼接 {len(segments)} 个录音段。', task, {'current': 0, 'total': len(segments), 'unit': '录音段'})
    for segment_index, segment in enumerate(segments, start=1):
        chunk_paths = [task_dir / 'chunks' / segment['id'] / f"{int(chunk['index']):06d}.bin" for chunk in segment.get('chunks', [])]
        if not chunk_paths:
            raise RuntimeError(f'Segment 没有 Chunk：{segment["id"]}')
        segment_path = reconstruct_segment(runtime_data, session_id, int(segment['index']), chunk_paths)
        segment_paths.append((int(segment['index']), segment_path))
        report_worker('reconstructing', f'正在拼接录音段（{segment_index}/{len(segments)}）。', task, {'current': segment_index, 'total': len(segments), 'unit': '录音段'})
    audio_path = reconstruct_session(runtime_data, session_id, segment_paths)
    source_hash, _ = file_sha256(audio_path)
    report_worker('transcribing', '本地 Whisper 正在识别录音。', task)
    transcript = transcribe(audio_path, model_name=args.model, language=args.language)
    transcript['sourceHash'] = source_hash
    transcript['remoteTaskId'] = task_id
    report_worker('uploading', '正在把识别结果回传 ECS。', task)
    request_upload(args.server, f'/tasks/{urllib.parse.quote(task_id)}/audio-artifact', audio_path, headers)
    transcript_response = request_json(
        args.server,
        f'/tasks/{urllib.parse.quote(task_id)}/transcript',
        'POST',
        {
            'sourceHash': source_hash,
            'model': transcript.get('model') or args.model,
            'language': transcript.get('language') or args.language,
            'transcript': transcript,
            'durationSeconds': float(task.get('durationMs') or 0) / 1000,
        },
        headers,
    )
    (task_dir / 'transcript.json').write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding='utf-8')
    report_worker('completed', '本机识别完成，等待本地 Codex 生成总结。', task)
    print(f'本地处理完成：{task_id} -> {transcript_response.get("status")}')
    return 1


def process_storage_tasks(args: argparse.Namespace) -> int:
    count = pull_storage_tasks(args)
    if not args.inbox.is_dir():
        return count
    for task_dir in args.inbox.iterdir():
        if not task_dir.is_dir() or not (task_dir / 'task.json').is_file() or not (task_dir / 'manifest.json').is_file():
            continue
        heartbeat_stop = threading.Event()
        heartbeat_thread: threading.Thread | None = None
        try:
            lease = load_lease(task_dir)
            heartbeat_thread = threading.Thread(target=lease_heartbeat_loop, args=(args, task_dir.name, lease, heartbeat_stop), daemon=True)
            heartbeat_thread.start()
            count += process_storage_task(args, task_dir)
        except Exception as error:
            if is_missing_remote_task_error(error):
                task_snapshot: dict[str, Any] | None = None
                try:
                    task_snapshot = json.loads((task_dir / 'task.json').read_text(encoding='utf-8'))
                except (OSError, json.JSONDecodeError):
                    pass
                orphaned_path = quarantine_task_dir(task_dir, str(error))
                report_worker('orphaned', '服务器已找不到这个任务，本地录音已保留并停止重复重试。', task_snapshot, error=str(error))
                print(f'任务已移入待核查目录：{task_dir.name} -> {orphaned_path}', file=sys.stderr)
                continue
            failed_task: dict[str, Any] | None = None
            try:
                failed_task = json.loads((task_dir / 'task.json').read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError):
                pass
            failure_detail = str(error)
            if failed_task is not None:
                failed_task['status'] = 'FAILED'
                failed_task['errorMessage'] = str(error)
                failed_task['errorStage'] = 'LOCAL_PROCESSING'
                try:
                    (task_dir / 'task.json').write_text(json.dumps(failed_task, ensure_ascii=False, indent=2), encoding='utf-8')
                except OSError as write_error:
                    failure_detail = f'{failure_detail}；本地失败状态保存失败：{write_error}'
            print(f'本地处理失败 {task_dir.name}: {error}', file=sys.stderr)
            try:
                lease = load_lease(task_dir)
                headers = worker_headers(args.worker_id, lease['leaseToken'])
                request_json(args.server, f'/tasks/{urllib.parse.quote(task_dir.name)}/status', 'POST', {'status': 'FAILED', 'workerId': args.worker_id, 'leaseToken': lease['leaseToken'], 'errorMessage': str(error)}, headers)
            except Exception as status_error:
                failure_detail = f'{failure_detail}；失败状态回传失败：{status_error}'
            report_worker('failed', '本地处理失败。', failed_task, error=failure_detail)
        finally:
            heartbeat_stop.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=2)
    return count


def watch_storage_tasks(args: argparse.Namespace) -> int:
    print(f'Storage Worker {args.worker_id} 已启动；FFmpeg/Whisper 仅在本机运行。', flush=True)
    report_worker('starting', '本地 Worker 已启动。')
    try:
        while True:
            process_storage_tasks(args)
            report_worker('waiting', '等待新的录音任务。', record_event=False)
            time.sleep(max(2, args.interval))
    except KeyboardInterrupt:
        report_worker('stopped', '本地 Worker 已停止。')
        print('Storage Worker 已停止。')
        return 0


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

    process_parser = subparsers.add_parser('process', help='在本机 FFmpeg/Whisper 处理存储模式任务')
    process_parser.add_argument('--model', default=os.environ.get('LIVENOTE_WHISPER_MODEL', 'medium'))
    process_parser.add_argument('--language', default=os.environ.get('LIVENOTE_WHISPER_LANGUAGE', 'zh'))
    process_parser.add_argument('--limit', type=int, default=1)
    process_parser.add_argument('--interval', type=int, default=POLL_SECONDS, help='没有任务时的轮询间隔（秒），默认 300 秒')
    process_parser.add_argument('--watch', action='store_true', help='持续轮询新任务')
    process_parser.set_defaults(handler=lambda args: watch_storage_tasks(args) if args.watch else process_storage_tasks(args))

    result_parser = subparsers.add_parser('upload-result', aliases=['submit-result'], help='把本地 knowledge.json 回传服务器草稿')
    result_parser.add_argument('task_id')
    result_parser.add_argument('result')
    result_parser.set_defaults(handler=upload_result)

    processing_parser = subparsers.add_parser('start-processing', help='把本地就绪任务标记为处理中')
    processing_parser.add_argument('task_id')
    processing_parser.set_defaults(handler=start_processing)
    watch_parser = subparsers.add_parser('watch', help='轮询指定任务、恢复下载并维持租约')
    watch_parser.add_argument('--limit', type=int, default=5)
    watch_parser.add_argument('--interval', type=int, default=POLL_SECONDS, help='轮询间隔（秒），默认 300 秒')
    watch_parser.set_defaults(handler=watch_tasks)
    return parser


if __name__ == '__main__':
    arguments = build_parser().parse_args()
    try:
        raise SystemExit(arguments.handler(arguments))
    except (OSError, RuntimeError, json.JSONDecodeError) as error:
        print(f'错误：{error}', file=sys.stderr)
        raise SystemExit(1)
