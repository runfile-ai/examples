"""Front-desk console — the human side of the write gate. Lists pending approvals
(book / reschedule / cancel / update_contact) and lets staff approve or reject.
The agent's gated tool call is blocked until this resolves it.

  python -m src.scripts.frontdesk_console            interactive
  python -m src.scripts.frontdesk_console auto        unattended: approve the next pending one (demos)
"""
from __future__ import annotations

import asyncio
import os
import sys

import asyncpg
from dotenv import load_dotenv

load_dotenv()

DSN = os.environ.get("DENTAL_DB_DSN") or (
    f"postgresql://{os.environ.get('ADMIN_DB_USER','postgres')}:{os.environ.get('ADMIN_DB_PASSWORD','postgres')}"
    f"@{os.environ.get('ADMIN_DB_HOST','localhost')}:{os.environ.get('ADMIN_DB_PORT','5433')}/{os.environ.get('DENTAL_DB_NAME','dental')}"
)


async def _pending(conn: asyncpg.Connection):
    return await conn.fetch(
        "SELECT id, action, payload, idempotency_key, created_at FROM agent.approvals WHERE status = 'pending' ORDER BY created_at"
    )


async def _resolve(conn: asyncpg.Connection, approval_id, status: str, decided_by: str) -> None:
    await conn.execute(
        "UPDATE agent.approvals SET status = $1, decided_by = $2, decided_at = now() WHERE id = $3",
        status, decided_by, approval_id,
    )


async def auto() -> None:
    conn = await asyncpg.connect(DSN)
    try:
        print("[frontdesk:auto] waiting for a pending approval…", flush=True)
        while True:
            rows = await _pending(conn)
            if rows:
                a = rows[0]
                await _resolve(conn, a["id"], "approved", "frontdesk-auto@demo")
                print(f"[frontdesk:auto] APPROVED {a['action']} {a['payload']}", flush=True)
                return
            await asyncio.sleep(1)
    finally:
        await conn.close()


async def interactive() -> None:
    conn = await asyncpg.connect(DSN)
    try:
        rows = await _pending(conn)
        if not rows:
            print("No pending approvals.")
            return
        for a in rows:
            print(f"\nApproval {a['id']}")
            print(f"  action : {a['action']}")
            print(f"  payload: {a['payload']}")
            ans = (await asyncio.to_thread(input, "  approve / reject / skip? ")).strip().lower()
            if ans in ("approve", "a"):
                who = (await asyncio.to_thread(input, "  your front-desk id: ")).strip() or "frontdesk-unknown"
                await _resolve(conn, a["id"], "approved", who)
                print("  → approved")
            elif ans in ("reject", "r"):
                who = (await asyncio.to_thread(input, "  your front-desk id: ")).strip() or "frontdesk-unknown"
                await _resolve(conn, a["id"], "rejected", who)
                print("  → rejected")
            else:
                print("  → skipped")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(auto() if len(sys.argv) > 1 and sys.argv[1] == "auto" else interactive())
