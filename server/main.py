from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ValidationError

try:
    from reconstruction import FFMPEG as RECONSTRUCTION_FFMPEG, FFPROBE as RECONSTRUCTION_FFPROBE, ReconstructionError, reconstruct_segment, reconstruct_session
except ImportError:
    from .reconstruction import FFMPEG as RECONSTRUCTION_FFMPEG, FFPROBE as RECONSTRUCTION_FFPROBE, ReconstructionError, reconstruct_segment, reconstruct_session
try:
    from asr import ASR_CHUNK_OVERLAP_SECONDS, ASR_CHUNK_SECONDS, DEFAULT_MODEL, AsrError, is_model_cached, save_transcript, transcribe
except ImportError:
    from .asr import ASR_CHUNK_OVERLAP_SECONDS, ASR_CHUNK_SECONDS, DEFAULT_MODEL, AsrError, is_model_cached, save_transcript, transcribe
try:
    from llm import is_configured as is_llm_configured
except ImportError:
    from .llm import is_configured as is_llm_configured
try:
    from content import ContentError, build_structured_report, load_transcript, save_report
except ImportError:
    from .content import ContentError, build_structured_report, load_transcript, save_report
try:
    from jobs import JobError, create_job, find_active_job, find_latest_job, get_job, recover_jobs
except ImportError:
    from .jobs import JobError, create_job, find_active_job, find_latest_job, get_job, recover_jobs
try:
    from retention import RetentionError, delete_session_data
except ImportError:
    from .retention import RetentionError, delete_session_data
try:
    from live_processing import LiveProcessingWorker, ensure_live_run, live_processing_has_pending_windows, live_processing_status, retry_live_run
except ImportError:
    from .live_processing import LiveProcessingWorker, ensure_live_run, live_processing_has_pending_windows, live_processing_status, retry_live_run
try:
    from auth import admin_auth_configured, admin_setup_available, authenticate_device, bearer_token, create_admin_credentials, create_admin_session, generate_device_token, generate_pairing_code, has_valid_worker_token, hash_secret, new_id, require_admin, require_worker
    from migrations import backup_before_migration, ensure_schema
except ImportError:
    from .auth import admin_auth_configured, admin_setup_available, authenticate_device, bearer_token, create_admin_credentials, create_admin_session, generate_device_token, generate_pairing_code, has_valid_worker_token, hash_secret, new_id, require_admin, require_worker
    from .migrations import backup_before_migration, ensure_schema

SERVER_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('LIVENOTE_DATA_DIR', SERVER_DIR / 'data'))
DB_PATH = Path(os.environ.get('LIVENOTE_DB_PATH', SERVER_DIR / 'livenote.sqlite3'))
MAX_CHUNK_BYTES = int(os.environ.get('LIVENOTE_MAX_CHUNK_BYTES', str(25 * 1024 * 1024)))
MAX_DIAGNOSTIC_BYTES = int(os.environ.get('LIVENOTE_MAX_DIAGNOSTIC_BYTES', str(10 * 1024 * 1024)))
MAX_AUDIO_ARTIFACT_BYTES = int(os.environ.get('LIVENOTE_MAX_AUDIO_ARTIFACT_BYTES', str(512 * 1024 * 1024)))
UPLOAD_DURATION_TOLERANCE_MS = int(os.environ.get('LIVENOTE_UPLOAD_DURATION_TOLERANCE_MS', '15000'))
LEASE_DURATION_MS = int(os.environ.get('LIVENOTE_TASK_LEASE_MS', str(30 * 60 * 1000)))
RUNTIME_ENV = os.environ.get('LIVENOTE_ENV', 'development').strip().lower()
API_KEY = os.environ.get('LIVENOTE_API_KEY', '')
CORS_ORIGINS = [origin.strip() for origin in os.environ.get('LIVENOTE_CORS_ORIGINS', 'http://localhost:5173,https://localhost:5173').split(',') if origin.strip()]
_default_instance_id = 'ecs' if os.environ.get('LIVENOTE_PROCESSING_MODE', 'local').strip().lower() == 'storage' else 'local'
INSTANCE_ID = os.environ.get('LIVENOTE_INSTANCE_ID', '').strip() or _default_instance_id
INSTANCE_LABEL = os.environ.get('LIVENOTE_INSTANCE_LABEL', '').strip() or ('ECS 云端' if INSTANCE_ID == 'ecs' else '本地 Server')
IDENTIFIER_PATTERN = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
TASK_STATUSES = {'READY', 'CLAIMED', 'LOCAL_READY', 'TRANSCRIBING', 'TRANSCRIBED', 'SUMMARIZING', 'PROCESSING', 'REVIEW', 'READY_TO_UPLOAD', 'COMPLETED', 'FAILED'}
ACTIVE_SESSION_STATUSES = ('RECORDING', 'PAUSED', 'FINALIZING')
LOCAL_PULL_ENABLED = os.environ.get('LIVENOTE_LOCAL_PULL_ENABLED', '1' if RUNTIME_ENV not in {'production', 'prod'} else '0').strip().lower() in {'1', 'true', 'yes', 'on'}
PROCESSING_MODE = os.environ.get('LIVENOTE_PROCESSING_MODE', 'local').strip().lower()
if PROCESSING_MODE not in {'local', 'storage'}:
    raise RuntimeError('LIVENOTE_PROCESSING_MODE 只能是 local 或 storage。')
STORAGE_ONLY_MODE = PROCESSING_MODE == 'storage'
REQUIRE_DEVICE_AUTH = os.environ.get('LIVENOTE_REQUIRE_DEVICE_AUTH', '1').strip().lower() in {'1', 'true', 'yes', 'on'}
LIVE_PROCESSING_ENABLED = (
    not STORAGE_ONLY_MODE
    and os.environ.get('LIVENOTE_LIVE_PROCESSING_ENABLED', '1').strip().lower() in {'1', 'true', 'yes', 'on'}
)
_worker_inbox_value = os.environ.get('LIVENOTE_WORKER_INBOX', str(SERVER_DIR.parent / 'worker-inbox'))
LOCAL_WORKER_INBOX = Path(_worker_inbox_value)
if not LOCAL_WORKER_INBOX.is_absolute():
    LOCAL_WORKER_INBOX = SERVER_DIR.parent / LOCAL_WORKER_INBOX

if RUNTIME_ENV in {'production', 'prod'} and not API_KEY:
    raise RuntimeError('生产环境必须设置 LIVENOTE_API_KEY。')
if RUNTIME_ENV in {'production', 'prod'} and not os.environ.get('LIVENOTE_CORS_ORIGINS', '').strip():
    raise RuntimeError('生产环境必须设置 LIVENOTE_CORS_ORIGINS。')
if RUNTIME_ENV in {'production', 'prod'} and not (
    os.environ.get('LIVENOTE_ADMIN_TOKEN', '').strip()
    or os.environ.get('LIVENOTE_ADMIN_PASSWORD', '').strip()
):
    raise RuntimeError('生产环境必须设置 LIVENOTE_ADMIN_PASSWORD（或兼容用的 LIVENOTE_ADMIN_TOKEN）。')
if RUNTIME_ENV in {'production', 'prod'} and not os.environ.get('LIVENOTE_WORKER_TOKEN', '').strip():
    raise RuntimeError('生产环境必须设置 LIVENOTE_WORKER_TOKEN。')


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def normalize_stale_sessions(connection: sqlite3.Connection) -> int:
    """Close sessions that have an end time but never completed upload finalization.

    An end time is durable evidence that the recorder stopped. Such a row must
    not remain in an active recording state forever, especially when the final
    Chunk or completion request was lost.
    """
    placeholders = ', '.join('?' for _ in ACTIVE_SESSION_STATUSES)
    rows = connection.execute(
        f'''SELECT session.id, session.ended_at,
                   (SELECT COUNT(*) FROM chunks chunk WHERE chunk.session_id = session.id) AS chunk_count
            FROM sessions session
            WHERE status IN ({placeholders}) AND ended_at IS NOT NULL''',
        ACTIVE_SESSION_STATUSES,
    ).fetchall()
    if not rows:
        return 0
    repaired_at = now_ms()
    for row in rows:
        repaired_status = 'FINALIZING' if row['chunk_count'] > 0 else 'INTERRUPTED'
        connection.execute(
            '''UPDATE sessions
               SET status = ?, updated_at = MAX(updated_at, ?)
               WHERE id = ?''',
            (repaired_status, max(repaired_at, int(row['ended_at'])), row['id']),
        )
        if repaired_status == 'INTERRUPTED':
            connection.execute(
                '''UPDATE segments
                   SET status = 'INTERRUPTED', ended_at = COALESCE(ended_at, ?)
                   WHERE session_id = ? AND status IN ('RECORDING', 'PAUSED', 'FINALIZING')''',
                (row['ended_at'], row['id']),
            )
    return len(rows)


@contextmanager
def connect():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys = ON')
    connection.execute('PRAGMA busy_timeout = 5000')
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
    finally:
        connection.close()


def init_db() -> None:
    database_exists = DB_PATH.exists()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as connection:
        connection.execute('PRAGMA journal_mode = WAL')
        connection.executescript(
            '''
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                started_at INTEGER NOT NULL,
                ended_at INTEGER,
                status TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS segments (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                segment_index INTEGER NOT NULL,
                started_at INTEGER NOT NULL,
                ended_at INTEGER,
                mime_type TEXT NOT NULL,
                media_settings TEXT NOT NULL,
                status TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                start_elapsed_ms INTEGER NOT NULL DEFAULT 0,
                UNIQUE(session_id, segment_index)
            );
            CREATE TABLE IF NOT EXISTS chunks (
                session_id TEXT NOT NULL REFERENCES sessions(id),
                segment_id TEXT NOT NULL REFERENCES segments(id),
                chunk_index INTEGER NOT NULL,
                size INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                elapsed_ms INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                received_at INTEGER NOT NULL,
                local_path TEXT NOT NULL,
                PRIMARY KEY(segment_id, chunk_index)
            );
            CREATE TABLE IF NOT EXISTS markers (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                marker_type TEXT NOT NULL,
                elapsed_ms INTEGER NOT NULL,
                wall_clock_ms INTEGER NOT NULL,
                note TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS processing_tasks (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                claimed_by TEXT,
                claimed_at INTEGER,
                error_message TEXT NOT NULL DEFAULT '',
                result_path TEXT,
                result_version INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_credentials (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                username TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            '''
        )
        segment_columns = {row['name'] for row in connection.execute('PRAGMA table_info(segments)').fetchall()}
        if 'start_elapsed_ms' not in segment_columns:
            connection.execute('ALTER TABLE segments ADD COLUMN start_elapsed_ms INTEGER NOT NULL DEFAULT 0')
        session_columns = {row['name'] for row in connection.execute('PRAGMA table_info(sessions)').fetchall()}
        task_columns = {row['name'] for row in connection.execute('PRAGMA table_info(processing_tasks)').fetchall()}
        live_window_columns = {row['name'] for row in connection.execute('PRAGMA table_info(live_processing_windows)').fetchall()}
        table_names = {row['name'] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
        requires_backup = database_exists and (
            not {'owner_id', 'device_id', 'published_revision_id'}.issubset(session_columns)
            or not {'lease_token_hash', 'lease_expires_at', 'requested_worker_id'}.issubset(task_columns)
            or not {'live_processing_runs', 'live_processing_windows'}.issubset(table_names)
            or not {'model', 'language', 'pipeline_version', 'prepare_duration_ms', 'asr_duration_ms', 'peak_staging_bytes'}.issubset(live_window_columns)
        )
        if requires_backup:
            connection.commit()
            backup_before_migration(DB_PATH, DATA_DIR / 'backups')
        ensure_schema(connection, DB_PATH, DATA_DIR)
        normalize_stale_sessions(connection)


LIVE_PROCESSING_WORKER = LiveProcessingWorker(DATA_DIR, DB_PATH, enabled=LIVE_PROCESSING_ENABLED)


class SessionPayload(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    title: str = Field(default='', max_length=200)
    startedAt: int
    endedAt: int | None = None
    status: str
    durationMs: int = Field(default=0, ge=0)
    createdAt: int
    updatedAt: int


class SegmentPayload(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    sessionId: str = Field(min_length=1, max_length=128)
    index: int = Field(ge=1)
    startedAt: int
    startElapsedMs: int = Field(default=0, ge=0)
    endedAt: int | None = None
    mimeType: str = Field(default='', max_length=128)
    mediaSettings: dict = Field(default_factory=dict)
    status: str = Field(max_length=32)
    durationMs: int = Field(default=0, ge=0)


class MarkerPayload(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    sessionId: str = Field(min_length=1, max_length=128)
    type: str = Field(max_length=32)
    elapsedMs: int = Field(ge=0)
    wallClockMs: int = Field(ge=0)
    note: str = Field(default='', max_length=4000)
    createdAt: int


class CompletionPayload(BaseModel):
    expectedChunkCount: int | None = Field(default=None, ge=0)


class TranscriptionPayload(BaseModel):
    model: str | None = Field(default=None, max_length=32)
    language: str = Field(default='zh', min_length=2, max_length=16)


class ProcessingJobResponse(BaseModel):
    id: str
    sessionId: str
    status: str
    stage: str
    message: str
    model: str
    language: str


class TaskClaimPayload(BaseModel):
    workerId: str = Field(min_length=1, max_length=128)


class TaskStatusPayload(BaseModel):
    status: str = Field(min_length=1, max_length=32)
    workerId: str | None = Field(default=None, max_length=128)
    leaseToken: str | None = Field(default=None, max_length=256)
    errorMessage: str = Field(default='', max_length=4000)


class KnowledgeStructurePayload(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    points: list[str] = Field(default_factory=list, max_length=100)


class KnowledgeQuestionPayload(BaseModel):
    question: str = Field(default='', max_length=4000)
    answer: str = Field(default='', max_length=8000)
    startMs: int | None = Field(default=None, ge=0)


class KnowledgeDocument(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    overview: str = Field(default='', max_length=20_000)
    keyPoints: list[str] = Field(default_factory=list, max_length=200)
    knowledgeStructure: list[KnowledgeStructurePayload] = Field(default_factory=list, max_length=100)
    questions: list[KnowledgeQuestionPayload] = Field(default_factory=list, max_length=200)
    actionItems: list[str] = Field(default_factory=list, max_length=200)
    confidenceNotes: list[str] = Field(default_factory=list, max_length=200)
    transcript: dict[str, Any] | None = None


class KnowledgeResultPayload(BaseModel):
    version: int = Field(default=1, ge=1)
    result: KnowledgeDocument


class SummaryResultPayload(BaseModel):
    runId: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=1)
    sourceHash: str = Field(min_length=32, max_length=128)
    result: KnowledgeDocument


class SummaryFailurePayload(BaseModel):
    errorMessage: str = Field(default='', max_length=4000)


class LocalTranscriptPayload(BaseModel):
    """Transcript produced by the PC that owns Whisper/FFmpeg."""

    sourceHash: str = Field(min_length=64, max_length=64, pattern=r'^[a-fA-F0-9]{64}$')
    model: str = Field(min_length=1, max_length=64)
    language: str = Field(default='zh', min_length=2, max_length=16)
    transcript: dict[str, Any]
    durationSeconds: float | None = Field(default=None, ge=0)


class UserCreatePayload(BaseModel):
    displayName: str = Field(min_length=1, max_length=120)


class AdminLoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=512)


class AdminSetupPayload(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8, max_length=512)


class UserPatchPayload(BaseModel):
    displayName: str | None = Field(default=None, min_length=1, max_length=120)
    status: str | None = Field(default=None, pattern='^(ACTIVE|DISABLED)$')


class AdminSessionPatchPayload(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    ownerId: str | None = Field(default=None, max_length=128)


class AdminTaskNotePayload(BaseModel):
    note: str = Field(default='', max_length=2000)


class AdminAssignSessionsPayload(BaseModel):
    ownerId: str = Field(min_length=1, max_length=128)
    onlyUnassigned: bool = True


class PairingCodePayload(BaseModel):
    expiresInMinutes: int = Field(default=10, ge=1, le=60)


class PairPayload(BaseModel):
    code: str = Field(min_length=6, max_length=32)
    label: str = Field(default='Android Chrome', max_length=120)


class RequestPullPayload(BaseModel):
    workerId: str = Field(min_length=1, max_length=128)


class PublishPayload(BaseModel):
    revisionId: str = Field(min_length=1, max_length=128)


def validate_identifier(value: str, label: str) -> None:
    if not IDENTIFIER_PATTERN.fullmatch(value):
        raise HTTPException(status_code=400, detail=f'{label} 格式无效')


def session_access(connection: sqlite3.Connection, session_id: str, request: Request | None = None) -> sqlite3.Row:
    session = connection.execute('SELECT * FROM sessions WHERE id = ?', (session_id,)).fetchone()
    if session is None:
        raise HTTPException(status_code=404, detail='Session 不存在')
    if request is not None and bearer_token(request):
        device = authenticate_device(connection, request)
        if session['owner_id'] != device['user_id']:
            raise HTTPException(status_code=403, detail='无权访问该 Session')
        connection.execute('UPDATE devices SET last_seen_at = ? WHERE id = ?', (now_ms(), device['id']))
        # session_access is called before upload_chunk opens its short
        # BEGIN IMMEDIATE section. Commit the heartbeat separately so the
        # chunk transaction never starts inside this implicit write transaction.
        connection.commit()
    elif REQUIRE_DEVICE_AUTH:
        raise HTTPException(status_code=401, detail='请先完成手机配对')
    elif session['owner_id'] is not None:
        raise HTTPException(status_code=401, detail='该 Session 需要设备凭证')
    return session


def task_payload(row: sqlite3.Row) -> dict[str, Any]:
    progress = None
    if 'completed_parts' in row.keys() and row['completed_parts'] is not None and row['status'] in {'TRANSCRIBING', 'TRANSCRIBED'}:
        duration_seconds = max(0.001, float(row['duration_ms'] or 0) / 1000)
        step_seconds = max(1, ASR_CHUNK_SECONDS - ASR_CHUNK_OVERLAP_SECONDS)
        total_parts = max(1, math.ceil(max(0.001, duration_seconds - ASR_CHUNK_OVERLAP_SECONDS) / step_seconds))
        completed_parts = total_parts if row['status'] == 'TRANSCRIBED' else min(total_parts, max(0, int(row['completed_parts'] or 0)))
        progress = {
            'completedParts': completed_parts,
            'totalParts': total_parts,
            'percent': round(completed_parts / total_parts * 100),
        }
    return {
        'id': row['id'],
        'sessionId': row['session_id'],
        'title': row['title'] if 'title' in row.keys() else None,
        'ownerId': row['owner_id'] if 'owner_id' in row.keys() else None,
        'ownerName': row['owner_name'] if 'owner_name' in row.keys() else None,
        'startedAt': row['started_at'] if 'started_at' in row.keys() else None,
        'durationMs': row['duration_ms'] if 'duration_ms' in row.keys() else None,
        'sessionStatus': row['session_status'] if 'session_status' in row.keys() else None,
        'sourceId': INSTANCE_ID,
        'sourceLabel': INSTANCE_LABEL,
        'status': row['status'],
        'attempts': row['attempts'],
        'claimedBy': row['claimed_by'],
        'claimedAt': row['claimed_at'],
        'requestedWorkerId': row['requested_worker_id'] if 'requested_worker_id' in row.keys() else None,
        'leaseExpiresAt': row['lease_expires_at'] if 'lease_expires_at' in row.keys() else None,
        'downloadedAt': row['downloaded_at'] if 'downloaded_at' in row.keys() else None,
        'errorMessage': row['error_message'],
        'errorStage': row['error_stage'] if 'error_stage' in row.keys() else '',
        'adminNote': row['admin_note'] if 'admin_note' in row.keys() else '',
        'resultVersion': row['result_version'],
        'createdAt': row['created_at'],
        'updatedAt': row['updated_at'],
        'progress': progress,
    }


def _local_worker_payload() -> dict[str, Any]:
    return {
        'enabled': LOCAL_PULL_ENABLED,
        'workerId': os.environ.get('LIVENOTE_WORKER_ID', 'local-pc'),
        'inbox': 'worker-inbox/',
    }


def authorize_worker(request: Request | None) -> None:
    if os.environ.get('LIVENOTE_WORKER_TOKEN', '').strip():
        if request is None:
            raise HTTPException(status_code=401, detail='缺少 Worker 凭证')
        require_worker(request)


def verify_task_lease(row: sqlite3.Row, worker_id: str | None, lease_token: str | None, *, allow_expired: bool = False) -> None:
    if row['claimed_by'] and worker_id and row['claimed_by'] != worker_id:
        raise HTTPException(status_code=409, detail='任务不属于当前 Worker')
    if row['lease_token_hash'] and (not lease_token or not secrets.compare_digest(row['lease_token_hash'], hash_secret(lease_token))):
        raise HTTPException(status_code=409, detail='任务租约无效')
    if not allow_expired and row['lease_expires_at'] and int(row['lease_expires_at']) < now_ms():
        raise HTTPException(status_code=409, detail='任务租约已过期')


def create_processing_task(connection: sqlite3.Connection, session_id: str, created_at: int | None = None) -> str:
    task_id = f'task-{uuid.uuid4()}'
    timestamp = created_at or now_ms()
    connection.execute(
        '''INSERT OR IGNORE INTO processing_tasks(id, session_id, status, created_at, updated_at)
           VALUES (?, ?, 'READY', ?, ?)''',
        (task_id, session_id, timestamp, timestamp),
    )
    existing = connection.execute('SELECT id FROM processing_tasks WHERE session_id = ?', (session_id,)).fetchone()
    return existing['id']


async def save_diagnostic_upload(upload: UploadFile, destination: Path) -> int:
    size = 0
    with destination.open('wb') as output:
        while True:
            block = await upload.read(1024 * 1024)
            if not block:
                break
            size += len(block)
            if size > MAX_DIAGNOSTIC_BYTES:
                destination.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail='诊断文件超过大小限制')
            output.write(block)
    return size


async def save_bounded_upload(upload: UploadFile, destination: Path, limit: int) -> tuple[int, str]:
    """Stream a worker artifact to disk without buffering it in the API process."""
    digest = hashlib.sha256()
    size = 0
    temporary = destination.with_name(f'.{destination.name}.part')
    try:
        with temporary.open('wb') as output:
            while True:
                block = await upload.read(1024 * 1024)
                if not block:
                    break
                size += len(block)
                if size > limit:
                    raise HTTPException(status_code=413, detail='上传文件超过大小限制')
                digest.update(block)
                output.write(block)
        temporary.replace(destination)
    except HTTPException:
        temporary.unlink(missing_ok=True)
        raise
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return size, digest.hexdigest()


init_db()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    recover_closed_sessions()
    LIVE_PROCESSING_WORKER.start()
    yield
    LIVE_PROCESSING_WORKER.stop()


app = FastAPI(title='LiveNote API', version='0.1.0', lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=['*'],
    allow_headers=['*'],
)


@app.middleware('http')
async def api_key_middleware(request: Request, call_next):
    if API_KEY and request.url.path.startswith('/api/v1/'):
        provided = request.headers.get('x-api-key', '')
        if not secrets.compare_digest(provided, API_KEY) and not has_valid_worker_token(request):
            return JSONResponse(status_code=401, content={'detail': '缺少有效 API Key'})
    return await call_next(request)


@app.get('/health')
def health() -> dict:
    return {
        'ok': True,
        'service': 'livenote-api',
        'storageSchema': 9,
        'capabilities': {
            'processingMode': PROCESSING_MODE,
            'storageOnly': STORAGE_ONLY_MODE,
            'ffmpeg': bool(shutil.which(RECONSTRUCTION_FFMPEG)) if not STORAGE_ONLY_MODE else False,
            'ffprobe': bool(shutil.which(RECONSTRUCTION_FFPROBE)) if not STORAGE_ONLY_MODE else False,
            'serverReconstruction': not STORAGE_ONLY_MODE,
            'serverAsr': not STORAGE_ONLY_MODE,
            'manualProcessing': True,
            'localTranscription': (
                is_model_cached(os.environ.get('LIVENOTE_WHISPER_MODEL', DEFAULT_MODEL))
                if not STORAGE_ONLY_MODE else False
            ),
            'liveIncrementalProcessing': LIVE_PROCESSING_ENABLED,
        },
    }


@app.get('/api/v1/health')
def api_health() -> dict:
    return health()


@app.post('/api/v1/auth/admin/login')
def admin_login(payload: AdminLoginPayload, request: Request) -> dict:
    with connect() as connection:
        session_token = create_admin_session(request, payload.username, payload.password, connection)
    return {
        'sessionToken': session_token,
        'expiresInSeconds': max(900, int(os.environ.get('LIVENOTE_ADMIN_SESSION_TTL_SECONDS', '43200'))),
    }


@app.get('/api/v1/auth/admin/status')
def admin_auth_status() -> dict:
    with connect() as connection:
        setup_available = admin_setup_available(connection)
        configured = admin_auth_configured(connection)
    return {'configured': configured, 'setupAvailable': setup_available}


@app.post('/api/v1/auth/admin/setup')
def admin_setup(payload: AdminSetupPayload, request: Request) -> dict:
    if RUNTIME_ENV in {'production', 'prod'}:
        raise HTTPException(status_code=403, detail='生产环境请通过服务配置初始化管理员账号')
    if request.client is not None and request.client.host not in {'127.0.0.1', '::1', 'localhost'}:
        raise HTTPException(status_code=403, detail='管理员初始化仅允许在本机执行')
    username = payload.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail='管理员账号不能为空')
    with connect() as connection:
        if not admin_setup_available(connection):
            raise HTTPException(status_code=409, detail='管理员账号已经设置，不能重复初始化')
        create_admin_credentials(connection, username, payload.password)
        session_token = create_admin_session(request, username, payload.password, connection)
    return {
        'sessionToken': session_token,
        'expiresInSeconds': max(900, int(os.environ.get('LIVENOTE_ADMIN_SESSION_TTL_SECONDS', '43200'))),
    }


def _audit(connection: sqlite3.Connection, actor: str, action: str, target: str, details: dict[str, Any] | None = None) -> None:
    connection.execute(
        'INSERT INTO audit_events(id, actor, action, target, details, created_at) VALUES (?, ?, ?, ?, ?, ?)',
        (new_id('audit'), actor, action, target, json.dumps(details or {}, ensure_ascii=False), now_ms()),
    )


@app.post('/api/v1/auth/pair')
def pair_device(payload: PairPayload) -> dict:
    code_hash = hash_secret(payload.code.strip())
    token = generate_device_token()
    device_id = new_id('device')
    timestamp = now_ms()
    with connect() as connection:
        pairing = connection.execute(
            '''SELECT id, user_id FROM pairing_codes
               WHERE code_hash = ? AND consumed_at IS NULL AND expires_at >= ?''',
            (code_hash, timestamp),
        ).fetchone()
        if pairing is None:
            raise HTTPException(status_code=400, detail='配对码无效或已过期')
        user = connection.execute('SELECT id, status FROM users WHERE id = ?', (pairing['user_id'],)).fetchone()
        if user is None or user['status'] != 'ACTIVE':
            raise HTTPException(status_code=403, detail='用户已停用')
        consumed = connection.execute('UPDATE pairing_codes SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL', (timestamp, pairing['id']))
        if consumed.rowcount == 0:
            raise HTTPException(status_code=409, detail='配对码已被使用')
        connection.execute(
            'INSERT INTO devices(id, user_id, label, token_hash, created_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)',
            (device_id, pairing['user_id'], payload.label, hash_secret(token), timestamp, timestamp),
        )
    return {'deviceId': device_id, 'userId': pairing['user_id'], 'token': token}


@app.get('/api/v1/auth/me')
def device_me(request: Request) -> dict:
    with connect() as connection:
        device = authenticate_device(connection, request)
        connection.execute('UPDATE devices SET last_seen_at = ? WHERE id = ?', (now_ms(), device['id']))
        normalize_stale_sessions(connection)
        user = connection.execute('SELECT id, display_name, status FROM users WHERE id = ?', (device['user_id'],)).fetchone()
    return {'device': {'id': device['id'], 'userId': device['user_id']}, 'user': {'id': user['id'], 'displayName': user['display_name'], 'status': user['status']}}


@app.get('/api/v1/sessions')
def list_device_sessions(request: Request, query: str = '', status: str | None = None, offset: int = 0, limit: int = 50) -> dict:
    """List sessions owned by the paired phone's user for result syncing."""
    offset = max(0, offset)
    limit = max(1, min(limit, 100))
    normalized_query = query.strip().lower()
    with connect() as connection:
        device = authenticate_device(connection, request)
        connection.execute('UPDATE devices SET last_seen_at = ? WHERE id = ?', (now_ms(), device['id']))
        clauses = ['session.owner_id = ?']
        parameters: list[Any] = [device['user_id']]
        if normalized_query:
            clauses.append('(lower(session.id) LIKE ? OR lower(session.title) LIKE ?)')
            normalized = f'%{normalized_query}%'
            parameters.extend([normalized, normalized])
        if status:
            clauses.append('session.status = ?')
            parameters.append(status.upper())
        where = ' AND '.join(clauses)
        total = connection.execute(f'SELECT COUNT(*) AS count FROM sessions session WHERE {where}', parameters).fetchone()['count']
        rows = connection.execute(
            f'''SELECT session.id, session.title, session.started_at, session.ended_at, session.status,
                       session.duration_ms, session.created_at, session.updated_at,
                       (SELECT COUNT(*) FROM segments segment WHERE segment.session_id = session.id) AS segment_count,
                       (SELECT COUNT(*) FROM chunks chunk WHERE chunk.session_id = session.id) AS chunk_count,
                       task.status AS task_status, task.result_version,
                       CASE WHEN session.published_revision_id IS NULL THEN 0 ELSE 1 END AS has_published_result
                FROM sessions session LEFT JOIN processing_tasks task ON task.session_id = session.id
                WHERE {where} ORDER BY session.updated_at DESC LIMIT ? OFFSET ?''',
            (*parameters, limit, offset),
        ).fetchall()
    live_by_session = {str(row['id']): live_processing_status(DB_PATH, str(row['id'])) for row in rows}
    return {
        'items': [
            {
                'id': row['id'],
                'title': row['title'],
                'startedAt': row['started_at'],
                'endedAt': row['ended_at'],
                'status': row['status'],
                'durationMs': row['duration_ms'],
                'createdAt': row['created_at'],
                'updatedAt': row['updated_at'],
                'segmentCount': row['segment_count'],
                'chunkCount': row['chunk_count'],
                'taskStatus': row['task_status'],
                'resultVersion': row['result_version'] or 0,
                'hasPublishedResult': bool(row['has_published_result']),
                'liveProcessing': live_by_session.get(str(row['id'])),
            }
            for row in rows
        ],
        'total': total,
        'offset': offset,
        'limit': limit,
    }


@app.get('/api/v1/sessions/{session_id}/live-processing')
def get_live_processing(session_id: str, request: Request = None) -> dict:
    validate_identifier(session_id, 'Session ID')
    with connect() as connection:
        session_access(connection, session_id, request)
    status = live_processing_status(DB_PATH, session_id)
    return {'sessionId': session_id, 'liveProcessing': status}


@app.get('/api/v1/sessions/{session_id}/live-transcript')
def get_live_transcript(session_id: str, request: Request = None) -> dict:
    validate_identifier(session_id, 'Session ID')
    with connect() as connection:
        session_access(connection, session_id, request)
    path = DATA_DIR / 'processed' / 'sessions' / session_id / 'transcript-live.json'
    if not path.is_file():
        raise HTTPException(status_code=409, detail='增量逐字稿尚未准备好。')
    try:
        transcript = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=500, detail=f'读取增量逐字稿失败：{error}') from error
    return {'sessionId': session_id, 'status': live_processing_status(DB_PATH, session_id), 'transcript': transcript}


@app.post('/api/v1/sessions/{session_id}/live-processing/retry')
def retry_live_processing(session_id: str, request: Request = None) -> dict:
    validate_identifier(session_id, 'Session ID')
    with connect() as connection:
        session_access(connection, session_id, request)
        retry_live_run(connection, session_id)
    LIVE_PROCESSING_WORKER.wake(session_id)
    return {'sessionId': session_id, 'liveProcessing': live_processing_status(DB_PATH, session_id)}


@app.get('/api/v1/admin/users')
def admin_list_users(request: Request, query: str = '', offset: int = 0, limit: int = 50) -> dict:
    require_admin(request)
    normalized_query = query.strip().lower()
    offset = max(0, offset)
    limit = max(1, min(limit, 200))
    with connect() as connection:
        where = ''
        parameters: list[Any] = []
        if normalized_query:
            where = 'WHERE lower(id) LIKE ? OR lower(display_name) LIKE ?'
            parameters.extend([f'%{normalized_query}%', f'%{normalized_query}%'])
        total = connection.execute(f'SELECT COUNT(*) AS count FROM users {where}', parameters).fetchone()['count']
        rows = connection.execute(
            f'''SELECT user.*, (SELECT COUNT(*) FROM sessions WHERE owner_id = user.id) AS session_count,
                (SELECT COUNT(*) FROM devices WHERE user_id = user.id AND revoked_at IS NULL) AS device_count
                FROM users user {where} ORDER BY user.created_at DESC LIMIT ? OFFSET ?''',
            (*parameters, limit, offset),
        ).fetchall()
    return {'items': [dict(row) for row in rows], 'total': total, 'offset': offset, 'limit': limit}


@app.post('/api/v1/admin/users')
def admin_create_user(request: Request, payload: UserCreatePayload) -> dict:
    require_admin(request)
    user_id = new_id('user')
    timestamp = now_ms()
    with connect() as connection:
        connection.execute('INSERT INTO users(id, display_name, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)', (user_id, payload.displayName.strip(), 'ACTIVE', timestamp, timestamp))
        _audit(connection, 'admin', 'CREATE_USER', user_id, {'displayName': payload.displayName.strip()})
    return {'id': user_id, 'displayName': payload.displayName.strip(), 'status': 'ACTIVE'}


@app.patch('/api/v1/admin/users/{user_id}')
def admin_update_user(user_id: str, request: Request, payload: UserPatchPayload) -> dict:
    require_admin(request)
    validate_identifier(user_id, 'User ID')
    updates: list[str] = []
    values: list[Any] = []
    if payload.displayName is not None:
        updates.append('display_name = ?')
        values.append(payload.displayName.strip())
    if payload.status is not None:
        updates.append('status = ?')
        values.append(payload.status)
    if not updates:
        raise HTTPException(status_code=400, detail='没有可更新字段')
    updates.append('updated_at = ?')
    values.extend([now_ms(), user_id])
    with connect() as connection:
        result = connection.execute(f'UPDATE users SET {", ".join(updates)} WHERE id = ?', values)
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail='用户不存在')
        row = connection.execute('SELECT id, display_name, status, created_at, updated_at FROM users WHERE id = ?', (user_id,)).fetchone()
        _audit(connection, 'admin', 'UPDATE_USER', user_id, payload.model_dump(exclude_none=True))
    return dict(row)


@app.post('/api/v1/admin/users/{user_id}/pairing-codes')
def admin_create_pairing_code(user_id: str, request: Request, payload: PairingCodePayload | None = None) -> dict:
    require_admin(request)
    validate_identifier(user_id, 'User ID')
    payload = payload or PairingCodePayload()
    code = generate_pairing_code()
    timestamp = now_ms()
    with connect() as connection:
        if connection.execute('SELECT 1 FROM users WHERE id = ? AND status = ?', (user_id, 'ACTIVE')).fetchone() is None:
            raise HTTPException(status_code=404, detail='用户不存在或已停用')
        connection.execute(
            'INSERT INTO pairing_codes(id, user_id, code_hash, expires_at, created_at) VALUES (?, ?, ?, ?, ?)',
            (new_id('pair'), user_id, hash_secret(code), timestamp + payload.expiresInMinutes * 60_000, timestamp),
        )
        _audit(connection, 'admin', 'CREATE_PAIRING_CODE', user_id, {})
    return {'code': code, 'expiresAt': timestamp + payload.expiresInMinutes * 60_000}


@app.get('/api/v1/admin/users/{user_id}/devices')
def admin_list_devices(user_id: str, request: Request) -> dict:
    require_admin(request)
    validate_identifier(user_id, 'User ID')
    with connect() as connection:
        rows = connection.execute('SELECT id, user_id, label, revoked_at, last_seen_at, created_at FROM devices WHERE user_id = ? ORDER BY created_at DESC', (user_id,)).fetchall()
    return {'items': [dict(row) for row in rows]}


@app.get('/api/v1/admin/sessions')
def admin_list_sessions(request: Request, ownerId: str | None = None, query: str = '', status: str | None = None, offset: int = 0, limit: int = 50, startedFrom: int | None = None, startedTo: int | None = None) -> dict:
    require_admin(request)
    offset = max(0, offset)
    limit = max(1, min(limit, 200))
    clauses: list[str] = []
    parameters: list[Any] = []
    if ownerId:
        if ownerId == 'UNASSIGNED':
            clauses.append('session.owner_id IS NULL')
        else:
            validate_identifier(ownerId, 'User ID')
            clauses.append('session.owner_id = ?')
            parameters.append(ownerId)
    if status:
        clauses.append('session.status = ?')
        parameters.append(status.upper())
    if startedFrom is not None:
        clauses.append('session.started_at >= ?')
        parameters.append(startedFrom)
    if startedTo is not None:
        clauses.append('session.started_at < ?')
        parameters.append(startedTo)
    if query.strip():
        clauses.append('(lower(session.id) LIKE ? OR lower(session.title) LIKE ?)')
        normalized = f'%{query.strip().lower()}%'
        parameters.extend([normalized, normalized])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ''
    with connect() as connection:
        total = connection.execute(f'SELECT COUNT(*) AS count FROM sessions session {where}', parameters).fetchone()['count']
        rows = connection.execute(
            f'''SELECT session.*, user.display_name AS owner_name,
                task.status AS task_status, task.result_version
                FROM sessions session LEFT JOIN users user ON user.id = session.owner_id
                LEFT JOIN processing_tasks task ON task.session_id = session.id
                {where} ORDER BY session.updated_at DESC LIMIT ? OFFSET ?''',
            (*parameters, limit, offset),
        ).fetchall()
    return {'items': [dict(row) for row in rows], 'total': total, 'offset': offset, 'limit': limit}


@app.post('/api/v1/admin/sessions/assign-owner')
def admin_assign_sessions_owner(request: Request, payload: AdminAssignSessionsPayload) -> dict:
    require_admin(request)
    validate_identifier(payload.ownerId, 'User ID')
    timestamp = now_ms()
    with connect() as connection:
        if connection.execute('SELECT 1 FROM users WHERE id = ? AND status = ?', (payload.ownerId, 'ACTIVE')).fetchone() is None:
            raise HTTPException(status_code=404, detail='用户不存在或已停用')
        where = 'owner_id IS NULL' if payload.onlyUnassigned else '1 = 1'
        result = connection.execute(
            f'UPDATE sessions SET owner_id = ?, updated_at = ? WHERE {where}',
            (payload.ownerId, timestamp),
        )
        _audit(connection, 'admin', 'BULK_ASSIGN_SESSIONS', payload.ownerId, {
            'updatedCount': result.rowcount,
            'onlyUnassigned': payload.onlyUnassigned,
        })
    return {'ok': True, 'ownerId': payload.ownerId, 'updatedCount': result.rowcount}


@app.get('/api/v1/admin/sessions/{session_id}')
def admin_get_session(session_id: str, request: Request) -> dict:
    require_admin(request)
    validate_identifier(session_id, 'Session ID')
    with connect() as connection:
        session = connection.execute(
            '''SELECT session.*, user.display_name AS owner_name
               FROM sessions session LEFT JOIN users user ON user.id = session.owner_id
               WHERE session.id = ?''',
            (session_id,),
        ).fetchone()
        if session is None:
            raise HTTPException(status_code=404, detail='Session 不存在')
        segments = connection.execute('SELECT id, segment_index, status, duration_ms, mime_type, started_at, ended_at FROM segments WHERE session_id = ? ORDER BY segment_index', (session_id,)).fetchall()
        task = connection.execute('SELECT * FROM processing_tasks WHERE session_id = ?', (session_id,)).fetchone()
        revisions = connection.execute('SELECT id, version, created_at FROM result_revisions WHERE session_id = ? ORDER BY version DESC', (session_id,)).fetchall()
    return {'session': dict(session), 'segments': [dict(row) for row in segments], 'task': task_payload(task) if task else None, 'revisions': [dict(row) for row in revisions]}


@app.patch('/api/v1/admin/sessions/{session_id}')
def admin_update_session(session_id: str, request: Request, payload: AdminSessionPatchPayload) -> dict:
    require_admin(request)
    validate_identifier(session_id, 'Session ID')
    updates: list[str] = []
    values: list[Any] = []
    if payload.title is not None:
        title = payload.title.strip()
        if not title:
            raise HTTPException(status_code=422, detail='会话标题不能为空')
        updates.append('title = ?')
        values.append(title)
    if payload.ownerId is not None:
        validate_identifier(payload.ownerId, 'User ID')
        values.append(payload.ownerId)
        updates.append('owner_id = ?')
    if not updates:
        raise HTTPException(status_code=400, detail='没有可更新字段')
    updates.append('updated_at = ?')
    values.extend([now_ms(), session_id])
    with connect() as connection:
        if payload.ownerId is not None and connection.execute('SELECT 1 FROM users WHERE id = ? AND status = ?', (payload.ownerId, 'ACTIVE')).fetchone() is None:
            raise HTTPException(status_code=404, detail='用户不存在或已停用')
        result = connection.execute(f'UPDATE sessions SET {", ".join(updates)} WHERE id = ?', values)
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail='Session 不存在')
        _audit(connection, 'admin', 'UPDATE_SESSION', session_id, payload.model_dump(exclude_none=True))
    return {'ok': True, 'sessionId': session_id}


@app.post('/api/v1/admin/sessions/{session_id}/interrupt')
def admin_interrupt_session(session_id: str, request: Request) -> dict:
    """Close an abandoned recording without deleting its stored audio."""
    require_admin(request)
    validate_identifier(session_id, 'Session ID')
    interrupted_at = now_ms()
    with connect() as connection:
        session = connection.execute(
            'SELECT status, duration_ms FROM sessions WHERE id = ?',
            (session_id,),
        ).fetchone()
        if session is None:
            raise HTTPException(status_code=404, detail='Session 不存在')
        if session['status'] not in {'RECORDING', 'PAUSED', 'FINALIZING'}:
            raise HTTPException(status_code=409, detail=f"Session 当前状态为 {session['status']}，无需结束遗留录音")
        last_elapsed = connection.execute(
            'SELECT MAX(elapsed_ms) AS elapsed_ms FROM chunks WHERE session_id = ?',
            (session_id,),
        ).fetchone()['elapsed_ms']
        duration_ms = max(int(session['duration_ms'] or 0), int(last_elapsed or 0))
        connection.execute(
            '''UPDATE sessions
               SET status = 'INTERRUPTED', ended_at = COALESCE(ended_at, ?),
                   duration_ms = ?, updated_at = ?
               WHERE id = ?''',
            (interrupted_at, duration_ms, interrupted_at, session_id),
        )
        connection.execute(
            '''UPDATE segments
               SET status = 'INTERRUPTED', ended_at = COALESCE(ended_at, ?)
               WHERE session_id = ? AND status IN ('RECORDING', 'PAUSED', 'FINALIZING')''',
            (interrupted_at, session_id),
        )
        _audit(connection, 'admin', 'INTERRUPT_SESSION', session_id, {
            'previousStatus': session['status'],
            'durationMs': duration_ms,
        })
    return {'ok': True, 'sessionId': session_id, 'status': 'INTERRUPTED', 'durationMs': duration_ms}


@app.delete('/api/v1/admin/sessions/{session_id}')
def admin_delete_session(session_id: str, request: Request) -> dict:
    require_admin(request)
    validate_identifier(session_id, 'Session ID')

    with connect() as connection:
        session = connection.execute('SELECT id, status FROM sessions WHERE id = ?', (session_id,)).fetchone()
        if session is None:
            raise HTTPException(status_code=404, detail='Session 不存在')
        active_task = connection.execute(
            """SELECT id, status FROM processing_tasks
               WHERE session_id = ?
                 AND status IN ('CLAIMED', 'LOCAL_READY', 'TRANSCRIBING', 'PROCESSING',
                                'TRANSCRIBED', 'SUMMARIZING', 'READY_TO_UPLOAD')""",
            (session_id,),
        ).fetchone()
        if active_task:
            raise HTTPException(status_code=409, detail=f'Session 正在由电脑端处理，不能删除：{active_task["id"]}')

    try:
        counts = delete_session_data(DATA_DIR, DB_PATH, session_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail='Session 不存在') from error
    except RetentionError as error:
        raise HTTPException(status_code=500, detail=f'Session 清理失败：{error}') from error

    with connect() as connection:
        _audit(connection, 'admin', 'DELETE_SESSION', session_id, {'status': session['status'], **counts})
    return {'ok': True, 'sessionId': session_id, **counts}


def recover_orphaned_processing_tasks() -> int:
    """Requeue processing tasks left behind by an interrupted or legacy flow."""
    with connect() as connection:
        rows = connection.execute(
            """SELECT id, session_id, result_version
               FROM processing_tasks
               WHERE status = 'PROCESSING'""",
        ).fetchall()

    recovered = 0
    for task in rows:
        task_id = task['id']
        session_id = task['session_id']
        if task['result_version']:
            with connect() as connection:
                connection.execute(
                    "UPDATE processing_tasks SET status = 'REVIEW', error_message = '', error_stage = '', updated_at = ? WHERE id = ? AND status = 'PROCESSING'",
                    (now_ms(), task_id),
                )
            recovered += 1
            continue

        if find_active_job(DATA_DIR, session_id) is not None:
            continue

        with connect() as connection:
            if live_processing_has_pending_windows(connection, session_id):
                connection.execute(
                    "UPDATE processing_tasks SET error_message = '等待实时识别窗口收尾后自动继续。', error_stage = 'LIVE_DRAIN', updated_at = ? WHERE id = ? AND status = 'PROCESSING'",
                    (now_ms(), task_id),
                )
                continue

        try:
            create_job(DATA_DIR, DB_PATH, session_id, DEFAULT_MODEL, 'zh', task_id=task_id)
            with connect() as connection:
                connection.execute(
                    "UPDATE processing_tasks SET error_message = '', error_stage = 'AUTO_PROCESS', updated_at = ? WHERE id = ? AND status = 'PROCESSING'",
                    (now_ms(), task_id),
                )
            recovered += 1
        except Exception as error:
            with connect() as connection:
                connection.execute(
                    "UPDATE processing_tasks SET status = 'FAILED', claimed_by = NULL, claimed_at = NULL, requested_worker_id = NULL, lease_token_hash = NULL, lease_expires_at = NULL, error_message = ?, error_stage = 'AUTO_PROCESS', updated_at = ? WHERE id = ? AND status = 'PROCESSING'",
                    (str(error), now_ms(), task_id),
                )
    return recovered


def recover_closed_sessions() -> int:
    """Create tasks for fully uploaded sessions whose final request was lost."""
    recovered = 0
    with connect() as connection:
        candidates = connection.execute(
            '''SELECT session.id, session.duration_ms
               FROM sessions session
               LEFT JOIN processing_tasks task ON task.session_id = session.id
               WHERE session.status IN ('RECORDING', 'FINALIZING')
                 AND session.ended_at IS NOT NULL
                 AND task.id IS NULL''',
        ).fetchall()
        for session in candidates:
            segments = connection.execute(
                'SELECT id, status, segment_index, ended_at FROM segments WHERE session_id = ? ORDER BY segment_index',
                (session['id'],),
            ).fetchall()
            if not segments:
                continue

            valid = True
            for segment in segments:
                if segment['ended_at'] is None:
                    valid = False
                    break
                indexes = [row['chunk_index'] for row in connection.execute(
                    'SELECT chunk_index FROM chunks WHERE segment_id = ? ORDER BY chunk_index',
                    (segment['id'],),
                ).fetchall()]
                if not indexes or indexes != list(range(len(indexes))):
                    valid = False
                    break
                if segment['status'] != 'COMPLETED':
                    connection.execute('UPDATE segments SET status = ? WHERE id = ?', ('COMPLETED', segment['id']))

            if not valid:
                continue
            last_elapsed = connection.execute(
                'SELECT MAX(elapsed_ms) AS elapsed_ms FROM chunks WHERE session_id = ?',
                (session['id'],),
            ).fetchone()['elapsed_ms']
            if last_elapsed is None or int(session['duration_ms'] or 0) > int(last_elapsed) + UPLOAD_DURATION_TOLERANCE_MS:
                continue
            completed_at = now_ms()
            connection.execute(
                'UPDATE sessions SET status = ?, ended_at = COALESCE(ended_at, ?), updated_at = ? WHERE id = ? AND status IN (\'RECORDING\', \'FINALIZING\')',
                ('COMPLETED', completed_at, completed_at, session['id']),
            )
            create_processing_task(connection, session['id'], completed_at)
            recovered += 1
    return recovered


@app.get('/api/v1/admin/tasks')
def admin_list_tasks(request: Request, status: str = 'ALL', offset: int = 0, limit: int = 50) -> dict:
    require_admin(request)
    normalized_status = status.upper().strip()
    if normalized_status != 'ALL' and normalized_status not in TASK_STATUSES:
        raise HTTPException(status_code=400, detail=f'任务状态无效：{status}')
    offset = max(0, offset)
    limit = max(1, min(limit, 200))
    where = '' if normalized_status == 'ALL' else 'WHERE task.status = ?'
    parameters: list[Any] = [] if normalized_status == 'ALL' else [normalized_status]
    with connect() as connection:
        total = connection.execute(f'SELECT COUNT(*) AS count FROM processing_tasks task {where}', parameters).fetchone()['count']
        rows = connection.execute(
            f'''SELECT task.*, session.title, session.duration_ms, session.status AS session_status,
                       session.owner_id AS owner_id,
                       user.display_name AS owner_name,
                       (SELECT COUNT(*)
                          FROM processing_parts part
                          JOIN processing_runs run ON run.id = part.run_id
                         WHERE run.task_id = task.id
                           AND run.generation = (SELECT COALESCE(MAX(latest.generation), 0) FROM processing_runs latest WHERE latest.task_id = task.id)
                           AND part.status = 'COMPLETED') AS completed_parts
                FROM processing_tasks task
                JOIN sessions session ON session.id = task.session_id
                LEFT JOIN users user ON user.id = session.owner_id
                {where} ORDER BY task.created_at ASC LIMIT ? OFFSET ?''',
            (*parameters, limit, offset),
        ).fetchall()
    return {'items': [task_payload(row) for row in rows], 'total': total, 'offset': offset, 'limit': limit}


@app.patch('/api/v1/admin/tasks/{task_id}/note')
def admin_update_task_note(task_id: str, request: Request, payload: AdminTaskNotePayload) -> dict:
    require_admin(request)
    validate_identifier(task_id, 'Task ID')
    note = payload.note.strip()
    updated_at = now_ms()
    with connect() as connection:
        result = connection.execute(
            'UPDATE processing_tasks SET admin_note = ?, updated_at = ? WHERE id = ?',
            (note, updated_at, task_id),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail='任务不存在')
        _audit(connection, 'admin', 'UPDATE_TASK_NOTE', task_id, {'note': note})
    return {'ok': True, 'taskId': task_id, 'adminNote': note}


@app.post('/api/v1/admin/tasks/{task_id}/request-pull')
def admin_request_pull(task_id: str, request: Request, payload: RequestPullPayload) -> dict:
    require_admin(request)
    validate_identifier(task_id, 'Task ID')
    with connect() as connection:
        current = connection.execute('SELECT status FROM processing_tasks WHERE id = ?', (task_id,)).fetchone()
        if current is None:
            raise HTTPException(status_code=404, detail='任务不存在')
        if current['status'] not in {'READY', 'FAILED', 'REVIEW'}:
            raise HTTPException(status_code=409, detail=f'任务当前不能请求拉取：{current["status"]}')
        result = connection.execute(
            "UPDATE processing_tasks SET requested_worker_id = ?, status = CASE WHEN status IN ('FAILED', 'REVIEW') THEN 'READY' ELSE status END, updated_at = ? WHERE id = ?",
            (payload.workerId, now_ms(), task_id),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail='任务不存在')
        _audit(connection, 'admin', 'REQUEST_PULL', task_id, {'workerId': payload.workerId})
        row = connection.execute('SELECT task.*, session.title, session.duration_ms, session.status AS session_status FROM processing_tasks task JOIN sessions session ON session.id = task.session_id WHERE task.id = ?', (task_id,)).fetchone()
    return {'task': task_payload(row)}


@app.get('/api/v1/admin/local-worker')
def admin_local_worker(request: Request) -> dict:
    """Report whether this API process can pull a task into its own PC inbox."""
    require_admin(request)
    return _local_worker_payload()


@app.post('/api/v1/admin/tasks/{task_id}/pull-local')
def admin_pull_task_local(task_id: str, request: Request, payload: RequestPullPayload) -> dict:
    """Pull one task into the same PC that is running the local API.

    This is intentionally an admin-only local-development convenience. A cloud
    API cannot use this endpoint to write files to a user's home PC; that case
    still requires the separate Worker agent.
    """
    require_admin(request)
    validate_identifier(task_id, 'Task ID')
    if not LOCAL_PULL_ENABLED:
        raise HTTPException(status_code=409, detail='当前服务器未启用本机领取，请使用电脑端 Worker。')

    worker_id = payload.workerId.strip()
    claimed_at = now_ms()
    lease_token = secrets.token_urlsafe(32)
    try:
        with connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute(
                'SELECT status, claimed_by, requested_worker_id, lease_expires_at, session_id FROM processing_tasks WHERE id = ?',
                (task_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail='任务不存在')
            if row['status'] in {'CLAIMED', 'LOCAL_READY', 'PROCESSING'} and row['claimed_by'] == worker_id:
                if row['status'] == 'LOCAL_READY':
                    existing = connection.execute(
                        '''SELECT task.*, session.title, session.duration_ms, session.status AS session_status
                           FROM processing_tasks task JOIN sessions session ON session.id = task.session_id
                           WHERE task.id = ?''',
                        (task_id,),
                    ).fetchone()
                    return {'task': task_payload(existing), 'mode': 'local', 'localPath': f'worker-inbox/{task_id}/audio.webm', 'reused': True}
            if row['status'] not in {'READY', 'FAILED'}:
                raise HTTPException(status_code=409, detail=f'任务当前不能领取：{row["status"]}')
            if row['requested_worker_id'] and row['requested_worker_id'] != worker_id:
                raise HTTPException(status_code=409, detail='任务已指定其他 Worker')
            connection.execute(
                '''UPDATE processing_tasks SET status = 'CLAIMED', attempts = attempts + 1,
                   claimed_by = ?, claimed_at = ?, lease_token_hash = ?, lease_expires_at = NULL,
                   error_message = '', error_stage = '', updated_at = ? WHERE id = ?''',
                (worker_id, claimed_at, hash_secret(lease_token), claimed_at, task_id),
            )
            session_id = row['session_id']

        with connect() as connection:
            output_path = _reconstruct_completed_session(connection, session_id)
        task_dir = LOCAL_WORKER_INBOX / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        audio_path = task_dir / 'audio.webm'
        temporary_path = audio_path.with_suffix('.webm.part')
        digest = hashlib.sha256()
        size = 0
        with output_path.open('rb') as source, temporary_path.open('wb') as destination:
            while block := source.read(1024 * 1024):
                destination.write(block)
                digest.update(block)
                size += len(block)
        temporary_path.replace(audio_path)
        lease_path = task_dir / 'lease.json'
        lease_path.write_text(json.dumps({'workerId': worker_id, 'leaseToken': lease_token}, ensure_ascii=False, indent=2), encoding='utf-8')
        manifest = {
            'taskId': task_id,
            'sessionId': session_id,
            'workerId': worker_id,
            'source': 'local-api-pull',
            'sha256': digest.hexdigest(),
            'size': size,
            'downloadedAt': now_ms(),
        }
        (task_dir / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')

        with connect() as connection:
            row = connection.execute('SELECT status, claimed_by, lease_token_hash, lease_expires_at FROM processing_tasks WHERE id = ?', (task_id,)).fetchone()
            verify_task_lease(row, worker_id, lease_token)
            if row['status'] != 'CLAIMED':
                raise HTTPException(status_code=409, detail=f'任务状态已变化：{row["status"]}')
            connection.execute(
                'UPDATE processing_tasks SET status = \'LOCAL_READY\', downloaded_at = ?, updated_at = ? WHERE id = ?',
                (now_ms(), now_ms(), task_id),
            )
            task = connection.execute(
                '''SELECT task.*, session.title, session.duration_ms, session.status AS session_status
                   FROM processing_tasks task JOIN sessions session ON session.id = task.session_id
                   WHERE task.id = ?''',
                (task_id,),
            ).fetchone()
            _audit(connection, 'admin', 'LOCAL_PULL_TASK', task_id, {'workerId': worker_id, 'size': size})
        (task_dir / 'task.json').write_text(
            json.dumps(task_payload(task), ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        return {'task': task_payload(task), 'mode': 'local', 'localPath': f'worker-inbox/{task_id}/audio.webm', 'size': size, 'reused': False}
    except HTTPException:
        raise
    except Exception as error:
        with connect() as connection:
            connection.execute(
                '''UPDATE processing_tasks
                   SET status = 'FAILED', claimed_by = NULL, claimed_at = NULL,
                       requested_worker_id = NULL, lease_token_hash = NULL,
                       lease_expires_at = NULL, downloaded_at = NULL,
                       error_message = ?, error_stage = 'LOCAL_PULL', updated_at = ?
                   WHERE id = ? AND status = 'CLAIMED' AND claimed_by = ?''',
                (str(error), now_ms(), task_id, worker_id),
            )
        raise HTTPException(status_code=500, detail=f'本机领取失败：{error}') from error


@app.get('/api/v1/admin/tasks/{task_id}/results')
def admin_task_results(task_id: str, request: Request) -> dict:
    require_admin(request)
    validate_identifier(task_id, 'Task ID')
    with connect() as connection:
        task = connection.execute('SELECT id, session_id, status FROM processing_tasks WHERE id = ?', (task_id,)).fetchone()
        if task is None:
            raise HTTPException(status_code=404, detail='任务不存在')
        rows = connection.execute('SELECT id, task_id, session_id, version, content_json, created_at FROM result_revisions WHERE task_id = ? ORDER BY version DESC', (task_id,)).fetchall()
    return {
        'taskId': task['id'],
        'sessionId': task['session_id'],
        'status': task['status'],
        'items': [
            {'id': row['id'], 'taskId': row['task_id'], 'sessionId': row['session_id'], 'version': row['version'], 'createdAt': row['created_at'], 'result': json.loads(row['content_json'])}
            for row in rows
        ],
    }


@app.get('/api/v1/admin/tasks/{task_id}/transcript')
def admin_task_transcript(task_id: str, request: Request) -> dict:
    """Read a completed task transcript without advancing its processing state."""
    require_admin(request)
    validate_identifier(task_id, 'Task ID')
    with connect() as connection:
        task = connection.execute('SELECT id, session_id, status FROM processing_tasks WHERE id = ?', (task_id,)).fetchone()
        if task is None:
            raise HTTPException(status_code=404, detail='任务不存在')
        if task['status'] not in {'TRANSCRIBED', 'SUMMARIZING', 'REVIEW', 'COMPLETED'}:
            raise HTTPException(status_code=409, detail=f'识别文字尚未准备好：{task["status"]}')
        run = connection.execute(
            """SELECT id, generation, source_hash, model, language, status, completed_at
               FROM processing_runs WHERE task_id = ? AND status = 'COMPLETED' ORDER BY generation DESC LIMIT 1""",
            (task_id,),
        ).fetchone()
        if run is None:
            raise HTTPException(status_code=409, detail='没有完成的转写运行记录')
    transcript_path = DATA_DIR / 'processed' / 'sessions' / task['session_id'] / 'transcript.json'
    if not transcript_path.is_file():
        raise HTTPException(status_code=409, detail='逐字稿文件不存在')
    try:
        transcript = json.loads(transcript_path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=500, detail=f'读取逐字稿失败：{error}') from error
    return {'taskId': task['id'], 'sessionId': task['session_id'], 'status': task['status'], 'run': dict(run), 'transcript': transcript}


@app.get('/api/v1/admin/tasks/{task_id}/audio')
def admin_task_audio(task_id: str, request: Request) -> FileResponse:
    """Rebuild a completed task's recording for the admin review player."""
    require_admin(request)
    validate_identifier(task_id, 'Task ID')
    with connect() as connection:
        task = connection.execute('SELECT session_id FROM processing_tasks WHERE id = ?', (task_id,)).fetchone()
        if task is None:
            raise HTTPException(status_code=404, detail='任务不存在')
        session_id = task['session_id']
        try:
            output_path = _session_audio_for_playback(connection, session_id)
        except ReconstructionError as error:
            raise HTTPException(status_code=409, detail=f'整场音频暂不可用：{error}') from error
    return FileResponse(output_path, media_type='audio/webm', filename=f'{session_id}.webm')


@app.post('/api/v1/admin/tasks/{task_id}/start-processing')
def admin_start_processing(task_id: str, request: Request, payload: RequestPullPayload) -> dict:
    """Move a locally pulled task into manual processing from the console."""
    require_admin(request)
    validate_identifier(task_id, 'Task ID')
    worker_id = payload.workerId.strip()
    with connect() as connection:
        task = connection.execute('SELECT status, claimed_by FROM processing_tasks WHERE id = ?', (task_id,)).fetchone()
        if task is None:
            raise HTTPException(status_code=404, detail='任务不存在')
        if task['status'] == 'PROCESSING':
            pass
        elif task['status'] == 'LOCAL_READY' and task['claimed_by'] == worker_id:
            connection.execute(
                "UPDATE processing_tasks SET status = 'PROCESSING', error_message = '', error_stage = 'MANUAL', updated_at = ? WHERE id = ?",
                (now_ms(), task_id),
            )
            _audit(connection, 'admin', 'START_MANUAL_PROCESSING', task_id, {'workerId': worker_id})
        else:
            raise HTTPException(status_code=409, detail=f'任务当前不能开始处理：{task["status"]}')
        row = connection.execute(
            '''SELECT task.*, session.title, session.duration_ms, session.status AS session_status
               FROM processing_tasks task JOIN sessions session ON session.id = task.session_id
               WHERE task.id = ?''',
            (task_id,),
        ).fetchone()
    return {'task': task_payload(row)}


@app.post('/api/v1/admin/tasks/{task_id}/auto-process')
def admin_auto_process(task_id: str, request: Request, payload: RequestPullPayload) -> dict:
    """Start the built-in ASR and knowledge-draft pipeline for a local task."""
    require_admin(request)
    validate_identifier(task_id, 'Task ID')
    worker_id = payload.workerId.strip()
    if STORAGE_ONLY_MODE:
        with connect() as connection:
            row = connection.execute(
                '''SELECT task.*, session.title, session.duration_ms, session.status AS session_status,
                          user.display_name AS owner_name
                   FROM processing_tasks task
                   JOIN sessions session ON session.id = task.session_id
                   LEFT JOIN users user ON user.id = session.owner_id
                   WHERE task.id = ?''',
                (task_id,),
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail='任务不存在')
        return {'task': task_payload(row), 'job': None, 'deferred': False, 'processingMode': 'storage'}
    deferred = False
    with connect() as connection:
        task = connection.execute(
            'SELECT status, claimed_by, session_id FROM processing_tasks WHERE id = ?',
            (task_id,),
        ).fetchone()
        if task is None:
            raise HTTPException(status_code=404, detail='任务不存在')
        if task['status'] == 'REVIEW':
            pass
        elif task['status'] == 'PROCESSING' and task['claimed_by'] == worker_id:
            if live_processing_has_pending_windows(connection, task['session_id']):
                connection.execute(
                    "UPDATE processing_tasks SET error_message = '等待实时识别窗口收尾后自动继续。', error_stage = 'LIVE_DRAIN', updated_at = ? WHERE id = ?",
                    (now_ms(), task_id),
                )
                deferred = True
            else:
                connection.execute(
                    "UPDATE processing_tasks SET error_message = '', error_stage = 'AUTO_PROCESS', updated_at = ? WHERE id = ?",
                    (now_ms(), task_id),
                )
        elif task['status'] == 'LOCAL_READY' and task['claimed_by'] == worker_id:
            if live_processing_has_pending_windows(connection, task['session_id']):
                connection.execute(
                    """UPDATE processing_tasks SET status = 'PROCESSING', error_message = '等待实时识别窗口收尾后自动继续。',
                       error_stage = 'LIVE_DRAIN', updated_at = ? WHERE id = ?""",
                    (now_ms(), task_id),
                )
                deferred = True
            else:
                connection.execute(
                    """UPDATE processing_tasks SET status = 'PROCESSING', error_message = '',
                       error_stage = 'AUTO_PROCESS', updated_at = ? WHERE id = ?""",
                    (now_ms(), task_id),
                )
        else:
            raise HTTPException(status_code=409, detail=f'任务当前不能自动处理：{task["status"]}')

    job = None
    if task['status'] != 'REVIEW' and not deferred:
        try:
            job = create_job(DATA_DIR, DB_PATH, task['session_id'], DEFAULT_MODEL, 'zh', task_id=task_id)
        except Exception as error:
            with connect() as connection:
                connection.execute(
                    """UPDATE processing_tasks SET status = 'FAILED', error_message = ?,
                       error_stage = 'AUTO_PROCESS', updated_at = ? WHERE id = ?""",
                    (str(error), now_ms(), task_id),
                )
            raise HTTPException(status_code=500, detail=f'自动处理启动失败：{error}') from error

    if deferred:
        LIVE_PROCESSING_WORKER.wake(task['session_id'])

    with connect() as connection:
        row = connection.execute(
            '''SELECT task.*, session.title, session.duration_ms, session.status AS session_status,
                      user.display_name AS owner_name
               FROM processing_tasks task
               JOIN sessions session ON session.id = task.session_id
               LEFT JOIN users user ON user.id = session.owner_id
               WHERE task.id = ?''',
            (task_id,),
        ).fetchone()
    return {'task': task_payload(row), 'job': job, 'deferred': deferred}


@app.post('/api/v1/admin/tasks/{task_id}/result')
def admin_upload_task_result(task_id: str, request: Request, payload: KnowledgeResultPayload) -> dict:
    """Accept a manually prepared knowledge.json without requiring a CLI."""
    require_admin(request)
    validate_identifier(task_id, 'Task ID')
    return _store_admin_task_result(task_id, payload, 'UPLOAD_MANUAL_RESULT')


def _store_admin_task_result(task_id: str, payload: KnowledgeResultPayload, audit_action: str) -> dict:
    with connect() as connection:
        task = connection.execute('SELECT session_id, status FROM processing_tasks WHERE id = ?', (task_id,)).fetchone()
        if task is None:
            raise HTTPException(status_code=404, detail='任务不存在')
        if task['status'] not in {'PROCESSING', 'LOCAL_READY', 'TRANSCRIBED', 'SUMMARIZING', 'REVIEW'}:
            raise HTTPException(status_code=409, detail=f'任务当前不能回传结果：{task["status"]}')
        session_id = task['session_id']
        updated_at = now_ms()
        document = payload.result.model_dump(exclude_none=True)
        content_json = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        content_hash = hashlib.sha256(content_json.encode('utf-8')).hexdigest()
        existing = connection.execute(
            'SELECT id, version FROM result_revisions WHERE task_id = ? AND content_hash = ?',
            (task_id, content_hash),
        ).fetchone()
        if existing is None:
            latest = connection.execute(
                'SELECT COALESCE(MAX(version), 0) AS version FROM result_revisions WHERE task_id = ?',
                (task_id,),
            ).fetchone()['version']
            revision_id = new_id('revision')
            version = int(latest) + 1
            connection.execute(
                'INSERT INTO result_revisions(id, task_id, session_id, version, content_hash, content_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (revision_id, task_id, session_id, version, content_hash, content_json, updated_at),
            )
        else:
            revision_id = existing['id']
            version = existing['version']
        connection.execute(
            '''UPDATE processing_tasks SET status = 'REVIEW', result_version = ?,
               claimed_by = NULL, claimed_at = NULL, requested_worker_id = NULL,
               lease_token_hash = NULL, lease_expires_at = NULL,
               error_message = '', error_stage = '', updated_at = ? WHERE id = ?''',
            (version, updated_at, task_id),
        )
        _audit(connection, 'admin', audit_action, task_id, {'revisionId': revision_id, 'version': version})
    return {'ok': True, 'taskId': task_id, 'sessionId': session_id, 'revisionId': revision_id, 'version': version, 'status': 'REVIEW'}


@app.get('/api/v1/tasks/{task_id}/summary-input')
def get_summary_input(task_id: str, request: Request) -> dict:
    """Expose the durable transcript to the current Codex/local worker."""
    validate_identifier(task_id, 'Task ID')
    authorize_worker(request)
    with connect() as connection:
        task = connection.execute(
            '''SELECT task.id, task.session_id, task.status, task.created_at, task.updated_at,
                      session.title, session.owner_id, session.started_at, session.duration_ms,
                      user.display_name AS owner_name
               FROM processing_tasks task
               JOIN sessions session ON session.id = task.session_id
               LEFT JOIN users user ON user.id = session.owner_id
               WHERE task.id = ?''',
            (task_id,),
        ).fetchone()
        if task is None:
            raise HTTPException(status_code=404, detail='任务不存在')
        if task['status'] not in {'TRANSCRIBED', 'SUMMARIZING', 'REVIEW', 'COMPLETED'}:
            raise HTTPException(status_code=409, detail=f'逐字稿尚未准备好：{task["status"]}')
        run = connection.execute(
            """SELECT id, generation, source_hash, model, language, status, completed_at
               FROM processing_runs WHERE task_id = ? AND status = 'COMPLETED' ORDER BY generation DESC LIMIT 1""",
            (task_id,),
        ).fetchone()
        if run is None:
            raise HTTPException(status_code=409, detail='没有完成的转写运行记录')
        response_status = task['status']
        if task['status'] == 'TRANSCRIBED':
            connection.execute("UPDATE processing_tasks SET status = 'SUMMARIZING', error_stage = 'SUMMARY', updated_at = ? WHERE id = ?", (now_ms(), task_id))
            response_status = 'SUMMARIZING'
    transcript_path = DATA_DIR / 'processed' / 'sessions' / task['session_id'] / 'transcript.json'
    if not transcript_path.is_file():
        raise HTTPException(status_code=409, detail='逐字稿文件不存在')
    try:
        transcript = json.loads(transcript_path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=500, detail=f'读取逐字稿失败：{error}') from error
    return {
        'taskId': task_id,
        'sessionId': task['session_id'],
        'title': task['title'],
        'ownerId': task['owner_id'],
        'ownerName': task['owner_name'],
        'createdAt': task['created_at'],
        'startedAt': task['started_at'],
        'durationMs': task['duration_ms'],
        'status': response_status,
        'run': dict(run),
        'transcript': transcript,
    }


@app.get('/api/v1/tasks/{task_id}/summary-result')
def get_summary_result(task_id: str, request: Request) -> dict:
    """Read the latest generated draft for the local dashboard.

    This is intentionally Worker-authenticated: the local Worker already has
    permission to read the task transcript and submit this exact task's draft.
    It does not publish or change the review state.
    """
    validate_identifier(task_id, 'Task ID')
    authorize_worker(request)
    with connect() as connection:
        task = connection.execute(
            'SELECT id, session_id, status FROM processing_tasks WHERE id = ?',
            (task_id,),
        ).fetchone()
        if task is None:
            raise HTTPException(status_code=404, detail='任务不存在')
        rows = connection.execute(
            'SELECT id, task_id, session_id, version, content_json, created_at FROM result_revisions WHERE task_id = ? ORDER BY version DESC',
            (task_id,),
        ).fetchall()
    return {
        'taskId': task['id'],
        'sessionId': task['session_id'],
        'status': task['status'],
        'items': [
            {
                'id': row['id'],
                'taskId': row['task_id'],
                'sessionId': row['session_id'],
                'version': row['version'],
                'createdAt': row['created_at'],
                'result': json.loads(row['content_json']),
            }
            for row in rows
        ],
    }


@app.post('/api/v1/tasks/{task_id}/summary-result')
def submit_summary_result(task_id: str, payload: SummaryResultPayload, request: Request) -> dict:
    """Accept a Codex-generated summary only for the exact transcript run read."""
    validate_identifier(task_id, 'Task ID')
    authorize_worker(request)
    with connect() as connection:
        task = connection.execute('SELECT status FROM processing_tasks WHERE id = ?', (task_id,)).fetchone()
        run = connection.execute(
            """SELECT id, generation, source_hash, status FROM processing_runs
               WHERE id = ? AND task_id = ?""",
            (payload.runId, task_id),
        ).fetchone()
        if task is None:
            raise HTTPException(status_code=404, detail='任务不存在')
        if task['status'] not in {'TRANSCRIBED', 'SUMMARIZING'}:
            raise HTTPException(status_code=409, detail=f'任务当前不能提交总结：{task["status"]}')
        if run is None or run['status'] != 'COMPLETED' or int(run['generation']) != payload.generation or run['source_hash'] != payload.sourceHash:
            raise HTTPException(status_code=409, detail='总结所依据的逐字稿版本已变化，请重新读取。')
    return _store_admin_task_result(task_id, KnowledgeResultPayload(version=1, result=payload.result), 'SUBMIT_CODEX_SUMMARY')


@app.post('/api/v1/tasks/{task_id}/summary-failed')
def report_summary_failure(task_id: str, payload: SummaryFailurePayload, request: Request) -> dict:
    """Make a failed local summary visible and retryable instead of leaving it stuck."""
    validate_identifier(task_id, 'Task ID')
    authorize_worker(request)
    message = payload.errorMessage.strip() or '本地 Codex 总结失败。'
    with connect() as connection:
        task = connection.execute('SELECT status FROM processing_tasks WHERE id = ?', (task_id,)).fetchone()
        if task is None:
            raise HTTPException(status_code=404, detail='任务不存在')
        if task['status'] not in {'TRANSCRIBED', 'SUMMARIZING'}:
            raise HTTPException(status_code=409, detail=f'任务当前不能标记总结失败：{task["status"]}')
        updated_at = now_ms()
        connection.execute(
            """UPDATE processing_tasks
               SET status = 'FAILED', error_message = ?, error_stage = 'SUMMARY', updated_at = ?
               WHERE id = ?""",
            (message, updated_at, task_id),
        )
        row = _worker_task_row(connection, task_id)
    return {'ok': True, 'task': task_payload(row)}


@app.post('/api/v1/admin/tasks/{task_id}/result-local')
def admin_upload_local_task_result(task_id: str, request: Request) -> dict:
    """Read knowledge.json from the local task inbox and submit it without a file picker."""
    require_admin(request)
    validate_identifier(task_id, 'Task ID')
    if not LOCAL_PULL_ENABLED:
        raise HTTPException(status_code=409, detail='当前服务器未启用本机任务目录。')

    inbox_root = LOCAL_WORKER_INBOX.resolve()
    task_directory = (inbox_root / task_id).resolve()
    if inbox_root not in task_directory.parents:
        raise HTTPException(status_code=400, detail='任务目录无效')
    result_path = task_directory / 'knowledge.json'
    if not result_path.is_file():
        raise HTTPException(status_code=404, detail=f'任务目录中没有找到 knowledge.json：{result_path}')
    if result_path.stat().st_size > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail='knowledge.json 超过 5 MB 限制')
    try:
        document = json.loads(result_path.read_text(encoding='utf-8'))
        payload = KnowledgeResultPayload.model_validate(document)
    except (OSError, UnicodeError) as error:
        raise HTTPException(status_code=400, detail=f'读取 knowledge.json 失败：{error}') from error
    except (json.JSONDecodeError, ValidationError) as error:
        raise HTTPException(status_code=422, detail=f'knowledge.json 格式不符合要求：{error}') from error
    return _store_admin_task_result(task_id, payload, 'UPLOAD_LOCAL_RESULT')


@app.post('/api/v1/admin/tasks/{task_id}/release')
def admin_release_task(task_id: str, request: Request) -> dict:
    require_admin(request)
    validate_identifier(task_id, 'Task ID')
    with connect() as connection:
        result = connection.execute(
            """UPDATE processing_tasks
               SET status = 'READY', claimed_by = NULL, claimed_at = NULL,
                   requested_worker_id = NULL, lease_token_hash = NULL,
                   lease_expires_at = NULL, downloaded_at = NULL,
                   error_message = '', error_stage = '', updated_at = ?
               WHERE id = ? AND status IN ('CLAIMED', 'LOCAL_READY', 'PROCESSING', 'FAILED')""",
            (now_ms(), task_id),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=409, detail='任务不存在或当前不能释放')
        _audit(connection, 'admin', 'RELEASE_TASK', task_id, {})
    return {'ok': True, 'taskId': task_id, 'status': 'READY'}


@app.post('/api/v1/admin/tasks/{task_id}/retry')
def admin_retry_task(task_id: str, request: Request) -> dict:
    require_admin(request)
    validate_identifier(task_id, 'Task ID')
    with connect() as connection:
        task = connection.execute('SELECT status, error_stage FROM processing_tasks WHERE id = ?', (task_id,)).fetchone()
        if task is None or task['status'] != 'FAILED':
            raise HTTPException(status_code=409, detail='只有失败任务可以重试')
        updated_at = now_ms()
        summary_retry = task['error_stage'] == 'SUMMARY'
        if summary_retry:
            result = connection.execute(
                """UPDATE processing_tasks
                   SET status = 'TRANSCRIBED', error_message = '', error_stage = '',
                       claimed_by = NULL, claimed_at = NULL,
                       requested_worker_id = NULL, lease_token_hash = NULL,
                       lease_expires_at = NULL, updated_at = ?
                   WHERE id = ? AND status = 'FAILED'""",
                (updated_at, task_id),
            )
        else:
            result = connection.execute(
                """UPDATE processing_tasks
                   SET status = 'READY', error_message = '', error_stage = '',
                       claimed_by = NULL, claimed_at = NULL,
                       requested_worker_id = NULL, lease_token_hash = NULL,
                       lease_expires_at = NULL, downloaded_at = NULL, updated_at = ?
                   WHERE id = ? AND status = 'FAILED'""",
                (updated_at, task_id),
            )
        if result.rowcount == 0:
            raise HTTPException(status_code=409, detail='只有失败任务可以重试')
        _audit(connection, 'admin', 'RETRY_TASK', task_id, {'stage': 'SUMMARY' if summary_retry else 'PROCESSING'})
    return {'ok': True, 'taskId': task_id, 'status': 'TRANSCRIBED' if summary_retry else 'READY'}


@app.post('/api/v1/admin/devices/{device_id}/revoke')
def admin_revoke_device(device_id: str, request: Request) -> dict:
    require_admin(request)
    validate_identifier(device_id, 'Device ID')
    with connect() as connection:
        result = connection.execute('UPDATE devices SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ?', (now_ms(), device_id))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail='设备不存在')
        _audit(connection, 'admin', 'REVOKE_DEVICE', device_id, {})
    return {'ok': True, 'deviceId': device_id}


@app.post('/api/v1/diagnostics')
async def upload_diagnostic(
    request: Request,
    description: str = Form(default=''),
    snapshot: UploadFile = File(...),
    attachment: UploadFile | None = File(default=None),
) -> dict:
    if RUNTIME_ENV in {'production', 'prod'}:
        if bearer_token(request):
            with connect() as connection:
                authenticate_device(connection, request)
        else:
            require_admin(request)
    diagnostic_id = f'diag-{uuid.uuid4()}'
    diagnostic_dir = DATA_DIR / 'diagnostics' / diagnostic_id
    diagnostic_dir.mkdir(parents=True, exist_ok=False)
    files: list[dict[str, str | int]] = []
    try:
        snapshot_path = diagnostic_dir / 'snapshot.json'
        snapshot_size = await save_diagnostic_upload(snapshot, snapshot_path)
        files.append({'name': snapshot_path.name, 'size': snapshot_size, 'contentType': snapshot.content_type or 'application/json'})

        if attachment is not None:
            suffix = Path(attachment.filename or '').suffix.lower()
            allowed_suffixes = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.log', '.txt', '.json', '.har', '.csv'}
            if not (attachment.content_type and attachment.content_type.startswith('image/')) and suffix not in allowed_suffixes:
                raise HTTPException(status_code=415, detail='只支持图片或日志、文本、JSON 文件')
            attachment_path = diagnostic_dir / f'attachment{suffix or ".bin"}'
            attachment_size = await save_diagnostic_upload(attachment, attachment_path)
            files.append({'name': attachment_path.name, 'size': attachment_size, 'contentType': attachment.content_type or 'application/octet-stream'})

        metadata = {
            'id': diagnostic_id,
            'createdAt': now_ms(),
            'description': description[:2000],
            'files': files,
        }
        (diagnostic_dir / 'metadata.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
        return {'ok': True, 'diagnosticId': diagnostic_id, 'files': [str(item['name']) for item in files]}
    except Exception:
        shutil.rmtree(diagnostic_dir, ignore_errors=True)
        raise


@app.post('/api/v1/sessions')
def create_session(payload: SessionPayload, request: Request = None) -> dict:
    validate_identifier(payload.id, 'Session ID')
    with connect() as connection:
        if REQUIRE_DEVICE_AUTH and not bearer_token(request):
            raise HTTPException(status_code=401, detail='请先完成手机配对后再录音')
        deleted = connection.execute(
            'SELECT session_id FROM deleted_sessions WHERE session_id = ?',
            (payload.id,),
        ).fetchone()
        if deleted is not None:
            raise HTTPException(status_code=409, detail='Session 已删除，拒绝延迟上传重新创建')
        # A retry from a paired device may upsert its own Session, but it must
        # never be able to overwrite a Session belonging to another user.
        existing = connection.execute('SELECT id FROM sessions WHERE id = ?', (payload.id,)).fetchone()
        if existing is not None:
            session_access(connection, payload.id, request)
        owner_id = None
        device_id = None
        if request is not None and bearer_token(request):
            device = authenticate_device(connection, request)
            owner_id = device['user_id']
            device_id = device['id']
        server_status = 'FINALIZING' if payload.endedAt is not None else 'RECORDING'
        connection.execute(
            '''INSERT INTO sessions(id, title, started_at, ended_at, status, duration_ms, created_at, updated_at, owner_id, device_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET title=excluded.title, ended_at=excluded.ended_at,
               status=CASE WHEN sessions.status = 'COMPLETED' THEN sessions.status ELSE excluded.status END,
               duration_ms=excluded.duration_ms, updated_at=excluded.updated_at,
               owner_id=COALESCE(excluded.owner_id, sessions.owner_id), device_id=COALESCE(excluded.device_id, sessions.device_id)''',
            # Upload metadata is not completion proof. The queue calls this
            # endpoint before sending every Chunk; complete_session is the
            # only endpoint allowed to mark a Session completed.
            (payload.id, payload.title, payload.startedAt, payload.endedAt, server_status, payload.durationMs, payload.createdAt, payload.updatedAt, owner_id, device_id),
        )
    return {'id': payload.id, 'created': True}


@app.post('/api/v1/sessions/{session_id}/segments')
def create_segment(session_id: str, payload: SegmentPayload, request: Request = None) -> dict:
    validate_identifier(session_id, 'Session ID')
    validate_identifier(payload.id, 'Segment ID')
    if payload.sessionId != session_id:
        raise HTTPException(status_code=400, detail='sessionId 不匹配')
    with connect() as connection:
        session_access(connection, session_id, request)
        existing_index = connection.execute(
            'SELECT id FROM segments WHERE session_id = ? AND segment_index = ?',
            (session_id, payload.index),
        ).fetchone()
        if existing_index is not None and existing_index['id'] != payload.id:
            raise HTTPException(status_code=409, detail=f'Segment index 已存在：{payload.index}')
        connection.execute(
            '''INSERT INTO segments(id, session_id, segment_index, started_at, start_elapsed_ms, ended_at, mime_type,
               media_settings, status, duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET ended_at=excluded.ended_at, mime_type=excluded.mime_type,
               media_settings=excluded.media_settings,
               status=CASE WHEN segments.status = 'COMPLETED' THEN segments.status ELSE excluded.status END,
               duration_ms=excluded.duration_ms,
               start_elapsed_ms=excluded.start_elapsed_ms''',
            (payload.id, session_id, payload.index, payload.startedAt, payload.startElapsedMs, payload.endedAt, payload.mimeType, json.dumps(payload.mediaSettings, ensure_ascii=False), 'RECORDING', payload.durationMs),
        )
    return {'id': payload.id, 'created': True}


@app.put('/api/v1/sessions/{session_id}/segments/{segment_id}/chunks/{chunk_index}')
async def upload_chunk(
    session_id: str,
    segment_id: str,
    chunk_index: int,
    request: Request,
    x_chunk_sha256: str | None = Header(default=None),
    x_chunk_size: int | None = Header(default=None),
    x_chunk_elapsed_ms: int | None = Header(default=None),
) -> dict:
    validate_identifier(session_id, 'Session ID')
    validate_identifier(segment_id, 'Segment ID')
    if chunk_index < 0:
        raise HTTPException(status_code=400, detail='Chunk index 无效')
    if not x_chunk_sha256 or x_chunk_size is None or x_chunk_elapsed_ms is None:
        raise HTTPException(status_code=400, detail='缺少 Chunk 校验 Header')
    if x_chunk_size <= 0 or x_chunk_size > MAX_CHUNK_BYTES:
        raise HTTPException(status_code=413, detail=f'Chunk 超出大小限制（最大 {MAX_CHUNK_BYTES} bytes）')
    content_length = request.headers.get('content-length')
    if content_length and int(content_length) > MAX_CHUNK_BYTES:
        raise HTTPException(status_code=413, detail=f'Chunk 超出大小限制（最大 {MAX_CHUNK_BYTES} bytes）')
    if x_chunk_elapsed_ms < 0:
        raise HTTPException(status_code=400, detail='Chunk elapsedMs 无效')

    with connect() as connection:
        session = session_access(connection, session_id, request)
        segment = connection.execute('SELECT segment_index FROM segments WHERE id = ? AND session_id = ?', (segment_id, session_id)).fetchone()
        if session is None or segment is None:
            raise HTTPException(status_code=404, detail='Session 或 Segment 不存在')

        hasher = hashlib.sha256()
        body_size = 0
        relative_path = Path('sessions') / session_id / f"segment_{segment['segment_index']}" / 'chunks' / f'{chunk_index:06d}.bin'
        absolute_path = DATA_DIR / relative_path
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = absolute_path.with_name(f'.{absolute_path.name}.{uuid.uuid4().hex}.tmp')
        installed_path = False
        try:
            with temporary_path.open('wb') as output:
                async for part in request.stream():
                    body_size += len(part)
                    if body_size > MAX_CHUNK_BYTES:
                        raise HTTPException(status_code=413, detail=f'Chunk 超出大小限制（最大 {MAX_CHUNK_BYTES} bytes）')
                    hasher.update(part)
                    output.write(part)
            if body_size != x_chunk_size:
                raise HTTPException(status_code=400, detail='Chunk size 不一致')
            actual_sha256 = hasher.hexdigest()
            if actual_sha256 != x_chunk_sha256:
                raise HTTPException(status_code=400, detail='Chunk SHA256 不一致')

            # Do not hold a SQLite write lock while streaming the request body.
            # Lock only the short check/install/insert section so two retries
            # for the same Segment/Chunk remain genuinely idempotent.
            connection.execute('BEGIN IMMEDIATE')
            existing = connection.execute('SELECT size, sha256 FROM chunks WHERE segment_id = ? AND chunk_index = ?', (segment_id, chunk_index)).fetchone()
            if existing is not None:
                if existing['size'] == body_size and existing['sha256'] == actual_sha256:
                    temporary_path.unlink(missing_ok=True)
                    ensure_live_run(connection, session_id)
                    LIVE_PROCESSING_WORKER.wake(session_id)
                    return {'ok': True, 'already_exists': True, 'verified': True}
                raise HTTPException(status_code=409, detail='相同 Segment/Chunk index 的 SHA256 不一致')

            temporary_path.replace(absolute_path)
            installed_path = True
            received_at = now_ms()
            mime_type = request.headers.get('content-type', 'application/octet-stream')
            connection.execute(
                '''INSERT INTO chunks(session_id, segment_id, chunk_index, size, sha256, mime_type,
                   elapsed_ms, created_at, received_at, local_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (session_id, segment_id, chunk_index, body_size, actual_sha256, mime_type, x_chunk_elapsed_ms, received_at, received_at, str(relative_path)),
            )
            ensure_live_run(connection, session_id)
        except Exception:
            if temporary_path.exists(): temporary_path.unlink()
            if installed_path: absolute_path.unlink(missing_ok=True)
            raise
    LIVE_PROCESSING_WORKER.wake(session_id)
    return {'ok': True, 'already_exists': False, 'verified': True}


@app.post('/api/v1/sessions/{session_id}/segments/{segment_id}/complete')
def complete_segment(session_id: str, segment_id: str, payload: CompletionPayload | None = None, request: Request = None) -> dict:
    validate_identifier(session_id, 'Session ID')
    validate_identifier(segment_id, 'Segment ID')
    with connect() as connection:
        session_access(connection, session_id, request)
        segment = connection.execute('SELECT id FROM segments WHERE id = ? AND session_id = ?', (segment_id, session_id)).fetchone()
        if segment is None:
            raise HTTPException(status_code=404, detail='Segment 不存在')
        indexes = [row['chunk_index'] for row in connection.execute('SELECT chunk_index FROM chunks WHERE segment_id = ? ORDER BY chunk_index', (segment_id,)).fetchall()]
        if not indexes:
            raise HTTPException(status_code=409, detail='Segment 没有已上传的 Chunk')
        expected = payload.expectedChunkCount if payload else None
        if indexes != list(range(len(indexes))):
            raise HTTPException(status_code=409, detail=f'Segment Chunk index 不连续：{indexes}')
        if expected is not None and len(indexes) != expected:
            raise HTTPException(status_code=409, detail=f'Segment Chunk 不完整：收到 {len(indexes)}，预期 {expected}')
        result = connection.execute('UPDATE segments SET status = ?, ended_at = COALESCE(ended_at, ?) WHERE id = ? AND session_id = ?', ('COMPLETED', now_ms(), segment_id, session_id))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail='Segment 不存在')
        ensure_live_run(connection, session_id)
    LIVE_PROCESSING_WORKER.wake(session_id)
    return {'ok': True}


@app.post('/api/v1/sessions/{session_id}/complete')
def complete_session(session_id: str, payload: CompletionPayload | None = None, request: Request = None) -> dict:
    validate_identifier(session_id, 'Session ID')
    with connect() as connection:
        session_access(connection, session_id, request)
        session = connection.execute('SELECT status, duration_ms FROM sessions WHERE id = ?', (session_id,)).fetchone()
        if session is None:
            raise HTTPException(status_code=404, detail='Session 不存在')
        segments = connection.execute('SELECT id, status, segment_index, ended_at FROM segments WHERE session_id = ? ORDER BY segment_index', (session_id,)).fetchall()
        if not segments:
            raise HTTPException(status_code=409, detail='Session 仍有未完成的 Segment')
        # A phone can recover a session after a page lifecycle interruption.
        # In that case an older segment may have a definitive ended_at and a
        # complete, contiguous chunk set, but retain RECORDING on the server
        # because the original completion request never arrived. Treat that
        # segment as closed while finalizing the session; a segment without an
        # end time is still an active recording and must remain a hard error.
        for segment in segments:
            if segment['status'] == 'COMPLETED':
                continue
            if segment['ended_at'] is None:
                raise HTTPException(status_code=409, detail='Session 仍有未完成的 Segment')
            indexes = [row['chunk_index'] for row in connection.execute(
                'SELECT chunk_index FROM chunks WHERE segment_id = ? ORDER BY chunk_index',
                (segment['id'],),
            ).fetchall()]
            if not indexes or indexes != list(range(len(indexes))):
                raise HTTPException(status_code=409, detail=f'Segment Chunk 不完整：{segment["segment_index"]}')
            connection.execute('UPDATE segments SET status = ? WHERE id = ?', ('COMPLETED', segment['id']))
        segments = connection.execute('SELECT status, segment_index FROM segments WHERE session_id = ? ORDER BY segment_index', (session_id,)).fetchall()
        if any(segment['status'] != 'COMPLETED' for segment in segments):
            raise HTTPException(status_code=409, detail='Session 仍有未完成的 Segment')
        segment_indexes = [segment['segment_index'] for segment in segments]
        if segment_indexes != list(range(1, len(segment_indexes) + 1)):
            raise HTTPException(status_code=409, detail=f'Session Segment index 不连续：{segment_indexes}')
        actual_count = connection.execute('SELECT COUNT(*) AS count FROM chunks WHERE session_id = ?', (session_id,)).fetchone()['count']
        expected = payload.expectedChunkCount if payload else None
        if expected is not None and actual_count != expected:
            raise HTTPException(status_code=409, detail=f'Session Chunk 不完整：收到 {actual_count}，预期 {expected}')
        last_elapsed = connection.execute('SELECT MAX(elapsed_ms) AS elapsed_ms FROM chunks WHERE session_id = ?', (session_id,)).fetchone()['elapsed_ms']
        if last_elapsed is None or int(session['duration_ms'] or 0) > int(last_elapsed) + UPLOAD_DURATION_TOLERANCE_MS:
            raise HTTPException(status_code=409, detail='Session 音频时长与已上传 Chunk 不一致，请等待补传完成后再收尾')
        completed_at = now_ms()
        result = connection.execute('UPDATE sessions SET status = ?, ended_at = COALESCE(ended_at, ?), updated_at = ? WHERE id = ?', ('COMPLETED', completed_at, completed_at, session_id))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail='Session 不存在')
        create_processing_task(connection, session_id, completed_at)
        ensure_live_run(connection, session_id)
    LIVE_PROCESSING_WORKER.wake(session_id)
    return {'ok': True}


@app.get('/api/v1/tasks')
def list_processing_tasks(request: Request, status: str = 'READY', limit: int = 50) -> dict:
    authorize_worker(request)
    normalized_status = status.upper().strip()
    if normalized_status != 'ALL' and normalized_status not in TASK_STATUSES:
        raise HTTPException(status_code=400, detail=f'任务状态无效：{status}')
    limit = max(1, min(limit, 200))
    with connect() as connection:
        where = '' if normalized_status == 'ALL' else 'WHERE task.status = ?'
        parameters: tuple[Any, ...] = () if normalized_status == 'ALL' else (normalized_status,)
        rows = connection.execute(
            f'''SELECT task.*, session.title, session.duration_ms, session.status AS session_status,
                       session.owner_id AS owner_id, session.started_at AS started_at,
                       user.display_name AS owner_name
                FROM processing_tasks task JOIN sessions session ON session.id = task.session_id
                LEFT JOIN users user ON user.id = session.owner_id
                {where} ORDER BY task.created_at ASC LIMIT ?''',
            (*parameters, limit),
        ).fetchall()
    return {'tasks': [task_payload(row) for row in rows]}


@app.get('/api/v1/tasks/{task_id}')
def get_processing_task(task_id: str, request: Request) -> dict:
    validate_identifier(task_id, 'Task ID')
    authorize_worker(request)
    with connect() as connection:
        row = _worker_task_row(connection, task_id)
    if row is None:
        raise HTTPException(status_code=404, detail='任务不存在')
    return {'task': task_payload(row)}


def _worker_task_row(connection: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = connection.execute(
        '''SELECT task.*, session.title, session.duration_ms, session.status AS session_status,
                  session.owner_id AS owner_id, session.started_at AS started_at,
                  user.display_name AS owner_name
           FROM processing_tasks task JOIN sessions session ON session.id = task.session_id
           LEFT JOIN users user ON user.id = session.owner_id
           WHERE task.id = ?''',
        (task_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail='任务不存在')
    return row


@app.get('/api/v1/tasks/{task_id}/manifest')
def get_worker_task_manifest(task_id: str, request: Request = None) -> dict:
    """Return raw Chunk metadata for a remote PC worker.

    This endpoint deliberately does not reconstruct media. The ECS storage
    process can therefore run without FFmpeg, Whisper, or PyTorch.
    """
    validate_identifier(task_id, 'Task ID')
    authorize_worker(request)
    worker_id = request.headers.get('x-worker-id') if request else None
    lease_token = request.headers.get('x-task-lease') if request else None
    with connect() as connection:
        task = _worker_task_row(connection, task_id)
        verify_task_lease(task, worker_id, lease_token)
        segments = connection.execute(
            '''SELECT id, segment_index, started_at, ended_at, mime_type, duration_ms, start_elapsed_ms
               FROM segments WHERE session_id = ? ORDER BY segment_index''',
            (task['session_id'],),
        ).fetchall()
        manifest_segments: list[dict[str, Any]] = []
        for segment in segments:
            chunks = connection.execute(
                '''SELECT chunk_index, size, sha256, mime_type, elapsed_ms
                   FROM chunks WHERE segment_id = ? ORDER BY chunk_index''',
                (segment['id'],),
            ).fetchall()
            manifest_segments.append({
                'id': segment['id'],
                'index': segment['segment_index'],
                'startedAt': segment['started_at'],
                'endedAt': segment['ended_at'],
                'mimeType': segment['mime_type'],
                'durationMs': segment['duration_ms'],
                'startElapsedMs': segment['start_elapsed_ms'],
                'chunks': [
                    {
                        'index': chunk['chunk_index'],
                        'size': chunk['size'],
                        'sha256': chunk['sha256'],
                        'mimeType': chunk['mime_type'],
                        'elapsedMs': chunk['elapsed_ms'],
                        'downloadPath': f'/tasks/{task_id}/chunks/{segment["id"]}/{chunk["chunk_index"]}',
                    }
                    for chunk in chunks
                ],
            })
    return {
        'task': task_payload(task),
        'sessionId': task['session_id'],
        'processingMode': PROCESSING_MODE,
        'segments': manifest_segments,
    }


@app.get('/api/v1/tasks/{task_id}/chunks/{segment_id}/{chunk_index}')
def get_worker_chunk(task_id: str, segment_id: str, chunk_index: int, request: Request = None) -> FileResponse:
    """Stream one original Chunk to a leased remote worker."""
    validate_identifier(task_id, 'Task ID')
    validate_identifier(segment_id, 'Segment ID')
    if chunk_index < 0:
        raise HTTPException(status_code=400, detail='Chunk index 无效')
    authorize_worker(request)
    worker_id = request.headers.get('x-worker-id') if request else None
    lease_token = request.headers.get('x-task-lease') if request else None
    with connect() as connection:
        task = _worker_task_row(connection, task_id)
        verify_task_lease(task, worker_id, lease_token)
        row = connection.execute(
            '''SELECT chunk.local_path, chunk.size, chunk.sha256, chunk.mime_type
               FROM chunks chunk JOIN segments segment ON segment.id = chunk.segment_id
               WHERE chunk.segment_id = ? AND segment.session_id = ? AND chunk.chunk_index = ?''',
            (segment_id, task['session_id'], chunk_index),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail='Chunk 不存在')
    path = (DATA_DIR / row['local_path']).resolve()
    data_root = DATA_DIR.resolve()
    if data_root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail='Chunk 文件不存在')
    return FileResponse(path, media_type=row['mime_type'] or 'application/octet-stream', filename=f'{segment_id}-{chunk_index}.bin', headers={'X-Chunk-Sha256': row['sha256'], 'X-Chunk-Size': str(row['size'])})


@app.post('/api/v1/tasks/{task_id}/audio-artifact')
async def upload_worker_audio_artifact(
    task_id: str,
    request: Request,
    upload: UploadFile = File(...),
    x_file_sha256: str | None = Header(default=None),
) -> dict:
    """Accept the locally reconstructed, playable audio artifact."""
    validate_identifier(task_id, 'Task ID')
    authorize_worker(request)
    worker_id = request.headers.get('x-worker-id')
    lease_token = request.headers.get('x-task-lease')
    with connect() as connection:
        task = _worker_task_row(connection, task_id)
        verify_task_lease(task, worker_id, lease_token)
        if task['status'] not in {'CLAIMED', 'LOCAL_READY', 'PROCESSING'}:
            raise HTTPException(status_code=409, detail=f'当前不能上传音频：{task["status"]}')
        session_id = task['session_id']
    destination = DATA_DIR / 'reconstructed' / 'sessions' / session_id / 'session.webm'
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(f'.{destination.name}.{uuid.uuid4().hex}.upload')
    size, digest = await save_bounded_upload(upload, staging, MAX_AUDIO_ARTIFACT_BYTES)
    if x_file_sha256 and not secrets.compare_digest(digest, x_file_sha256.strip().lower()):
        staging.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail='音频文件 SHA-256 校验失败')
    staging.replace(destination)
    with connect() as connection:
        connection.execute('UPDATE processing_tasks SET downloaded_at = COALESCE(downloaded_at, ?), updated_at = ? WHERE id = ?', (now_ms(), now_ms(), task_id))
    return {'ok': True, 'taskId': task_id, 'sessionId': session_id, 'size': size, 'sha256': digest, 'path': str(destination.relative_to(DATA_DIR))}


@app.post('/api/v1/tasks/{task_id}/transcript')
def upload_worker_transcript(task_id: str, payload: LocalTranscriptPayload, request: Request = None) -> dict:
    """Persist a transcript generated on the user's local PC.

    The endpoint creates the same completed processing run used by the local
    pipeline, so the existing Codex summary and publication flow remains
    unchanged.
    """
    validate_identifier(task_id, 'Task ID')
    authorize_worker(request)
    worker_id = request.headers.get('x-worker-id') if request else None
    lease_token = request.headers.get('x-task-lease') if request else None
    timestamp = now_ms()
    with connect() as connection:
        task = _worker_task_row(connection, task_id)
        existing = connection.execute(
            '''SELECT id, generation, source_hash, model, language
               FROM processing_runs WHERE task_id = ? AND source_hash = ? AND status = 'COMPLETED'
               ORDER BY generation DESC LIMIT 1''',
            (task_id, payload.sourceHash.lower()),
        ).fetchone()
        if existing is not None and task['status'] in {'TRANSCRIBED', 'SUMMARIZING', 'REVIEW', 'COMPLETED'}:
            return {'ok': True, 'taskId': task_id, 'sessionId': task['session_id'], 'status': task['status'], 'runId': existing['id'], 'generation': existing['generation'], 'sourceHash': existing['source_hash'], 'reused': True}
        verify_task_lease(task, worker_id, lease_token)
        if task['status'] not in {'CLAIMED', 'LOCAL_READY', 'PROCESSING'}:
            raise HTTPException(status_code=409, detail=f'当前不能上传逐字稿：{task["status"]}')
        artifact_path = DATA_DIR / 'reconstructed' / 'sessions' / task['session_id'] / 'session.webm'
        if STORAGE_ONLY_MODE and not artifact_path.is_file():
            raise HTTPException(status_code=409, detail='请先回传本地重建后的可播放音频')
        latest = connection.execute('SELECT COALESCE(MAX(generation), 0) AS generation FROM processing_runs WHERE task_id = ?', (task_id,)).fetchone()['generation']
        run_id = f'run-{uuid.uuid4()}'
        generation = int(latest) + 1
        connection.execute(
            '''INSERT INTO processing_runs(id, task_id, session_id, generation, source_hash, model, language, status, started_at, heartbeat_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'COMPLETED', ?, ?, ?)''',
            (run_id, task_id, task['session_id'], generation, payload.sourceHash.lower(), payload.model, payload.language, timestamp, timestamp, timestamp),
        )
        transcript = dict(payload.transcript)
        transcript.update({'runId': run_id, 'generation': generation, 'sourceHash': payload.sourceHash.lower(), 'model': payload.model, 'language': payload.language})
        transcript_path = DATA_DIR / 'processed' / 'sessions' / task['session_id'] / 'transcript.json'
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = transcript_path.with_suffix('.json.tmp')
        temporary_path.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary_path.replace(transcript_path)
        connection.execute(
            '''UPDATE processing_tasks SET status = 'TRANSCRIBED', claimed_by = NULL, claimed_at = NULL,
               requested_worker_id = NULL, lease_token_hash = NULL, lease_expires_at = NULL,
               error_message = '', error_stage = '', updated_at = ? WHERE id = ?''',
            (timestamp, task_id),
        )
    return {'ok': True, 'taskId': task_id, 'sessionId': task['session_id'], 'status': 'TRANSCRIBED', 'runId': run_id, 'generation': generation, 'sourceHash': payload.sourceHash.lower(), 'reused': False}


@app.post('/api/v1/tasks/{task_id}/claim')
def claim_processing_task(task_id: str, payload: TaskClaimPayload, request: Request = None) -> dict:
    validate_identifier(task_id, 'Task ID')
    authorize_worker(request)
    claimed_at = now_ms()
    lease_token = secrets.token_urlsafe(32)
    with connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute('SELECT status, claimed_by, requested_worker_id, lease_expires_at FROM processing_tasks WHERE id = ?', (task_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail='任务不存在')
        if row['status'] in {'CLAIMED', 'LOCAL_READY', 'PROCESSING'} and row['lease_expires_at'] and row['lease_expires_at'] < claimed_at:
            connection.execute("UPDATE processing_tasks SET status = 'READY', claimed_by = NULL, lease_token_hash = NULL, lease_expires_at = NULL WHERE id = ?", (task_id,))
            row = connection.execute('SELECT status, claimed_by, requested_worker_id, lease_expires_at FROM processing_tasks WHERE id = ?', (task_id,)).fetchone()
        if row['status'] not in {'READY', 'FAILED'}:
            raise HTTPException(status_code=409, detail=f'任务当前不能领取：{row["status"]}')
        if row['requested_worker_id'] and row['requested_worker_id'] != payload.workerId:
            raise HTTPException(status_code=409, detail='任务已指定其他 Worker')
        connection.execute(
            '''UPDATE processing_tasks SET status = 'CLAIMED', attempts = attempts + 1,
               claimed_by = ?, claimed_at = ?, lease_token_hash = ?, lease_expires_at = ?, error_message = '', error_stage = '', updated_at = ? WHERE id = ?''',
            (payload.workerId, claimed_at, hash_secret(lease_token), claimed_at + LEASE_DURATION_MS, claimed_at, task_id),
        )
        task = _worker_task_row(connection, task_id)
    return {'task': task_payload(task), 'leaseToken': lease_token}


@app.post('/api/v1/tasks/{task_id}/status')
def update_processing_task(task_id: str, payload: TaskStatusPayload, request: Request = None) -> dict:
    validate_identifier(task_id, 'Task ID')
    authorize_worker(request)
    status = payload.status.upper().strip()
    if status not in TASK_STATUSES:
        raise HTTPException(status_code=400, detail=f'任务状态无效：{payload.status}')
    updated_at = now_ms()
    with connect() as connection:
        row = connection.execute('SELECT status, claimed_by, lease_token_hash, lease_expires_at FROM processing_tasks WHERE id = ?', (task_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail='任务不存在')
        # A worker must be able to report a terminal local failure even when
        # the long-running task crossed its lease deadline. The worker ID and
        # lease token are still verified, so this does not allow another
        # worker to close the task.
        verify_task_lease(row, payload.workerId, payload.leaseToken, allow_expired=status == 'FAILED')
        allowed = {
            'CLAIMED': {'LOCAL_READY', 'FAILED'},
            'LOCAL_READY': {'PROCESSING', 'FAILED'},
            'PROCESSING': {'REVIEW', 'FAILED'},
            'FAILED': {'READY'},
            'READY': {'CLAIMED'},
        }
        if status not in allowed.get(row['status'], set()):
            raise HTTPException(status_code=409, detail=f'不允许的任务状态转换：{row["status"]} → {status}')
        connection.execute(
            'UPDATE processing_tasks SET status = ?, error_message = ?, error_stage = CASE WHEN ? = \'FAILED\' THEN ? ELSE error_stage END, updated_at = ? WHERE id = ?',
            (status, payload.errorMessage, status, payload.errorMessage, updated_at, task_id),
        )
        task = _worker_task_row(connection, task_id)
    return {'task': task_payload(task)}


@app.post('/api/v1/tasks/{task_id}/heartbeat')
def heartbeat_processing_task(task_id: str, payload: TaskStatusPayload, request: Request = None) -> dict:
    validate_identifier(task_id, 'Task ID')
    authorize_worker(request)
    with connect() as connection:
        row = connection.execute('SELECT status, claimed_by, lease_token_hash, lease_expires_at FROM processing_tasks WHERE id = ?', (task_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail='任务不存在')
        verify_task_lease(row, payload.workerId, payload.leaseToken)
        if row['status'] not in {'CLAIMED', 'LOCAL_READY', 'PROCESSING'}:
            raise HTTPException(status_code=409, detail=f'当前状态不能续租：{row["status"]}')
        updated_at = now_ms()
        connection.execute('UPDATE processing_tasks SET lease_expires_at = ?, updated_at = ? WHERE id = ?', (updated_at + LEASE_DURATION_MS, updated_at, task_id))
        task = _worker_task_row(connection, task_id)
    return {'task': task_payload(task)}


@app.get('/api/v1/tasks/{task_id}/audio')
def get_task_audio(task_id: str, request: Request = None, x_task_lease: str | None = Header(default=None)) -> FileResponse:
    validate_identifier(task_id, 'Task ID')
    authorize_worker(request)
    with connect() as connection:
        task = connection.execute('SELECT session_id, claimed_by, lease_token_hash, lease_expires_at FROM processing_tasks WHERE id = ?', (task_id,)).fetchone()
        if task is None:
            raise HTTPException(status_code=404, detail='任务不存在')
        verify_task_lease(task, request.headers.get('x-worker-id') if request else None, x_task_lease)
        session_id = task['session_id']
    try:
        with connect() as connection:
            output_path = _session_audio_for_playback(connection, session_id)
    except ReconstructionError as error:
        raise HTTPException(status_code=409, detail=f'整场音频重组失败：{error}') from error
    with connect() as connection:
        connection.execute('UPDATE processing_tasks SET downloaded_at = ?, updated_at = ? WHERE id = ?', (now_ms(), now_ms(), task_id))
    return FileResponse(output_path, media_type='audio/webm', filename=f'{session_id}.webm')


@app.post('/api/v1/tasks/{task_id}/result')
def upload_task_result(task_id: str, payload: KnowledgeResultPayload, request: Request = None) -> dict:
    validate_identifier(task_id, 'Task ID')
    authorize_worker(request)
    with connect() as connection:
        task = connection.execute('SELECT session_id, status, claimed_by, lease_token_hash, lease_expires_at FROM processing_tasks WHERE id = ?', (task_id,)).fetchone()
        if task is None:
            raise HTTPException(status_code=404, detail='任务不存在')
        verify_task_lease(task, request.headers.get('x-worker-id') if request else None, request.headers.get('x-task-lease') if request else None)
        if task['status'] != 'PROCESSING':
            raise HTTPException(status_code=409, detail=f'只有 PROCESSING 任务可以提交结果，当前为 {task["status"]}')
        session_id = task['session_id']
        updated_at = now_ms()
        document = payload.result.model_dump(exclude_none=True)
        content_json = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        content_hash = hashlib.sha256(content_json.encode('utf-8')).hexdigest()
        existing = connection.execute('SELECT id, version FROM result_revisions WHERE task_id = ? AND content_hash = ?', (task_id, content_hash)).fetchone()
        if existing is None:
            latest = connection.execute('SELECT COALESCE(MAX(version), 0) AS version FROM result_revisions WHERE task_id = ?', (task_id,)).fetchone()['version']
            revision_id = new_id('revision')
            version = int(latest) + 1
            connection.execute(
                'INSERT INTO result_revisions(id, task_id, session_id, version, content_hash, content_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (revision_id, task_id, session_id, version, content_hash, content_json, updated_at),
            )
        else:
            revision_id = existing['id']
            version = existing['version']
        connection.execute(
            '''UPDATE processing_tasks SET status = 'REVIEW', result_version = ?,
               claimed_by = NULL, claimed_at = NULL, requested_worker_id = NULL,
               lease_token_hash = NULL, lease_expires_at = NULL,
               error_message = '', error_stage = '', updated_at = ? WHERE id = ?''',
            (version, updated_at, task_id),
        )
    return {'ok': True, 'taskId': task_id, 'sessionId': session_id, 'revisionId': revision_id, 'version': version, 'status': 'REVIEW'}


@app.post('/api/v1/admin/tasks/{task_id}/publish')
def publish_task_result(task_id: str, request: Request, payload: PublishPayload) -> dict:
    require_admin(request)
    validate_identifier(task_id, 'Task ID')
    validate_identifier(payload.revisionId, 'Revision ID')
    with connect() as connection:
        revision = connection.execute('SELECT * FROM result_revisions WHERE id = ? AND task_id = ?', (payload.revisionId, task_id)).fetchone()
        if revision is None:
            raise HTTPException(status_code=404, detail='总结版本不存在')
        task = connection.execute('SELECT session_id FROM processing_tasks WHERE id = ?', (task_id,)).fetchone()
        if task is None:
            raise HTTPException(status_code=404, detail='任务不存在')
        updated_at = now_ms()
        connection.execute('UPDATE sessions SET published_revision_id = ?, updated_at = ? WHERE id = ?', (revision['id'], updated_at, revision['session_id']))
        connection.execute(
            '''UPDATE processing_tasks SET status = 'COMPLETED', result_path = ?,
               result_version = ?, claimed_by = NULL, claimed_at = NULL,
               requested_worker_id = NULL, lease_token_hash = NULL,
               lease_expires_at = NULL, error_message = '', error_stage = '',
               updated_at = ? WHERE id = ?''',
            (f'processed/sessions/{revision["session_id"]}/knowledge.json', revision['version'], updated_at, task_id),
        )
        _audit(connection, 'admin', 'PUBLISH_RESULT', revision['session_id'], {'revisionId': revision['id']})
        result = json.loads(revision['content_json'])
    result_path = DATA_DIR / 'processed' / 'sessions' / revision['session_id'] / 'knowledge.json'
    result_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = result_path.with_suffix('.json.tmp')
    temporary_path.write_text(json.dumps({'sessionId': revision['session_id'], 'taskId': task_id, 'revisionId': revision['id'], 'version': revision['version'], 'updatedAt': updated_at, **result}, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary_path.replace(result_path)
    return {'ok': True, 'taskId': task_id, 'revisionId': revision['id'], 'status': 'COMPLETED'}


@app.get('/api/v1/sessions/{session_id}/result')
def get_session_result(session_id: str, request: Request = None) -> dict:
    validate_identifier(session_id, 'Session ID')
    with connect() as connection:
        session = session_access(connection, session_id, request)
        task = connection.execute('SELECT id, status, result_version, updated_at FROM processing_tasks WHERE session_id = ?', (session_id,)).fetchone()
    if task is None:
        raise HTTPException(status_code=404, detail='该 Session 还没有处理任务')
    published_revision = None
    with connect() as connection:
        published_revision = connection.execute('SELECT id, version, content_json, created_at FROM result_revisions WHERE id = (SELECT published_revision_id FROM sessions WHERE id = ?)', (session_id,)).fetchone()
    path = DATA_DIR / 'processed' / 'sessions' / session_id / 'knowledge.json'
    if published_revision is None and task['status'] == 'COMPLETED' and path.is_file():
        try:
            legacy_result = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=500, detail='总结结果文件损坏') from error
        return {'sessionId': session_id, 'taskId': task['id'], 'status': task['status'], 'version': task['result_version'], 'updatedAt': task['updated_at'], 'result': legacy_result, 'revisionId': None}
    if published_revision is None:
        return {'sessionId': session_id, 'taskId': task['id'], 'status': task['status'], 'version': task['result_version'], 'updatedAt': task['updated_at'], 'result': None}
    return {'sessionId': session_id, 'taskId': task['id'], 'status': task['status'], 'version': published_revision['version'], 'updatedAt': task['updated_at'], 'revisionId': published_revision['id'], 'result': json.loads(published_revision['content_json'])}


@app.post('/api/v1/sessions/{session_id}/markers')
def create_marker(session_id: str, payload: MarkerPayload, request: Request = None) -> dict:
    validate_identifier(session_id, 'Session ID')
    validate_identifier(payload.id, 'Marker ID')
    if payload.sessionId != session_id:
        raise HTTPException(status_code=400, detail='sessionId 不匹配')
    with connect() as connection:
        session_access(connection, session_id, request)
        existing = connection.execute('SELECT 1 FROM markers WHERE id = ?', (payload.id,)).fetchone()
        if existing is not None:
            return {'ok': True, 'already_exists': True}
        connection.execute(
            'INSERT INTO markers(id, session_id, marker_type, elapsed_ms, wall_clock_ms, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (payload.id, session_id, payload.type, payload.elapsedMs, payload.wallClockMs, payload.note, payload.createdAt),
        )
    return {'ok': True, 'already_exists': False}


@app.delete('/api/v1/sessions/{session_id}')
def delete_session(session_id: str, request: Request = None) -> dict:
    validate_identifier(session_id, 'Session ID')
    with connect() as connection:
        session_access(connection, session_id, request)

    active_job = None
    jobs_dir = DATA_DIR / 'processed' / 'jobs'
    if jobs_dir.is_dir():
        for path in jobs_dir.glob('job-*.json'):
            try:
                state = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(state, dict) and state.get('sessionId') == session_id and state.get('status') in {'QUEUED', 'RUNNING'}:
                active_job = state.get('id', path.stem)
                break
    if active_job:
        raise HTTPException(status_code=409, detail=f'Session 正在处理，不能删除：{active_job}')

    with connect() as connection:
        active_task = connection.execute(
            """SELECT id, status FROM processing_tasks
               WHERE session_id = ?
                 AND status IN ('CLAIMED', 'LOCAL_READY', 'TRANSCRIBING', 'PROCESSING',
                                'TRANSCRIBED', 'SUMMARIZING', 'READY_TO_UPLOAD')""",
            (session_id,),
        ).fetchone()
    if active_task:
        raise HTTPException(status_code=409, detail=f'Session 正在由电脑端处理，不能删除：{active_task["id"]}')

    try:
        counts = delete_session_data(DATA_DIR, DB_PATH, session_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail='Session 不存在') from error
    except RetentionError as error:
        raise HTTPException(status_code=500, detail=f'Session 清理失败：{error}') from error
    return {'ok': True, 'sessionId': session_id, **counts}


@app.get('/api/v1/sessions/{session_id}/upload-state')
def upload_state(session_id: str, request: Request = None) -> dict:
    validate_identifier(session_id, 'Session ID')
    with connect() as connection:
        session_access(connection, session_id, request)
        segments = connection.execute('SELECT id FROM segments WHERE session_id = ? ORDER BY segment_index', (session_id,)).fetchall()
        result = []
        for segment in segments:
            chunks = connection.execute('SELECT chunk_index, size, sha256 FROM chunks WHERE segment_id = ? ORDER BY chunk_index', (segment['id'],)).fetchall()
            result.append({'segmentId': segment['id'], 'chunks': [{'index': chunk['chunk_index'], 'size': chunk['size'], 'sha256': chunk['sha256']} for chunk in chunks]})
    return {'sessionId': session_id, 'segments': result}


def _segment_chunk_paths(connection: sqlite3.Connection, session_id: str, segment_id: str) -> tuple[int, list[Path]]:
    segment = connection.execute('SELECT segment_index FROM segments WHERE id = ? AND session_id = ?', (segment_id, session_id)).fetchone()
    if segment is None:
        raise HTTPException(status_code=404, detail='Segment 不存在')
    rows = connection.execute('SELECT chunk_index, local_path FROM chunks WHERE segment_id = ? ORDER BY chunk_index', (segment_id,)).fetchall()
    indexes = [row['chunk_index'] for row in rows]
    if not indexes:
        raise HTTPException(status_code=409, detail='Segment 没有已上传的 Chunk')
    if indexes != list(range(len(indexes))):
        raise HTTPException(status_code=409, detail=f'Segment Chunk index 不连续：{indexes}')
    return segment['segment_index'], [DATA_DIR / row['local_path'] for row in rows]


def _require_completed_session(connection: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
    session = connection.execute('SELECT status, duration_ms FROM sessions WHERE id = ?', (session_id,)).fetchone()
    if session is None:
        raise HTTPException(status_code=404, detail='Session 不存在')
    if session['status'] != 'COMPLETED':
        raise HTTPException(status_code=409, detail='Session 尚未完成上传收尾，请等待上传队列结束后再处理')
    segments = connection.execute('SELECT id, segment_index, status FROM segments WHERE session_id = ? ORDER BY segment_index', (session_id,)).fetchall()
    if not segments or any(segment['status'] != 'COMPLETED' for segment in segments):
        raise HTTPException(status_code=409, detail='Session 音频尚未全部上传完成，请等待上传队列结束后再处理')
    for segment in segments:
        _segment_chunk_paths(connection, session_id, segment['id'])
    last_elapsed = connection.execute('SELECT MAX(elapsed_ms) AS elapsed_ms FROM chunks WHERE session_id = ?', (session_id,)).fetchone()['elapsed_ms']
    if last_elapsed is None or int(session['duration_ms'] or 0) > int(last_elapsed) + UPLOAD_DURATION_TOLERANCE_MS:
        raise HTTPException(status_code=409, detail='Session 音频时长与已上传 Chunk 不一致，请等待补传完成后再处理')
    return segments


def _reconstruct_completed_session(connection: sqlite3.Connection, session_id: str) -> Path:
    """Restore Chunk bytes into Segment files before combining Segments."""
    artifact = DATA_DIR / 'reconstructed' / 'sessions' / session_id / 'session.webm'
    if artifact.is_file():
        return artifact
    if STORAGE_ONLY_MODE:
        raise HTTPException(status_code=409, detail='电脑端尚未回传可播放音频，请等待本地处理完成')
    segments = _require_completed_session(connection, session_id)
    segment_paths: list[tuple[int, Path]] = []
    for segment in segments:
        segment_index, chunk_paths = _segment_chunk_paths(connection, session_id, segment['id'])
        segment_paths.append((segment_index, reconstruct_segment(DATA_DIR, session_id, segment_index, chunk_paths)))
    return reconstruct_session(DATA_DIR, session_id, segment_paths)


def _session_audio_for_playback(connection: sqlite3.Connection, session_id: str) -> Path:
    """Return a durable PC-produced artifact, or reconstruct in local mode."""
    return _reconstruct_completed_session(connection, session_id)


@app.post('/api/v1/sessions/{session_id}/segments/{segment_id}/reconstruct')
def reconstruct_segment_endpoint(session_id: str, segment_id: str, request: Request = None) -> dict:
    validate_identifier(session_id, 'Session ID')
    validate_identifier(segment_id, 'Segment ID')
    if STORAGE_ONLY_MODE:
        raise HTTPException(status_code=409, detail='当前为存储模式，Segment 音频由电脑端处理后回传整场音频')
    with connect() as connection:
        session_access(connection, session_id, request)
        segment_index, chunk_paths = _segment_chunk_paths(connection, session_id, segment_id)
    try:
        output_path = reconstruct_segment(DATA_DIR, session_id, segment_index, chunk_paths)
    except ReconstructionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {'ok': True, 'sessionId': session_id, 'segmentId': segment_id, 'segmentIndex': segment_index, 'path': str(output_path.relative_to(DATA_DIR))}


@app.get('/api/v1/sessions/{session_id}/segments/{segment_id}/audio')
def get_segment_audio(session_id: str, segment_id: str, request: Request = None) -> FileResponse:
    validate_identifier(session_id, 'Session ID')
    validate_identifier(segment_id, 'Segment ID')
    if STORAGE_ONLY_MODE:
        raise HTTPException(status_code=409, detail='当前为存储模式，Segment 音频由电脑端处理后回传整场音频')
    with connect() as connection:
        session_access(connection, session_id, request)
        segment_index, chunk_paths = _segment_chunk_paths(connection, session_id, segment_id)
    try:
        output_path = reconstruct_segment(DATA_DIR, session_id, segment_index, chunk_paths)
    except ReconstructionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return FileResponse(output_path, media_type='audio/webm', filename=f'{session_id}-segment-{segment_index}.webm')


@app.post('/api/v1/sessions/{session_id}/reconstruct')
def reconstruct_session_endpoint(session_id: str, request: Request = None) -> dict:
    validate_identifier(session_id, 'Session ID')
    with connect() as connection:
        session_access(connection, session_id, request)
        segments = _require_completed_session(connection, session_id)
        if STORAGE_ONLY_MODE:
            output_path = _session_audio_for_playback(connection, session_id)
            return {'ok': True, 'sessionId': session_id, 'segmentCount': len(segments), 'path': str(output_path.relative_to(DATA_DIR)), 'source': 'local-worker'}
        segment_paths: list[tuple[int, Path]] = []
        for segment in segments:
            _, chunk_paths = _segment_chunk_paths(connection, session_id, segment['id'])
            try:
                segment_path = reconstruct_segment(DATA_DIR, session_id, segment['segment_index'], chunk_paths)
            except ReconstructionError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            segment_paths.append((segment['segment_index'], segment_path))
    try:
        output_path = reconstruct_session(DATA_DIR, session_id, segment_paths)
    except ReconstructionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {'ok': True, 'sessionId': session_id, 'segmentCount': len(segment_paths), 'path': str(output_path.relative_to(DATA_DIR))}


@app.get('/api/v1/sessions/{session_id}/audio')
def get_session_audio(session_id: str, request: Request = None) -> FileResponse:
    validate_identifier(session_id, 'Session ID')
    with connect() as connection:
        session_access(connection, session_id, request)
        segments = _require_completed_session(connection, session_id)
        if STORAGE_ONLY_MODE:
            output_path = _session_audio_for_playback(connection, session_id)
            return FileResponse(output_path, media_type='audio/webm', filename=f'{session_id}.webm')
        segment_paths: list[tuple[int, Path]] = []
        for segment in segments:
            _, chunk_paths = _segment_chunk_paths(connection, session_id, segment['id'])
            try:
                segment_path = reconstruct_segment(DATA_DIR, session_id, segment['segment_index'], chunk_paths)
            except ReconstructionError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            segment_paths.append((segment['segment_index'], segment_path))
    try:
        output_path = reconstruct_session(DATA_DIR, session_id, segment_paths)
    except ReconstructionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return FileResponse(output_path, media_type='audio/webm', filename=f'{session_id}.webm')


def _legacy_processing_disabled() -> None:
    raise HTTPException(status_code=410, detail='旧版本地 ASR/报告流程已停用，请使用电脑端处理任务。')


@app.post('/api/v1/sessions/{session_id}/transcribe')
def transcribe_session(session_id: str, payload: TranscriptionPayload | None = None) -> dict:
    _legacy_processing_disabled()
    validate_identifier(session_id, 'Session ID')
    with connect() as connection:
        segments = _require_completed_session(connection, session_id)
        segment_paths: list[tuple[int, Path]] = []
        for segment in segments:
            _, chunk_paths = _segment_chunk_paths(connection, session_id, segment['id'])
            try:
                segment_path = reconstruct_segment(DATA_DIR, session_id, segment['segment_index'], chunk_paths)
            except ReconstructionError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            segment_paths.append((segment['segment_index'], segment_path))
    try:
        audio_path = reconstruct_session(DATA_DIR, session_id, segment_paths)
        transcript = transcribe(audio_path, model_name=payload.model if payload and payload.model else DEFAULT_MODEL, language=payload.language if payload else 'zh')
        output_path = save_transcript(DATA_DIR, session_id, transcript)
    except (ReconstructionError, AsrError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {'ok': True, 'sessionId': session_id, 'transcriptPath': str(output_path.relative_to(DATA_DIR)), **transcript}


@app.get('/api/v1/sessions/{session_id}/transcript')
def get_transcript(session_id: str) -> dict:
    _legacy_processing_disabled()
    validate_identifier(session_id, 'Session ID')
    path = DATA_DIR / 'processed' / 'sessions' / session_id / 'transcript.json'
    if not path.is_file():
        raise HTTPException(status_code=404, detail='该 Session 尚未生成逐字稿')
    try:
        import json
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception as error:
        raise HTTPException(status_code=500, detail=f'逐字稿读取失败：{error}') from error


@app.post('/api/v1/sessions/{session_id}/report')
def create_report(session_id: str) -> dict:
    _legacy_processing_disabled()
    validate_identifier(session_id, 'Session ID')
    with connect() as connection:
        session_row = connection.execute('SELECT * FROM sessions WHERE id = ?', (session_id,)).fetchone()
        if session_row is None:
            raise HTTPException(status_code=404, detail='Session 不存在')
        marker_rows = connection.execute('SELECT * FROM markers WHERE session_id = ? ORDER BY elapsed_ms, created_at', (session_id,)).fetchall()
    try:
        transcript = load_transcript(DATA_DIR, session_id)
        report = build_structured_report(dict(session_row), [dict(row) for row in marker_rows], transcript, now_ms())
        output_path = save_report(DATA_DIR, session_id, report)
    except ContentError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {'ok': True, 'sessionId': session_id, 'reportPath': str(output_path.relative_to(DATA_DIR)), **report}


@app.get('/api/v1/sessions/{session_id}/report')
def get_report(session_id: str) -> dict:
    _legacy_processing_disabled()
    validate_identifier(session_id, 'Session ID')
    path = DATA_DIR / 'processed' / 'sessions' / session_id / 'report.json'
    if not path.is_file():
        raise HTTPException(status_code=404, detail='该 Session 尚未生成报告草稿')
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception as error:
        raise HTTPException(status_code=500, detail=f'报告草稿读取失败：{error}') from error


@app.post('/api/v1/sessions/{session_id}/process', status_code=202, response_model=ProcessingJobResponse)
def start_processing_job(session_id: str, payload: TranscriptionPayload | None = None, request: Request = None) -> dict:
    if request is not None:
        _legacy_processing_disabled()
    validate_identifier(session_id, 'Session ID')
    with connect() as connection:
        _require_completed_session(connection, session_id)
    model = payload.model if payload and payload.model else DEFAULT_MODEL
    language = payload.language if payload else 'zh'
    try:
        return create_job(DATA_DIR, DB_PATH, session_id, model, language)
    except Exception as error:
        raise HTTPException(status_code=500, detail=f'无法创建处理任务：{error}') from error


@app.get('/api/v1/jobs/{job_id}')
def get_processing_job(job_id: str) -> dict:
    _legacy_processing_disabled()
    validate_identifier(job_id, 'Job ID')
    try:
        state = get_job(DATA_DIR, job_id)
    except JobError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    if state is None:
        raise HTTPException(status_code=404, detail='处理任务不存在')
    return state


@app.get('/api/v1/sessions/{session_id}/processing')
def get_latest_processing_job(session_id: str, request: Request = None) -> dict:
    if request is not None:
        _legacy_processing_disabled()
    validate_identifier(session_id, 'Session ID')
    with connect() as connection:
        if connection.execute('SELECT 1 FROM sessions WHERE id = ?', (session_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail='Session 不存在')
    state = find_latest_job(DATA_DIR, session_id)
    if state is None:
        raise HTTPException(status_code=404, detail='该 Session 尚未创建处理任务')
    return state


if __name__ == '__main__':
    import uvicorn

    uvicorn.run('main:app', host='0.0.0.0', port=int(os.environ.get('PORT', '8000')), reload=False)
