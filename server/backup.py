from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class BackupError(RuntimeError):
    pass


SERVER_DIR = Path(__file__).resolve().parent


def _default_path(name: str, fallback: Path) -> Path:
    return Path(os.environ.get(name, str(fallback)))


def _ensure_destination_is_safe(data_dir: Path, destination: Path) -> None:
    source = data_dir.resolve()
    target = destination.resolve()
    if target == source or source in target.parents:
        raise BackupError('备份目录不能位于 LiveNote 数据目录内部。')


def _database_counts(connection: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in ('sessions', 'segments', 'chunks', 'markers'):
        try:
            counts[table] = int(connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0])
        except sqlite3.Error:
            counts[table] = 0
    return counts


def create_backup(data_dir: Path, db_path: Path, destination: Path) -> Path:
    data_dir = data_dir.resolve()
    db_path = db_path.resolve()
    destination = destination.resolve()
    if not db_path.is_file():
        raise BackupError(f'SQLite 数据库不存在：{db_path}')
    if not data_dir.is_dir():
        raise BackupError(f'数据目录不存在：{data_dir}')
    _ensure_destination_is_safe(data_dir, destination)

    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup_dir = destination / f'livenote-{timestamp}-{uuid.uuid4().hex[:8]}'
    backup_dir.mkdir(parents=True, exist_ok=False)
    backup_db = backup_dir / 'livenote.sqlite3'
    backup_data = backup_dir / 'data'

    try:
        source_connection = sqlite3.connect(db_path)
        try:
            target_connection = sqlite3.connect(backup_db)
            try:
                source_connection.backup(target_connection)
                counts = _database_counts(source_connection)
            finally:
                target_connection.close()
        finally:
            source_connection.close()

        shutil.copytree(data_dir, backup_data)
        manifest: dict[str, Any] = {
            'createdAt': datetime.now(timezone.utc).isoformat(),
            'database': 'livenote.sqlite3',
            'dataDirectory': 'data',
            'sourceDatabase': str(db_path),
            'sourceDataDirectory': str(data_dir),
            'counts': counts,
            'warning': '数据库和文件目录不是同一时刻快照；正式备份前应暂停录音上传或使用文件系统快照。',
        }
        manifest_path = backup_dir / 'manifest.json'
        temporary_manifest = backup_dir / '.manifest.json.tmp'
        temporary_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary_manifest.replace(manifest_path)
        return backup_dir
    except Exception:
        shutil.rmtree(backup_dir, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description='备份 LiveNote SQLite、音频和处理结果。')
    parser.add_argument('--data-dir', type=Path, default=_default_path('LIVENOTE_DATA_DIR', SERVER_DIR / 'data'))
    parser.add_argument('--db-path', type=Path, default=_default_path('LIVENOTE_DB_PATH', SERVER_DIR / 'livenote.sqlite3'))
    parser.add_argument('--destination', type=Path, required=True, help='备份输出目录，不能放在 data-dir 内部。')
    args = parser.parse_args()
    try:
        backup_dir = create_backup(args.data_dir, args.db_path, args.destination)
    except (BackupError, OSError, sqlite3.Error) as error:
        parser.error(str(error))
    print(backup_dir)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
