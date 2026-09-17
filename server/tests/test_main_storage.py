import os
import hashlib
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


_ROOT = Path(tempfile.mkdtemp(prefix='livenote-main-test-'))
with patch.dict(os.environ, {
    'LIVENOTE_DATA_DIR': str(_ROOT / 'data'),
    'LIVENOTE_DB_PATH': str(_ROOT / 'livenote.sqlite3'),
}, clear=False):
    from server import main


class MainStorageTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(_ROOT, ignore_errors=True)

    def test_api_writes_are_committed_before_connection_closes(self) -> None:
        main.DB_PATH = _ROOT / 'livenote.sqlite3'
        main.DATA_DIR = _ROOT / 'data'
        main.init_db()
        schema_connection = sqlite3.connect(main.DB_PATH)
        try:
            columns = {row[1] for row in schema_connection.execute('PRAGMA table_info(segments)').fetchall()}
        finally:
            schema_connection.close()
        self.assertIn('start_elapsed_ms', columns)
        now = main.now_ms()
        payload = main.SessionPayload(
            id='commit-check',
            title='commit-check',
            startedAt=now,
            status='COMPLETED',
            createdAt=now,
            updatedAt=now,
        )

        main.create_session(payload)

        connection = sqlite3.connect(main.DB_PATH)
        try:
            count = connection.execute('SELECT COUNT(*) FROM sessions WHERE id = ?', ('commit-check',)).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 1)

    def test_chunk_upload_is_idempotent_and_delete_cleans_server_copy(self) -> None:
        from fastapi.testclient import TestClient

        client = TestClient(main.app)
        health = client.get('/api/v1/health')
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()['storageSchema'], 2)
        self.assertIn('llmConfigured', health.json()['capabilities'])
        now = main.now_ms()
        session_id = 'api-contract-session'
        segment_id = 'api-contract-segment'
        session = {
            'id': session_id,
            'title': 'API contract',
            'startedAt': now,
            'endedAt': None,
            'status': 'COMPLETED',
            'durationMs': 1200,
            'createdAt': now,
            'updatedAt': now,
        }
        segment = {
            'id': segment_id,
            'sessionId': session_id,
            'index': 1,
            'startedAt': now,
            'startElapsedMs': 0,
            'endedAt': now + 1200,
            'mimeType': 'audio/webm',
            'mediaSettings': {},
            'status': 'COMPLETED',
            'durationMs': 1200,
        }
        self.assertEqual(client.post('/api/v1/sessions', json=session).status_code, 200)
        self.assertEqual(client.post(f'/api/v1/sessions/{session_id}/segments', json=segment).status_code, 200)

        body = b'contract-audio'
        import hashlib
        headers = {
            'content-type': 'audio/webm',
            'x-chunk-sha256': hashlib.sha256(body).hexdigest(),
            'x-chunk-size': str(len(body)),
            'x-chunk-elapsed-ms': '1200',
        }
        first = client.put(f'/api/v1/sessions/{session_id}/segments/{segment_id}/chunks/0', content=body, headers=headers)
        second = client.put(f'/api/v1/sessions/{session_id}/segments/{segment_id}/chunks/0', content=body, headers=headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()['already_exists'])

        deleted = client.delete(f'/api/v1/sessions/{session_id}')
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()['chunks'], 1)

    def test_latest_processing_job_can_be_restored_after_page_reload(self) -> None:
        main.DB_PATH = _ROOT / 'livenote.sqlite3'
        main.DATA_DIR = _ROOT / 'data'
        main.init_db()
        session_id = 'processing-restore-session'
        now = main.now_ms()
        main.create_session(main.SessionPayload(
            id=session_id,
            title='processing restore',
            startedAt=now,
            status='COMPLETED',
            createdAt=now,
            updatedAt=now,
        ))
        job_path = main.DATA_DIR / 'processed' / 'jobs' / 'job-restore.json'
        job_path.parent.mkdir(parents=True, exist_ok=True)
        job_path.write_text(json.dumps({
            'id': 'job-restore',
            'sessionId': session_id,
            'status': 'RUNNING',
            'stage': 'ASR',
            'message': '正在转写。',
            'createdAt': 10,
            'updatedAt': 20,
        }), encoding='utf-8')

        restored = main.get_latest_processing_job(session_id)
        self.assertEqual(restored['id'], 'job-restore')
        self.assertEqual(restored['stage'], 'ASR')

    def test_processing_rejects_a_partial_upload(self) -> None:
        session_id = 'partial-processing-session'
        segment_id = 'partial-processing-segment'
        now = main.now_ms()
        client_session = main.SessionPayload(
            id=session_id,
            title='partial processing',
            startedAt=now,
            status='COMPLETED',
            createdAt=now,
            updatedAt=now,
        )
        main.create_session(client_session)
        main.create_segment(session_id, main.SegmentPayload(
            id=segment_id,
            sessionId=session_id,
            index=1,
            startedAt=now,
            endedAt=now + 1000,
            mimeType='audio/webm',
            status='COMPLETED',
            durationMs=1000,
        ))
        with self.assertRaises(main.HTTPException) as context:
            main.start_processing_job(session_id, None)
        self.assertEqual(context.exception.status_code, 409)

    def test_processing_rejects_a_legacy_session_with_missing_tail_chunks(self) -> None:
        session_id = 'legacy-short-session'
        segment_id = 'legacy-short-segment'
        now = main.now_ms()
        main.create_session(main.SessionPayload(
            id=session_id,
            title='legacy short',
            startedAt=now,
            status='COMPLETED',
            durationMs=52_000,
            createdAt=now,
            updatedAt=now,
        ))
        # Simulate a legacy server record that was marked completed before
        # the final 33 seconds reached the server.
        with main.connect() as connection:
            connection.execute('UPDATE sessions SET status = ? WHERE id = ?', ('COMPLETED', session_id))
            connection.execute(
                'INSERT INTO segments(id, session_id, segment_index, started_at, ended_at, mime_type, media_settings, status, duration_ms, start_elapsed_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (segment_id, session_id, 1, now, now + 19_000, 'audio/webm', '{}', 'COMPLETED', 19_000, 0),
            )
            connection.execute(
                'INSERT INTO chunks(session_id, segment_id, chunk_index, size, sha256, mime_type, elapsed_ms, created_at, received_at, local_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (session_id, segment_id, 0, 1, 'hash', 'audio/webm', 19_000, now, now, 'sessions/legacy-short/segment_1/chunks/000000.bin'),
            )
        with self.assertRaises(main.HTTPException) as context:
            main.start_processing_job(session_id, None)
        self.assertEqual(context.exception.status_code, 409)

    def test_complete_segment_rejects_gapped_indexes_without_expected_count(self) -> None:
        session_id = 'gapped-completion-session'
        segment_id = 'gapped-completion-segment'
        now = main.now_ms()
        main.create_session(main.SessionPayload(
            id=session_id,
            title='gapped completion',
            startedAt=now,
            status='RECORDING',
            createdAt=now,
            updatedAt=now,
        ))
        main.create_segment(session_id, main.SegmentPayload(
            id=segment_id,
            sessionId=session_id,
            index=1,
            startedAt=now,
            endedAt=now + 1000,
            mimeType='audio/webm',
            status='RECORDING',
            durationMs=1000,
        ))
        with main.connect() as connection:
            connection.execute(
                'INSERT INTO chunks(session_id, segment_id, chunk_index, size, sha256, mime_type, elapsed_ms, created_at, received_at, local_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (session_id, segment_id, 1, 1, 'hash', 'audio/webm', 1000, now, now, 'sessions/gapped/segment_1/chunks/000001.bin'),
            )

        with self.assertRaises(main.HTTPException) as context:
            main.complete_segment(session_id, segment_id)
        self.assertEqual(context.exception.status_code, 409)

    def test_complete_session_rejects_non_contiguous_segment_indexes(self) -> None:
        session_id = 'gapped-session-completion'
        now = main.now_ms()
        main.create_session(main.SessionPayload(
            id=session_id,
            title='gapped session completion',
            startedAt=now,
            status='RECORDING',
            durationMs=1000,
            createdAt=now,
            updatedAt=now,
        ))
        for segment_index in (1, 3):
            segment_id = f'gapped-session-segment-{segment_index}'
            main.create_segment(session_id, main.SegmentPayload(
                id=segment_id,
                sessionId=session_id,
                index=segment_index,
                startedAt=now,
                endedAt=now + 1000,
                mimeType='audio/webm',
                status='COMPLETED',
                durationMs=1000,
            ))
            with main.connect() as connection:
                connection.execute(
                    'INSERT INTO chunks(session_id, segment_id, chunk_index, size, sha256, mime_type, elapsed_ms, created_at, received_at, local_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (session_id, segment_id, 0, 1, f'hash-{segment_index}', 'audio/webm', 1000, now, now, f'sessions/gapped-session/segment_{segment_index}/chunks/000000.bin'),
                )
            with main.connect() as connection:
                connection.execute('UPDATE segments SET status = ? WHERE id = ?', ('COMPLETED', segment_id))

        with self.assertRaises(main.HTTPException) as context:
            main.complete_session(session_id)
        self.assertEqual(context.exception.status_code, 409)

    def test_real_webm_multi_segment_upload_reconstructs_full_session(self) -> None:
        from fastapi.testclient import TestClient

        session_id = 'real-webm-session'
        now = main.now_ms()
        client = TestClient(main.app)
        self.assertEqual(client.post('/api/v1/sessions', json={
            'id': session_id,
            'title': 'real webm integration',
            'startedAt': now,
            'endedAt': now + 2200,
            'status': 'COMPLETED',
            'durationMs': 2200,
            'createdAt': now,
            'updatedAt': now + 2200,
        }).status_code, 200)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for segment_index, frequency in ((1, 440), (2, 660)):
                segment_id = f'real-webm-segment-{segment_index}'
                segment_started = now + (segment_index - 1) * 1100
                segment_elapsed = (segment_index - 1) * 1100
                self.assertEqual(client.post(f'/api/v1/sessions/{session_id}/segments', json={
                    'id': segment_id,
                    'sessionId': session_id,
                    'index': segment_index,
                    'startedAt': segment_started,
                    'startElapsedMs': segment_elapsed,
                    'endedAt': segment_started + 1100,
                    'mimeType': 'audio/webm;codecs=opus',
                    'mediaSettings': {},
                    'status': 'COMPLETED',
                    'durationMs': 1100,
                }).status_code, 200)

                source = root / f'segment-{segment_index}.webm'
                generated = subprocess.run([
                    'ffmpeg', '-y', '-v', 'error',
                    '-f', 'lavfi', '-i', f'sine=frequency={frequency}:duration=1.1',
                    '-c:a', 'libopus', '-b:a', '32k', str(source),
                ], capture_output=True, text=True, timeout=60, check=False)
                self.assertEqual(generated.returncode, 0, generated.stderr)
                body = source.read_bytes()
                headers = {
                    'content-type': 'audio/webm;codecs=opus',
                    'x-chunk-sha256': hashlib.sha256(body).hexdigest(),
                    'x-chunk-size': str(len(body)),
                    'x-chunk-elapsed-ms': str(segment_elapsed + 1100),
                }
                uploaded = client.put(
                    f'/api/v1/sessions/{session_id}/segments/{segment_id}/chunks/0',
                    content=body,
                    headers=headers,
                )
                self.assertEqual(uploaded.status_code, 200, uploaded.text)
                self.assertEqual(client.post(
                    f'/api/v1/sessions/{session_id}/segments/{segment_id}/complete',
                    json={'expectedChunkCount': 1},
                ).status_code, 200)

        self.assertEqual(client.post(f'/api/v1/sessions/{session_id}/complete', json={'expectedChunkCount': 2}).status_code, 200)
        segment_audio = client.get(f'/api/v1/sessions/{session_id}/segments/real-webm-segment-1/audio')
        self.assertEqual(segment_audio.status_code, 200, segment_audio.text)
        self.assertGreater(len(segment_audio.content), 0)
        audio = client.get(f'/api/v1/sessions/{session_id}/audio')
        self.assertEqual(audio.status_code, 200, audio.text)
        self.assertGreater(len(audio.content), 0)

        output = _ROOT / 'reconstructed' / 'integration-check.webm'
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(audio.content)
        try:
            probed = subprocess.run([
                'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                '-of', 'default=nw=1:nk=1', str(output),
            ], capture_output=True, text=True, timeout=60, check=False)
            self.assertEqual(probed.returncode, 0, probed.stderr)
            self.assertGreater(float(probed.stdout.strip()), 1.5)
        finally:
            output.unlink(missing_ok=True)


if __name__ == '__main__':
    unittest.main()
