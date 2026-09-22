from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
import uuid
import base64
from typing import Any

from fastapi import HTTPException, Request


ADMIN_SESSION_TTL_SECONDS = max(900, int(os.environ.get('LIVENOTE_ADMIN_SESSION_TTL_SECONDS', '43200')))
_admin_sessions: dict[str, float] = {}
_admin_sessions_lock = threading.Lock()
_admin_login_failures: dict[str, tuple[int, float]] = {}
_admin_login_failures_lock = threading.Lock()


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def bearer_token(request: Request) -> str:
    authorization = request.headers.get('authorization', '')
    if authorization.lower().startswith('bearer '):
        return authorization[7:].strip()
    return ''


def require_static_token(request: Request, env_name: str, role: str) -> str:
    expected = os.environ.get(env_name, '').strip()
    if not expected:
        raise HTTPException(status_code=503, detail=f'{role} 凭证尚未配置')
    supplied = request.headers.get('x-admin-token' if role == 'admin' else 'x-worker-token', '') or bearer_token(request)
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail='凭证无效')
    return supplied


def admin_username() -> str:
    return os.environ.get('LIVENOTE_ADMIN_USERNAME', 'admin').strip() or 'admin'


def hash_admin_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=16_384, r=8, p=1, dklen=64)
    return 'scrypt$16384$8$1$' + base64.urlsafe_b64encode(salt).decode('ascii') + '$' + base64.urlsafe_b64encode(digest).decode('ascii')


def verify_admin_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n_value, r_value, p_value, salt_value, digest_value = encoded.split('$')
        if algorithm != 'scrypt':
            return False
        salt = base64.urlsafe_b64decode(salt_value.encode('ascii'))
        expected = base64.urlsafe_b64decode(digest_value.encode('ascii'))
        actual = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=int(n_value), r=int(r_value), p=int(p_value), dklen=len(expected))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, base64.binascii.Error):
        return False


def _database_credentials(connection: sqlite3.Connection | None) -> sqlite3.Row | None:
    if connection is None:
        return None
    return connection.execute('SELECT username, password_hash FROM admin_credentials WHERE id = 1').fetchone()


def admin_setup_available(connection: sqlite3.Connection | None = None) -> bool:
    if _database_credentials(connection) is not None:
        return False
    return not (os.environ.get('LIVENOTE_ADMIN_PASSWORD', '').strip() or os.environ.get('LIVENOTE_ADMIN_TOKEN', '').strip())


def admin_auth_configured(connection: sqlite3.Connection | None = None) -> bool:
    return not admin_setup_available(connection)


def create_admin_credentials(connection: sqlite3.Connection, username: str, password: str) -> None:
    if not admin_setup_available(connection):
        raise HTTPException(status_code=409, detail='管理员账号已经设置，不能重复初始化')
    connection.execute(
        'INSERT INTO admin_credentials(id, username, password_hash, created_at, updated_at) VALUES (1, ?, ?, ?, ?)',
        (username.strip(), hash_admin_password(password), int(time.time() * 1000), int(time.time() * 1000)),
    )


def _password_matches(password: str, connection: sqlite3.Connection | None = None) -> bool:
    database_row = _database_credentials(connection)
    if database_row is not None:
        return verify_admin_password(password, database_row['password_hash'])
    configured_password = os.environ.get('LIVENOTE_ADMIN_PASSWORD', '')
    if configured_password:
        return hmac.compare_digest(password, configured_password)
    # During migration, allow the old secret to be used as the password. This
    # keeps existing local deployments usable until the new password is set.
    legacy_token = os.environ.get('LIVENOTE_ADMIN_TOKEN', '').strip()
    return bool(legacy_token) and hmac.compare_digest(password, legacy_token)


def _login_client_key(request: Request) -> str:
    return request.client.host if request.client else 'unknown'


def _login_allowed(client_key: str) -> bool:
    now = time.monotonic()
    with _admin_login_failures_lock:
        attempts, blocked_until = _admin_login_failures.get(client_key, (0, 0.0))
        if blocked_until > now:
            return False
        if blocked_until:
            _admin_login_failures.pop(client_key, None)
    return attempts < 5


def _record_login_failure(client_key: str) -> None:
    now = time.monotonic()
    with _admin_login_failures_lock:
        attempts, blocked_until = _admin_login_failures.get(client_key, (0, 0.0))
        attempts += 1
        _admin_login_failures[client_key] = (attempts, now + 60.0 if attempts >= 5 else 0.0)


def _clear_login_failures(client_key: str) -> None:
    with _admin_login_failures_lock:
        _admin_login_failures.pop(client_key, None)


def create_admin_session(request: Request, username: str, password: str, connection: sqlite3.Connection | None = None) -> str:
    normalized_username = username.strip()
    client_key = _login_client_key(request)
    if not _login_allowed(client_key):
        raise HTTPException(status_code=429, detail='登录尝试过多，请稍后再试')
    database_row = _database_credentials(connection)
    expected_username = database_row['username'] if database_row is not None else admin_username()
    if not hmac.compare_digest(normalized_username, expected_username) or not _password_matches(password, connection):
        _record_login_failure(client_key)
        raise HTTPException(status_code=401, detail='管理员账号或密码错误')
    _clear_login_failures(client_key)
    token = secrets.token_urlsafe(32)
    with _admin_sessions_lock:
        now = time.monotonic()
        expired = [session for session, expires_at in _admin_sessions.items() if expires_at <= now]
        for session in expired:
            _admin_sessions.pop(session, None)
        _admin_sessions[hash_secret(token)] = now + ADMIN_SESSION_TTL_SECONDS
    return token


def require_admin_session(request: Request) -> str:
    token = request.headers.get('x-admin-session', '').strip()
    if not token:
        raise HTTPException(status_code=401, detail='缺少管理员登录会话')
    session_key = hash_secret(token)
    with _admin_sessions_lock:
        expires_at = _admin_sessions.get(session_key)
        if expires_at is None or expires_at <= time.monotonic():
            _admin_sessions.pop(session_key, None)
            raise HTTPException(status_code=401, detail='管理员登录会话已失效')
        _admin_sessions[session_key] = time.monotonic() + ADMIN_SESSION_TTL_SECONDS
    return token


def require_admin(request: Request) -> str:
    session_token = request.headers.get('x-admin-session', '').strip()
    if session_token:
        return require_admin_session(request)
    return require_static_token(request, 'LIVENOTE_ADMIN_TOKEN', 'admin')


def require_worker(request: Request) -> str:
    return require_static_token(request, 'LIVENOTE_WORKER_TOKEN', 'worker')


def has_valid_worker_token(request: Request | None) -> bool:
    """Return whether a request carries the configured Worker credential.

    This is intentionally separate from ``require_worker`` so the API gateway
    can let authenticated Workers reach their task endpoints without also
    requiring the server-wide browser/API key.
    """
    expected = os.environ.get('LIVENOTE_WORKER_TOKEN', '').strip()
    if not expected or request is None:
        return False
    supplied = request.headers.get('x-worker-token', '') or bearer_token(request)
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def generate_pairing_code() -> str:
    return f'{secrets.randbelow(1_000_000):06d}'


def generate_device_token() -> str:
    return secrets.token_urlsafe(32)


def authenticate_device(connection: sqlite3.Connection, request: Request) -> sqlite3.Row:
    token = bearer_token(request)
    if not token:
        raise HTTPException(status_code=401, detail='缺少设备凭证')
    row = connection.execute(
        '''SELECT device.id, device.user_id, user.status AS user_status, device.revoked_at
           FROM devices device JOIN users user ON user.id = device.user_id
           WHERE device.token_hash = ?''',
        (hash_secret(token),),
    ).fetchone()
    if row is None or row['revoked_at'] is not None or row['user_status'] != 'ACTIVE':
        raise HTTPException(status_code=401, detail='设备凭证无效或已撤销')
    return row


def new_id(prefix: str) -> str:
    return f'{prefix}-{uuid.uuid4()}'
