from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from server.live_processing import ensure_live_run
from server.migrations import ensure_schema
from server.processing_pipeline import _claim_task, now_ms


class ProcessingCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix='livenote-processing-test-'))
        self.db_path = self.root / 'test.sqlite3'
        connection = sqlite3.connect(self.db_path)
        connection.executescript('''
            CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL, started_at INTEGER NOT NULL,
                ended_at INTEGER, status TEXT NOT NULL, duration_ms INTEGER NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
            CREATE TABLE segments (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, segment_index INTEGER NOT NULL,
                started_at INTEGER NOT NULL DEFAULT 0, ended_at INTEGER, mime_type TEXT NOT NULL DEFAULT '',
                media_settings TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'COMPLETED',
                duration_ms INTEGER NOT NULL DEFAULT 0, start_elapsed_ms INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE chunks (session_id TEXT NOT NULL, segment_id TEXT NOT NULL, chunk_index INTEGER NOT NULL,
                size INTEGER NOT NULL, sha256 TEXT NOT NULL, mime_type TEXT NOT NULL, elapsed_ms INTEGER NOT NULL,
                created_at INTEGER NOT NULL, received_at INTEGER NOT NULL, local_path TEXT NOT NULL,
                PRIMARY KEY(segment_id, chunk_index));
            CREATE TABLE processing_tasks (id TEXT PRIMARY KEY, session_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, claimed_by TEXT, claimed_at INTEGER, error_message TEXT NOT NULL DEFAULT '',
                result_path TEXT, result_version INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
        ''')
        ensure_schema(connection, self.db_path, self.root / 'data')
        timestamp = now_ms()
        connection.execute('''INSERT INTO sessions(id, title, started_at, ended_at, status, duration_ms, created_at, updated_at)
                              VALUES (?, ?, ?, ?, ?, ?, ?, ?)''', ('session-1', 'test', timestamp, timestamp, 'COMPLETED', 1000, timestamp, timestamp))
        connection.execute('INSERT INTO processing_tasks(id, session_id, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)', ('task-1', 'session-1', 'READY', timestamp, timestamp))
        connection.commit()
        connection.close()

    def tearDown(self) -> None:
        for path in self.root.rglob('*'):
            if path.is_file():
                path.unlink()
        for path in sorted(self.root.rglob('*'), reverse=True):
            if path.is_dir():
                path.rmdir()
        self.root.rmdir()

    def test_claim_is_atomic_and_increments_attempts(self) -> None:
        first = _claim_task(self.db_path, 'processor-a')
        second = _claim_task(self.db_path, 'processor-b')
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        connection = sqlite3.connect(self.db_path)
        row = connection.execute('SELECT status, claimed_by, attempts FROM processing_tasks WHERE id = ?', ('task-1',)).fetchone()
        connection.close()
        self.assertEqual(row, ('TRANSCRIBING', 'processor-a', 1))

    def test_schema_contains_durable_runs_and_parts(self) -> None:
        connection = sqlite3.connect(self.db_path)
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        live_window_columns = {row[1] for row in connection.execute('PRAGMA table_info(live_processing_windows)').fetchall()}
        schema_version = connection.execute("SELECT value FROM meta_schema WHERE key='schema_version'").fetchone()[0]
        connection.close()
        self.assertIn('processing_runs', tables)
        self.assertIn('processing_parts', tables)
        self.assertTrue({'model', 'language', 'pipeline_version'}.issubset(live_window_columns))
        self.assertEqual(schema_version, '9')

    def test_stale_run_is_released_for_recovery(self) -> None:
        timestamp = now_ms()
        connection = sqlite3.connect(self.db_path)
        connection.execute("UPDATE processing_tasks SET status='TRANSCRIBING', claimed_by='dead-worker', updated_at=? WHERE id='task-1'", (timestamp - 700_000,))
        connection.execute(
            '''INSERT INTO processing_runs(id, task_id, session_id, generation, source_hash, model, language, status, started_at, heartbeat_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'RUNNING', ?, ?)''',
            ('run-1', 'task-1', 'session-1', 1, 'a' * 64, 'base', 'auto', timestamp - 700_000, timestamp - 700_000),
        )
        connection.commit()
        connection.close()
        claimed = _claim_task(self.db_path, 'new-worker')
        self.assertIsNotNone(claimed)
        connection = sqlite3.connect(self.db_path)
        task = connection.execute("SELECT status, claimed_by FROM processing_tasks WHERE id='task-1'").fetchone()
        run = connection.execute("SELECT status FROM processing_runs WHERE id='run-1'").fetchone()
        connection.close()
        self.assertEqual(task, ('TRANSCRIBING', 'new-worker'))
        self.assertEqual(run, ('INTERRUPTED',))

    def test_claim_waits_until_completed_session_live_windows_are_drained(self) -> None:
        timestamp = now_ms()
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            '''INSERT INTO segments(id, session_id, segment_index, started_at, ended_at, mime_type, media_settings, status, duration_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            ('segment-live-gate', 'session-1', 1, timestamp, timestamp, 'audio/webm', '{}', 'COMPLETED', 120000),
        )
        for index in range(4):
            connection.execute(
                '''INSERT INTO chunks(session_id, segment_id, chunk_index, size, sha256, mime_type, elapsed_ms, created_at, received_at, local_path)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                ('session-1', 'segment-live-gate', index, 1, f'hash-{index}', 'audio/webm', (index + 1) * 30000, timestamp, timestamp, f'sessions/session-1/{index}.bin'),
            )
        run_id = ensure_live_run(connection, 'session-1')
        connection.execute('UPDATE live_processing_runs SET status = ? WHERE id = ?', ('READY', run_id))
        connection.commit()
        connection.close()

        self.assertIsNone(_claim_task(self.db_path, 'final-worker'))
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            '''INSERT INTO live_processing_windows(
                   id, run_id, session_id, segment_id, window_index, input_start_chunk, input_end_chunk,
                   start_ms, end_ms, core_start_ms, core_end_ms, input_hash, status, attempts,
                   transcript_json, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            ('window-live-gate', run_id, 'session-1', 'segment-live-gate', 0, 0, 3,
             0, 120000, 0, 120000, 'prefix-hash', 'COMPLETED', 1, '{"segments": []}', timestamp),
        )
        connection.execute('UPDATE live_processing_runs SET status = ? WHERE id = ?', ('WAITING_FOR_CHUNKS', run_id))
        connection.commit()
        connection.close()

        claimed = _claim_task(self.db_path, 'final-worker')
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed['session_id'], 'session-1')
