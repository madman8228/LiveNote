import os
import hashlib
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


_ROOT = Path(tempfile.mkdtemp(prefix='livenote-main-test-'))
with patch.dict(os.environ, {
    'LIVENOTE_DATA_DIR': str(_ROOT / 'data'),
    'LIVENOTE_DB_PATH': str(_ROOT / 'livenote.sqlite3'),
    'LIVENOTE_REQUIRE_DEVICE_AUTH': '0',
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

    def test_strict_device_auth_rejects_anonymous_session_create(self) -> None:
        from fastapi.testclient import TestClient

        previous = main.REQUIRE_DEVICE_AUTH
        main.REQUIRE_DEVICE_AUTH = True
        try:
            client = TestClient(main.app)
            now = main.now_ms()
            response = client.post('/api/v1/sessions', json={
                'id': 'strict-auth-check', 'title': 'strict auth', 'startedAt': now,
                'endedAt': None, 'status': 'RECORDING', 'durationMs': 0,
                'createdAt': now, 'updatedAt': now,
            })
            self.assertEqual(response.status_code, 401)
            connection = sqlite3.connect(main.DB_PATH)
            try:
                self.assertIsNone(connection.execute('SELECT id FROM sessions WHERE id = ?', ('strict-auth-check',)).fetchone())
            finally:
                connection.close()
        finally:
            main.REQUIRE_DEVICE_AUTH = previous

    def test_paired_chunk_upload_does_not_leave_auth_transaction_open(self) -> None:
        from fastapi.testclient import TestClient
        import hashlib

        client = TestClient(main.app)
        previous_admin = os.environ.get('LIVENOTE_ADMIN_TOKEN')
        os.environ['LIVENOTE_ADMIN_TOKEN'] = 'paired-upload-admin-token'
        try:
            admin_headers = {'X-Admin-Token': 'paired-upload-admin-token'}
            user = client.post('/api/v1/admin/users', headers=admin_headers, json={'displayName': '上传事务用户'})
            self.assertEqual(user.status_code, 200, user.text)
            user_id = user.json()['id']
            pairing = client.post(f'/api/v1/admin/users/{user_id}/pairing-codes', headers=admin_headers, json={})
            self.assertEqual(pairing.status_code, 200, pairing.text)
            device = client.post('/api/v1/auth/pair', json={'code': pairing.json()['code'], 'label': 'paired upload test'})
            self.assertEqual(device.status_code, 200, device.text)
            device_headers = {'Authorization': f"Bearer {device.json()['token']}"}
            now = main.now_ms()
            session_id = 'paired-upload-session'
            segment_id = 'paired-upload-segment'
            session = {
                'id': session_id, 'title': 'paired upload', 'startedAt': now,
                'endedAt': None, 'status': 'RECORDING', 'durationMs': 1000,
                'createdAt': now, 'updatedAt': now,
            }
            segment = {
                'id': segment_id, 'sessionId': session_id, 'index': 1,
                'startedAt': now, 'startElapsedMs': 0, 'endedAt': None,
                'mimeType': 'audio/webm', 'mediaSettings': {}, 'status': 'RECORDING',
                'durationMs': 1000,
            }
            self.assertEqual(client.post('/api/v1/sessions', headers=device_headers, json=session).status_code, 200)
            self.assertEqual(client.post(f'/api/v1/sessions/{session_id}/segments', headers=device_headers, json=segment).status_code, 200)
            body = b'paired-upload-audio'
            headers = {
                **device_headers,
                'content-type': 'audio/webm',
                'x-chunk-sha256': hashlib.sha256(body).hexdigest(),
                'x-chunk-size': str(len(body)),
                'x-chunk-elapsed-ms': '1000',
            }
            uploaded = client.put(f'/api/v1/sessions/{session_id}/segments/{segment_id}/chunks/0', content=body, headers=headers)
            self.assertEqual(uploaded.status_code, 200, uploaded.text)
        finally:
            if previous_admin is None:
                os.environ.pop('LIVENOTE_ADMIN_TOKEN', None)
            else:
                os.environ['LIVENOTE_ADMIN_TOKEN'] = previous_admin

    def test_admin_session_list_filters_by_started_time_range(self) -> None:
        from fastapi.testclient import TestClient

        client = TestClient(main.app)
        previous_admin = os.environ.get('LIVENOTE_ADMIN_TOKEN')
        os.environ['LIVENOTE_ADMIN_TOKEN'] = 'session-filter-admin-token'
        try:
            now = main.now_ms()
            for session_id, started_at in (('filter-session-old', now - 2 * 86_400_000), ('filter-session-new', now)):
                main.create_session(main.SessionPayload(
                    id=session_id, title=session_id, startedAt=started_at, endedAt=None,
                    status='RECORDING', durationMs=0, createdAt=started_at, updatedAt=started_at,
                ))
            response = client.get('/api/v1/admin/sessions', headers={'X-Admin-Token': 'session-filter-admin-token'}, params={
                'query': 'filter-session-',
                'startedFrom': now - 1_000,
                'startedTo': now + 1_000,
            })
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual([item['id'] for item in response.json()['items']], ['filter-session-new'])
        finally:
            if previous_admin is None:
                os.environ.pop('LIVENOTE_ADMIN_TOKEN', None)
            else:
                os.environ['LIVENOTE_ADMIN_TOKEN'] = previous_admin

    def test_admin_pairing_assigns_device_and_isolates_sessions(self) -> None:
        from fastapi.testclient import TestClient

        previous_admin = os.environ.get('LIVENOTE_ADMIN_TOKEN')
        os.environ['LIVENOTE_ADMIN_TOKEN'] = 'admin-test-token'
        try:
            client = TestClient(main.app)
            admin_headers = {'X-Admin-Token': 'admin-test-token'}
            created = client.post('/api/v1/admin/users', headers=admin_headers, json={'displayName': '测试用户'})
            self.assertEqual(created.status_code, 200)
            user_id = created.json()['id']
            pairing = client.post(f'/api/v1/admin/users/{user_id}/pairing-codes', headers=admin_headers, json={})
            self.assertEqual(pairing.status_code, 200)
            paired = client.post('/api/v1/auth/pair', json={'code': pairing.json()['code'], 'label': 'test chrome'})
            self.assertEqual(paired.status_code, 200)
            device_headers = {'Authorization': f"Bearer {paired.json()['token']}"}
            now = main.now_ms()
            created_session = client.post('/api/v1/sessions', headers=device_headers, json={
                'id': 'paired-session', 'title': '绑定会话', 'startedAt': now, 'endedAt': None,
                'status': 'RECORDING', 'durationMs': 0, 'createdAt': now, 'updatedAt': now,
            })
            self.assertEqual(created_session.status_code, 200)
            self.assertEqual(client.get('/api/v1/sessions/paired-session/result').status_code, 401)
            self.assertEqual(client.get('/api/v1/sessions/paired-session/audio').status_code, 401)
            self.assertNotEqual(client.get('/api/v1/sessions/paired-session/audio', headers=device_headers).status_code, 401)
            self.assertEqual(client.get('/api/v1/sessions/paired-session/upload-state', headers=device_headers).status_code, 200)
            owned_sessions = client.get('/api/v1/sessions', headers=device_headers)
            self.assertEqual(owned_sessions.status_code, 200)
            self.assertTrue(any(item['id'] == 'paired-session' for item in owned_sessions.json()['items']))
            self.assertEqual(client.get('/api/v1/sessions').status_code, 401)
            sessions = client.get('/api/v1/admin/sessions', headers=admin_headers)
            self.assertEqual(sessions.status_code, 200)
            self.assertTrue(any(item['id'] == 'paired-session' and item['owner_id'] == user_id for item in sessions.json()['items']))

            other_user = client.post('/api/v1/admin/users', headers=admin_headers, json={'displayName': '另一个用户'})
            self.assertEqual(other_user.status_code, 200)
            other_pairing = client.post(
                f"/api/v1/admin/users/{other_user.json()['id']}/pairing-codes",
                headers=admin_headers,
                json={},
            )
            self.assertEqual(other_pairing.status_code, 200)
            other_device = client.post('/api/v1/auth/pair', json={'code': other_pairing.json()['code'], 'label': 'other chrome'})
            self.assertEqual(other_device.status_code, 200)
            other_headers = {'Authorization': f"Bearer {other_device.json()['token']}"}
            self.assertEqual(client.get('/api/v1/sessions/paired-session/live-processing', headers=other_headers).status_code, 403)
            overwrite = client.post('/api/v1/sessions', headers=other_headers, json={
                'id': 'paired-session', 'title': '不应覆盖', 'startedAt': now, 'endedAt': None,
                'status': 'RECORDING', 'durationMs': 999, 'createdAt': now, 'updatedAt': now,
            })
            self.assertEqual(overwrite.status_code, 403)
            reassigned = client.patch(
                '/api/v1/admin/sessions/paired-session',
                headers=admin_headers,
                json={'title': '改名后的会话', 'ownerId': other_user.json()['id']},
            )
            self.assertEqual(reassigned.status_code, 200, reassigned.text)
            refreshed = client.get('/api/v1/admin/sessions', headers=admin_headers)
            edited = next(item for item in refreshed.json()['items'] if item['id'] == 'paired-session')
            self.assertEqual(edited['title'], '改名后的会话')
            self.assertEqual(edited['owner_id'], other_user.json()['id'])
            blank_title = client.patch(
                '/api/v1/admin/sessions/paired-session',
                headers=admin_headers,
                json={'title': '   '},
            )
            self.assertEqual(blank_title.status_code, 422)
        finally:
            if previous_admin is None:
                os.environ.pop('LIVENOTE_ADMIN_TOKEN', None)
            else:
                os.environ['LIVENOTE_ADMIN_TOKEN'] = previous_admin

    def test_chunk_upload_is_idempotent_and_delete_cleans_server_copy(self) -> None:
        from fastapi.testclient import TestClient

        client = TestClient(main.app)
        health = client.get('/api/v1/health')
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()['storageSchema'], 9)
        self.assertTrue(health.json()['capabilities']['manualProcessing'])
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

        live_artifact = main.DATA_DIR / 'processed' / 'live' / session_id / 'segment_1' / 'window-00000.json'
        live_artifact.parent.mkdir(parents=True, exist_ok=True)
        live_artifact.write_text('{"segments": []}', encoding='utf-8')

        deleted = client.delete(f'/api/v1/sessions/{session_id}')
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()['chunks'], 1)
        self.assertFalse(live_artifact.exists())
        with sqlite3.connect(main.DB_PATH) as connection:
            self.assertIsNone(connection.execute('SELECT id FROM live_processing_runs WHERE session_id = ?', (session_id,)).fetchone())

    def test_deleted_session_rejects_late_upload_recreation(self) -> None:
        from fastapi.testclient import TestClient

        client = TestClient(main.app)
        now = main.now_ms()
        session_id = 'delete-race-late-recreate'
        session = {
            'id': session_id,
            'title': '竞态删除测试',
            'startedAt': now,
            'endedAt': now + 1000,
            'status': 'COMPLETED',
            'durationMs': 1000,
            'createdAt': now,
            'updatedAt': now,
        }
        self.assertEqual(client.post('/api/v1/sessions', json=session).status_code, 200)
        self.assertEqual(client.delete(f'/api/v1/sessions/{session_id}').status_code, 200)

        late_upload = {**session, 'status': 'RECORDING', 'endedAt': None, 'updatedAt': now + 2000}
        recreated = client.post('/api/v1/sessions', json=late_upload)
        self.assertEqual(recreated.status_code, 409, recreated.text)
        late_segment = client.post(f'/api/v1/sessions/{session_id}/segments', json={
            'id': 'delete-race-late-segment',
            'sessionId': session_id,
            'index': 1,
            'startedAt': now,
            'startElapsedMs': 0,
            'endedAt': None,
            'mimeType': 'audio/webm',
            'mediaSettings': {},
            'status': 'RECORDING',
            'durationMs': 0,
        })
        self.assertEqual(late_segment.status_code, 404, late_segment.text)

        connection = sqlite3.connect(main.DB_PATH)
        try:
            self.assertIsNone(connection.execute('SELECT id FROM sessions WHERE id = ?', (session_id,)).fetchone())
        finally:
            connection.close()

    def test_ended_recording_session_is_repaired_to_interrupted(self) -> None:
        now = main.now_ms()
        session_id = 'ended-recording-without-completion'
        main.create_session(main.SessionPayload(
            id=session_id,
            title='结束但未收尾',
            startedAt=now - 1000,
            endedAt=now,
            status='RECORDING',
            durationMs=1000,
            createdAt=now - 1000,
            updatedAt=now,
        ))

        with main.connect() as connection:
            repaired = main.normalize_stale_sessions(connection)
            status = connection.execute('SELECT status FROM sessions WHERE id = ?', (session_id,)).fetchone()['status']

        self.assertGreaterEqual(repaired, 1)
        self.assertEqual(status, 'INTERRUPTED')

    def test_admin_can_delete_session_and_requires_admin_token(self) -> None:
        from fastapi.testclient import TestClient

        client = TestClient(main.app)
        now = main.now_ms()
        session_id = 'admin-delete-session'
        self.assertEqual(client.post('/api/v1/sessions', json={
            'id': session_id, 'title': '管理员删除测试', 'startedAt': now, 'endedAt': now,
            'status': 'COMPLETED', 'durationMs': 0, 'createdAt': now, 'updatedAt': now,
        }).status_code, 200)
        previous_admin = os.environ.get('LIVENOTE_ADMIN_TOKEN')
        os.environ['LIVENOTE_ADMIN_TOKEN'] = 'admin-delete-token'
        try:
            self.assertEqual(client.delete(f'/api/v1/admin/sessions/{session_id}', headers={'X-Admin-Token': 'wrong-token'}).status_code, 401)
            deleted = client.delete(f'/api/v1/admin/sessions/{session_id}', headers={'X-Admin-Token': 'admin-delete-token'})
            self.assertEqual(deleted.status_code, 200)
            self.assertEqual(client.get(f'/api/v1/admin/sessions/{session_id}', headers={'X-Admin-Token': 'admin-delete-token'}).status_code, 404)
        finally:
            if previous_admin is None:
                os.environ.pop('LIVENOTE_ADMIN_TOKEN', None)
            else:
                os.environ['LIVENOTE_ADMIN_TOKEN'] = previous_admin

    def test_admin_can_interrupt_abandoned_recording_without_deleting_audio(self) -> None:
        from fastapi.testclient import TestClient

        client = TestClient(main.app)
        now = main.now_ms()
        session_id = 'admin-interrupt-session'
        segment_id = 'admin-interrupt-segment'
        main.create_session(main.SessionPayload(
            id=session_id, title='遗留录音', startedAt=now - 5000, endedAt=None,
            status='RECORDING', durationMs=0, createdAt=now - 5000, updatedAt=now - 5000,
        ))
        main.create_segment(session_id, main.SegmentPayload(
            id=segment_id, sessionId=session_id, index=1, startedAt=now - 5000,
            endedAt=None, mimeType='audio/webm', status='RECORDING', durationMs=0,
        ))
        previous_admin = os.environ.get('LIVENOTE_ADMIN_TOKEN')
        os.environ['LIVENOTE_ADMIN_TOKEN'] = 'admin-interrupt-token'
        try:
            response = client.post(
                f'/api/v1/admin/sessions/{session_id}/interrupt',
                headers={'X-Admin-Token': 'admin-interrupt-token'},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['status'], 'INTERRUPTED')
            with main.connect() as connection:
                session = connection.execute('SELECT status, ended_at FROM sessions WHERE id = ?', (session_id,)).fetchone()
                segment = connection.execute('SELECT status, ended_at FROM segments WHERE id = ?', (segment_id,)).fetchone()
            self.assertEqual(session['status'], 'INTERRUPTED')
            self.assertIsNotNone(session['ended_at'])
            self.assertEqual(segment['status'], 'INTERRUPTED')
            self.assertIsNotNone(segment['ended_at'])
            repeated = client.post(
                f'/api/v1/admin/sessions/{session_id}/interrupt',
                headers={'X-Admin-Token': 'admin-interrupt-token'},
            )
            self.assertEqual(repeated.status_code, 409)
        finally:
            if previous_admin is None:
                os.environ.pop('LIVENOTE_ADMIN_TOKEN', None)
            else:
                os.environ['LIVENOTE_ADMIN_TOKEN'] = previous_admin

    def test_completed_session_creates_task_and_phone_can_read_result(self) -> None:
        from fastapi.testclient import TestClient

        session_id = 'task-lifecycle-session'
        segment_id = 'task-lifecycle-segment'
        now = main.now_ms()
        main.create_session(main.SessionPayload(
            id=session_id,
            title='task lifecycle',
            startedAt=now,
            endedAt=now + 1000,
            status='COMPLETED',
            durationMs=1000,
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
            status='COMPLETED',
            durationMs=1000,
        ))
        with main.connect() as connection:
            connection.execute(
                'INSERT INTO chunks(session_id, segment_id, chunk_index, size, sha256, mime_type, elapsed_ms, created_at, received_at, local_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (session_id, segment_id, 0, 1, 'task-hash', 'audio/webm', 1000, now, now, 'sessions/task-lifecycle/segment_1/chunks/000000.bin'),
            )

        main.complete_segment(session_id, segment_id)
        main.complete_session(session_id)
        client = TestClient(main.app)
        listed = client.get('/api/v1/tasks?status=READY')
        self.assertEqual(listed.status_code, 200)
        task = next(item for item in listed.json()['tasks'] if item['sessionId'] == session_id)
        self.assertEqual(task['status'], 'READY')

        claimed = client.post(f"/api/v1/tasks/{task['id']}/claim", json={'workerId': 'test-worker'})
        self.assertEqual(claimed.status_code, 200)
        self.assertEqual(claimed.json()['task']['status'], 'CLAIMED')
        lease_token = claimed.json()['leaseToken']
        local_ready = client.post(f"/api/v1/tasks/{task['id']}/status", json={'status': 'LOCAL_READY', 'workerId': 'test-worker', 'leaseToken': lease_token})
        self.assertEqual(local_ready.status_code, 200)
        changed = client.post(f"/api/v1/tasks/{task['id']}/status", json={'status': 'PROCESSING', 'workerId': 'test-worker', 'leaseToken': lease_token})
        self.assertEqual(changed.status_code, 200)
        result = client.post(f"/api/v1/tasks/{task['id']}/result", headers={'X-Worker-Id': 'test-worker', 'X-Task-Lease': lease_token}, json={
            'version': 1,
            'result': {
                'title': '任务结果',
                'overview': '处理完成',
                'keyPoints': ['第一点'],
                'knowledgeStructure': [{'title': '结构', 'points': ['内容']}],
            },
        })
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['status'], 'REVIEW')
        with main.connect() as connection:
            review_task = connection.execute(
                'SELECT status, claimed_by, requested_worker_id, lease_token_hash, lease_expires_at FROM processing_tasks WHERE id = ?',
                (task['id'],),
            ).fetchone()
        self.assertEqual(review_task['status'], 'REVIEW')
        self.assertIsNone(review_task['claimed_by'])
        self.assertIsNone(review_task['requested_worker_id'])
        self.assertIsNone(review_task['lease_token_hash'])
        self.assertIsNone(review_task['lease_expires_at'])
        fetched = client.get(f'/api/v1/sessions/{session_id}/result')
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()['status'], 'REVIEW')
        self.assertIsNone(fetched.json()['result'])
        previous_admin = os.environ.get('LIVENOTE_ADMIN_TOKEN')
        os.environ['LIVENOTE_ADMIN_TOKEN'] = 'publish-test-token'
        try:
            published = client.post(f'/api/v1/admin/tasks/{task["id"]}/publish', headers={'X-Admin-Token': 'publish-test-token'}, json={'revisionId': result.json()['revisionId']})
            self.assertEqual(published.status_code, 200)
            visible = client.get(f'/api/v1/sessions/{session_id}/result')
            self.assertEqual(visible.status_code, 200)
            self.assertEqual(visible.json()['status'], 'COMPLETED')
            self.assertEqual(visible.json()['result']['title'], '任务结果')
            with main.connect() as connection:
                completed_task = connection.execute(
                    'SELECT status, claimed_by, requested_worker_id, lease_token_hash, lease_expires_at FROM processing_tasks WHERE id = ?',
                    (task['id'],),
                ).fetchone()
            self.assertEqual(completed_task['status'], 'COMPLETED')
            self.assertIsNone(completed_task['claimed_by'])
            self.assertIsNone(completed_task['requested_worker_id'])
            self.assertIsNone(completed_task['lease_token_hash'])
            self.assertIsNone(completed_task['lease_expires_at'])
        finally:
            if previous_admin is None:
                os.environ.pop('LIVENOTE_ADMIN_TOKEN', None)
            else:
                os.environ['LIVENOTE_ADMIN_TOKEN'] = previous_admin

    def test_admin_can_start_local_task_and_upload_result_without_cli(self) -> None:
        from fastapi.testclient import TestClient

        session_id = 'admin-result-session'
        segment_id = 'admin-result-segment'
        now = main.now_ms()
        main.create_session(main.SessionPayload(
            id=session_id,
            title='admin result flow',
            startedAt=now,
            endedAt=now + 1000,
            status='COMPLETED',
            durationMs=1000,
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
            status='COMPLETED',
            durationMs=1000,
        ))
        with main.connect() as connection:
            connection.execute(
                'INSERT INTO chunks(session_id, segment_id, chunk_index, size, sha256, mime_type, elapsed_ms, created_at, received_at, local_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (session_id, segment_id, 0, 1, 'admin-result-hash', 'audio/webm', 1000, now, now, 'sessions/admin-result/segment_1/chunks/000000.bin'),
            )
        main.complete_segment(session_id, segment_id)
        main.complete_session(session_id)
        client = TestClient(main.app)
        task = next(item for item in client.get('/api/v1/tasks?status=READY').json()['tasks'] if item['sessionId'] == session_id)
        with main.connect() as connection:
            connection.execute(
                "UPDATE processing_tasks SET status = 'LOCAL_READY', claimed_by = 'local-pc' WHERE id = ?",
                (task['id'],),
            )
        previous_admin = os.environ.get('LIVENOTE_ADMIN_TOKEN')
        os.environ['LIVENOTE_ADMIN_TOKEN'] = 'admin-result-token'
        try:
            headers = {'X-Admin-Token': 'admin-result-token'}
            started = client.post(
                f"/api/v1/admin/tasks/{task['id']}/start-processing",
                headers=headers,
                json={'workerId': 'local-pc'},
            )
            self.assertEqual(started.status_code, 200, started.text)
            uploaded = client.post(
                f"/api/v1/admin/tasks/{task['id']}/result",
                headers=headers,
                json={'version': 1, 'result': {'title': '页面回传', 'overview': '无需命令行'}},
            )
            self.assertEqual(uploaded.status_code, 200, uploaded.text)
            self.assertEqual(uploaded.json()['status'], 'REVIEW')
        finally:
            if previous_admin is None:
                os.environ.pop('LIVENOTE_ADMIN_TOKEN', None)
            else:
                os.environ['LIVENOTE_ADMIN_TOKEN'] = previous_admin

    def test_releasing_or_retrying_task_clears_worker_assignment(self) -> None:
        from fastapi.testclient import TestClient

        session_id = 'task-release-assignment-session'
        task_id = 'task-release-assignment'
        now = main.now_ms()
        main.create_session(main.SessionPayload(
            id=session_id,
            title='release assignment',
            startedAt=now,
            endedAt=now + 1000,
            status='COMPLETED',
            durationMs=1000,
            createdAt=now,
            updatedAt=now,
        ))
        with main.connect() as connection:
            connection.execute(
                '''INSERT INTO processing_tasks(
                       id, session_id, status, attempts, claimed_by, claimed_at,
                       requested_worker_id, lease_token_hash, lease_expires_at,
                       downloaded_at, error_message, error_stage, result_version,
                       created_at, updated_at
                   ) VALUES (?, ?, 'CLAIMED', 1, ?, ?, ?, ?, NULL, ?, '', '', 0, ?, ?)''',
                (task_id, session_id, 'worker-a', now, 'worker-a', 'hash-a', now, now, now),
            )

        previous_admin = os.environ.get('LIVENOTE_ADMIN_TOKEN')
        os.environ['LIVENOTE_ADMIN_TOKEN'] = 'task-assignment-admin-token'
        try:
            client = TestClient(main.app)
            headers = {'X-Admin-Token': 'task-assignment-admin-token'}
            released = client.post(f'/api/v1/admin/tasks/{task_id}/release', headers=headers)
            self.assertEqual(released.status_code, 200, released.text)
            with main.connect() as connection:
                released_row = connection.execute(
                    'SELECT status, claimed_by, requested_worker_id, downloaded_at, error_message FROM processing_tasks WHERE id = ?',
                    (task_id,),
                ).fetchone()
            self.assertEqual(released_row['status'], 'READY')
            self.assertIsNone(released_row['claimed_by'])
            self.assertIsNone(released_row['requested_worker_id'])
            self.assertIsNone(released_row['downloaded_at'])
            self.assertEqual(released_row['error_message'], '')

            with main.connect() as connection:
                connection.execute(
                    "UPDATE processing_tasks SET status = 'FAILED', requested_worker_id = 'worker-b', error_message = 'download failed', error_stage = 'DOWNLOAD' WHERE id = ?",
                    (task_id,),
                )
            retried = client.post(f'/api/v1/admin/tasks/{task_id}/retry', headers=headers)
            self.assertEqual(retried.status_code, 200, retried.text)
            with main.connect() as connection:
                retried_row = connection.execute(
                    'SELECT status, requested_worker_id, error_message, error_stage FROM processing_tasks WHERE id = ?',
                    (task_id,),
                ).fetchone()
            self.assertEqual(retried_row['status'], 'READY')
            self.assertIsNone(retried_row['requested_worker_id'])
            self.assertEqual(retried_row['error_message'], '')
            self.assertEqual(retried_row['error_stage'], '')
        finally:
            if previous_admin is None:
                os.environ.pop('LIVENOTE_ADMIN_TOKEN', None)
            else:
                os.environ['LIVENOTE_ADMIN_TOKEN'] = previous_admin

    def test_legacy_mobile_processing_routes_are_disabled(self) -> None:
        from fastapi.testclient import TestClient

        client = TestClient(main.app)
        self.assertEqual(client.post('/api/v1/sessions/legacy/transcribe').status_code, 410)
        self.assertEqual(client.post('/api/v1/sessions/legacy/process').status_code, 410)
        self.assertEqual(client.get('/api/v1/sessions/legacy/report').status_code, 410)
        self.assertEqual(client.get('/api/v1/sessions/legacy/processing').status_code, 410)

    def test_worker_task_listing_requires_worker_credential_when_configured(self) -> None:
        from fastapi.testclient import TestClient

        previous_worker = os.environ.get('LIVENOTE_WORKER_TOKEN')
        os.environ['LIVENOTE_WORKER_TOKEN'] = 'worker-list-token'
        try:
            client = TestClient(main.app)
            self.assertEqual(client.get('/api/v1/tasks').status_code, 401)
            allowed = client.get('/api/v1/tasks', headers={'X-Worker-Token': 'worker-list-token'})
            self.assertEqual(allowed.status_code, 200, allowed.text)
        finally:
            if previous_worker is None:
                os.environ.pop('LIVENOTE_WORKER_TOKEN', None)
            else:
                os.environ['LIVENOTE_WORKER_TOKEN'] = previous_worker

    def test_browser_worker_can_claim_and_mark_task_local_ready(self) -> None:
        from fastapi.testclient import TestClient

        session_id = 'browser-worker-session'
        segment_id = 'browser-worker-segment'
        now = main.now_ms()
        main.create_session(main.SessionPayload(
            id=session_id, title='browser worker', startedAt=now, endedAt=now + 1000,
            status='COMPLETED', durationMs=1000, createdAt=now, updatedAt=now,
        ))
        main.create_segment(session_id, main.SegmentPayload(
            id=segment_id, sessionId=session_id, index=1, startedAt=now,
            endedAt=now + 1000, mimeType='audio/webm', status='COMPLETED', durationMs=1000,
        ))
        relative_path = Path('sessions') / session_id / 'segment_1' / 'chunks' / '000000.bin'
        audio_path = main.DATA_DIR / relative_path
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b'browser-worker-audio')
        with main.connect() as connection:
            connection.execute(
                'INSERT INTO chunks(session_id, segment_id, chunk_index, size, sha256, mime_type, elapsed_ms, created_at, received_at, local_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (session_id, segment_id, 0, audio_path.stat().st_size, 'browser-worker-hash', 'audio/webm', 1000, now, now, str(relative_path).replace('\\', '/')),
            )
        main.complete_segment(session_id, segment_id)
        main.complete_session(session_id)
        previous_worker = os.environ.get('LIVENOTE_WORKER_TOKEN')
        previous_admin = os.environ.get('LIVENOTE_ADMIN_TOKEN')
        os.environ['LIVENOTE_WORKER_TOKEN'] = 'browser-worker-token'
        os.environ['LIVENOTE_ADMIN_TOKEN'] = 'browser-admin-token'
        try:
            client = TestClient(main.app)
            headers = {'X-Worker-Token': 'browser-worker-token'}
            listed = client.get('/api/v1/tasks?status=READY', headers=headers)
            self.assertEqual(listed.status_code, 200)
            task = next(item for item in listed.json()['tasks'] if item['sessionId'] == session_id)
            claimed = client.post(
                f"/api/v1/tasks/{task['id']}/claim",
                headers=headers,
                json={'workerId': 'browser-pc'},
            )
            self.assertEqual(claimed.status_code, 200)
            lease = claimed.json()['leaseToken']
            local_ready = client.post(
                f"/api/v1/tasks/{task['id']}/status",
                headers={**headers, 'X-Worker-Id': 'browser-pc', 'X-Task-Lease': lease},
                json={'status': 'LOCAL_READY', 'workerId': 'browser-pc', 'leaseToken': lease},
            )
            self.assertEqual(local_ready.status_code, 200, local_ready.text)
            self.assertEqual(local_ready.json()['task']['status'], 'LOCAL_READY')
            started = client.post(
                f"/api/v1/admin/tasks/{task['id']}/start-processing",
                headers={'X-Admin-Token': 'browser-admin-token'},
                json={'workerId': 'browser-pc'},
            )
            self.assertEqual(started.status_code, 200, started.text)
            self.assertEqual(started.json()['task']['status'], 'PROCESSING')
            uploaded = client.post(
                f"/api/v1/admin/tasks/{task['id']}/result",
                headers={'X-Admin-Token': 'browser-admin-token'},
                json={'version': 1, 'result': {'title': '浏览器 Worker 闭环', 'overview': '云端任务可继续处理。'}},
            )
            self.assertEqual(uploaded.status_code, 200, uploaded.text)
            published = client.post(
                f"/api/v1/admin/tasks/{task['id']}/publish",
                headers={'X-Admin-Token': 'browser-admin-token'},
                json={'revisionId': uploaded.json()['revisionId']},
            )
            self.assertEqual(published.status_code, 200, published.text)
            visible = client.get(f'/api/v1/sessions/{session_id}/result')
            self.assertEqual(visible.status_code, 200)
            self.assertEqual(visible.json()['status'], 'COMPLETED')
            self.assertEqual(visible.json()['result']['title'], '浏览器 Worker 闭环')
        finally:
            if previous_worker is None:
                os.environ.pop('LIVENOTE_WORKER_TOKEN', None)
            else:
                os.environ['LIVENOTE_WORKER_TOKEN'] = previous_worker
            if previous_admin is None:
                os.environ.pop('LIVENOTE_ADMIN_TOKEN', None)
            else:
                os.environ['LIVENOTE_ADMIN_TOKEN'] = previous_admin

    def test_local_control_console_flow_writes_audio_and_publishes_result(self) -> None:
        from fastapi.testclient import TestClient

        session_id = 'local-control-e2e-session'
        segment_id = 'local-control-e2e-segment'
        now = main.now_ms()
        main.create_session(main.SessionPayload(
            id=session_id, title='本机控制台闭环', startedAt=now, endedAt=now + 1000,
            status='COMPLETED', durationMs=1000, createdAt=now, updatedAt=now,
        ))
        main.create_segment(session_id, main.SegmentPayload(
            id=segment_id, sessionId=session_id, index=1, startedAt=now,
            endedAt=now + 1000, mimeType='audio/webm', status='COMPLETED', durationMs=1000,
        ))
        with main.connect() as connection:
            connection.execute(
                'INSERT INTO chunks(session_id, segment_id, chunk_index, size, sha256, mime_type, elapsed_ms, created_at, received_at, local_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (session_id, segment_id, 0, 1, 'local-control-e2e-hash', 'audio/webm', 1000, now, now, 'sessions/local-control-e2e/segment_1/chunks/000000.bin'),
            )
        main.complete_segment(session_id, segment_id)
        main.complete_session(session_id)

        previous_admin = os.environ.get('LIVENOTE_ADMIN_TOKEN')
        previous_inbox = main.LOCAL_WORKER_INBOX
        main.LOCAL_PULL_ENABLED = True
        main.LOCAL_WORKER_INBOX = _ROOT / 'worker-inbox-e2e'
        main.LOCAL_WORKER_INBOX.mkdir(parents=True, exist_ok=True)
        source_path = _ROOT / 'reconstructed-e2e.webm'
        source_path.write_bytes(b'reconstructed-e2e-audio')
        os.environ['LIVENOTE_ADMIN_TOKEN'] = 'local-control-e2e-token'
        try:
            client = TestClient(main.app)
            headers = {'X-Admin-Token': 'local-control-e2e-token'}
            task = next(
                item for item in client.get('/api/v1/admin/tasks?status=READY', headers=headers).json()['items']
                if item['sessionId'] == session_id
            )
            with patch.object(main, '_reconstruct_completed_session', return_value=source_path):
                pulled = client.post(
                    f"/api/v1/admin/tasks/{task['id']}/pull-local",
                    headers=headers,
                    json={'workerId': 'local-pc'},
                )
            self.assertEqual(pulled.status_code, 200, pulled.text)
            self.assertEqual(pulled.json()['task']['status'], 'LOCAL_READY')
            task_dir = main.LOCAL_WORKER_INBOX / task['id']
            self.assertEqual((task_dir / 'audio.webm').read_bytes(), b'reconstructed-e2e-audio')
            self.assertTrue((task_dir / 'manifest.json').is_file())

            started = client.post(
                f"/api/v1/admin/tasks/{task['id']}/start-processing",
                headers=headers,
                json={'workerId': 'local-pc'},
            )
            self.assertEqual(started.status_code, 200, started.text)
            uploaded = client.post(
                f"/api/v1/admin/tasks/{task['id']}/result",
                headers=headers,
                json={'version': 1, 'result': {'title': '闭环结果', 'overview': '手机可以读取。', 'keyPoints': ['本机流程完成']}},
            )
            self.assertEqual(uploaded.status_code, 200, uploaded.text)
            published = client.post(
                f"/api/v1/admin/tasks/{task['id']}/publish",
                headers=headers,
                json={'revisionId': uploaded.json()['revisionId']},
            )
            self.assertEqual(published.status_code, 200, published.text)
            result = client.get(f'/api/v1/sessions/{session_id}/result')
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()['status'], 'COMPLETED')
            self.assertEqual(result.json()['result']['title'], '闭环结果')
        finally:
            main.LOCAL_WORKER_INBOX = previous_inbox
            if previous_admin is None:
                os.environ.pop('LIVENOTE_ADMIN_TOKEN', None)
            else:
                os.environ['LIVENOTE_ADMIN_TOKEN'] = previous_admin

    def test_local_control_console_reads_knowledge_json_from_task_directory(self) -> None:
        from fastapi.testclient import TestClient

        session_id = 'local-result-file-session'
        task_id = 'local-result-file-task'
        now = main.now_ms()
        main.create_session(main.SessionPayload(
            id=session_id,
            title='本地结果文件',
            startedAt=now,
            endedAt=now,
            status='COMPLETED',
            durationMs=0,
            createdAt=now,
            updatedAt=now,
        ))
        with main.connect() as connection:
            connection.execute(
                '''INSERT INTO processing_tasks(
                       id, session_id, status, attempts, claimed_by,
                       created_at, updated_at
                   ) VALUES (?, ?, 'PROCESSING', 1, ?, ?, ?)''',
                (task_id, session_id, 'local-pc', now, now),
            )

        previous_admin = os.environ.get('LIVENOTE_ADMIN_TOKEN')
        previous_inbox = main.LOCAL_WORKER_INBOX
        main.LOCAL_PULL_ENABLED = True
        main.LOCAL_WORKER_INBOX = _ROOT / 'local-result-file-inbox'
        result_path = main.LOCAL_WORKER_INBOX / task_id / 'knowledge.json'
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps({
            'version': 1,
            'result': {
                'title': '从任务目录读取',
                'overview': '无需打开文件选择器。',
                'keyPoints': ['本地结果自动回传'],
            },
        }, ensure_ascii=False), encoding='utf-8')
        os.environ['LIVENOTE_ADMIN_TOKEN'] = 'local-result-file-token'
        try:
            client = TestClient(main.app)
            response = client.post(
                f'/api/v1/admin/tasks/{task_id}/result-local',
                headers={'X-Admin-Token': 'local-result-file-token'},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['status'], 'REVIEW')
            with main.connect() as connection:
                task = connection.execute('SELECT status FROM processing_tasks WHERE id = ?', (task_id,)).fetchone()
            self.assertEqual(task['status'], 'REVIEW')
        finally:
            main.LOCAL_WORKER_INBOX = previous_inbox
            if previous_admin is None:
                os.environ.pop('LIVENOTE_ADMIN_TOKEN', None)
            else:
                os.environ['LIVENOTE_ADMIN_TOKEN'] = previous_admin

    def test_concurrent_same_chunk_upload_is_idempotent(self) -> None:
        from fastapi.testclient import TestClient

        main.DB_PATH = _ROOT / 'livenote.sqlite3'
        main.DATA_DIR = _ROOT / 'data'
        main.init_db()
        session_id = 'concurrent-upload-session'
        segment_id = 'concurrent-upload-segment'
        now = main.now_ms()
        main.create_session(main.SessionPayload(
            id=session_id,
            title='concurrent upload',
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
            mimeType='audio/webm',
            status='RECORDING',
        ))
        body = b'concurrent-audio'
        headers = {
            'content-type': 'audio/webm',
            'x-chunk-sha256': hashlib.sha256(body).hexdigest(),
            'x-chunk-size': str(len(body)),
            'x-chunk-elapsed-ms': '1000',
        }

        def upload():
            with TestClient(main.app) as client:
                return client.put(
                    f'/api/v1/sessions/{session_id}/segments/{segment_id}/chunks/0',
                    content=body,
                    headers=headers,
                )

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda _index: upload(), (1, 2)))

        self.assertEqual([response.status_code for response in responses], [200, 200])
        self.assertEqual(sum(response.json()['already_exists'] for response in responses), 1)
        with main.connect() as connection:
            count = connection.execute('SELECT COUNT(*) FROM chunks WHERE segment_id = ?', (segment_id,)).fetchone()[0]
        self.assertEqual(count, 1)

    def test_concurrent_task_claim_only_allows_one_worker(self) -> None:
        from fastapi.testclient import TestClient

        session_id = 'concurrent-claim-session'
        task_id = 'concurrent-claim-task'
        now = main.now_ms()
        main.create_session(main.SessionPayload(
            id=session_id,
            title='concurrent claim',
            startedAt=now,
            endedAt=now,
            status='COMPLETED',
            durationMs=0,
            createdAt=now,
            updatedAt=now,
        ))
        with main.connect() as connection:
            connection.execute('UPDATE sessions SET status = ? WHERE id = ?', ('COMPLETED', session_id))
            connection.execute(
                'INSERT INTO processing_tasks(id, session_id, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)',
                (task_id, session_id, 'READY', now, now),
            )

        def claim(worker_id: str):
            with TestClient(main.app) as client:
                return client.post(f'/api/v1/tasks/{task_id}/claim', json={'workerId': worker_id})

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(claim, ('worker-a', 'worker-b')))

        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
        with main.connect() as connection:
            task = connection.execute(
                'SELECT status, claimed_by, lease_token_hash FROM processing_tasks WHERE id = ?',
                (task_id,),
            ).fetchone()
        self.assertEqual(task['status'], 'CLAIMED')
        self.assertIn(task['claimed_by'], {'worker-a', 'worker-b'})
        self.assertTrue(task['lease_token_hash'])

    def test_expired_task_lease_can_be_reclaimed_and_old_lease_is_rejected(self) -> None:
        from fastapi.testclient import TestClient

        session_id = 'expired-lease-session'
        task_id = 'expired-lease-task'
        old_worker = 'worker-before-restart'
        old_lease = 'old-lease-token'
        now = main.now_ms()
        main.create_session(main.SessionPayload(
            id=session_id,
            title='expired lease',
            startedAt=now,
            endedAt=now,
            status='COMPLETED',
            durationMs=0,
            createdAt=now,
            updatedAt=now,
        ))
        with main.connect() as connection:
            connection.execute(
                '''INSERT INTO processing_tasks(
                       id, session_id, status, attempts, claimed_by, claimed_at,
                       lease_token_hash, lease_expires_at, created_at, updated_at
                   ) VALUES (?, ?, 'CLAIMED', 1, ?, ?, ?, ?, ?, ?)''',
                (task_id, session_id, old_worker, now - 10_000, main.hash_secret(old_lease), now - 1, now - 10_000, now - 10_000),
            )

        client = TestClient(main.app)
        reclaimed = client.post(f'/api/v1/tasks/{task_id}/claim', json={'workerId': 'worker-after-restart'})
        self.assertEqual(reclaimed.status_code, 200, reclaimed.text)
        new_lease = reclaimed.json()['leaseToken']
        self.assertNotEqual(new_lease, old_lease)

        stale_update = client.post(
            f'/api/v1/tasks/{task_id}/status',
            json={'status': 'LOCAL_READY', 'workerId': old_worker, 'leaseToken': old_lease},
        )
        self.assertEqual(stale_update.status_code, 409)

        current_update = client.post(
            f'/api/v1/tasks/{task_id}/status',
            json={'status': 'LOCAL_READY', 'workerId': 'worker-after-restart', 'leaseToken': new_lease},
        )
        self.assertEqual(current_update.status_code, 200, current_update.text)

    def test_api_key_protects_api_routes_when_configured(self) -> None:
        from fastapi.testclient import TestClient

        previous_key = main.API_KEY
        main.API_KEY = 'test-api-key'
        try:
            client = TestClient(main.app)
            self.assertEqual(client.get('/api/v1/health').status_code, 401)
            authorized = client.get('/api/v1/health', headers={'X-API-Key': 'test-api-key'})
            self.assertEqual(authorized.status_code, 200)
            self.assertEqual(authorized.json()['storageSchema'], 9)
        finally:
            main.API_KEY = previous_key

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

    def test_admin_task_list_does_not_start_legacy_processing_as_a_read_side_effect(self) -> None:
        from fastapi.testclient import TestClient

        session_id = 'orphaned-processing-session'
        task_id = 'orphaned-processing-task'
        now = main.now_ms()
        main.create_session(main.SessionPayload(
            id=session_id,
            title='孤儿处理中任务',
            startedAt=now,
            endedAt=now,
            status='COMPLETED',
            durationMs=0,
            createdAt=now,
            updatedAt=now,
        ))
        with main.connect() as connection:
            connection.execute(
                '''INSERT INTO processing_tasks(
                       id, session_id, status, attempts, claimed_by,
                       error_stage, error_message, created_at, updated_at
                   ) VALUES (?, ?, 'PROCESSING', 1, ?, 'MANUAL', '', ?, ?)''',
                (task_id, session_id, 'local-pc', now, now),
            )

        previous_admin = os.environ.get('LIVENOTE_ADMIN_TOKEN')
        os.environ['LIVENOTE_ADMIN_TOKEN'] = 'orphaned-processing-admin-token'
        try:
            client = TestClient(main.app)
            with patch.object(main, 'create_job', return_value={'id': 'job-recovered'}) as create_job:
                response = client.get(
                    '/api/v1/admin/tasks?status=ALL',
                    headers={'X-Admin-Token': 'orphaned-processing-admin-token'},
                )
            self.assertEqual(response.status_code, 200, response.text)
            create_job.assert_not_called()
            listed = {item['id']: item for item in response.json()['items']}
            self.assertEqual(listed[task_id]['status'], 'PROCESSING')
        finally:
            if previous_admin is None:
                os.environ.pop('LIVENOTE_ADMIN_TOKEN', None)
            else:
                os.environ['LIVENOTE_ADMIN_TOKEN'] = previous_admin

    def test_admin_auto_process_waits_for_live_windows_then_resumes_automatically(self) -> None:
        from fastapi.testclient import TestClient
        from server import jobs

        session_id = 'live-drain-admin-session'
        segment_id = 'live-drain-admin-segment'
        task_id = 'live-drain-admin-task'
        now = main.now_ms()
        main.create_session(main.SessionPayload(
            id=session_id,
            title='实时收尾后处理',
            startedAt=now,
            endedAt=None,
            status='COMPLETED',
            durationMs=4000,
            createdAt=now,
            updatedAt=now,
        ))
        with main.connect() as connection:
            connection.execute("UPDATE sessions SET status = 'COMPLETED', ended_at = ? WHERE id = ?", (now, session_id))
        main.create_segment(session_id, main.SegmentPayload(
            id=segment_id,
            sessionId=session_id,
            index=1,
            startedAt=now,
            endedAt=now + 4000,
            mimeType='audio/webm',
            status='COMPLETED',
            durationMs=4000,
        ))
        with main.connect() as connection:
            for chunk_index in range(4):
                connection.execute(
                    '''INSERT INTO chunks(
                           session_id, segment_id, chunk_index, size, sha256,
                           mime_type, elapsed_ms, created_at, received_at, local_path
                       ) VALUES (?, ?, ?, 1, ?, 'audio/webm', ?, ?, ?, ?)''',
                    (session_id, segment_id, chunk_index, f'hash-{chunk_index}', (chunk_index + 1) * 1000, now, now, f'uploads/{session_id}/{chunk_index}.webm'),
                )
            connection.execute(
                '''INSERT INTO processing_tasks(
                       id, session_id, status, attempts, claimed_by,
                       error_stage, error_message, created_at, updated_at
                   ) VALUES (?, ?, 'LOCAL_READY', 1, ?, '', '', ?, ?)''',
                (task_id, session_id, 'local-pc', now, now),
            )
            main.ensure_live_run(connection, session_id)

        with main.connect() as connection:
            run = connection.execute('SELECT id, status FROM live_processing_runs WHERE session_id = ?', (session_id,)).fetchone()
            segment = connection.execute('SELECT id, segment_index FROM segments WHERE session_id = ?', (session_id,)).fetchone()
            chunk_count = connection.execute('SELECT COUNT(*) AS count FROM chunks WHERE session_id = ?', (session_id,)).fetchone()['count']
            session = connection.execute('SELECT status FROM sessions WHERE id = ?', (session_id,)).fetchone()
            self.assertTrue(main.live_processing_has_pending_windows(connection, session_id), f'run={dict(run) if run else None}, segment={dict(segment) if segment else None}, session={dict(session) if session else None}, chunks={chunk_count}')

        previous_admin = os.environ.get('LIVENOTE_ADMIN_TOKEN')
        os.environ['LIVENOTE_ADMIN_TOKEN'] = 'live-drain-admin-token'
        try:
            client = TestClient(main.app)
            with patch.object(main, 'create_job') as create_job, patch.object(main.LIVE_PROCESSING_WORKER, 'wake') as wake:
                response = client.post(
                    f'/api/v1/admin/tasks/{task_id}/auto-process',
                    headers={'X-Admin-Token': 'live-drain-admin-token'},
                    json={'workerId': 'local-pc'},
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()['deferred'])
            create_job.assert_not_called()
            wake.assert_called_once_with(session_id)
            self.assertEqual(response.json()['task']['status'], 'PROCESSING')
            self.assertEqual(response.json()['task']['errorStage'], 'LIVE_DRAIN')

            with main.connect() as connection:
                connection.execute(
                    "UPDATE live_processing_runs SET status = 'WAITING_FOR_CHUNKS' WHERE session_id = ?",
                    (session_id,),
                )
            with patch.object(jobs, 'find_active_job', return_value=None), patch.object(jobs, 'create_job', return_value={'id': 'job-live-drain'} ) as create_job:
                resumed = main.LIVE_PROCESSING_WORKER._resume_deferred_final_jobs()
            self.assertEqual(resumed, 1)
            create_job.assert_called_once()
            with main.connect() as connection:
                task = connection.execute('SELECT status, error_stage FROM processing_tasks WHERE id = ?', (task_id,)).fetchone()
            self.assertEqual((task['status'], task['error_stage']), ('PROCESSING', 'AUTO_PROCESS'))
        finally:
            if previous_admin is None:
                os.environ.pop('LIVENOTE_ADMIN_TOKEN', None)
            else:
                os.environ['LIVENOTE_ADMIN_TOKEN'] = previous_admin

    def test_duplicate_processing_requests_share_one_active_job(self) -> None:
        from server import jobs

        data_dir = _ROOT / 'duplicate-job-data'
        db_path = _ROOT / 'duplicate-job.sqlite3'
        with patch.object(jobs._executor, 'submit') as submit:
            first = jobs.create_job(data_dir, db_path, 'duplicate-job-session', 'medium', 'zh')
            second = jobs.create_job(data_dir, db_path, 'duplicate-job-session', 'medium', 'zh')

        self.assertEqual(first['id'], second['id'])
        self.assertEqual(submit.call_count, 1)

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

    def test_complete_session_recovers_closed_segment_left_recording(self) -> None:
        session_id = 'closed-recording-segment-session'
        first_segment_id = 'closed-recording-segment-one'
        second_segment_id = 'closed-recording-segment-two'
        now = main.now_ms()
        main.create_session(main.SessionPayload(
            id=session_id,
            title='closed recording segment',
            startedAt=now,
            status='RECORDING',
            durationMs=2_000,
            createdAt=now,
            updatedAt=now,
        ))
        main.create_segment(session_id, main.SegmentPayload(
            id=first_segment_id,
            sessionId=session_id,
            index=1,
            startedAt=now,
            endedAt=now + 1_000,
            mimeType='audio/webm',
            status='RECORDING',
            durationMs=1_000,
        ))
        main.create_segment(session_id, main.SegmentPayload(
            id=second_segment_id,
            sessionId=session_id,
            index=2,
            startedAt=now + 1_000,
            endedAt=now + 2_000,
            mimeType='audio/webm',
            status='COMPLETED',
            durationMs=1_000,
        ))
        with main.connect() as connection:
            for segment_id, elapsed_ms in ((first_segment_id, 1_000), (second_segment_id, 2_000)):
                connection.execute(
                    'INSERT INTO chunks(session_id, segment_id, chunk_index, size, sha256, mime_type, elapsed_ms, created_at, received_at, local_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (session_id, segment_id, 0, 1, f'hash-{segment_id}', 'audio/webm', elapsed_ms, now, now, f'sessions/{session_id}/{segment_id}/000000.bin'),
                )
            connection.execute('UPDATE segments SET status = ? WHERE id = ?', ('COMPLETED', second_segment_id))

        main.complete_session(session_id)

        with main.connect() as connection:
            statuses = connection.execute(
                'SELECT segment_index, status FROM segments WHERE session_id = ? ORDER BY segment_index',
                (session_id,),
            ).fetchall()
            session = connection.execute('SELECT status FROM sessions WHERE id = ?', (session_id,)).fetchone()
            task = connection.execute('SELECT status FROM processing_tasks WHERE session_id = ?', (session_id,)).fetchone()
        self.assertEqual([(row['segment_index'], row['status']) for row in statuses], [(1, 'COMPLETED'), (2, 'COMPLETED')])
        self.assertEqual(session['status'], 'COMPLETED')
        self.assertEqual(task['status'], 'READY')

    def test_startup_recovery_creates_task_for_uploaded_closed_session(self) -> None:
        session_id = 'startup-recovery-session'
        segment_id = 'startup-recovery-segment'
        now = main.now_ms()
        main.create_session(main.SessionPayload(
            id=session_id,
            title='startup recovery',
            startedAt=now,
            endedAt=now + 1_000,
            status='RECORDING',
            durationMs=1_000,
            createdAt=now,
            updatedAt=now + 1_000,
        ))
        main.create_segment(session_id, main.SegmentPayload(
            id=segment_id,
            sessionId=session_id,
            index=1,
            startedAt=now,
            endedAt=now + 1_000,
            mimeType='audio/webm',
            status='RECORDING',
            durationMs=1_000,
        ))
        with main.connect() as connection:
            connection.execute(
                'INSERT INTO chunks(session_id, segment_id, chunk_index, size, sha256, mime_type, elapsed_ms, created_at, received_at, local_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (session_id, segment_id, 0, 1, 'hash-startup-recovery', 'audio/webm', 1_000, now, now, f'sessions/{session_id}/segment_1/000000.bin'),
            )

        self.assertEqual(main.recover_closed_sessions(), 1)

        with main.connect() as connection:
            session = connection.execute('SELECT status FROM sessions WHERE id = ?', (session_id,)).fetchone()
            segment = connection.execute('SELECT status FROM segments WHERE id = ?', (segment_id,)).fetchone()
            task = connection.execute('SELECT status FROM processing_tasks WHERE session_id = ?', (session_id,)).fetchone()
        self.assertEqual(session['status'], 'COMPLETED')
        self.assertEqual(segment['status'], 'COMPLETED')
        self.assertEqual(task['status'], 'READY')

    def test_admin_task_list_reports_transcription_progress(self) -> None:
        from fastapi.testclient import TestClient

        session_id = 'progress-session'
        task_id = 'progress-task'
        run_id = 'progress-run'
        now = main.now_ms()
        main.create_session(main.SessionPayload(
            id=session_id,
            title='progress session',
            startedAt=now,
            endedAt=now + 600_000,
            status='COMPLETED',
            durationMs=600_000,
            createdAt=now,
            updatedAt=now + 600_000,
        ))
        with main.connect() as connection:
            connection.execute(
                'INSERT INTO processing_tasks(id, session_id, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)',
                (task_id, session_id, 'TRANSCRIBING', now, now + 1),
            )
            connection.execute(
                'INSERT INTO processing_runs(id, task_id, session_id, generation, source_hash, model, language, status, started_at, heartbeat_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (run_id, task_id, session_id, 1, 'source', 'medium', 'auto', 'RUNNING', now, now + 1),
            )
            connection.execute(
                'INSERT INTO processing_parts(id, run_id, task_id, part_index, start_ms, end_ms, source_hash, model, language, status, started_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                ('progress-part', run_id, task_id, 0, 0, 300_000, 'source', 'medium', 'auto', 'COMPLETED', now, now + 1),
            )
        previous_admin = os.environ.get('LIVENOTE_ADMIN_TOKEN')
        os.environ['LIVENOTE_ADMIN_TOKEN'] = 'progress-admin-token'
        try:
            response = TestClient(main.app).get('/api/v1/admin/tasks', headers={'X-Admin-Token': 'progress-admin-token'}, params={'query': ''})
            self.assertEqual(response.status_code, 200, response.text)
            item = next(item for item in response.json()['items'] if item['id'] == task_id)
            self.assertEqual(item['progress']['completedParts'], 1)
            self.assertEqual(item['progress']['totalParts'], 3)
            self.assertEqual(item['progress']['percent'], 33)
        finally:
            if previous_admin is None:
                os.environ.pop('LIVENOTE_ADMIN_TOKEN', None)
            else:
                os.environ['LIVENOTE_ADMIN_TOKEN'] = previous_admin

    def test_admin_task_transcript_is_read_only_and_requires_completed_transcription(self) -> None:
        from fastapi.testclient import TestClient

        session_id = 'transcript-view-session'
        task_id = 'transcript-view-task'
        run_id = 'transcript-view-run'
        now = main.now_ms()
        main.create_session(main.SessionPayload(
            id=session_id,
            title='transcript view session',
            startedAt=now,
            endedAt=now + 60_000,
            status='COMPLETED',
            durationMs=60_000,
            createdAt=now,
            updatedAt=now + 60_000,
        ))
        with main.connect() as connection:
            connection.execute(
                'INSERT INTO processing_tasks(id, session_id, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)',
                (task_id, session_id, 'TRANSCRIBED', now, now + 1),
            )
            connection.execute(
                'INSERT INTO processing_runs(id, task_id, session_id, generation, source_hash, model, language, status, started_at, heartbeat_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (run_id, task_id, session_id, 1, 'source', 'medium', 'zh', 'COMPLETED', now, now + 1, now + 2),
            )
        transcript_path = main.DATA_DIR / 'processed' / 'sessions' / session_id / 'transcript.json'
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_path.write_text(json.dumps({
            'sessionId': session_id,
            'model': 'medium',
            'device': 'cpu',
            'language': 'zh',
            'text': '这是识别文字。',
            'segments': [{'index': 0, 'startMs': 0, 'endMs': 2500, 'text': '这是识别文字。'}],
        }, ensure_ascii=False), encoding='utf-8')
        previous_admin = os.environ.get('LIVENOTE_ADMIN_TOKEN')
        os.environ['LIVENOTE_ADMIN_TOKEN'] = 'transcript-view-admin-token'
        try:
            response = TestClient(main.app).get(
                f'/api/v1/admin/tasks/{task_id}/transcript',
                headers={'X-Admin-Token': 'transcript-view-admin-token'},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['transcript']['text'], '这是识别文字。')
            self.assertEqual(response.json()['transcript']['segments'][0]['startMs'], 0)
            with main.connect() as connection:
                self.assertEqual(connection.execute('SELECT status FROM processing_tasks WHERE id = ?', (task_id,)).fetchone()['status'], 'TRANSCRIBED')
        finally:
            if previous_admin is None:
                os.environ.pop('LIVENOTE_ADMIN_TOKEN', None)
            else:
                os.environ['LIVENOTE_ADMIN_TOKEN'] = previous_admin

    def test_segment_index_cannot_be_reused_with_another_segment_id(self) -> None:
        session_id = 'duplicate-segment-index-session'
        now = main.now_ms()
        main.create_session(main.SessionPayload(
            id=session_id,
            title='duplicate segment index',
            startedAt=now,
            status='RECORDING',
            createdAt=now,
            updatedAt=now,
        ))
        main.create_segment(session_id, main.SegmentPayload(
            id='duplicate-segment-one',
            sessionId=session_id,
            index=1,
            startedAt=now,
            status='RECORDING',
        ))
        with self.assertRaises(main.HTTPException) as context:
            main.create_segment(session_id, main.SegmentPayload(
                id='duplicate-segment-two',
                sessionId=session_id,
                index=1,
                startedAt=now,
                status='RECORDING',
            ))
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

    def test_incremental_final_transcript_summary_publish_and_phone_result_flow(self) -> None:
        from fastapi.testclient import TestClient
        from server import processing_pipeline

        session_id = 'incremental-final-chain-session'
        segment_id = 'incremental-final-chain-segment'
        now = main.now_ms()
        main.create_session(main.SessionPayload(
            id=session_id,
            title='增量最终链路',
            startedAt=now,
            endedAt=None,
            status='RECORDING',
            durationMs=1000,
            createdAt=now,
            updatedAt=now,
        ))
        main.create_segment(session_id, main.SegmentPayload(
            id=segment_id,
            sessionId=session_id,
            index=1,
            startedAt=now,
            endedAt=None,
            mimeType='audio/webm',
            status='RECORDING',
            durationMs=1000,
        ))
        relative_path = Path('sessions') / session_id / 'segment_1' / 'chunks' / '000000.bin'
        audio_path = main.DATA_DIR / relative_path
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        body = b'incremental-final-chain-audio'
        audio_path.write_bytes(body)
        with main.connect() as connection:
            connection.execute(
                '''INSERT INTO chunks(session_id, segment_id, chunk_index, size, sha256, mime_type,
                                      elapsed_ms, created_at, received_at, local_path)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (session_id, segment_id, 0, len(body), hashlib.sha256(body).hexdigest(), 'audio/webm', 1000, now, now, str(relative_path).replace('\\', '/')),
            )
        main.complete_segment(session_id, segment_id)
        main.complete_session(session_id)

        with main.connect() as connection:
            task = connection.execute('SELECT id FROM processing_tasks WHERE session_id = ?', (session_id,)).fetchone()
            self.assertIsNotNone(task)
            task_id = task['id']
            connection.execute(
                '''UPDATE processing_tasks
                   SET status = 'TRANSCRIBING', claimed_by = ?, attempts = attempts + 1, updated_at = ?
                   WHERE id = ?''',
                ('incremental-final-worker', now, task_id),
            )

        fake_audio = _ROOT / 'reconstructed' / 'incremental-final-chain.webm'
        fake_audio.parent.mkdir(parents=True, exist_ok=True)
        fake_audio.write_bytes(b'valid-audio-is-covered-by-reconstruction-tests')
        try:
            with patch.object(processing_pipeline, '_rebuild_audio', return_value=fake_audio), \
                 patch.object(processing_pipeline, '_probe_duration_seconds', return_value=1.0), \
                 patch.object(processing_pipeline, '_validate_audio_input', return_value={'durationSeconds': 1.0, 'format': 'webm'}), \
                 patch.object(processing_pipeline, 'transcribe_range', return_value={
                     'model': 'tiny',
                     'device': 'cpu',
                     'language': 'zh',
                     'segments': [{'startMs': 0, 'endMs': 900, 'text': '最终链路逐字稿'}],
                 }):
                processed = processing_pipeline.process_task(
                    main.DATA_DIR,
                    main.DB_PATH,
                    task_id,
                    'tiny',
                    'zh',
                    'incremental-final-worker',
                )
            self.assertEqual(processed['status'], 'TRANSCRIBED')

            previous_admin = os.environ.get('LIVENOTE_ADMIN_TOKEN')
            os.environ['LIVENOTE_ADMIN_TOKEN'] = 'incremental-final-chain-admin'
            try:
                client = TestClient(main.app)
                admin_headers = {'X-Admin-Token': 'incremental-final-chain-admin'}
                transcript_response = client.get(f'/api/v1/admin/tasks/{task_id}/transcript', headers=admin_headers)
                self.assertEqual(transcript_response.status_code, 200, transcript_response.text)
                self.assertEqual(transcript_response.json()['transcript']['text'], '最终链路逐字稿')

                summary_input = client.get(f'/api/v1/tasks/{task_id}/summary-input')
                self.assertEqual(summary_input.status_code, 200, summary_input.text)
                summary_data = summary_input.json()
                summary = {
                    'title': '最终链路总结',
                    'overview': '录音内容已完成整理。',
                    'keyPoints': ['最终链路逐字稿已生成'],
                    'knowledgeStructure': [],
                    'questions': [],
                    'actionItems': ['按审核结果发布'],
                    'confidenceNotes': [],
                }
                submitted = client.post(
                    f'/api/v1/tasks/{task_id}/summary-result',
                    json={
                        'runId': summary_data['run']['id'],
                        'generation': summary_data['run']['generation'],
                        'sourceHash': summary_data['run']['source_hash'],
                        'result': summary,
                    },
                )
                self.assertEqual(submitted.status_code, 200, submitted.text)
                self.assertEqual(submitted.json()['status'], 'REVIEW')

                published = client.post(
                    f'/api/v1/admin/tasks/{task_id}/publish',
                    headers=admin_headers,
                    json={'revisionId': submitted.json()['revisionId']},
                )
                self.assertEqual(published.status_code, 200, published.text)
                phone_result = client.get(f'/api/v1/sessions/{session_id}/result')
                self.assertEqual(phone_result.status_code, 200, phone_result.text)
                self.assertEqual(phone_result.json()['status'], 'COMPLETED')
                self.assertEqual(phone_result.json()['result']['title'], '最终链路总结')
            finally:
                if previous_admin is None:
                    os.environ.pop('LIVENOTE_ADMIN_TOKEN', None)
                else:
                    os.environ['LIVENOTE_ADMIN_TOKEN'] = previous_admin
        finally:
            fake_audio.unlink(missing_ok=True)


    def test_incremental_processing_state_and_retry_endpoints(self) -> None:
        from fastapi.testclient import TestClient

        client = TestClient(main.app)
        now = main.now_ms()
        session_id = 'live-api-state-check'
        segment_id = 'live-api-state-segment'
        self.assertEqual(client.post('/api/v1/sessions', json={
            'id': session_id, 'title': 'live state check', 'startedAt': now,
            'endedAt': None, 'status': 'RECORDING', 'durationMs': 30000,
            'createdAt': now, 'updatedAt': now,
        }).status_code, 200)
        self.assertEqual(client.post(f'/api/v1/sessions/{session_id}/segments', json={
            'id': segment_id, 'sessionId': session_id, 'index': 1,
            'startedAt': now, 'startElapsedMs': 0, 'endedAt': None,
            'mimeType': 'audio/webm', 'mediaSettings': {}, 'status': 'RECORDING', 'durationMs': 30000,
        }).status_code, 200)
        body = b'live-state-check'
        uploaded = client.put(
            f'/api/v1/sessions/{session_id}/segments/{segment_id}/chunks/0',
            content=body,
            headers={
                'content-type': 'audio/webm',
                'x-chunk-sha256': hashlib.sha256(body).hexdigest(),
                'x-chunk-size': str(len(body)),
                'x-chunk-elapsed-ms': '30000',
            },
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        status = client.get(f'/api/v1/sessions/{session_id}/live-processing')
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()['liveProcessing']['uploadedChunks'], 1)
        retry = client.post(f'/api/v1/sessions/{session_id}/live-processing/retry')
        self.assertEqual(retry.status_code, 200, retry.text)


if __name__ == '__main__':
    unittest.main()
