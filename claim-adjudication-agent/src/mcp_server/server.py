"""claims-mcp — stdio MCP server (Python).

Intent-shaped tools over the local Postgres. The tool CONTRACT is the swap point:
today each read body is SQL on ext.*; later it is a payer feed / X12 transaction.
Signatures and returned shapes do not change.

The non-negotiables of this agent are enforced here, not in the prompt:
  • adjudicate_line runs the DETERMINISTIC engine (src/mcp_server/engine.py). The
    model never computes money and never invents a code.
  • post_adjudication is the DECISION GATE: any deny / reduce / pend line, or a
    claim total above the auto-finalize ceiling, BLOCKS on a human reviewer. Only a
    clean, all-pay, within-ceiling claim auto-finalizes — and even then with full
    evidence recorded.
  • agent.decisions.evidence stores the Halo handles the decision rested on; since
    a handle is sha256(content), that evidence is tamper-evident (halo_verify).

Run as a module:  python -m src.mcp_server.server
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from . import halo
from .db import dumps, get_pool
from .engine import adjudicate_line as engine_adjudicate_line

SESSION_ID = uuid.UUID(os.environ.get("AGENT_SESSION_ID") or str(uuid.uuid4()))
CHANNEL = os.environ.get("CLAIMS_CHANNEL", "queue")
AUTO_FINALIZE_CEILING = int(os.environ.get("AUTO_FINALIZE_CEILING_CENTS", "20000"))

_LINE_STATUS = {"pay": "paid", "deny": "denied", "reduce": "reduced", "pend": "pended"}


async def ensure_session(claim_id: str | None = None) -> None:
    pool = await get_pool()
    async with pool.acquire() as c:
        await c.execute(
            "INSERT INTO agent.sessions (id, claim_id, channel, status) VALUES ($1, $2, $3, 'active') "
            "ON CONFLICT (id) DO NOTHING",
            SESSION_ID, claim_id, CHANNEL,
        )


async def _record_tool_call(tool: str, args: Any, env_root: str | None, latency_ms: int, ok: bool, error: str | None) -> None:
    try:
        pool = await get_pool()
        async with pool.acquire() as c:
            await c.execute(
                "INSERT INTO agent.tool_calls (id, session_id, tool, args, envelope_root, latency_ms, ok, error) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
                uuid.uuid4(), SESSION_ID, tool, args or {}, env_root, latency_ms, ok, error,
            )
    except Exception:
        pass


async def _await_approval(action: str, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
    timeout_s = int(os.environ.get("APPROVAL_TIMEOUT_SECONDS", "900"))
    poll_s = int(os.environ.get("APPROVAL_POLL_SECONDS", "2"))
    pool = await get_pool()
    async with pool.acquire() as c:
        existing = await c.fetchrow("SELECT id, status, decided_by FROM agent.approvals WHERE idempotency_key = $1", idempotency_key)
        if existing and existing["status"] != "pending":
            return dict(existing)
        if existing:
            approval_id = existing["id"]
        else:
            approval_id = uuid.uuid4()
            await c.execute(
                "INSERT INTO agent.approvals (id, session_id, action, payload, idempotency_key, status) "
                "VALUES ($1,$2,$3,$4,$5,'pending') ON CONFLICT (idempotency_key) DO NOTHING",
                approval_id, SESSION_ID, action, payload, idempotency_key,
            )
    waited = 0
    while waited < timeout_s:
        async with pool.acquire() as c:
            r = await c.fetchrow("SELECT id, status, decided_by FROM agent.approvals WHERE id = $1", approval_id)
        if r and r["status"] != "pending":
            return dict(r)
        await asyncio.sleep(poll_s)
        waited += poll_s
    return {"id": approval_id, "status": "timeout", "decided_by": None}


# ── reads ─────────────────────────────────────────────────────────────────────
async def get_claim(args: dict[str, Any]) -> halo.Envelope | dict[str, Any]:
    claim_id = str(args["claim_id"])
    pool = await get_pool()
    async with pool.acquire() as c:
        claim = await c.fetchrow("SELECT * FROM ext.claims WHERE id = $1", claim_id)
        if not claim:
            return {"error": "claim_not_found", "claim_id": claim_id}
        lines = [dict(r) for r in await c.fetch(
            "SELECT * FROM ext.claim_lines WHERE claim_id = $1 ORDER BY line_number", claim_id)]
        line_summary = [
            {"line_number": ln["line_number"], "procedure_code": ln["procedure_code"], "tooth": ln["tooth"],
             "date_of_service": ln["date_of_service"], "units": ln["units"], "charged_cents": ln["charged_cents"],
             "status": ln["status"]}
            for ln in lines
        ]
        summary = {
            "claim_id": claim["id"], "claim_number": claim["claim_number"], "member_id": claim["member_id"],
            "provider_id": claim["provider_id"], "date_received": claim["date_received"],
            "place_of_service": claim["place_of_service"], "total_charged_cents": claim["total_charged_cents"],
            "status": claim["status"], "n_lines": len(lines), "lines": line_summary,
        }
        env = await halo.encode(c, "claim", summary, {
            "full_lines": lines,
            "diagnosis_codes": claim["diagnosis_codes"],
            "attachments": claim["attachments"],
        })
        env.map_root = await halo.accumulate(c, SESSION_ID, claim_id, env, {"claim_id": claim_id})
    return env


async def get_member_coverage(args: dict[str, Any]) -> dict[str, Any]:
    member_id = str(args["member_id"])
    pool = await get_pool()
    async with pool.acquire() as c:
        m = await c.fetchrow(
            "SELECT m.*, p.name AS plan_name, p.type AS plan_type, p.annual_max_cents, p.deductible_cents, "
            "p.oop_max_cents, p.coinsurance FROM ext.members m LEFT JOIN ext.plans p ON p.id = m.plan_id WHERE m.id = $1",
            member_id,
        )
    if not m:
        return {"error": "member_not_found", "member_id": member_id}
    today = date.today()
    eligible = (
        m["status"] == "active"
        and (m["effective_date"] is None or m["effective_date"] <= today)
        and (m["term_date"] is None or m["term_date"] >= today)
    )
    return {
        "member_id": m["id"],
        "name": f"{m['first_name']} {m['last_name']}",
        "dob": m["dob"],
        "status": m["status"],
        "eligible": eligible,
        "effective_date": m["effective_date"],
        "term_date": m["term_date"],
        "plan": {
            "id": m["plan_id"], "name": m["plan_name"], "type": m["plan_type"],
            "annual_max_cents": m["annual_max_cents"], "deductible_cents": m["deductible_cents"],
            "oop_max_cents": m["oop_max_cents"], "coinsurance": m["coinsurance"],
        },
    }


async def get_benefit_rules(args: dict[str, Any]) -> dict[str, Any]:
    plan_id = str(args["plan_id"])
    codes = list(args.get("procedure_codes", []))
    pool = await get_pool()
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT * FROM ext.benefit_rules WHERE plan_id = $1 AND procedure_code = ANY($2)", plan_id, codes
        )
    return {"plan_id": plan_id, "rules": [dict(r) for r in rows], "count": len(rows)}


async def get_accumulators(args: dict[str, Any]) -> dict[str, Any]:
    member_id = str(args["member_id"])
    plan_year = int(args.get("plan_year") or date.today().year)
    pool = await get_pool()
    async with pool.acquire() as c:
        r = await c.fetchrow("SELECT * FROM ext.accumulators WHERE member_id = $1 AND plan_year = $2", member_id, plan_year)
    if not r:
        return {"member_id": member_id, "plan_year": plan_year, "deductible_met_cents": 0, "annual_max_used_cents": 0, "oop_met_cents": 0}
    return dict(r)


async def get_claim_history(args: dict[str, Any]) -> halo.Envelope:
    member_id = str(args["member_id"])
    code = args.get("code")
    window_months = int(args.get("window_months") or 12)
    exclude_claim_id = args.get("exclude_claim_id")  # the claim under adjudication — never count itself
    pool = await get_pool()
    async with pool.acquire() as c:
        rows = [dict(r) for r in await c.fetch(
            """SELECT cl.id, cl.claim_id, cl.line_number, cl.procedure_code, cl.tooth, cl.date_of_service,
                      cl.status, cl.plan_paid_cents, c.date_received
                 FROM ext.claim_lines cl JOIN ext.claims c ON c.id = cl.claim_id
                WHERE c.member_id = $1
                  AND ($2::text IS NULL OR cl.procedure_code = $2)
                  AND ($4::text IS NULL OR cl.claim_id <> $4)
                  AND cl.date_of_service >= (current_date - make_interval(months => $3))
                ORDER BY cl.date_of_service DESC""",
            member_id, code, window_months, exclude_claim_id,
        )]
        by_code: dict[str, int] = {}
        for r in rows:
            by_code[r["procedure_code"]] = by_code.get(r["procedure_code"], 0) + 1
        summary = {
            "member_id": member_id, "code_filter": code, "window_months": window_months,
            "total": len(rows), "by_code": by_code,
            "recent": [{"procedure_code": r["procedure_code"], "tooth": r["tooth"], "date_of_service": r["date_of_service"], "status": r["status"]} for r in rows[:8]],
        }
        env = await halo.encode(c, "claim_history", summary, {"lines": rows})
        env.map_root = await halo.accumulate(c, SESSION_ID, member_id, env, {"member_id": member_id})
    return env


async def check_network(args: dict[str, Any]) -> dict[str, Any]:
    provider_id = str(args["provider_id"])
    plan_id = str(args["plan_id"])
    pool = await get_pool()
    async with pool.acquire() as c:
        r = await c.fetchrow("SELECT in_network FROM ext.network WHERE plan_id = $1 AND provider_id = $2", plan_id, provider_id)
        prov = await c.fetchrow("SELECT name, specialty FROM ext.providers WHERE id = $1", provider_id)
    return {
        "provider_id": provider_id, "plan_id": plan_id,
        "in_network": bool(r["in_network"]) if r else False,
        "known": r is not None,
        "provider": dict(prov) if prov else None,
    }


async def get_allowed_amount(args: dict[str, Any]) -> dict[str, Any]:
    plan_id = str(args["plan_id"])
    codes = list(args.get("procedure_codes", []))
    pool = await get_pool()
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT procedure_code, allowed_cents FROM ext.fee_schedule WHERE plan_id = $1 AND procedure_code = ANY($2)",
            plan_id, codes,
        )
    return {"plan_id": plan_id, "allowed": {r["procedure_code"]: r["allowed_cents"] for r in rows}}


async def lookup_reason_code(args: dict[str, Any]) -> dict[str, Any]:
    code = str(args["code"])
    pool = await get_pool()
    async with pool.acquire() as c:
        r = await c.fetchrow("SELECT code, kind, description FROM ext.reason_codes WHERE code = $1", code)
    return dict(r) if r else {"code": code, "error": "unknown_reason_code"}


# ── the deterministic engine, exposed as a tool ───────────────────────────────
async def adjudicate_line(args: dict[str, Any]) -> dict[str, Any]:
    result = engine_adjudicate_line(
        line=args.get("line", {}),
        rule=args.get("rule"),
        accumulators=args.get("accumulators", {}),
        allowed_cents=args.get("allowed_cents"),
        in_network=bool(args.get("in_network", True)),
        plan=args.get("plan", {}),
        checks=args.get("checks", {}),
    )
    return result.to_dict()


# ── halo fetch / verify ───────────────────────────────────────────────────────
async def halo_fetch(args: dict[str, Any]) -> Any:
    pool = await get_pool()
    async with pool.acquire() as c:
        return await halo.get_json(c, str(args["handle"]))


async def halo_fetch_many(args: dict[str, Any]) -> Any:
    pool = await get_pool()
    async with pool.acquire() as c:
        return await halo.get_many(c, list(args.get("handles", [])))


async def halo_verify(args: dict[str, Any]) -> Any:
    handles = list(args.get("handles", []))
    pool = await get_pool()
    async with pool.acquire() as c:
        results = [await halo.verify(c, h) for h in handles]
    return {"checked": len(results), "all_intact": all(r["intact"] for r in results), "results": results}


# ── writes ────────────────────────────────────────────────────────────────────
async def record_decision(args: dict[str, Any]) -> dict[str, Any]:
    """Write proposed decisions for a claim's lines, each with its evidence handles."""
    claim_id = str(args["claim_id"])
    lines = list(args.get("lines", []))
    pool = await get_pool()
    written = []
    async with pool.acquire() as c:
        for ln in lines:
            await c.execute(
                """INSERT INTO agent.decisions
                     (id, session_id, claim_id, line_number, decision, allowed_cents, plan_paid_cents,
                      patient_resp_cents, deductible_cents, coinsurance_cents, copay_cents, carc, rarc,
                      rule_basis, evidence, computed_by, status)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,'engine','proposed')
                   ON CONFLICT (claim_id, line_number) DO UPDATE SET
                     decision=EXCLUDED.decision, allowed_cents=EXCLUDED.allowed_cents,
                     plan_paid_cents=EXCLUDED.plan_paid_cents, patient_resp_cents=EXCLUDED.patient_resp_cents,
                     deductible_cents=EXCLUDED.deductible_cents, coinsurance_cents=EXCLUDED.coinsurance_cents,
                     copay_cents=EXCLUDED.copay_cents, carc=EXCLUDED.carc, rarc=EXCLUDED.rarc,
                     rule_basis=EXCLUDED.rule_basis, evidence=EXCLUDED.evidence, status='proposed'
                   WHERE agent.decisions.status <> 'final'""",
                uuid.uuid4(), SESSION_ID, claim_id, int(ln["line_number"]), str(ln["decision"]),
                ln.get("allowed_cents"), ln.get("plan_paid_cents"), ln.get("patient_resp_cents"),
                ln.get("deductible_cents"), ln.get("coinsurance_cents"), ln.get("copay_cents"),
                ln.get("carc"), ln.get("rarc"), ln.get("rule_basis"), ln.get("evidence", []),
            )
            written.append({"line_number": ln["line_number"], "decision": ln["decision"]})
    return {"claim_id": claim_id, "recorded": written, "count": len(written), "status": "proposed"}


async def pend_claim(args: dict[str, Any]) -> dict[str, Any]:
    claim_id = str(args["claim_id"])
    reason = str(args.get("reason", ""))
    pool = await get_pool()
    async with pool.acquire() as c:
        await c.execute("UPDATE ext.claims SET status='pended' WHERE id=$1", claim_id)
        await c.execute(
            "INSERT INTO agent.approvals (id, session_id, action, payload, idempotency_key, status) "
            "VALUES ($1,$2,'pend_review',$3,$4,'pending') ON CONFLICT (idempotency_key) DO NOTHING",
            uuid.uuid4(), SESSION_ID, {"claim_id": claim_id, "reason": reason}, f"pend:{claim_id}",
        )
    return {"claim_id": claim_id, "pended": True, "reason": reason}


async def post_adjudication(args: dict[str, Any]) -> dict[str, Any]:
    """DECISION GATE. Commit proposed decisions to ext.claim_lines (the 835/EOB).
    Blocks on a human unless the claim is clean (all 'pay') and within the ceiling.
    Idempotent per (claim_id, line_number) — a retry cannot pay twice."""
    claim_id = str(args["claim_id"])
    pool = await get_pool()
    async with pool.acquire() as c:
        decisions = [dict(r) for r in await c.fetch(
            "SELECT * FROM agent.decisions WHERE claim_id = $1 ORDER BY line_number", claim_id)]
    if not decisions:
        return {"committed": False, "error": "no_proposed_decisions", "claim_id": claim_id}
    if all(d["status"] == "final" for d in decisions):
        return {"committed": True, "idempotent": True, "claim_id": claim_id, "note": "already finalized"}

    total_plan_paid = sum(d["plan_paid_cents"] or 0 for d in decisions)
    non_pay = [d["line_number"] for d in decisions if d["decision"] != "pay"]
    needs_human = bool(non_pay) or total_plan_paid > AUTO_FINALIZE_CEILING

    if needs_human:
        decision = await _await_approval(
            "post_adjudication",
            {"claim_id": claim_id, "total_plan_paid_cents": total_plan_paid,
             "non_pay_lines": non_pay, "reason": "deny/reduce/pend or over ceiling"},
            f"post:{claim_id}",
        )
        if decision["status"] != "approved":
            return {"committed": False, "approval_status": decision["status"], "claim_id": claim_id}
        approver = decision["decided_by"]
    else:
        approver = "auto-finalize@engine"

    async with pool.acquire() as c:
        async with c.transaction():
            for d in decisions:
                line_status = _LINE_STATUS.get(d["decision"], "pended")
                await c.execute(
                    """UPDATE ext.claim_lines SET status=$3, allowed_cents=$4, plan_paid_cents=$5,
                         patient_resp_cents=$6, carc=$7, rarc=$8 WHERE claim_id=$1 AND line_number=$2""",
                    claim_id, d["line_number"], line_status, d["allowed_cents"], d["plan_paid_cents"],
                    d["patient_resp_cents"], d["carc"], d["rarc"],
                )
                await c.execute(
                    "UPDATE agent.decisions SET status='final', approver=$2, decided_at=now() WHERE id=$1",
                    d["id"], approver,
                )
            any_denied = any(d["decision"] == "deny" for d in decisions)
            any_pended = any(d["decision"] == "pend" for d in decisions)
            claim_status = "pended" if any_pended else ("denied" if any_denied and all(d["decision"] == "deny" for d in decisions) else "adjudicated")
            await c.execute("UPDATE ext.claims SET status=$2 WHERE id=$1", claim_id, claim_status)

    return {
        "committed": True, "claim_id": claim_id, "approver": approver,
        "auto_finalized": not needs_human, "lines_finalized": len(decisions),
        "total_plan_paid_cents": total_plan_paid, "claim_status": claim_status,
    }


# ── tool catalogue ────────────────────────────────────────────────────────────
Handler = Callable[[dict[str, Any]], Awaitable[Any]]
HANDLERS: dict[str, Handler] = {
    "get_claim": get_claim,
    "get_member_coverage": get_member_coverage,
    "get_benefit_rules": get_benefit_rules,
    "get_accumulators": get_accumulators,
    "get_claim_history": get_claim_history,
    "check_network": check_network,
    "get_allowed_amount": get_allowed_amount,
    "lookup_reason_code": lookup_reason_code,
    "adjudicate_line": adjudicate_line,
    "record_decision": record_decision,
    "pend_claim": pend_claim,
    "post_adjudication": post_adjudication,
    "halo_fetch": halo_fetch,
    "halo_fetch_many": halo_fetch_many,
    "halo_verify": halo_verify,
}


def _ann(**kw: bool) -> types.ToolAnnotations:
    return types.ToolAnnotations(**kw)


_OBJ = {"type": "object"}
TOOLS: list[types.Tool] = [
    types.Tool(name="get_claim",
        description="Fetch an 837-shaped claim: header + service lines + diagnosis + attachment refs. Returns a Halo envelope; the line codes/amounts are in the summary, while `full_lines` / `diagnosis_codes` / `attachments` are handles — pull attachments only if a line needs clinical review. Keyed into the claim map.",
        inputSchema={"type": "object", "properties": {"claim_id": {"type": "string"}}, "required": ["claim_id"]},
        annotations=_ann(readOnlyHint=True, openWorldHint=True)),
    types.Tool(name="get_member_coverage",
        description="Member eligibility + plan terms (270/271 later): status, effective/term dates, and the plan's annual max / deductible / OOP max / coinsurance table.",
        inputSchema={"type": "object", "properties": {"member_id": {"type": "string"}}, "required": ["member_id"]},
        annotations=_ann(readOnlyHint=True, openWorldHint=True)),
    types.Tool(name="get_benefit_rules",
        description="Per-procedure benefit rules for a plan — coverage %, category, frequency, waiting months, preauth — for ONLY the codes on this claim (don't pull the whole plan).",
        inputSchema={"type": "object", "properties": {"plan_id": {"type": "string"}, "procedure_codes": {"type": "array", "items": {"type": "string"}}}, "required": ["plan_id", "procedure_codes"]},
        annotations=_ann(readOnlyHint=True)),
    types.Tool(name="get_accumulators",
        description="Running totals for the member's plan year: deductible met, annual max used, OOP met. Feed these to adjudicate_line.",
        inputSchema={"type": "object", "properties": {"member_id": {"type": "string"}, "plan_year": {"type": "integer"}}, "required": ["member_id"]},
        annotations=_ann(readOnlyHint=True)),
    types.Tool(name="get_claim_history",
        description="Prior claim lines for the member (for frequency and duplicate checks). Halo envelope: summary `by_code` + `recent`, with a `lines` handle. Slice by `code` and `window_months`. Pass `exclude_claim_id` (the claim you are adjudicating) so its own lines are never counted as prior history.",
        inputSchema={"type": "object", "properties": {"member_id": {"type": "string"}, "code": {"type": "string"}, "window_months": {"type": "integer"}, "exclude_claim_id": {"type": "string"}}, "required": ["member_id"]},
        annotations=_ann(readOnlyHint=True, openWorldHint=True)),
    types.Tool(name="check_network",
        description="Is the provider in-network for the plan? Returns in_network and the provider record.",
        inputSchema={"type": "object", "properties": {"provider_id": {"type": "string"}, "plan_id": {"type": "string"}}, "required": ["provider_id", "plan_id"]},
        annotations=_ann(readOnlyHint=True)),
    types.Tool(name="get_allowed_amount",
        description="Fee-schedule allowed amounts for the given codes on a plan. Feed allowed_cents to adjudicate_line.",
        inputSchema={"type": "object", "properties": {"plan_id": {"type": "string"}, "procedure_codes": {"type": "array", "items": {"type": "string"}}}, "required": ["plan_id", "procedure_codes"]},
        annotations=_ann(readOnlyHint=True)),
    types.Tool(name="lookup_reason_code",
        description="CARC / RARC reference description. Select reason codes from these — never invent a code.",
        inputSchema={"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
        annotations=_ann(readOnlyHint=True)),
    types.Tool(name="adjudicate_line",
        description="THE DETERMINISTIC ENGINE (not a model call). Given a line, its benefit rule, the accumulators, the allowed amount, network status, the plan, and your judged `checks` (missing_info / is_duplicate / within_frequency / past_waiting / preauth_on_file), it returns the exact money (allowed, plan_paid, patient_resp, deductible, coinsurance) and the SUGGESTED CARC/RARC. Reproducible. You pass the inputs and pick codes; you do NOT do the arithmetic yourself.",
        inputSchema={"type": "object", "properties": {
            "line": {"type": "object", "description": "{procedure_code, charged_cents, units, ...}"},
            "rule": {"type": "object", "description": "the benefit rule for this code (or null if none)"},
            "accumulators": {"type": "object"},
            "allowed_cents": {"type": "integer"},
            "in_network": {"type": "boolean"},
            "plan": {"type": "object", "description": "annual_max_cents, deductible_cents, oop_max_cents"},
            "checks": {"type": "object", "description": "your judgement: missing_info, is_duplicate, within_frequency, past_waiting, preauth_on_file"},
        }, "required": ["line", "rule", "accumulators", "in_network", "plan"]},
        annotations=_ann(readOnlyHint=True)),
    types.Tool(name="record_decision",
        description="Record PROPOSED decisions for a claim's lines (writes agent.decisions), each with its evidence: the Halo handles the decision rested on. Pass lines:[{line_number, decision, allowed_cents, plan_paid_cents, patient_resp_cents, deductible_cents, coinsurance_cents, copay_cents, carc, rarc, rule_basis, evidence:[handles]}].",
        inputSchema={"type": "object", "properties": {"claim_id": {"type": "string"}, "lines": {"type": "array", "items": {"type": "object"}}}, "required": ["claim_id", "lines"]},
        annotations=_ann(idempotentHint=True)),
    types.Tool(name="pend_claim",
        description="Route a claim to a human reviewer (e.g. missing information, needs clinical review). Sets the claim pended and queues a reviewer approval.",
        inputSchema={"type": "object", "properties": {"claim_id": {"type": "string"}, "reason": {"type": "string"}}, "required": ["claim_id", "reason"]},
        annotations=_ann(idempotentHint=True)),
    types.Tool(name="post_adjudication",
        description="DECISION GATE. Commit the proposed decisions to ext.claim_lines (the 835/EOB). HUMAN-GATED: any deny/reduce/pend line, or a claim total over the auto-finalize ceiling, BLOCKS until a human reviewer confirms. A clean, all-pay, within-ceiling claim auto-finalizes (still with full evidence). Idempotent per line — a retry cannot pay twice.",
        inputSchema={"type": "object", "properties": {"claim_id": {"type": "string"}}, "required": ["claim_id"]},
        annotations=_ann(idempotentHint=True, openWorldHint=True)),
    types.Tool(name="halo_fetch",
        description="Fetch the decoded content behind one Halo handle (h:sha256:...).",
        inputSchema={"type": "object", "properties": {"handle": {"type": "string"}}, "required": ["handle"]},
        annotations=_ann(readOnlyHint=True)),
    types.Tool(name="halo_fetch_many",
        description="Fetch many Halo handles in one round trip (batched drill-down).",
        inputSchema={"type": "object", "properties": {"handles": {"type": "array", "items": {"type": "string"}}}, "required": ["handles"]},
        annotations=_ann(readOnlyHint=True)),
    types.Tool(name="halo_verify",
        description="Re-hash stored Halo nodes and confirm each still matches its handle — the tamper-evidence check for the evidence a decision rested on.",
        inputSchema={"type": "object", "properties": {"handles": {"type": "array", "items": {"type": "string"}}}, "required": ["handles"]},
        annotations=_ann(readOnlyHint=True)),
]

server: Server = Server("claims-mcp")


@server.list_tools()
async def _list_tools() -> list[types.Tool]:
    return TOOLS


@server.call_tool()
async def _call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    handler = HANDLERS.get(name)
    if handler is None:
        return [types.TextContent(type="text", text=f"unknown tool: {name}")]
    started = time.monotonic()
    try:
        out = await handler(arguments)
        if isinstance(out, halo.Envelope):
            payload: Any = out.to_dict()
            env_root = out.map_root or next(iter(out.refs.values()), None)
        elif isinstance(out, dict) and "refs" in out:
            payload = out
            env_root = next(iter(out["refs"].values()), None)
        else:
            payload = out
            env_root = None
        await _record_tool_call(name, arguments, env_root, int((time.monotonic() - started) * 1000), True, None)
        return [types.TextContent(type="text", text=dumps(payload))]
    except Exception as err:  # noqa: BLE001
        await _record_tool_call(name, arguments, None, int((time.monotonic() - started) * 1000), False, str(err))
        return [types.TextContent(type="text", text=f"error in {name}: {err}")]


async def main() -> None:
    await ensure_session(os.environ.get("CLAIMS_SESSION_CLAIM_ID"))
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
