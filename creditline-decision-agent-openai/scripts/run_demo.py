"""End-to-end demo for the OpenAI build: one escalated, human-overridden decision.

Drives the SAME `mimic-creditline` MCP tools (no LLM / API key required) through
the full flow for the seeded escalation case, recording the OpenAI agent's
provenance, then simulates the officer override. Proves the environment +
provenance wiring without an OpenAI key.

    python -m scripts.run_demo

The live, model-driven version is `python -m agent.main`.
"""
from __future__ import annotations

import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

load_dotenv()
os.environ.setdefault("APPROVAL_POLL_SECONDS", "1")
os.environ.setdefault("APPROVAL_TIMEOUT_SECONDS", "60")

# Reuse the environment layer (MCP server + officer console) from the sibling.
# Append (don't insert) so THIS project's `agent` package wins; only the
# sibling-only `mcp_servers` package is resolved from there.
SIBLING = Path(__file__).resolve().parents[2] / "creditline-decision-agent"
sys.path.append(str(SIBLING))

import importlib.util  # noqa: E402

from agent.decision import evaluate  # noqa: E402  (this project's copy)
from agent.prompts import MODEL_VERSION, PROMPT_VERSION_HASH  # noqa: E402
from mcp_servers.mimic_creditline import server as S  # noqa: E402  (from sibling)


# Load the sibling's officer console by path (both projects have a `scripts`
# package, so a normal import would collide with this project's own).
def _load_sibling_officer():
    spec = importlib.util.spec_from_file_location(
        "sibling_officer_console", str(SIBLING / "scripts" / "officer_console.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_officer = _load_sibling_officer()
_admin_dsn = _officer._admin_dsn
resolve = _officer.resolve

DEMO_REQUEST_ID = "11111111-1111-1111-1111-111111111111"


async def _approval_id_for(decision_id: str) -> str:
    conn = await asyncpg.connect(_admin_dsn())
    try:
        for _ in range(30):
            row = await conn.fetchrow(
                "SELECT approval_id FROM approvals WHERE decision_id = $1 "
                "ORDER BY requested_at DESC LIMIT 1",
                __import__("uuid").UUID(decision_id),
            )
            if row:
                return str(row["approval_id"])
            await asyncio.sleep(0.5)
    finally:
        await conn.close()
    raise RuntimeError("approval row never appeared")


async def main() -> None:
    print("=== Credit-Line Decision Agent (OpenAI Agents SDK) — deterministic demo ===\n")

    req = await S.creditline_get_request(DEMO_REQUEST_ID)
    cust = await S.creditline_get_customer(req["customer_id"])
    bureau = await S.creditline_pull_bureau(req["customer_id"])
    policy = await S.creditline_get_active_policy()
    customer = cust["customer"]

    print(f"Applicant       : {customer['full_name']}")
    print(f"Request          : {req['request_type']} → limit {req['requested_limit']}")
    print(f"Active policy     : {policy['version']}  thresholds {policy['thresholds']}\n")

    rec = evaluate(
        requested_limit=Decimal(str(req["requested_limit"])),
        annual_income=Decimal(str(customer["annual_income"])),
        bureau=bureau,
        policy_thresholds=policy["thresholds"],
    )
    print(f"Computed DTI      : {rec.dti:.3f}")
    print(f"Recommendation    : {rec.outcome.upper()}")
    for r in rec.reasons:
        print(f"   • {r}")
    print()

    decision = await S.creditline_record_decision(
        request_id=DEMO_REQUEST_ID,
        outcome=rec.outcome,
        rationale="; ".join(rec.reasons),
        model_version=MODEL_VERSION,
        prompt_version_hash=PROMPT_VERSION_HASH,
        policy_version=policy["version"],
        bureau_report_id=bureau["bureau_report_id"],
        approved_limit=float(rec.approved_limit) if rec.approved_limit is not None else None,
    )
    print(f"Recorded decision : {decision['decision_id']}  "
          f"requires_human_approval={decision['requires_human_approval']}")
    print(f"   model_version       = {MODEL_VERSION}")
    print(f"   prompt_version_hash = {PROMPT_VERSION_HASH}\n")

    if not decision["requires_human_approval"]:
        print("Auto-approved — no human gate. Done.")
        return

    print("Opening human-in-the-loop approval gate (agent now blocks)…")
    summary = (
        f"{customer['full_name']} requests {req['requested_limit']}. "
        f"Above ceiling and DTI {rec.dti:.3f} over appetite. Recommend escalate."
    )
    gate = asyncio.create_task(
        S.creditline_request_approval(decision_id=decision["decision_id"], summary=summary)
    )

    async def officer():
        approval_id = await _approval_id_for(decision["decision_id"])
        await asyncio.sleep(1)
        print("  [officer] reviewing… modifying to an approved limit of 12,000")
        await resolve(
            approval_id=approval_id,
            action="modify",
            approver_id="co-114-jmalik",
            justification="Strong relationship, clean record; approve reduced 12,000 limit.",
            modified_limit=12000,
        )

    officer_task = asyncio.create_task(officer())
    result = await gate
    await officer_task

    print("\nApproval resolved:")
    print(f"   status        : {result['status']}")
    print(f"   is_override   : {result['is_override']}  ← Art.14 / SR 11-7 effective challenge")
    print(f"   approved limit: {result['modified_limit']}")
    print(f"   approver       : {result['approver_id']} ({result['approver_role']})")

    await S.creditline_notify_customer(
        request_id=DEMO_REQUEST_ID,
        outcome="approved",
        approved_limit=result["modified_limit"],
        idempotency_key=decision["decision_id"],
    )
    print("\n=== demo complete: escalated → human override → approved at 12,000 ===")


if __name__ == "__main__":
    asyncio.run(main())
