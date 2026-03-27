"""PostgreSQL 接続管理"""

import psycopg2
import psycopg2.pool
from pgvector.psycopg2 import register_vector

from .config import DATABASE_URL

_pool = None


def get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(1, 5, DATABASE_URL)
    return _pool


def get_conn():
    conn = get_pool().getconn()
    register_vector(conn)
    return conn


def put_conn(conn):
    get_pool().putconn(conn)
