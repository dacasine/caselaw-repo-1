"""Postgres connection pool for the PA-RAG pipeline.

Reads CASELAW_PG_URL from environment (or .env file).
Provides a thread-safe connection pool via psycopg_pool.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None


def _load_env() -> None:
    for env_path in [Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"]:
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
            break


def get_pg_url() -> str:
    _load_env()
    url = os.environ.get("CASELAW_PG_URL", "")
    if not url:
        raise RuntimeError(
            "CASELAW_PG_URL not set. Example: "
            "CASELAW_PG_URL=postgres://caselaw:pw@localhost:5432/caselaw"
        )
    return url


def get_pool(min_size: int = 2, max_size: int = 32) -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            get_pg_url(),
            min_size=min_size,
            max_size=max_size,
            kwargs={"autocommit": False},
        )
    return _pool


def get_conn() -> psycopg.Connection:
    return psycopg.connect(get_pg_url(), autocommit=False)
