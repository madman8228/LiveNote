"""Configuration loader shared by the multi-server Worker and dashboard."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class WorkerTarget:
    id: str
    label: str
    server: str
    worker_id: str
    inbox: Path
    api_key: str = ''
    worker_token: str = ''


def _required_string(item: dict[str, Any], key: str, target_id: str) -> str:
    value = str(item.get(key) or '').strip()
    if not value:
        raise ValueError(f'多服务配置中的 {target_id} 缺少 {key}')
    return value


def _secret(item: dict[str, Any], value_key: str, env_key: str) -> str:
    configured_env = str(item.get(env_key) or '').strip()
    if configured_env:
        return os.environ.get(configured_env, '')
    return str(item.get(value_key) or '').strip()


def load_targets(config_path: str | Path) -> list[WorkerTarget]:
    path = Path(config_path).expanduser().resolve()
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f'无法读取多服务配置：{path}：{error}') from error
    raw_targets = document.get('targets') if isinstance(document, dict) else document
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ValueError('多服务配置必须包含非空 targets 数组')

    targets: list[WorkerTarget] = []
    ids: set[str] = set()
    worker_ids: set[str] = set()
    inboxes: set[Path] = set()
    for raw in raw_targets:
        if not isinstance(raw, dict):
            raise ValueError('每个多服务目标必须是对象')
        target_id = _required_string(raw, 'id', '目标')
        if target_id in ids:
            raise ValueError(f'多服务目标 id 重复：{target_id}')
        label = _required_string(raw, 'label', target_id)
        server = _required_string(raw, 'server', target_id).rstrip('/')
        worker_id = _required_string(raw, 'workerId', target_id)
        inbox_value = _required_string(raw, 'inbox', target_id)
        inbox = Path(inbox_value).expanduser()
        if not inbox.is_absolute():
            inbox = (ROOT / inbox).resolve()
        if worker_id in worker_ids:
            raise ValueError(f'多服务目标 workerId 重复：{worker_id}')
        if inbox in inboxes:
            raise ValueError(f'多服务目标任务目录重复：{inbox}')
        ids.add(target_id)
        worker_ids.add(worker_id)
        inboxes.add(inbox)
        targets.append(WorkerTarget(
            id=target_id,
            label=label,
            server=server,
            worker_id=worker_id,
            inbox=inbox,
            api_key=_secret(raw, 'apiKey', 'apiKeyEnv'),
            worker_token=_secret(raw, 'workerToken', 'workerTokenEnv'),
        ))
    return targets


def public_target(target: WorkerTarget) -> dict[str, str]:
    """Return dashboard-safe target metadata without credentials."""
    return {
        'id': target.id,
        'label': target.label,
        'server': target.server,
        'workerId': target.worker_id,
        'inbox': str(target.inbox),
    }
