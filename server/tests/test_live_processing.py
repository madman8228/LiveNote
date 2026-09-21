from __future__ import annotations

import json
import gc
import hashlib
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from server import live_processing


class LiveProcessingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / 'live.sqlite3'
        with sqlite3.connect(self.db_path) as connection:
            connection.executescript(
                '''
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, duration_ms INTEGER NOT NULL
                );
                CREATE TABLE segments (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, segment_index INTEGER NOT NULL,
                    start_elapsed_ms INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE chunks (
                    session_id TEXT NOT NULL, segment_id TEXT NOT NULL, chunk_index INTEGER NOT NULL,
                    size INTEGER NOT NULL, sha256 TEXT NOT NULL, elapsed_ms INTEGER NOT NULL,
                    local_path TEXT NOT NULL, PRIMARY KEY(segment_id, chunk_index)
                );
                CREATE TABLE live_processing_runs (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL UNIQUE, model TEXT NOT NULL,
                    language TEXT NOT NULL, pipeline_version TEXT NOT NULL, status TEXT NOT NULL,
                    claimed_by TEXT, lease_expires_at INTEGER, last_contiguous_chunk INTEGER NOT NULL DEFAULT -1,
                    processed_until_ms INTEGER NOT NULL DEFAULT 0, source_prefix_hash TEXT NOT NULL DEFAULT '',
                    heartbeat_at INTEGER NOT NULL, error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
                );
                CREATE TABLE live_processing_windows (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, session_id TEXT NOT NULL, segment_id TEXT NOT NULL,
                    window_index INTEGER NOT NULL, model TEXT NOT NULL DEFAULT '', language TEXT NOT NULL DEFAULT '', pipeline_version TEXT NOT NULL DEFAULT '',
                    input_start_chunk INTEGER NOT NULL, input_end_chunk INTEGER NOT NULL,
                    start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, core_start_ms INTEGER NOT NULL,
                    core_end_ms INTEGER NOT NULL, input_hash TEXT NOT NULL, status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, transcript_json TEXT, artifact_path TEXT,
                    artifact_hash TEXT, started_at INTEGER, completed_at INTEGER, error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL DEFAULT 0,
                    prepare_duration_ms INTEGER NOT NULL DEFAULT 0, asr_duration_ms INTEGER NOT NULL DEFAULT 0,
                    peak_staging_bytes INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(run_id, segment_id, window_index)
                );
                '''
            )
        self.session_id = 'session-live-test'
        self.segment_id = 'segment-live-test'
        with sqlite3.connect(self.db_path) as connection:
            connection.execute('INSERT INTO sessions(id, status, duration_ms) VALUES (?, ?, ?)', (self.session_id, 'RECORDING', 120000))
            connection.execute('INSERT INTO segments(id, session_id, segment_index, start_elapsed_ms) VALUES (?, ?, ?, ?)', (self.segment_id, self.session_id, 1, 0))

    def tearDown(self) -> None:
        gc.collect()
        self.temp.cleanup()

    def _insert_chunks(self, indexes: list[int], segment_id: str | None = None, segment_index: int = 1) -> None:
        target_segment_id = segment_id or self.segment_id
        with sqlite3.connect(self.db_path) as connection:
            for index in indexes:
                relative = Path('sessions') / self.session_id / f'segment_{segment_index}' / 'chunks' / f'{index:06d}.bin'
                absolute = self.root / relative
                absolute.parent.mkdir(parents=True, exist_ok=True)
                content = f'chunk-{index}'.encode('ascii')
                absolute.write_bytes(content)
                connection.execute(
                    '''INSERT INTO chunks(session_id, segment_id, chunk_index, size, sha256, elapsed_ms, local_path)
                       VALUES (?, ?, ?, ?, ?, ?, ?)''',
                    (self.session_id, target_segment_id, index, len(content), hashlib.sha256(content).hexdigest(), (index + 1) * 30000, str(relative)),
                )

    def test_status_counts_windows_per_segment(self) -> None:
        second_segment_id = 'segment-live-test-2'
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                'INSERT INTO segments(id, session_id, segment_index, start_elapsed_ms) VALUES (?, ?, ?, ?)',
                (second_segment_id, self.session_id, 2, 120000),
            )
        self._insert_chunks([0, 1], segment_id=self.segment_id, segment_index=1)
        self._insert_chunks([0, 1], segment_id=second_segment_id, segment_index=2)
        with sqlite3.connect(self.db_path) as connection:
            live_processing.ensure_live_run(connection, self.session_id)
        status = live_processing.live_processing_status(self.db_path, self.session_id)
        self.assertEqual(status['uploadedChunks'], 4)
        self.assertEqual(status['totalWindows'], 2)

    def test_missing_chunk_does_not_advance(self) -> None:
        self._insert_chunks([0, 2, 3])
        connection = sqlite3.connect(self.db_path)
        try:
            live_processing.ensure_live_run(connection, self.session_id)
            connection.commit()
        finally:
            connection.close()
        self.assertFalse(live_processing.process_session_once(self.root, self.db_path, self.session_id))
        status = live_processing.live_processing_status(self.db_path, self.session_id)
        self.assertEqual(status['completedWindows'], 0)

    def test_chunk_integrity_mismatch_blocks_processing(self) -> None:
        self._insert_chunks([0, 1, 2, 3])
        corrupted = self.root / 'sessions' / self.session_id / 'segment_1' / 'chunks' / '000002.bin'
        corrupted.write_bytes(b'corrupted')
        with sqlite3.connect(self.db_path) as connection:
            live_processing.ensure_live_run(connection, self.session_id)
        with patch.object(live_processing, 'transcribe_range') as transcribe:
            self.assertFalse(live_processing.process_session_once(self.root, self.db_path, self.session_id))
            transcribe.assert_not_called()
        status = live_processing.live_processing_status(self.db_path, self.session_id)
        self.assertEqual(status['status'], live_processing.FAILED)
        self.assertEqual(status['errorCode'], 'UNEXPECTED')
        self.assertEqual(status['completedWindows'], 0)
        self.assertFalse(list((self.root / 'processed' / 'live' / self.session_id / 'segment_1').glob('.prefix.*.tmp')))

    def test_batch_creates_durable_window_and_partial_transcript(self) -> None:
        self._insert_chunks([0, 1, 2, 3])
        connection = sqlite3.connect(self.db_path)
        try:
            live_processing.ensure_live_run(connection, self.session_id)
            connection.commit()
        finally:
            connection.close()

        fake_result = {'model': 'test', 'device': 'cpu', 'language': 'zh', 'segments': [{'startMs': 1000, 'endMs': 2500, 'text': '第一批内容'}]}
        with patch.object(live_processing, 'transcribe_range', return_value=fake_result) as transcribe:
            self.assertTrue(live_processing.process_session_once(self.root, self.db_path, self.session_id))
            transcribe.assert_called_once()

        status = live_processing.live_processing_status(self.db_path, self.session_id)
        self.assertEqual(status['completedWindows'], 1)
        self.assertEqual(status['processedDurationMs'], 120000)
        with sqlite3.connect(self.db_path) as connection:
            self.assertEqual(connection.execute(
                'SELECT last_contiguous_chunk FROM live_processing_runs WHERE session_id = ?',
                (self.session_id,),
            ).fetchone()[0], 3)
            metrics = connection.execute(
                'SELECT prepare_duration_ms, asr_duration_ms, peak_staging_bytes FROM live_processing_windows WHERE run_id = (SELECT id FROM live_processing_runs WHERE session_id = ?)',
                (self.session_id,),
            ).fetchone()
            self.assertGreaterEqual(metrics[0], 0)
            self.assertGreaterEqual(metrics[1], 0)
            self.assertGreater(metrics[2], 0)
        transcript_path = self.root / 'processed' / 'sessions' / self.session_id / 'transcript-live.json'
        transcript = json.loads(transcript_path.read_text(encoding='utf-8'))
        self.assertEqual(transcript['text'], '第一批内容')
        self.assertEqual(transcript['segments'][0]['startMs'], 1000)

        self._insert_chunks([4, 5, 6, 7])
        with sqlite3.connect(self.db_path) as connection:
            live_processing.ensure_live_run(connection, self.session_id)
            connection.commit()
        with patch.object(live_processing, 'transcribe_range', return_value={'segments': [{'startMs': 150000, 'endMs': 151000, 'text': '第二批内容'}]}):
            self.assertTrue(live_processing.process_session_once(self.root, self.db_path, self.session_id))
        status = live_processing.live_processing_status(self.db_path, self.session_id)
        self.assertEqual(status['completedWindows'], 2)

    def test_worker_drains_multiple_ready_windows_without_another_upload(self) -> None:
        self._insert_chunks(list(range(8)))
        with sqlite3.connect(self.db_path) as connection:
            live_processing.ensure_live_run(connection, self.session_id)

        worker = live_processing.LiveProcessingWorker(self.root, self.db_path, interval_seconds=0.01)
        try:
            with patch.object(
                live_processing,
                'transcribe_range',
                side_effect=[
                    {'segments': [{'startMs': 0, 'endMs': 100, 'text': '第一窗口'}]},
                    {'segments': [{'startMs': 120000, 'endMs': 120100, 'text': '第二窗口'}]},
                ],
            ) as transcribe:
                worker.start()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    status = live_processing.live_processing_status(self.db_path, self.session_id)
                    if status['completedWindows'] == 2:
                        break
                    time.sleep(0.02)
                else:
                    self.fail(f'Worker 未排空已到达窗口：{status}')
                self.assertEqual(transcribe.call_count, 2)
        finally:
            worker.stop()

    def test_final_processing_waits_for_pending_completed_session_window(self) -> None:
        self._insert_chunks([0, 1, 2, 3])
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute('UPDATE sessions SET status = ? WHERE id = ?', ('COMPLETED', self.session_id))
            live_processing.ensure_live_run(connection, self.session_id)
            connection.execute('UPDATE live_processing_runs SET status = ? WHERE session_id = ?', (live_processing.READY, self.session_id))
            self.assertTrue(live_processing.live_processing_has_pending_windows(connection, self.session_id))

        with patch.object(live_processing, 'transcribe_range', return_value={'segments': [{'startMs': 0, 'endMs': 100, 'text': '已排空'}]}):
            self.assertTrue(live_processing.process_session_once(self.root, self.db_path, self.session_id))
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            self.assertFalse(live_processing.live_processing_has_pending_windows(connection, self.session_id))

    def test_expired_live_lease_can_be_reclaimed_without_duplicate_window(self) -> None:
        self._insert_chunks([0, 1, 2, 3])
        with sqlite3.connect(self.db_path) as connection:
            live_processing.ensure_live_run(connection, self.session_id)
            connection.execute(
                "UPDATE live_processing_runs SET status = ?, claimed_by = ?, lease_expires_at = ? WHERE session_id = ?",
                (live_processing.CLAIMED, 'dead-live-worker', live_processing.now_ms() - 1, self.session_id),
            )

        fake_result = {'segments': [{'startMs': 0, 'endMs': 1000, 'text': '恢复后的窗口'}]}
        with patch.object(live_processing, 'transcribe_range', return_value=fake_result) as transcribe:
            self.assertTrue(live_processing.process_session_once(self.root, self.db_path, self.session_id, worker_id='new-live-worker'))
            transcribe.assert_called_once()

        with sqlite3.connect(self.db_path) as connection:
            window_count = connection.execute('SELECT COUNT(*) FROM live_processing_windows').fetchone()[0]
            run = connection.execute('SELECT status, claimed_by, lease_expires_at FROM live_processing_runs WHERE session_id = ?', (self.session_id,)).fetchone()
        self.assertEqual(window_count, 1)
        self.assertEqual(run[0], live_processing.COMPLETED)
        self.assertIsNone(run[1])
        self.assertIsNone(run[2])

    def test_restarted_worker_reclaims_expired_claim_from_durable_queue(self) -> None:
        self._insert_chunks([0, 1, 2, 3])
        with sqlite3.connect(self.db_path) as connection:
            live_processing.ensure_live_run(connection, self.session_id)
            connection.execute(
                '''UPDATE live_processing_runs
                   SET status = ?, claimed_by = ?, lease_expires_at = ?, updated_at = ?
                   WHERE session_id = ?''',
                (live_processing.CLAIMED, 'crashed-worker', live_processing.now_ms() - 1, live_processing.now_ms() - 1, self.session_id),
            )

        worker = live_processing.LiveProcessingWorker(self.root, self.db_path, interval_seconds=0.01)
        try:
            with patch.object(
                live_processing,
                'transcribe_range',
                return_value={'segments': [{'startMs': 0, 'endMs': 1000, 'text': '重启后恢复'}]},
            ) as transcribe:
                worker.start()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    status = live_processing.live_processing_status(self.db_path, self.session_id)
                    if status['completedWindows'] == 1:
                        break
                    time.sleep(0.02)
                else:
                    self.fail(f'Worker 重启后未接管过期租约：{status}')
                transcribe.assert_called_once()
        finally:
            worker.stop()

        status = live_processing.live_processing_status(self.db_path, self.session_id)
        self.assertIn(status['status'], {live_processing.COMPLETED, live_processing.WAITING_FOR_CHUNKS})
        self.assertEqual(status['completedWindows'], 1)

    def test_finalized_run_rejects_late_worker(self) -> None:
        self._insert_chunks([0, 1, 2, 3])
        with sqlite3.connect(self.db_path) as connection:
            live_processing.ensure_live_run(connection, self.session_id)
        live_processing.finalize_live_run(self.db_path, self.session_id, 'f' * 64)
        with patch.object(live_processing, 'transcribe_range') as transcribe:
            self.assertFalse(live_processing.process_session_once(self.root, self.db_path, self.session_id, worker_id='late-worker'))
            transcribe.assert_not_called()
        status = live_processing.live_processing_status(self.db_path, self.session_id)
        self.assertEqual(status['status'], live_processing.FINALIZED)
        self.assertEqual(status['completedWindows'], 0)

    def test_old_worker_cannot_commit_after_lease_changes_owner(self) -> None:
        self._insert_chunks([0, 1, 2, 3])
        with sqlite3.connect(self.db_path) as connection:
            live_processing.ensure_live_run(connection, self.session_id)

        def finish_after_reassignment(*_args, **_kwargs):
            with sqlite3.connect(self.db_path) as connection:
                connection.execute(
                    'UPDATE live_processing_runs SET status = ?, claimed_by = ?, lease_expires_at = ? WHERE session_id = ?',
                    (live_processing.CLAIMED, 'new-live-worker', live_processing.now_ms() + 120000, self.session_id),
                )
            return {'segments': [{'startMs': 0, 'endMs': 1000, 'text': '过期 Worker 不应提交'}]}

        with patch.object(live_processing, 'transcribe_range', side_effect=finish_after_reassignment):
            self.assertFalse(live_processing.process_session_once(self.root, self.db_path, self.session_id, worker_id='old-live-worker'))
        with sqlite3.connect(self.db_path) as connection:
            run = connection.execute('SELECT status, claimed_by FROM live_processing_runs WHERE session_id = ?', (self.session_id,)).fetchone()
            windows = connection.execute('SELECT COUNT(*) FROM live_processing_windows WHERE status = ?', (live_processing.COMPLETED,)).fetchone()[0]
        self.assertEqual(run, (live_processing.CLAIMED, 'new-live-worker'))
        self.assertEqual(windows, 0)

    def test_asr_failure_remains_retryable_and_retry_clears_lease(self) -> None:
        self._insert_chunks([0, 1, 2, 3])
        with sqlite3.connect(self.db_path) as connection:
            live_processing.ensure_live_run(connection, self.session_id)
        with patch.object(live_processing, 'transcribe_range', side_effect=live_processing.AsrError('音频文件尚未可读')):
            self.assertFalse(live_processing.process_session_once(self.root, self.db_path, self.session_id))
        status = live_processing.live_processing_status(self.db_path, self.session_id)
        self.assertEqual(status['status'], live_processing.WAITING_FOR_DECODABLE_PREFIX)
        self.assertTrue(status['canRetry'])
        with sqlite3.connect(self.db_path) as connection:
            run = connection.execute('SELECT claimed_by, lease_expires_at FROM live_processing_runs WHERE session_id = ?', (self.session_id,)).fetchone()
        self.assertEqual(run, (None, None))
        with patch.object(live_processing, 'transcribe_range') as transcribe:
            self.assertFalse(live_processing.process_session_once(self.root, self.db_path, self.session_id))
            transcribe.assert_not_called()
        with sqlite3.connect(self.db_path) as connection:
            live_processing.retry_live_run(connection, self.session_id)
        with patch.object(live_processing, 'transcribe_range', return_value={'segments': [{'startMs': 0, 'endMs': 100, 'text': '重试成功'}]}):
            self.assertTrue(live_processing.process_session_once(self.root, self.db_path, self.session_id))

    def test_completed_session_processes_short_tail(self) -> None:
        self._insert_chunks([0, 1])
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute('UPDATE sessions SET status = ? WHERE id = ?', ('COMPLETED', self.session_id))
            live_processing.ensure_live_run(connection, self.session_id)
            connection.commit()
        finally:
            connection.close()
        with patch.object(live_processing, 'transcribe_range', return_value={'segments': [{'startMs': 0, 'endMs': 100, 'text': '尾部'}]}):
            self.assertTrue(live_processing.process_session_once(self.root, self.db_path, self.session_id))
        status = live_processing.live_processing_status(self.db_path, self.session_id)
        self.assertEqual(status['completedWindows'], 1)

    def test_merge_only_removes_overlapping_duplicate_text(self) -> None:
        merged = live_processing.merge_transcript_segments([
            {'startMs': 0, 'endMs': 1000, 'text': '重复内容'},
            {'startMs': 900, 'endMs': 1200, 'text': '重复 内容'},
            {'startMs': 1100, 'endMs': 1400, 'text': '重复内容但语义不同'},
        ])
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]['endMs'], 1200)
        self.assertEqual(merged[1]['text'], '重复内容但语义不同')


if __name__ == '__main__':
    unittest.main()
