"""Shared asyncpg pool for the MCP server. DSN points at the local `dental`
database that holds both the ext.* and agent.* schemas. jsonb columns are
encoded/decoded transparently to/from Python objects."""
from __future__ import annotations

import json
import os
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import asyncpg
from dotenv import load_dotenv

load_dotenv()

_DSN = os.environ.get("DENTAL_DB_DSN")
if not _DSN:
    raise RuntimeError("DENTAL_DB_DSN is not set")

_pool: asyncpg.Pool | None = None


def _default(o: Any) -> Any:
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    raise TypeError(f"not JSON serialisable: {type(o)}")


def dumps(value: Any) -> str:
    """JSON encode tolerant of datetime / date / Decimal (for envelopes + tool results)."""
    return json.dumps(value, default=_default)


async def _init_conn(conn: asyncpg.Connection) -> None:
    # Pass/return jsonb and json as Python objects.
    for typ in ("jsonb", "json"):
        await conn.set_type_codec(
            typ,
            encoder=lambda v: json.dumps(v, default=_default),
            decoder=json.loads,
            schema="pg_catalog",
        )


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=8, init=_init_conn)
    return _pool
