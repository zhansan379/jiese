"""账号认证：pbkdf2 密码哈希 + 随机 token（存库校验），全部基于标准库。"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets

from fastapi import HTTPException, Header

from .db import conn_ctx

_PBKDF2_ITER = 120_000
_USERNAME_RE = re.compile(r"^[\w\u4e00-\u9fa5@.]{2,30}$")


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ITER).hex()
    return f"pbkdf2${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$", 2)
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ITER).hex()
    return hmac.compare_digest(calc, digest)


def validate_credentials(username: str, password: str) -> None:
    if not _USERNAME_RE.match(username or ""):
        raise HTTPException(400, "用户名需为 2-30 位字母/数字/中文，可含 @ . （邮箱可用作用户名）")
    if not password or len(password) < 6:
        raise HTTPException(400, "密码至少 6 位")


def issue_token(user_id: int) -> str:
    token = secrets.token_hex(32)
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO auth_tokens (token, user_id) VALUES (%s, %s)", (token, user_id))
    return token


def revoke_token(token: str) -> None:
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM auth_tokens WHERE token=%s", (token,))


def _auth_user(authorization: str | None) -> dict | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[7:].strip()
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT u.id, u.username, u.email FROM auth_tokens t"
            " JOIN users u ON u.id = t.user_id WHERE t.token = %s",
            (token,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def current_user(authorization: str | None = Header(default=None)) -> dict:
    """FastAPI 依赖：校验 Bearer token，返回用户行；未登录抛 401。"""
    user = _auth_user(authorization)
    if user is None:
        raise HTTPException(401, "未登录或登录已过期")
    return user


def optional_user(authorization: str | None = Header(default=None)) -> dict | None:
    """FastAPI 依赖：游客也可用的接口（如点赞点踩），返回用户行或 None。"""
    return _auth_user(authorization)
