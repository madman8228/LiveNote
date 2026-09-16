from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

SERVER_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('LIVENOTE_DATA_DIR', SERVER_DIR / 'data'))
DB_PATH = Path(os.environ.get('LIVENOTE_DB_PATH', SERVER_DIR / 'livenote.sqlite3'))


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys = ON')
    return connection


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as connection:
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


class SessionPayload(BaseModel):
    id: str
    title: str
    startedAt: int
    endedAt: int | None = None
    status: str
    durationMs: int = 0
    createdAt: int
    updatedAt: int


class SegmentPayload(BaseModel):
    id: str
    sessionId: str
    index: int
    startedAt: int
    endedAt: int | None = None
    mimeType: str = ''
    mediaSettings: dict = Field(default_factory=dict)
    status: str
    durationMs: int = 0


class MarkerPayload(BaseModel):
    id: str
    sessionId: str
    type: str
    elapsedMs: int
    wallClockMs: int
    note: str = ''
    createdAt: int


init_db()
app = FastAPI(title='LiveNote API', version='0.1.0')
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=False,
    allow_methods=['*'],
    allow_headers=['*'],
)


@app.get('/health')
def health() -> dict:
    return {'ok': True, 'service': 'livenote-api'}


@app.post('/api/v1/sessions')
def create_session(payload: SessionPayload) -> dict:
    with connect() as connection:
        connection.execute(
            '''INSERT INTO sessions(id, title, started_at, ended_at, status, duration_ms, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET title=excluded.title, ended_at=excluded.ended_at,
               status=excluded.status, duration_ms=excluded.duration_ms, updated_at=excluded.updated_at''',
            (payload.id, payload.title, payload.startedAt, payload.endedAt, payload.status, payload.durationMs, payload.createdAt, payload.updatedAt),
        )
    return {'id': payload.id, 'created': True}


@app.post('/api/v1/sessions/{session_id}/segments')
def create_segment(session_id: str, payload: SegmentPayload) -> dict:
    if payload.sessionId != session_id:
        raise HTTPException(status_code=400, detail='sessionId 不匹配')
    with connect() as connection:
        if connection.execute('SELECT 1 FROM sessions WHERE id = ?', (session_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail='Session 不存在')
        connection.execute(
            '''INSERT INTO segments(id, session_id, segment_index, started_at, ended_at, mime_type,
               media_settings, status, duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET ended_at=excluded.ended_at, mime_type=excluded.mime_type,
               media_settings=excluded.media_settings, status=excluded.status, duration_ms=excluded.duration_ms''',
            (payload.id, session_id, payload.index, payload.startedAt, payload.endedAt, payload.mimeType, json.dumps(payload.mediaSettings, ensure_ascii=False), payload.status, payload.durationMs),
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
    if not x_chunk_sha256 or x_chunk_size is None or x_chunk_elapsed_ms is None:
        raise HTTPException(status_code=400, detail='缺少 Chunk 校验 Header')
    body = await request.body()
    if len(body) != x_chunk_size:
        raise HTTPException(status_code=400, detail='Chunk size 不一致')
    actual_sha256 = hashlib.sha256(body).hexdigest()
    if actual_sha256 != x_chunk_sha256:
        raise HTTPException(status_code=400, detail='Chunk SHA256 不一致')

    with connect() as connection:
        session = connection.execute('SELECT 1 FROM sessions WHERE id = ?', (session_id,)).fetchone()
        segment = connection.execute('SELECT segment_index FROM segments WHERE id = ? AND session_id = ?', (segment_id, session_id)).fetchone()
        if session is None or segment is None:
            raise HTTPException(status_code=404, detail='Session 或 Segment 不存在')

        existing = connection.execute('SELECT size, sha256, local_path FROM chunks WHERE segment_id = ? AND chunk_index = ?', (segment_id, chunk_index)).fetchone()
        if existing is not None:
            if existing['size'] == len(body) and existing['sha256'] == actual_sha256:
                return {'ok': True, 'already_exists': True, 'verified': True}
            raise HTTPException(status_code=409, detail='相同 Segment/Chunk index 的 SHA256 不一致')

        mime_type = request.headers.get('content-type', 'application/octet-stream')
        relative_path = Path('sessions') / session_id / f"segment_{segment['segment_index']}" / 'chunks' / f'{chunk_index:06d}.bin'
        absolute_path = DATA_DIR / relative_path
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = absolute_path.with_name(f'.{absolute_path.name}.{uuid.uuid4().hex}.tmp')
        temporary_path.write_bytes(body)
        temporary_path.replace(absolute_path)
        received_at = now_ms()
        connection.execute(
            '''INSERT INTO chunks(session_id, segment_id, chunk_index, size, sha256, mime_type,
               elapsed_ms, created_at, received_at, local_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (session_id, segment_id, chunk_index, len(body), actual_sha256, mime_type, x_chunk_elapsed_ms, received_at, received_at, str(relative_path)),
        )
    return {'ok': True, 'already_exists': False, 'verified': True}


@app.post('/api/v1/sessions/{session_id}/segments/{segment_id}/complete')
def complete_segment(session_id: str, segment_id: str) -> dict:
    with connect() as connection:
        result = connection.execute('UPDATE segments SET status = ?, ended_at = COALESCE(ended_at, ?) WHERE id = ? AND session_id = ?', ('COMPLETED', now_ms(), segment_id, session_id))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail='Segment 不存在')
    return {'ok': True}


@app.post('/api/v1/sessions/{session_id}/complete')
def complete_session(session_id: str) -> dict:
    with connect() as connection:
        result = connection.execute('UPDATE sessions SET status = ?, ended_at = COALESCE(ended_at, ?), updated_at = ? WHERE id = ?', ('COMPLETED', now_ms(), now_ms(), session_id))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail='Session 不存在')
    return {'ok': True}


@app.post('/api/v1/sessions/{session_id}/markers')
def create_marker(session_id: str, payload: MarkerPayload) -> dict:
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


@app.get('/api/v1/sessions/{session_id}/upload-state')
def upload_state(session_id: str) -> dict:
    with connect() as connection:
        if connection.execute('SELECT 1 FROM sessions WHERE id = ?', (session_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail='Session 不存在')
        segments = connection.execute('SELECT id FROM segments WHERE session_id = ? ORDER BY segment_index', (session_id,)).fetchall()
        result = []
        for segment in segments:
            chunks = connection.execute('SELECT chunk_index, size, sha256 FROM chunks WHERE segment_id = ? ORDER BY chunk_index', (segment['id'],)).fetchall()
            result.append({'segmentId': segment['id'], 'chunks': [{'index': chunk['chunk_index'], 'size': chunk['size'], 'sha256': chunk['sha256']} for chunk in chunks]})
    return {'sessionId': session_id, 'segments': result}


if __name__ == '__main__':
    import uvicorn

    uvicorn.run('main:app', host='0.0.0.0', port=int(os.environ.get('PORT', '8000')), reload=False)
