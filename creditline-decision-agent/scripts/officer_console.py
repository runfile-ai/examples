"""Credit-officer console — the human side of the HITL gate.

Lists pending approvals with full decision context and lets a credit officer
resolve them. A *reject* or *modify* against the agent's recommendation is the
EU AI Act Art. 14 / SR 11-7 §5.2 effective-challenge / override event.

Run in a second terminal while the agent (or scripts/run_demo.py) is blocked
waiting on the gate:

    python -m scripts.officer_console

Uses the admin DSN: the human officer is a distinct actor from the agent, and
must be able to write the final disposition the agent's role could record only
as a recommendation.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys

import asyncpg
from dotenv import load_dotenv

load_dotenv()


def _admin_dsn() -> str:
    host = os.environ.get("ADMIN_DB_HOST", "localhost")
    port = os.environ.get("ADMIN_DB_PORT", "5433")
    user = os.environ.get("ADMIN_DB_USER", "postgres")
    pw = os.environ.get("ADMIN_DB_PASSWORD", "postgres")
    return f"postgresql://{user}:{pw}@{host}:{port}/mimic_creditline"


async def _pending(conn) -> list:
    return await conn.fetch(
        """
        SELECT a.approval_id, a.decision_id, a.approver_role, a.justification AS summary,
               d.outcome, d.rationale, d.approved_limit, d.policy_version,
               r.request_id, r.requested_limit, c.full_name, c.annual_income
        FROM approvals a
        JOIN decisions d ON d.decision_id = a.decision_id
        JOIN credit_line_requests r ON r.request_id = d.request_id
        JOIN customers c ON c.customer_id = d.customer_id
        WHERE a.status = 'pending'
        ORDER BY a.requested_at
        """
    )


async def resolve(
    *,
    approval_id: str,
    action: str,            # confirm | reject | modify
    approver_id: str,
    justification: str,
    modified_limit: float | None = None,
) -> None:
    """Programmatic resolver (used by the console and by the demo)."""
    conn = await asyncpg.connect(_admin_dsn())
    try:
        appr = await conn.fetchrow(
            "SELECT a.decision_id, d.request_id, r.requested_limit "
            "FROM approvals a JOIN decisions d ON d.decision_id = a.decision_id "
            "JOIN credit_line_requests r ON r.request_id = d.request_id "
            "WHERE a.approval_id = $1",
            __import__("uuid").UUID(approval_id),
        )
        if appr is None:
            raise SystemExit(f"approval {approval_id} not found")

        if action == "confirm":
            status, is_override = "confirmed", False
            final_outcome, final_limit = "approved", appr["requested_limit"]
        elif action == "reject":
            status, is_override = "rejected", True
            final_outcome, final_limit = "denied", None
        elif action == "modify":
            status, is_override = "modified", True
            final_outcome = "approved"
            final_limit = modified_limit
        else:
            raise SystemExit(f"unknown action {action!r}")

        async with conn.transaction():
            await conn.execute(
                """
                UPDATE approvals
                SET status = $1, is_override = $2, modified_limit = $3,
                    approver_id = $4, justification = $5, resolved_at = $6
                WHERE approval_id = $7
                """,
                status,
                is_override,
                modified_limit,
                approver_id,
                justification,
                dt.datetime.now(dt.timezone.utc),
                __import__("uuid").UUID(approval_id),
            )
            # Write the human's final disposition onto the decision + request.
            await conn.execute(
                "UPDATE decisions SET outcome = $1, approved_limit = $2 WHERE decision_id = $3",
                final_outcome,
                final_limit,
                appr["decision_id"],
            )
            await conn.execute(
                "UPDATE credit_line_requests SET status = $1 WHERE request_id = $2",
                final_outcome,
                appr["request_id"],
            )
    finally:
        await conn.close()


async def interactive() -> None:
    conn = await asyncpg.connect(_admin_dsn())
    try:
        rows = await _pending(conn)
    finally:
        await conn.close()

    if not rows:
        print("No pending approvals.")
        return

    print("Pending approvals:\n")
    for i, r in enumerate(rows):
        print(f"[{i}] approval {r['approval_id']}")
        print(f"    applicant   : {r['full_name']} (income {r['annual_income']})")
        print(f"    request      : {r['requested_limit']} | agent outcome: {r['outcome']}")
        print(f"    policy       : {r['policy_version']}")
        print(f"    agent summary: {r['summary']}")
        print(f"    rationale    : {r['rationale']}\n")

    idx = int(input("Select approval index: ").strip())
    chosen = rows[idx]
    action = input("Action [confirm/reject/modify]: ").strip().lower()
    approver_id = input("Your officer id: ").strip() or "officer-unknown"
    modified_limit = None
    if action == "modify":
        modified_limit = float(input("Modified (approved) limit: ").strip())
    justification = input("Justification: ").strip()

    await resolve(
        approval_id=str(chosen["approval_id"]),
        action=action,
        approver_id=approver_id,
        justification=justification,
        modified_limit=modified_limit,
    )
    print(f"\nResolved approval {chosen['approval_id']} as {action!r}.")


async def auto_resolve(action: str = "modify", modified_limit: float = 12000.0) -> None:
    """Unattended resolver: wait for the next pending approval and resolve it.

    For hands-off demos of the live agent — stands in for the human officer so
    the blocking gate gets unblocked without an interactive console.
    """
    print(f"[auto-officer] waiting for a pending approval (will {action})…")
    while True:
        conn = await asyncpg.connect(_admin_dsn())
        try:
            rows = await _pending(conn)
        finally:
            await conn.close()
        if rows:
            chosen = rows[0]
            await resolve(
                approval_id=str(chosen["approval_id"]),
                action=action,
                approver_id="co-114-jmalik",
                justification=(
                    "Auto-officer: strong relationship and clean delinquency record; "
                    f"approving a reduced {modified_limit:.0f} limit to keep DTI in appetite."
                ),
                modified_limit=modified_limit if action == "modify" else None,
            )
            print(f"[auto-officer] resolved {chosen['approval_id']} as {action!r}")
            return
        await asyncio.sleep(1)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "auto":
        asyncio.run(auto_resolve())
    else:
        asyncio.run(interactive())
