"""Run local Codex summaries for transcripts stored on a remote LiveNote API.

The bridge deliberately runs on the user's PC. ECS only exposes the durable
transcript and receives the structured draft; no model credentials or Codex
process are placed on the storage server.

Examples:
  python tools/livenote_codex_bridge.py once
  python tools/livenote_codex_bridge.py once --task-id task-...
  python tools/livenote_codex_bridge.py watch
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
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


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SERVER = os.environ.get('LIVENOTE_SERVER_URL', 'http://127.0.0.1:8000/api/v1').rstrip('/')
API_KEY = os.environ.get('LIVENOTE_API_KEY', '')
WORKER_TOKEN = os.environ.get('LIVENOTE_WORKER_TOKEN', '')
WORKER_ID = os.environ.get('LIVENOTE_WORKER_ID', 'local-pc')
POLL_SECONDS = max(60, int(os.environ.get('LIVENOTE_CODEX_POLL_SECONDS', '300')))


def report_codex(phase: str, message: str, task: dict[str, Any] | None = None, error: str = '') -> None:
    task_snapshot = None
    if task:
        task_snapshot = {
            'id': task.get('taskId') or task.get('id'),
            'title': task.get('title') or task.get('sessionTitle') or '未命名录音',
            'status': task.get('status'),
        }
    update_status('codex', phase, message, task=task_snapshot, error=error)


def request_json(server: str, path: str, method: str = 'GET', payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    headers = {'Accept': 'application/json'}
    if body is not None:
        headers['Content-Type'] = 'application/json'
    if API_KEY:
        headers['X-API-Key'] = API_KEY
    if WORKER_TOKEN:
        headers['X-Worker-Token'] = WORKER_TOKEN
    request = urllib.request.Request(f'{server}{path}', data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as error:
        detail = error.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'服务器请求失败 {error.code}: {detail}') from error
    except urllib.error.URLError as error:
        raise RuntimeError(f'无法连接服务器：{error.reason}') from error


def summary_schema_path() -> Path:
    return ROOT / 'tools' / 'knowledge-summary.schema.json'


def read_summary_input(server: str, task_id: str) -> dict[str, Any]:
    return request_json(server, f'/tasks/{urllib.parse.quote(task_id)}/summary-input')


def build_prompt(summary_input: dict[str, Any]) -> str:
    task = summary_input.get('taskId') or '指定任务'
    transcript = summary_input.get('transcript') or {}
    return f'''你是 LiveNote 的本地总结助手。只总结下面这一条任务对应的逐字稿，不要读取、猜测或处理任何其他会话。

任务编号：{task}

请输出一个 JSON 对象，必须符合给定 JSON Schema。字段含义：
- title：简短主题
- overview：用老人容易看懂的中文概括
- keyPoints：关键知识点
- knowledgeStructure：按主题分组的知识结构
- questions：重要问答；无法确认的问题不要编造，startMs 可以省略
- actionItems：行动建议
- confidenceNotes：需要提醒读者的不确定性、个人经验和风险

必须忠实区分“嘉宾说法”“听众经历”和“有充分证据的结论”。健康相关内容不要把偏方、个人体验或观点写成已经证实的治疗方法；有风险时明确建议咨询专业医生。

逐字稿 JSON：
{json.dumps(transcript, ensure_ascii=False)}
'''


def parse_model_json(text: str) -> dict[str, Any]:
    candidate = text.strip()
    fenced = re.search(r'```(?:json)?\s*(\{.*\})\s*```', candidate, re.DOTALL | re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1)
    else:
        start = candidate.find('{')
        end = candidate.rfind('}')
        if start >= 0 and end > start:
            candidate = candidate[start:end + 1]
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as error:
        raise RuntimeError(f'Codex 输出不是有效 JSON：{error}') from error
    if not isinstance(value, dict):
        raise RuntimeError('Codex 输出必须是 JSON 对象。')
    return value


def run_codex(prompt: str, model: str | None = None) -> dict[str, Any]:
    temporary = tempfile.NamedTemporaryFile(prefix='livenote-codex-', suffix='.json', delete=False)
    output_path = Path(temporary.name)
    temporary.close()
    command = [
        'codex', 'exec', '--ephemeral', '--skip-git-repo-check',
        '--sandbox', 'read-only', '--output-schema', str(summary_schema_path()),
        '--output-last-message', str(output_path), '--color', 'never',
    ]
    if model:
        command.extend(['--model', model])
    command.append('-')
    try:
        completed = subprocess.run(command, input=prompt, text=True, capture_output=True, timeout=1800, check=False)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or '').strip()
            raise RuntimeError(f'本地 Codex 执行失败：{detail[-1500:]}')
        if not output_path.is_file():
            raise RuntimeError('本地 Codex 没有生成总结结果。')
        return parse_model_json(output_path.read_text(encoding='utf-8'))
    finally:
        output_path.unlink(missing_ok=True)


def submit_summary(server: str, summary_input: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    run = summary_input.get('run') or {}
    task_id = str(summary_input.get('taskId') or '')
    payload = {
        'runId': run.get('id'),
        'generation': run.get('generation'),
        'sourceHash': run.get('source_hash'),
        'result': result,
    }
    if not task_id or not payload['runId'] or not payload['generation'] or not payload['sourceHash']:
        raise RuntimeError('ECS 返回的总结运行版本信息不完整。')
    return request_json(server, f'/tasks/{urllib.parse.quote(task_id)}/summary-result', 'POST', payload)


def find_tasks(server: str, task_id: str | None = None) -> list[str]:
    if task_id:
        return [task_id]
    response = request_json(server, '/tasks?status=ALL&limit=200')
    return [str(task['id']) for task in response.get('tasks', []) if task.get('status') in {'TRANSCRIBED', 'SUMMARIZING'}]


def process_one(server: str, task_id: str, model: str | None = None) -> bool:
    summary_input = read_summary_input(server, task_id)
    report_codex('summarizing', '本地 Codex 正在生成总结。', summary_input)
    result = run_codex(build_prompt(summary_input), model)
    response = submit_summary(server, summary_input, result)
    report_codex('completed', '总结已回传 ECS，等待管理员审核。', summary_input)
    print(f'本地 Codex 总结完成：{task_id} -> {response.get("status")}', flush=True)
    return True


def run_once(args: argparse.Namespace) -> int:
    task_ids = find_tasks(args.server, getattr(args, 'task_id', None))
    if not task_ids:
        report_codex('waiting', '等待新的待总结任务。')
        print('没有待总结任务。')
        return 0
    completed = 0
    for task_id in task_ids[:args.limit]:
        try:
            completed += int(process_one(args.server, task_id, args.model))
        except Exception as error:
            report_codex('failed', '本地 Codex 总结失败。', {'taskId': task_id}, str(error))
            print(f'总结失败 {task_id}: {error}', file=sys.stderr)
    return 0 if completed or not task_ids else 1


def run_watch(args: argparse.Namespace) -> int:
    print(f'本地 Codex Bridge 已启动，轮询 {args.server}。', flush=True)
    report_codex('starting', '本地 Codex Bridge 已启动。')
    try:
        while True:
            run_once(args)
            time.sleep(max(60, args.interval))
    except KeyboardInterrupt:
        report_codex('stopped', '本地 Codex Bridge 已停止。')
        print('本地 Codex Bridge 已停止。')
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='LiveNote 本地 Codex 总结桥接器')
    parser.add_argument('--server', default=DEFAULT_SERVER, help='LiveNote ECS API 地址')
    parser.add_argument('--model', default=os.environ.get('LIVENOTE_CODEX_MODEL') or None)
    parser.add_argument('--limit', type=int, default=1)
    parser.add_argument('--interval', type=int, default=POLL_SECONDS, help='轮询间隔（秒），默认 300 秒')
    sub = parser.add_subparsers(dest='command', required=True)
    once = sub.add_parser('once', help='处理当前待总结任务')
    once.add_argument('--task-id', default=None)
    once.set_defaults(handler=run_once)
    watch = sub.add_parser('watch', help='持续自动总结')
    watch.set_defaults(handler=run_watch)
    return parser


if __name__ == '__main__':
    arguments = build_parser().parse_args()
    try:
        raise SystemExit(arguments.handler(arguments))
    except (OSError, RuntimeError, json.JSONDecodeError) as error:
        print(f'错误：{error}', file=sys.stderr)
        raise SystemExit(1)
