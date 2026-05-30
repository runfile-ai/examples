"""asyncpg connection pool for the mimic_creditline database.

A single lazily-initialised pool is shared across tool calls. The DSN comes from
``MIMIC_DB_DSN`` and points at the least-privilege ``creditline_agent`` role.
"""
from __future__ import annotations

import os

import asyncpg

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        dsn = os.environ.get("MIMIC_DB_DSN")
        if not dsn:
            raise RuntimeError("MIMIC_DB_DSN is not set")
        _pool = await asyncpg.create_pool(dsn, min_size=1, max_size=8)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
