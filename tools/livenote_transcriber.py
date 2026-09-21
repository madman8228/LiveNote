"""Run the local, restartable Whisper processor for LiveNote tasks."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.processing_pipeline import ProcessingError, run_once, watch


DATA_DIR = Path(os.environ.get('LIVENOTE_DATA_DIR', ROOT / 'server' / 'data'))
DB_PATH = Path(os.environ.get('LIVENOTE_DB_PATH', ROOT / 'server' / 'livenote.sqlite3'))
MODEL = os.environ.get('LIVENOTE_WHISPER_MODEL', 'medium')
LANGUAGE = os.environ.get('LIVENOTE_WHISPER_LANGUAGE', 'auto')
WORKER_ID = os.environ.get('LIVENOTE_TRANSCRIBER_ID', 'local-transcriber')
SERVER = os.environ.get('LIVENOTE_SERVER_URL', 'http://127.0.0.1:8000/api/v1').rstrip('/')
WORKER_TOKEN = os.environ.get('LIVENOTE_WORKER_TOKEN', '')
API_KEY = os.environ.get('LIVENOTE_API_KEY', '')


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def list_tasks() -> int:
    with _connect() as connection:
        rows = connection.execute(
            """SELECT task.id, task.status, task.error_message, session.title, session.duration_ms
               FROM processing_tasks task JOIN sessions session ON session.id = task.session_id
               ORDER BY task.updated_at DESC""",
        ).fetchall()
    for row in rows:
        print(f"{row['id']} | {row['status']} | {row['duration_ms'] or 0}ms | {row['title'] or '未命名'} | {row['error_message'] or ''}")
    return 0


def prepare_summary(task_id: str) -> int:
    with _connect() as connection:
        task = connection.execute('SELECT id, session_id, status FROM processing_tasks WHERE id = ?', (task_id,)).fetchone()
        if task is None:
            raise ProcessingError('任务不存在。')
        transcript_path = DATA_DIR / 'processed' / 'sessions' / task['session_id'] / 'transcript.json'
        if task['status'] not in {'TRANSCRIBED', 'SUMMARIZING', 'REVIEW', 'COMPLETED'} or not transcript_path.is_file():
            raise ProcessingError(f"任务尚未准备好总结：{task['status']}")
        transcript = json.loads(transcript_path.read_text(encoding='utf-8'))
        run = connection.execute(
            "SELECT id, generation, source_hash, model, language FROM processing_runs WHERE task_id=? AND status='COMPLETED' ORDER BY generation DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if task['status'] == 'TRANSCRIBED':
            connection.execute("UPDATE processing_tasks SET status='SUMMARIZING', updated_at=strftime('%s','now')*1000 WHERE id=?", (task_id,))
    print(json.dumps({'taskId': task_id, 'sessionId': task['session_id'], 'status': 'SUMMARIZING', 'run': dict(run) if run else None, 'transcript': transcript}, ensure_ascii=False, indent=2))
    return 0


def submit_summary(server: str, task_id: str, result_path: Path) -> int:
    payload = json.loads(result_path.read_text(encoding='utf-8'))
    required = {'runId', 'generation', 'sourceHash', 'result'}
    if not required.issubset(payload):
        raise ProcessingError('总结文件必须包含 runId、generation、sourceHash、result。')
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
    if WORKER_TOKEN:
        headers['X-Worker-Token'] = WORKER_TOKEN
    if API_KEY:
        headers['X-API-Key'] = API_KEY
    request = urllib.request.Request(f'{server}/tasks/{task_id}/summary-result', data=body, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            print(response.read().decode('utf-8'))
    except urllib.error.HTTPError as error:
        raise ProcessingError(f'总结提交失败 {error.code}: {error.read().decode("utf-8", errors="replace")}') from error
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='LiveNote 本机自动转写器')
    sub = parser.add_subparsers(dest='command', required=True)
    once = sub.add_parser('once', help='处理一个待转写任务')
    once.add_argument('--model', default=MODEL)
    once.add_argument('--language', default=LANGUAGE)
    once.add_argument('--worker-id', default=WORKER_ID)
    once.set_defaults(handler=lambda args: print(json.dumps(run_once(DATA_DIR, DB_PATH, args.model, args.language, args.worker_id), ensure_ascii=False)) or 0)
    loop = sub.add_parser('watch', help='持续处理上传完成的任务')
    loop.add_argument('--model', default=MODEL)
    loop.add_argument('--language', default=LANGUAGE)
    loop.add_argument('--worker-id', default=WORKER_ID)
    loop.add_argument('--interval', type=int, default=5)
    loop.set_defaults(handler=lambda args: watch(DATA_DIR, DB_PATH, args.model, args.language, args.worker_id, args.interval) or 0)
    listing = sub.add_parser('list', help='列出本机转写任务')
    listing.set_defaults(handler=lambda _args: list_tasks())
    summary = sub.add_parser('summary-prepare', help='输出给当前 Codex 聊天读取的逐字稿')
    summary.add_argument('task_id')
    summary.set_defaults(handler=lambda args: prepare_summary(args.task_id))
    submit = sub.add_parser('summary-submit', help='提交当前 Codex 根据逐字稿生成的总结草稿')
    submit.add_argument('task_id')
    submit.add_argument('result', type=Path, help='包含运行版本信息和 result 的 JSON 文件')
    submit.add_argument('--server', default=SERVER)
    submit.set_defaults(handler=lambda args: submit_summary(args.server, args.task_id, args.result))
    return parser


if __name__ == '__main__':
    args = build_parser().parse_args()
    try:
        raise SystemExit(args.handler(args))
    except (OSError, RuntimeError, ProcessingError, json.JSONDecodeError) as error:
        print(f'错误：{error}', file=sys.stderr)
        raise SystemExit(1)
