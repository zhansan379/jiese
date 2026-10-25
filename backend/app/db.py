"""PostgreSQL 连接层：线程池 + 字典行游标（FastAPI 线程池中安全使用）。"""
from __future__ import annotations

from contextlib import contextmanager

import psycopg2.pool
from psycopg2.extras import DictCursor

from .config import settings

_pool = None


def init_pool(minconn: int = 1, maxconn: int = 6):
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn, maxconn, cursor_factory=DictCursor, **settings.dsn
        )
    return _pool


def close_pool():
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None


@contextmanager
def conn_ctx(autocommit: bool = True):
    """从池中取连接，autocommit 模式执行（本应用读多写少，无长事务需求）。"""
    conn = _pool.getconn()
    try:
        conn.autocommit = autocommit
        yield conn
    finally:
        _pool.putconn(conn)
