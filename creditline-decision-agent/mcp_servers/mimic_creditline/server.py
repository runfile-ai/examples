"""mimic-creditline MCP server.

Seven domain-shaped tools over the simulated ``mimic_creditline`` Postgres.
Transport: stdio (local demo) — run with ``python -m
mcp_servers.mimic_creditline.server``. Inputs are validated with Pydantic; tools
return JSON-safe dicts so downstream records are semantic, not opaque SQL.

The agent is given ONLY these tools. Nothing here can rewrite history; the
HITL gate (`creditline_request_approval`) blocks the run until a human resolves
the pending approval row out of band.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import os
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import db
from .models import (
    GetActivePolicyIn,
    GetCustomerIn,
    GetRequestIn,
    NotifyCustomerIn,
    PullBureauIn,
    RecordDecisionIn,
    RequestApprovalIn,
)

mcp = FastMCP("mimic-creditline")


# ── serialisation helper ─────────────────────────────────────────────────────
def _jsonify(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(v) for v in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _row(record: Any) -> dict[str, Any] | None:
    return _jsonify(dict(record)) if record is not None else None


# Canonical prompt source — CLAUDE.md governs the agent in every runtime.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CLAUDE_MD = _PROJECT_ROOT / "CLAUDE.md"
_FALLBACK_PROMPT = (
    b"You are the Credit-Line Decision Agent. Act only through the mimic-creditline "
    b"MCP tools; intake -> bureau -> policy -> score -> decide -> record -> human "
    b"approval. Never auto-deny; every adverse outcome escalates to a human."
)


def _prompt_version_hash() -> str:
    try:
        data = _CLAUDE_MD.read_bytes()
    except OSError:
        data = _FALLBACK_PROMPT
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ── 0. get_agent_provenance ──────────────────────────────────────────────────
@mcp.tool(
    annotations=ToolAnnotations(
        title="Get canonical agent prompt provenance",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def creditline_get_agent_provenance() -> dict[str, Any]:
    """Return the canonical ``prompt_version_hash`` (sha256 over CLAUDE.md).

    Call this before recording a decision and pass the returned
    ``prompt_version_hash`` into ``creditline_record_decision``. This ensures the
    recorded provenance pins the exact governing instructions regardless of how
    the agent was launched (Claude Code CLI or Agent SDK).
    """
    return {
        "prompt_version_hash": _prompt_version_hash(),
        "prompt_source": "CLAUDE.md",
        "agent_id": "creditline-decision-agent",
    }


# ── 1. get_request ───────────────────────────────────────────────────────────
@mcp.tool(
    annotations=ToolAnnotations(
        title="Get credit-line request",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def creditline_get_request(request_id: str) -> dict[str, Any]:
    """Fetch the inbound credit-line request by id."""
    args = GetRequestIn(request_id=request_id)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rec = await conn.fetchrow(
            "SELECT * FROM credit_line_requests WHERE request_id = $1",
            uuid.UUID(args.request_id),
        )
    if rec is None:
        return {"error": "request_not_found", "request_id": request_id}
    return _row(rec)  # type: ignore[return-value]


# ── 2. get_customer ──────────────────────────────────────────────────────────
@mcp.tool(
    annotations=ToolAnnotations(
        title="Get customer profile + existing lines",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def creditline_get_customer(customer_id: str) -> dict[str, Any]:
    """Fetch the customer profile together with their existing credit lines."""
    args = GetCustomerIn(customer_id=customer_id)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        cust = await conn.fetchrow(
            "SELECT * FROM customers WHERE customer_id = $1", uuid.UUID(args.customer_id)
        )
        if cust is None:
            return {"error": "customer_not_found", "customer_id": customer_id}
        lines = await conn.fetch(
            "SELECT * FROM credit_lines WHERE customer_id = $1 ORDER BY opened_at",
            uuid.UUID(args.customer_id),
        )
    return {
        "customer": _row(cust),
        "credit_lines": [_row(r) for r in lines],
    }


# ── 3. pull_bureau ───────────────────────────────────────────────────────────
@mcp.tool(
    annotations=ToolAnnotations(
        title="Pull simulated credit-bureau report",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=False,  # a real pull is a fresh hard inquiry
        openWorldHint=True,    # external (simulated) system
    )
)
async def creditline_pull_bureau(
    customer_id: str, bureau_name: str = "experian_sim"
) -> dict[str, Any]:
    """Pull the most recent simulated bureau report for the customer.

    Models an external bureau call. Returns the latest report on file for the
    given (customer, bureau); the seeded data provides deterministic reports so
    the resulting decision is reproducible for audit.
    """
    args = PullBureauIn(customer_id=customer_id, bureau_name=bureau_name)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rec = await conn.fetchrow(
            """
            SELECT * FROM bureau_reports
            WHERE customer_id = $1 AND bureau_name = $2
            ORDER BY pulled_at DESC
            LIMIT 1
            """,
            uuid.UUID(args.customer_id),
            args.bureau_name,
        )
    if rec is None:
        return {"error": "no_bureau_report", "customer_id": customer_id, "bureau": bureau_name}
    return _row(rec)  # type: ignore[return-value]


# ── 4. get_active_policy ─────────────────────────────────────────────────────
@mcp.tool(
    annotations=ToolAnnotations(
        title="Retrieve active versioned decision policy",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def creditline_get_active_policy() -> dict[str, Any]:
    """Return the currently active policy (effective_to IS NULL): the retrieval node."""
    GetActivePolicyIn()
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rec = await conn.fetchrow(
            "SELECT * FROM decision_policies WHERE effective_to IS NULL "
            "ORDER BY effective_from DESC LIMIT 1"
        )
    if rec is None:
        return {"error": "no_active_policy"}
    out = _row(rec)
    # thresholds is stored as jsonb; asyncpg returns it as a string.
    if isinstance(out.get("thresholds"), str):  # type: ignore[union-attr]
        out["thresholds"] = json.loads(out["thresholds"])  # type: ignore[index]
    return out  # type: ignore[return-value]


# ── 5. record_decision ───────────────────────────────────────────────────────
@mcp.tool(
    annotations=ToolAnnotations(
        title="Persist decision outcome + provenance",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,  # by request_id (UNIQUE)
        openWorldHint=False,
    )
)
async def creditline_record_decision(
    request_id: str,
    outcome: str,
    rationale: str,
    model_version: str,
    prompt_version_hash: str,
    policy_version: str,
    bureau_report_id: str,
    approved_limit: float | None = None,
) -> dict[str, Any]:
    """Persist the decision + full provenance. Idempotent by request_id.

    ``requires_human_approval`` is computed server-side from the active policy:
    any non-approved outcome, or an approval above the auto-approve ceiling,
    must be confirmed by a human (GDPR Art. 22 / EU AI Act Art. 14).
    """
    args = RecordDecisionIn(
        request_id=request_id,
        outcome=outcome,
        rationale=rationale,
        model_version=model_version,
        prompt_version_hash=prompt_version_hash,
        policy_version=policy_version,
        bureau_report_id=bureau_report_id,
        approved_limit=Decimal(str(approved_limit)) if approved_limit is not None else None,
    )
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            req = await conn.fetchrow(
                "SELECT customer_id, requested_limit FROM credit_line_requests WHERE request_id = $1",
                uuid.UUID(args.request_id),
            )
            if req is None:
                return {"error": "request_not_found", "request_id": request_id}

            # Idempotency: return the existing decision if one already exists.
            existing = await conn.fetchrow(
                "SELECT decision_id, requires_human_approval FROM decisions WHERE request_id = $1",
                uuid.UUID(args.request_id),
            )
            if existing is not None:
                return {
                    "decision_id": str(existing["decision_id"]),
                    "requires_human_approval": existing["requires_human_approval"],
                    "idempotent_replay": True,
                }

            ceiling = await conn.fetchval(
                "SELECT (thresholds->>'auto_approve_ceiling')::numeric "
                "FROM decision_policies WHERE version = $1",
                args.policy_version,
            )
            above_ceiling = (
                args.outcome == "approved"
                and args.approved_limit is not None
                and ceiling is not None
                and args.approved_limit > ceiling
            )
            requires_human = args.outcome != "approved" or above_ceiling

            decision_id = uuid.uuid4()
            await conn.execute(
                """
                INSERT INTO decisions
                    (decision_id, request_id, customer_id, outcome, approved_limit,
                     rationale, model_version, prompt_version_hash, policy_version,
                     bureau_report_id, requires_human_approval)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                """,
                decision_id,
                uuid.UUID(args.request_id),
                req["customer_id"],
                args.outcome,
                args.approved_limit,
                args.rationale,
                args.model_version,
                args.prompt_version_hash,
                args.policy_version,
                uuid.UUID(args.bureau_report_id),
                requires_human,
            )
            # Reflect the outcome onto the request row.
            await conn.execute(
                "UPDATE credit_line_requests SET status = $1 WHERE request_id = $2",
                args.outcome,
                uuid.UUID(args.request_id),
            )

    return {
        "decision_id": str(decision_id),
        "requires_human_approval": requires_human,
        "idempotent_replay": False,
    }


# ── 6. request_approval (HITL interrupt) ─────────────────────────────────────
@mcp.tool(
    annotations=ToolAnnotations(
        title="Open human-in-the-loop approval gate (blocks until resolved)",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,  # by decision_id
        openWorldHint=True,   # waits on a human
    )
)
async def creditline_request_approval(
    decision_id: str, summary: str, approver_role: str = "lead_credit_officer"
) -> dict[str, Any]:
    """Open the HITL gate and BLOCK until a human credit officer resolves it.

    Creates (or reuses) a pending ``approvals`` row, then polls until its status
    leaves ``pending``. The human resolves it out of band (see
    ``scripts/officer_console.py``). On resolution returns who/what/why; if the
    human went against the agent's recommendation, ``is_override`` is true — the
    Art. 14 / SR 11-7 effective-challenge evidence.
    """
    args = RequestApprovalIn(decision_id=decision_id, summary=summary, approver_role=approver_role)
    timeout = float(os.environ.get("APPROVAL_TIMEOUT_SECONDS", "900"))
    poll = float(os.environ.get("APPROVAL_POLL_SECONDS", "2"))
    pool = await db.get_pool()

    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT approval_id, status FROM approvals WHERE decision_id = $1 "
            "ORDER BY requested_at DESC LIMIT 1",
            uuid.UUID(args.decision_id),
        )
        if existing is not None and existing["status"] == "pending":
            approval_id = existing["approval_id"]
        elif existing is not None and existing["status"] != "pending":
            # Already resolved — idempotent replay.
            return await _approval_result(conn, existing["approval_id"])
        else:
            approval_id = uuid.uuid4()
            await conn.execute(
                """
                INSERT INTO approvals (approval_id, decision_id, approver_role, status, justification)
                VALUES ($1, $2, $3, 'pending', $4)
                """,
                approval_id,
                uuid.UUID(args.decision_id),
                args.approver_role,
                args.summary,
            )

    waited = 0.0
    while waited < timeout:
        async with pool.acquire() as conn:
            status = await conn.fetchval(
                "SELECT status FROM approvals WHERE approval_id = $1", approval_id
            )
            if status and status != "pending":
                return await _approval_result(conn, approval_id)
        await asyncio.sleep(poll)
        waited += poll

    return {"status": "timeout", "approval_id": str(approval_id), "waited_seconds": waited}


async def _approval_result(conn: Any, approval_id: uuid.UUID) -> dict[str, Any]:
    rec = await conn.fetchrow("SELECT * FROM approvals WHERE approval_id = $1", approval_id)
    return {
        "approval_id": str(rec["approval_id"]),
        "decision_id": str(rec["decision_id"]),
        "status": rec["status"],
        "is_override": rec["is_override"],
        "modified_limit": float(rec["modified_limit"]) if rec["modified_limit"] is not None else None,
        "approver_id": rec["approver_id"],
        "approver_role": rec["approver_role"],
        "justification": rec["justification"],
        "resolved_at": rec["resolved_at"].isoformat() if rec["resolved_at"] else None,
    }


# ── 7. notify_customer (optional, v1.5) ──────────────────────────────────────
@mcp.tool(
    annotations=ToolAnnotations(
        title="Send decision letter (simulated)",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,  # by idempotency key
        openWorldHint=True,   # external (simulated) delivery
    )
)
async def creditline_notify_customer(
    request_id: str, outcome: str, idempotency_key: str, approved_limit: float | None = None
) -> dict[str, Any]:
    """Simulate sending the decision letter to the customer. Idempotent by key."""
    args = NotifyCustomerIn(
        request_id=request_id,
        outcome=outcome,
        idempotency_key=idempotency_key,
        approved_limit=Decimal(str(approved_limit)) if approved_limit is not None else None,
    )
    # No outbound side effect in the simulation — return a deterministic receipt.
    return {
        "delivered": True,
        "channel": "letter_sim",
        "request_id": args.request_id,
        "outcome": args.outcome,
        "approved_limit": float(args.approved_limit) if args.approved_limit is not None else None,
        "idempotency_key": args.idempotency_key,
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
