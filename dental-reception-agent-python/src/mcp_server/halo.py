"""Halo — the content-addressed store at the tool-result boundary.

Heavy tool results are NOT returned to the model raw. Instead the heavy parts are
written to agent.halo_nodes keyed by a content handle (h:sha256:...), and the tool
returns a compact ENVELOPE: a small summary plus `refs` (handles) to the heavy
parts. The model reasons on the envelope and fetches only the handles a step
actually needs (halo_fetch / halo_fetch_many).

Because the store is Postgres-backed and persistent, a handle seen early in a
session is fetchable late. Maps (agent.halo_maps) keep the latest root per entity
(e.g. a patient id) so repeated calls about the same patient fold into one growing
map — "argument-join".
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from .db import dumps

Handle = str  # "h:sha256:<hex>"


@dataclass
class Envelope:
    """What the model actually sees: a compact summary + handles."""

    kind: str
    summary: Any
    refs: dict[str, Handle] = field(default_factory=dict)
    map_root: Handle | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": self.kind, "summary": self.summary, "refs": self.refs}
        if self.map_root is not None:
            d["map_root"] = self.map_root
        return d


def _handle_for(data: bytes) -> Handle:
    return "h:sha256:" + hashlib.sha256(data).hexdigest()


async def put_json(conn: asyncpg.Connection, value: Any) -> Handle:
    """Store a JSON value as a content-addressed node; return its handle."""
    data = dumps(value).encode("utf-8")
    handle = _handle_for(data)
    await conn.execute(
        "INSERT INTO agent.halo_nodes (handle, bytes) VALUES ($1, $2) ON CONFLICT (handle) DO NOTHING",
        handle,
        data,
    )
    return handle


async def get_json(conn: asyncpg.Connection, handle: Handle) -> Any:
    """Fetch a single node's decoded JSON."""
    row = await conn.fetchrow("SELECT bytes FROM agent.halo_nodes WHERE handle = $1", handle)
    if row is None:
        return {"error": "handle_not_found", "handle": handle}
    return json.loads(bytes(row["bytes"]).decode("utf-8"))


async def get_many(conn: asyncpg.Connection, handles: list[Handle]) -> dict[Handle, Any]:
    """Fetch many nodes in one round trip (batched drill-down)."""
    if not handles:
        return {}
    rows = await conn.fetch("SELECT handle, bytes FROM agent.halo_nodes WHERE handle = ANY($1)", handles)
    found = {r["handle"]: bytes(r["bytes"]) for r in rows}
    out: dict[Handle, Any] = {}
    for h in handles:
        b = found.get(h)
        out[h] = json.loads(b.decode("utf-8")) if b is not None else {"error": "handle_not_found", "handle": h}
    return out


async def encode(conn: asyncpg.Connection, kind: str, summary: Any, heavy: dict[str, Any]) -> Envelope:
    """Encode a tool result into an envelope: stash each heavy field under a handle,
    keep a compact summary inline."""
    refs: dict[str, Handle] = {}
    for name, value in heavy.items():
        if value is not None:
            refs[name] = await put_json(conn, value)
    return Envelope(kind=kind, summary=summary, refs=refs)


async def accumulate(
    conn: asyncpg.Connection, session_id: str, map_id: str, envelope: Envelope, source: Any
) -> Handle:
    """Accumulate an envelope into the per-entity map (argument-join). Stores the
    envelope as a node and records it as the latest root for (session, map_id)."""
    root = await put_json(conn, envelope.to_dict())
    await conn.execute(
        """INSERT INTO agent.halo_maps (session_id, map_id, root, source, updated_at)
             VALUES ($1, $2, $3, $4, now())
             ON CONFLICT (session_id, map_id) DO UPDATE SET root = EXCLUDED.root,
               source = EXCLUDED.source, updated_at = now()""",
        session_id,
        map_id,
        root,
        source,
    )
    return root
