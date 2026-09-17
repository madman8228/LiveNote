from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any


class RestoreError(RuntimeError):
    pass


SERVER_DIR = Path(__file__).resolve().parent


def _default_path(name: str, fallback: Path) -> Path:
    return Path(os.environ.get(name, str(fallback)))


def _check_target_is_outside_source(source: Path, target: Path, label: str) -> None:
    source = source.resolve()
    target = target.resolve()
    if source == target or source in target.parents or target in source.parents:
        raise RestoreError(f'{label} 不能与备份目录相同或互相包含：{target}')


def _read_manifest(backup_dir: Path) -> dict[str, Any] | None:
    manifest_path = backup_dir / 'manifest.json'
    if not manifest_path.is_file():
        return None
    try:
        value = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise RestoreError(f'备份 manifest.json 无法读取：{error}') from error
    if not isinstance(value, dict):
        raise RestoreError('备份 manifest.json 格式无效。')
    return value


def _database_counts(connection: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in ('sessions', 'segments', 'chunks', 'markers'):
        try:
            counts[table] = int(connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0])
        except sqlite3.Error:
            counts[table] = 0
    return counts


def inspect_backup(backup_dir: Path) -> dict[str, Any]:
    backup_dir = backup_dir.resolve()
    backup_db = backup_dir / 'livenote.sqlite3'
    backup_data = backup_dir / 'data'
    if not backup_dir.is_dir():
        raise RestoreError(f'备份目录不存在：{backup_dir}')
    if not backup_db.is_file():
        raise RestoreError(f'备份数据库不存在：{backup_db}')
    if not backup_data.is_dir():
        raise RestoreError(f'备份数据目录不存在：{backup_data}')

    try:
        connection = sqlite3.connect(backup_db)
        try:
            integrity = connection.execute('PRAGMA integrity_check').fetchone()[0]
            if integrity != 'ok':
                raise RestoreError(f'备份数据库完整性检查失败：{integrity}')
            counts = _database_counts(connection)
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise RestoreError(f'备份数据库无法打开：{error}') from error

    return {
        'backupDirectory': str(backup_dir),
        'database': str(backup_db),
        'dataDirectory': str(backup_data),
        'counts': counts,
        'manifest': _read_manifest(backup_dir),
    }


def restore_backup(
    backup_dir: Path,
    data_dir: Path,
    db_path: Path,
    *,
    replace: bool = False,
) -> dict[str, Any]:
    backup_dir = backup_dir.resolve()
    data_dir = data_dir.resolve()
    db_path = db_path.resolve()
    _check_target_is_outside_source(backup_dir, data_dir, '数据目录')
    _check_target_is_outside_source(backup_dir, db_path, '数据库路径')
    inspection = inspect_backup(backup_dir)

    data_dir.parent.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not replace and (data_dir.exists() or db_path.exists()):
        raise RestoreError('目标数据库或数据目录已存在；如确认覆盖，请显式使用 --replace。')

    token = uuid.uuid4().hex
    staged_db = db_path.with_name(f'.{db_path.name}.restore-{token}.tmp')
    staged_data = data_dir.with_name(f'.{data_dir.name}.restore-{token}.tmp')
    previous_db = db_path.with_name(f'.{db_path.name}.before-restore-{token}')
    previous_data = data_dir.with_name(f'.{data_dir.name}.before-restore-{token}')
    installed_db = False
    installed_data = False
    moved_previous_db = False
    moved_previous_data = False

    try:
        shutil.copy2(backup_dir / 'livenote.sqlite3', staged_db)
        shutil.copytree(backup_dir / 'data', staged_data)

        if replace:
            if db_path.exists():
                db_path.replace(previous_db)
                moved_previous_db = True
            if data_dir.exists():
                data_dir.replace(previous_data)
                moved_previous_data = True

        staged_db.replace(db_path)
        installed_db = True
        staged_data.replace(data_dir)
        installed_data = True
    except Exception as error:
        if installed_data and data_dir.exists(): shutil.rmtree(data_dir, ignore_errors=True)
        if installed_db and db_path.exists(): db_path.unlink(missing_ok=True)
        if moved_previous_data and previous_data.exists(): previous_data.replace(data_dir)
        if moved_previous_db and previous_db.exists(): previous_db.replace(db_path)
        raise RestoreError(f'备份恢复失败：{error}') from error
    finally:
        if staged_db.exists(): staged_db.unlink(missing_ok=True)
        if staged_data.exists(): shutil.rmtree(staged_data, ignore_errors=True)

    return {
        **inspection,
        'restoredDatabase': str(db_path),
        'restoredDataDirectory': str(data_dir),
        'previousDatabase': str(previous_db) if moved_previous_db else None,
        'previousDataDirectory': str(previous_data) if moved_previous_data else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='校验并恢复 LiveNote 数据备份。')
    parser.add_argument('--backup', type=Path, required=True, help='由 server/backup.py 创建的备份目录。')
    parser.add_argument('--data-dir', type=Path, default=_default_path('LIVENOTE_DATA_DIR', SERVER_DIR / 'data'))
    parser.add_argument('--db-path', type=Path, default=_default_path('LIVENOTE_DB_PATH', SERVER_DIR / 'livenote.sqlite3'))
    parser.add_argument('--verify-only', action='store_true', help='只校验备份，不恢复。')
    parser.add_argument('--replace', action='store_true', help='显式替换目标；旧目标会保留为 before-restore 副本。')
    args = parser.parse_args()
    try:
        result = inspect_backup(args.backup) if args.verify_only else restore_backup(args.backup, args.data_dir, args.db_path, replace=args.replace)
    except (RestoreError, OSError, sqlite3.Error) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
