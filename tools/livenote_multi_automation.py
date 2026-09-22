"""Run one local Worker supervisor against multiple LiveNote servers."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from livenote_multi_config import WorkerTarget, load_targets
except ImportError:
    from tools.livenote_multi_config import WorkerTarget, load_targets


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = Path(os.environ.get('LIVENOTE_RUNTIME_LOG_DIR', ROOT / '.runtime-logs'))


@dataclass
class Child:
    kind: str
    target: WorkerTarget
    command: list[str]
    process: subprocess.Popen[Any]


def _scope(target: WorkerTarget) -> str:
    return target.id.replace(' ', '-').lower()


def _child_environment(target: WorkerTarget) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update({
        'LIVENOTE_SERVER_URL': target.server,
        'LIVENOTE_API_KEY': target.api_key,
        'LIVENOTE_WORKER_TOKEN': target.worker_token,
        'LIVENOTE_WORKER_ID': target.worker_id,
        'LIVENOTE_WORKER_INBOX': str(target.inbox),
        'LIVENOTE_RUNTIME_SCOPE': _scope(target),
        'LIVENOTE_SOURCE_ID': target.id,
        'LIVENOTE_SOURCE_LABEL': target.label,
    })
    return environment


def child_command(kind: str, target: WorkerTarget, python: str = sys.executable) -> list[str]:
    if kind == 'worker':
        return [python, str(ROOT / 'tools' / 'livenote_worker.py'), '--server', target.server,
                '--inbox', str(target.inbox), '--worker-id', target.worker_id, 'process', '--watch']
    if kind == 'codex':
        return [python, str(ROOT / 'tools' / 'livenote_codex_bridge.py'), '--server', target.server, 'watch']
    raise ValueError(f'未知的多服务子进程类型：{kind}')


def _log_path(kind: str, target: WorkerTarget, stream: str) -> Path:
    return RUNTIME_DIR / f'{"storage-worker" if kind == "worker" else "codex-bridge"}-{_scope(target)}.{stream}.log'


def start_child(kind: str, target: WorkerTarget, python: str = sys.executable) -> Child:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    stdout = _log_path(kind, target, 'stdout').open('a', encoding='utf-8')
    stderr = _log_path(kind, target, 'stderr').open('a', encoding='utf-8')
    try:
        process = subprocess.Popen(
            child_command(kind, target, python),
            cwd=ROOT,
            env=_child_environment(target),
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
    finally:
        stdout.close()
        stderr.close()
    return Child(kind=kind, target=target, command=child_command(kind, target, python), process=process)


def validate_config(config_path: str | Path) -> list[dict[str, str]]:
    return [{
        'id': target.id,
        'label': target.label,
        'server': target.server,
        'workerId': target.worker_id,
        'inbox': str(target.inbox),
        'workerTokenConfigured': '是' if bool(target.worker_token) else '否',
    } for target in load_targets(config_path)]


def run(config_path: str | Path, python: str = sys.executable) -> int:
    targets = load_targets(config_path)
    children: list[Child] = []
    stopping = False

    def stop(_signal: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    previous_handlers = {name: signal.getsignal(name) for name in (signal.SIGINT, signal.SIGTERM)}
    for name in previous_handlers:
        signal.signal(name, stop)
    try:
        for target in targets:
            children.append(start_child('worker', target, python))
            children.append(start_child('codex', target, python))
            print(f'已启动：{target.label}（Worker + Codex）', flush=True)
        while not stopping:
            for child in children:
                if child.process.poll() is not None:
                    print(f'{child.target.label} 的 {child.kind} 已退出，5 秒后重启。', flush=True)
                    time.sleep(5)
                    replacement = start_child(child.kind, child.target, python)
                    child.process = replacement.process
            time.sleep(1)
    finally:
        for child in children:
            if child.process.poll() is None:
                child.process.terminate()
        for child in children:
            try:
                child.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.process.kill()
        for name, handler in previous_handlers.items():
            signal.signal(name, handler)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description='LiveNote 多服务 Worker 自动处理器')
    parser.add_argument('--config', required=True, help='多服务 JSON 配置路径')
    parser.add_argument('--python', default=sys.executable, help='Python 可执行文件')
    parser.add_argument('--validate-only', action='store_true', help='只校验配置，不启动子进程')
    args = parser.parse_args()
    if args.validate_only:
        import json
        print(json.dumps(validate_config(args.config), ensure_ascii=False, indent=2))
        return 0
    return run(args.config, args.python)


if __name__ == '__main__':
    raise SystemExit(main())
