from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f'PRAGMA table_info({table})').fetchall()}


def _add_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> bool:
    if column in _columns(connection, table):
        return False
    connection.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
    return True


def backup_before_migration(db_path: Path, backup_root: Path) -> Path | None:
    if not db_path.exists():
        return None
    backup_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    destination = backup_root / f'livenote-before-schema-{timestamp}.sqlite3'
    source = sqlite3.connect(db_path)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return destination


def ensure_schema(connection: sqlite3.Connection, db_path: Path, data_dir: Path) -> bool:
    changed = False
    connection.executescript(
        '''
        CREATE TABLE IF NOT EXISTS meta_schema (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS devices (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            label TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            revoked_at INTEGER,
            last_seen_at INTEGER,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pairing_codes (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            code_hash TEXT NOT NULL UNIQUE,
            expires_at INTEGER NOT NULL,
            consumed_at INTEGER,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS deleted_sessions (
            session_id TEXT PRIMARY KEY,
            deleted_at INTEGER NOT NULL
        );
        '''
    )
    changed = _add_column(connection, 'sessions', 'owner_id', 'TEXT REFERENCES users(id)') or changed
    changed = _add_column(connection, 'sessions', 'device_id', 'TEXT REFERENCES devices(id)') or changed
    changed = _add_column(connection, 'sessions', 'published_revision_id', 'TEXT') or changed
    for column, definition in {
        'requested_worker_id': 'TEXT',
        'lease_token_hash': 'TEXT',
        'lease_expires_at': 'INTEGER',
        'downloaded_at': 'INTEGER',
        'error_stage': "TEXT NOT NULL DEFAULT ''",
        'admin_note': "TEXT NOT NULL DEFAULT ''",
    }.items():
        changed = _add_column(connection, 'processing_tasks', column, definition) or changed

    connection.executescript(
        '''
        CREATE TABLE IF NOT EXISTS result_revisions (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES processing_tasks(id) ON DELETE CASCADE,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            version INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            content_json TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            UNIQUE(task_id, content_hash)
        );
        CREATE TABLE IF NOT EXISTS audit_events (
            id TEXT PRIMARY KEY,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            target TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '{}',
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workers (
            id TEXT PRIMARY KEY,
            last_seen_at INTEGER NOT NULL,
            active_task_id TEXT,
            status TEXT NOT NULL DEFAULT 'ONLINE'
        );
        CREATE TABLE IF NOT EXISTS processing_runs (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES processing_tasks(id) ON DELETE CASCADE,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            generation INTEGER NOT NULL,
            source_hash TEXT NOT NULL,
            model TEXT NOT NULL,
            language TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at INTEGER NOT NULL,
            heartbeat_at INTEGER NOT NULL,
            completed_at INTEGER,
            error_message TEXT NOT NULL DEFAULT '',
            UNIQUE(task_id, generation)
        );
        CREATE TABLE IF NOT EXISTS processing_parts (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES processing_runs(id) ON DELETE CASCADE,
            task_id TEXT NOT NULL REFERENCES processing_tasks(id) ON DELETE CASCADE,
            part_index INTEGER NOT NULL,
            start_ms INTEGER NOT NULL,
            end_ms INTEGER NOT NULL,
            source_hash TEXT NOT NULL,
            model TEXT NOT NULL,
            language TEXT NOT NULL,
            status TEXT NOT NULL,
            transcript_json TEXT,
            started_at INTEGER,
            completed_at INTEGER,
            error_message TEXT NOT NULL DEFAULT '',
            UNIQUE(run_id, part_index)
        );
        CREATE TABLE IF NOT EXISTS live_processing_runs (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id) ON DELETE CASCADE,
            model TEXT NOT NULL,
            language TEXT NOT NULL,
            pipeline_version TEXT NOT NULL,
            status TEXT NOT NULL,
            claimed_by TEXT,
            lease_expires_at INTEGER,
            last_contiguous_chunk INTEGER NOT NULL DEFAULT -1,
            processed_until_ms INTEGER NOT NULL DEFAULT 0,
            source_prefix_hash TEXT NOT NULL DEFAULT '',
            heartbeat_at INTEGER NOT NULL,
            error_code TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS live_processing_windows (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES live_processing_runs(id) ON DELETE CASCADE,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            segment_id TEXT NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
            window_index INTEGER NOT NULL,
            model TEXT NOT NULL DEFAULT '',
            language TEXT NOT NULL DEFAULT '',
            pipeline_version TEXT NOT NULL DEFAULT '',
            input_start_chunk INTEGER NOT NULL,
            input_end_chunk INTEGER NOT NULL,
            start_ms INTEGER NOT NULL,
            end_ms INTEGER NOT NULL,
            core_start_ms INTEGER NOT NULL,
            core_end_ms INTEGER NOT NULL,
            input_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            transcript_json TEXT,
            artifact_path TEXT,
            artifact_hash TEXT,
            started_at INTEGER,
            completed_at INTEGER,
            error_code TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            updated_at INTEGER NOT NULL DEFAULT 0,
            prepare_duration_ms INTEGER NOT NULL DEFAULT 0,
            asr_duration_ms INTEGER NOT NULL DEFAULT 0,
            peak_staging_bytes INTEGER NOT NULL DEFAULT 0,
            UNIQUE(run_id, segment_id, window_index)
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_owner_updated ON sessions(owner_id, updated_at);
        CREATE INDEX IF NOT EXISTS idx_tasks_status_updated ON processing_tasks(status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_pairing_codes_expiry ON pairing_codes(expires_at);
        CREATE INDEX IF NOT EXISTS idx_deleted_sessions_deleted_at ON deleted_sessions(deleted_at);
        CREATE INDEX IF NOT EXISTS idx_revisions_session_created ON result_revisions(session_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_processing_runs_task_status ON processing_runs(task_id, status, heartbeat_at);
        CREATE INDEX IF NOT EXISTS idx_processing_parts_run_status ON processing_parts(run_id, status, part_index);
        CREATE INDEX IF NOT EXISTS idx_live_processing_runs_status ON live_processing_runs(status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_live_processing_windows_status ON live_processing_windows(run_id, status, segment_id, window_index);
        '''
    )
    for column, definition in {
        'model': "TEXT NOT NULL DEFAULT ''",
        'language': "TEXT NOT NULL DEFAULT ''",
        'pipeline_version': "TEXT NOT NULL DEFAULT ''",
        'prepare_duration_ms': 'INTEGER NOT NULL DEFAULT 0',
        'asr_duration_ms': 'INTEGER NOT NULL DEFAULT 0',
        'peak_staging_bytes': 'INTEGER NOT NULL DEFAULT 0',
    }.items():
        changed = _add_column(connection, 'live_processing_windows', column, definition) or changed
    connection.execute(
        '''UPDATE live_processing_windows
           SET model = COALESCE(NULLIF(model, ''), (SELECT model FROM live_processing_runs WHERE id = live_processing_windows.run_id), ''),
               language = COALESCE(NULLIF(language, ''), (SELECT language FROM live_processing_runs WHERE id = live_processing_windows.run_id), ''),
               pipeline_version = COALESCE(NULLIF(pipeline_version, ''), (SELECT pipeline_version FROM live_processing_runs WHERE id = live_processing_windows.run_id), '')
           WHERE model = '' OR language = '' OR pipeline_version = '' '''
    )
    connection.execute("INSERT OR REPLACE INTO meta_schema(key, value) VALUES ('schema_version', '9')")
    return changed
