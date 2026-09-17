from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

try:
    from reconstruction import FFMPEG as RECONSTRUCTION_FFMPEG, FFPROBE as RECONSTRUCTION_FFPROBE, ReconstructionError, reconstruct_segment, reconstruct_session
except ImportError:
    from .reconstruction import FFMPEG as RECONSTRUCTION_FFMPEG, FFPROBE as RECONSTRUCTION_FFPROBE, ReconstructionError, reconstruct_segment, reconstruct_session
try:
    from asr import DEFAULT_MODEL, AsrError, is_model_cached, save_transcript, transcribe
except ImportError:
    from .asr import DEFAULT_MODEL, AsrError, is_model_cached, save_transcript, transcribe
try:
    from llm import is_configured as is_llm_configured
except ImportError:
    from .llm import is_configured as is_llm_configured
try:
    from content import ContentError, build_structured_report, load_transcript, save_report
except ImportError:
    from .content import ContentError, build_structured_report, load_transcript, save_report
try:
    from jobs import JobError, create_job, find_latest_job, get_job, recover_jobs
except ImportError:
    from .jobs import JobError, create_job, find_latest_job, get_job, recover_jobs
try:
    from retention import RetentionError, delete_session_data
except ImportError:
    from .retention import RetentionError, delete_session_data
try:
    from image_renderer import render_summary_svg
except ImportError:
    from .image_renderer import render_summary_svg

SERVER_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('LIVENOTE_DATA_DIR', SERVER_DIR / 'data'))
DB_PATH = Path(os.environ.get('LIVENOTE_DB_PATH', SERVER_DIR / 'livenote.sqlite3'))
MAX_CHUNK_BYTES = int(os.environ.get('LIVENOTE_MAX_CHUNK_BYTES', str(25 * 1024 * 1024)))
MAX_DIAGNOSTIC_BYTES = int(os.environ.get('LIVENOTE_MAX_DIAGNOSTIC_BYTES', str(10 * 1024 * 1024)))
UPLOAD_DURATION_TOLERANCE_MS = int(os.environ.get('LIVENOTE_UPLOAD_DURATION_TOLERANCE_MS', '15000'))
RUNTIME_ENV = os.environ.get('LIVENOTE_ENV', 'development').strip().lower()
API_KEY = os.environ.get('LIVENOTE_API_KEY', '')
CORS_ORIGINS = [origin.strip() for origin in os.environ.get('LIVENOTE_CORS_ORIGINS', 'http://localhost:5173,https://localhost:5173').split(',') if origin.strip()]
IDENTIFIER_PATTERN = re.compile(r'^[A-Za-z0-9_-]{1,128}$')

if RUNTIME_ENV in {'production', 'prod'} and not API_KEY:
    raise RuntimeError('生产环境必须设置 LIVENOTE_API_KEY。')
if RUNTIME_ENV in {'production', 'prod'} and not os.environ.get('LIVENOTE_CORS_ORIGINS', '').strip():
    raise RuntimeError('生产环境必须设置 LIVENOTE_CORS_ORIGINS。')


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


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
            '''
        )
        segment_columns = {row['name'] for row in connection.execute('PRAGMA table_info(segments)').fetchall()}
        if 'start_elapsed_ms' not in segment_columns:
            connection.execute('ALTER TABLE segments ADD COLUMN start_elapsed_ms INTEGER NOT NULL DEFAULT 0')


class SessionPayload(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    title: str
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
    mimeType: str = ''
    mediaSettings: dict = Field(default_factory=dict)
    status: str
    durationMs: int = Field(default=0, ge=0)


class MarkerPayload(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    sessionId: str = Field(min_length=1, max_length=128)
    type: str
    elapsedMs: int = Field(ge=0)
    wallClockMs: int = Field(ge=0)
    note: str = ''
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


def validate_identifier(value: str, label: str) -> None:
    if not IDENTIFIER_PATTERN.fullmatch(value):
        raise HTTPException(status_code=400, detail=f'{label} 格式无效')


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


init_db()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    recover_jobs(DATA_DIR, DB_PATH)
    yield


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
        if not secrets.compare_digest(provided, API_KEY):
            return JSONResponse(status_code=401, content={'detail': '缺少有效 API Key'})
    return await call_next(request)


@app.get('/health')
def health() -> dict:
    return {
        'ok': True,
        'service': 'livenote-api',
        'storageSchema': 2,
        'capabilities': {
            'ffmpeg': bool(shutil.which(RECONSTRUCTION_FFMPEG)),
            'ffprobe': bool(shutil.which(RECONSTRUCTION_FFPROBE)),
            'asrModel': DEFAULT_MODEL,
            'asrModelCached': is_model_cached(DEFAULT_MODEL),
            'llmConfigured': is_llm_configured(),
        },
    }


@app.get('/api/v1/health')
def api_health() -> dict:
    return health()


@app.post('/api/v1/diagnostics')
async def upload_diagnostic(
    description: str = Form(default=''),
    snapshot: UploadFile = File(...),
    attachment: UploadFile | None = File(default=None),
) -> dict:
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
def create_session(payload: SessionPayload) -> dict:
    validate_identifier(payload.id, 'Session ID')
    with connect() as connection:
        connection.execute(
            '''INSERT INTO sessions(id, title, started_at, ended_at, status, duration_ms, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET title=excluded.title, ended_at=excluded.ended_at,
               status=excluded.status, duration_ms=excluded.duration_ms, updated_at=excluded.updated_at''',
            # Upload metadata is not completion proof. The queue calls this
            # endpoint before sending every Chunk; complete_session is the
            # only endpoint allowed to mark a Session completed.
            (payload.id, payload.title, payload.startedAt, payload.endedAt, 'RECORDING', payload.durationMs, payload.createdAt, payload.updatedAt),
        )
    return {'id': payload.id, 'created': True}


@app.post('/api/v1/sessions/{session_id}/segments')
def create_segment(session_id: str, payload: SegmentPayload) -> dict:
    validate_identifier(session_id, 'Session ID')
    validate_identifier(payload.id, 'Segment ID')
    if payload.sessionId != session_id:
        raise HTTPException(status_code=400, detail='sessionId 不匹配')
    with connect() as connection:
        if connection.execute('SELECT 1 FROM sessions WHERE id = ?', (session_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail='Session 不存在')
        connection.execute(
            '''INSERT INTO segments(id, session_id, segment_index, started_at, start_elapsed_ms, ended_at, mime_type,
               media_settings, status, duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET ended_at=excluded.ended_at, mime_type=excluded.mime_type,
               media_settings=excluded.media_settings, status='RECORDING', duration_ms=excluded.duration_ms,
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
        session = connection.execute('SELECT 1 FROM sessions WHERE id = ?', (session_id,)).fetchone()
        segment = connection.execute('SELECT segment_index FROM segments WHERE id = ? AND session_id = ?', (segment_id, session_id)).fetchone()
        if session is None or segment is None:
            raise HTTPException(status_code=404, detail='Session 或 Segment 不存在')

        existing = connection.execute('SELECT size, sha256, local_path FROM chunks WHERE segment_id = ? AND chunk_index = ?', (segment_id, chunk_index)).fetchone()
        hasher = hashlib.sha256()
        body_size = 0
        relative_path = Path('sessions') / session_id / f"segment_{segment['segment_index']}" / 'chunks' / f'{chunk_index:06d}.bin'
        absolute_path = DATA_DIR / relative_path
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = absolute_path.with_name(f'.{absolute_path.name}.{uuid.uuid4().hex}.tmp')
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
            if existing is not None:
                if existing['size'] == body_size and existing['sha256'] == actual_sha256:
                    temporary_path.unlink(missing_ok=True)
                    return {'ok': True, 'already_exists': True, 'verified': True}
                raise HTTPException(status_code=409, detail='相同 Segment/Chunk index 的 SHA256 不一致')

            temporary_path.replace(absolute_path)
            received_at = now_ms()
            mime_type = request.headers.get('content-type', 'application/octet-stream')
            connection.execute(
                '''INSERT INTO chunks(session_id, segment_id, chunk_index, size, sha256, mime_type,
                   elapsed_ms, created_at, received_at, local_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (session_id, segment_id, chunk_index, body_size, actual_sha256, mime_type, x_chunk_elapsed_ms, received_at, received_at, str(relative_path)),
            )
        except Exception:
            if temporary_path.exists(): temporary_path.unlink()
            raise
    return {'ok': True, 'already_exists': False, 'verified': True}


@app.post('/api/v1/sessions/{session_id}/segments/{segment_id}/complete')
def complete_segment(session_id: str, segment_id: str, payload: CompletionPayload | None = None) -> dict:
    validate_identifier(session_id, 'Session ID')
    validate_identifier(segment_id, 'Segment ID')
    with connect() as connection:
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
    return {'ok': True}


@app.post('/api/v1/sessions/{session_id}/complete')
def complete_session(session_id: str, payload: CompletionPayload | None = None) -> dict:
    validate_identifier(session_id, 'Session ID')
    with connect() as connection:
        session = connection.execute('SELECT status, duration_ms FROM sessions WHERE id = ?', (session_id,)).fetchone()
        if session is None:
            raise HTTPException(status_code=404, detail='Session 不存在')
        segments = connection.execute('SELECT status, segment_index FROM segments WHERE session_id = ? ORDER BY segment_index', (session_id,)).fetchall()
        if not segments or any(segment['status'] != 'COMPLETED' for segment in segments):
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
        result = connection.execute('UPDATE sessions SET status = ?, ended_at = COALESCE(ended_at, ?), updated_at = ? WHERE id = ?', ('COMPLETED', now_ms(), now_ms(), session_id))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail='Session 不存在')
    return {'ok': True}


@app.post('/api/v1/sessions/{session_id}/markers')
def create_marker(session_id: str, payload: MarkerPayload) -> dict:
    validate_identifier(session_id, 'Session ID')
    validate_identifier(payload.id, 'Marker ID')
    if payload.sessionId != session_id:
        raise HTTPException(status_code=400, detail='sessionId 不匹配')
    with connect() as connection:
        if connection.execute('SELECT 1 FROM sessions WHERE id = ?', (session_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail='Session 不存在')
        existing = connection.execute('SELECT 1 FROM markers WHERE id = ?', (payload.id,)).fetchone()
        if existing is not None:
            return {'ok': True, 'already_exists': True}
        connection.execute(
            'INSERT INTO markers(id, session_id, marker_type, elapsed_ms, wall_clock_ms, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (payload.id, session_id, payload.type, payload.elapsedMs, payload.wallClockMs, payload.note, payload.createdAt),
        )
    return {'ok': True, 'already_exists': False}


@app.delete('/api/v1/sessions/{session_id}')
def delete_session(session_id: str) -> dict:
    validate_identifier(session_id, 'Session ID')
    with connect() as connection:
        if connection.execute('SELECT 1 FROM sessions WHERE id = ?', (session_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail='Session 不存在')

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

    try:
        counts = delete_session_data(DATA_DIR, DB_PATH, session_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail='Session 不存在') from error
    except RetentionError as error:
        raise HTTPException(status_code=500, detail=f'Session 清理失败：{error}') from error
    return {'ok': True, 'sessionId': session_id, **counts}


@app.get('/api/v1/sessions/{session_id}/upload-state')
def upload_state(session_id: str) -> dict:
    validate_identifier(session_id, 'Session ID')
    with connect() as connection:
        if connection.execute('SELECT 1 FROM sessions WHERE id = ?', (session_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail='Session 不存在')
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


@app.post('/api/v1/sessions/{session_id}/segments/{segment_id}/reconstruct')
def reconstruct_segment_endpoint(session_id: str, segment_id: str) -> dict:
    validate_identifier(session_id, 'Session ID')
    validate_identifier(segment_id, 'Segment ID')
    with connect() as connection:
        segment_index, chunk_paths = _segment_chunk_paths(connection, session_id, segment_id)
    try:
        output_path = reconstruct_segment(DATA_DIR, session_id, segment_index, chunk_paths)
    except ReconstructionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {'ok': True, 'sessionId': session_id, 'segmentId': segment_id, 'segmentIndex': segment_index, 'path': str(output_path.relative_to(DATA_DIR))}


@app.get('/api/v1/sessions/{session_id}/segments/{segment_id}/audio')
def get_segment_audio(session_id: str, segment_id: str) -> FileResponse:
    validate_identifier(session_id, 'Session ID')
    validate_identifier(segment_id, 'Segment ID')
    with connect() as connection:
        segment_index, chunk_paths = _segment_chunk_paths(connection, session_id, segment_id)
    try:
        output_path = reconstruct_segment(DATA_DIR, session_id, segment_index, chunk_paths)
    except ReconstructionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return FileResponse(output_path, media_type='audio/webm', filename=f'{session_id}-segment-{segment_index}.webm')


@app.post('/api/v1/sessions/{session_id}/reconstruct')
def reconstruct_session_endpoint(session_id: str) -> dict:
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
        output_path = reconstruct_session(DATA_DIR, session_id, segment_paths)
    except ReconstructionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {'ok': True, 'sessionId': session_id, 'segmentCount': len(segment_paths), 'path': str(output_path.relative_to(DATA_DIR))}


@app.get('/api/v1/sessions/{session_id}/audio')
def get_session_audio(session_id: str) -> FileResponse:
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
        output_path = reconstruct_session(DATA_DIR, session_id, segment_paths)
    except ReconstructionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return FileResponse(output_path, media_type='audio/webm', filename=f'{session_id}.webm')


@app.post('/api/v1/sessions/{session_id}/transcribe')
def transcribe_session(session_id: str, payload: TranscriptionPayload | None = None) -> dict:
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
    validate_identifier(session_id, 'Session ID')
    path = DATA_DIR / 'processed' / 'sessions' / session_id / 'report.json'
    if not path.is_file():
        raise HTTPException(status_code=404, detail='该 Session 尚未生成报告草稿')
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception as error:
        raise HTTPException(status_code=500, detail=f'报告草稿读取失败：{error}') from error


@app.get('/api/v1/sessions/{session_id}/summary.svg')
def get_summary_image(session_id: str) -> Response:
    validate_identifier(session_id, 'Session ID')
    path = DATA_DIR / 'processed' / 'sessions' / session_id / 'report.json'
    if not path.is_file():
        raise HTTPException(status_code=404, detail='该 Session 尚未生成报告草稿')
    try:
        report = json.loads(path.read_text(encoding='utf-8'))
        svg = render_summary_svg(report)
    except (OSError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=500, detail=f'总结图片生成失败：{error}') from error
    return Response(content=svg, media_type='image/svg+xml', headers={'Content-Disposition': f'inline; filename="{session_id}-summary.svg"'})


@app.post('/api/v1/sessions/{session_id}/process', status_code=202, response_model=ProcessingJobResponse)
def start_processing_job(session_id: str, payload: TranscriptionPayload | None = None) -> dict:
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
    validate_identifier(job_id, 'Job ID')
    try:
        state = get_job(DATA_DIR, job_id)
    except JobError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    if state is None:
        raise HTTPException(status_code=404, detail='处理任务不存在')
    return state


@app.get('/api/v1/sessions/{session_id}/processing')
def get_latest_processing_job(session_id: str) -> dict:
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
