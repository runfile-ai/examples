"""Deterministic end-to-end demo (no LLM / API key). Drives the REAL dental MCP
server over stdio: identity gate → verify → Halo patient summary → coverage →
derive open slots → hold → human-gated book (auto-approved here) → prove the
no-double-book constraint → show the booking.

  python -m src.scripts.run_demo
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import asyncpg
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SESSION_ID = str(uuid.uuid4())

DSN = os.environ.get("DENTAL_DB_DSN") or (
    f"postgresql://{os.environ.get('ADMIN_DB_USER','postgres')}:{os.environ.get('ADMIN_DB_PASSWORD','postgres')}"
    f"@{os.environ.get('ADMIN_DB_HOST','localhost')}:{os.environ.get('ADMIN_DB_PORT','5433')}/{os.environ.get('DENTAL_DB_NAME','dental')}"
)


async def call(session: ClientSession, name: str, args: dict[str, Any]) -> Any:
    res = await session.call_tool(name, args)
    return json.loads(res.content[0].text)


async def auto_approve(action: str) -> None:
    """Stand in for the front-desk: approve the next pending gated write."""
    c = await asyncpg.connect(DSN)
    try:
        for _ in range(60):
            row = await c.fetchrow(
                "SELECT id, payload FROM agent.approvals WHERE action = $1 AND status = 'pending' ORDER BY created_at LIMIT 1",
                action,
            )
            if row:
                await c.execute(
                    "UPDATE agent.approvals SET status='approved', decided_by='frontdesk-auto@demo', decided_at=now() WHERE id=$1",
                    row["id"],
                )
                print(f"  [front-desk] APPROVED {action} {row['payload']}")
                return
            await asyncio.sleep(0.5)
    finally:
        await c.close()


def fmt(iso: str) -> str:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M") + "Z"


async def main() -> None:
    print("=== Dental reception agent — deterministic demo (real MCP server over stdio) ===\n")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "src.mcp_server.server"],
        cwd=str(PROJECT_ROOT),
        env={
            **os.environ,
            "DENTAL_DB_DSN": DSN,
            "AGENT_SESSION_ID": SESSION_ID,
            "DENTAL_CHANNEL": "demo",
            "APPROVAL_POLL_SECONDS": "1",
            "APPROVAL_TIMEOUT_SECONDS": "60",
            "PYTHONPATH": str(PROJECT_ROOT),
        },
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print(f"MCP server exposes {len(tools.tools)} tools.\n")

            # 1. LOOKUP — thin candidates only (no PHI).
            found = await call(session, "find_patient", {"query": "+14155550142"})
            cand = found["candidates"][0]
            print(f"find_patient(\"+14155550142\") → {found['count']} candidate: {cand['first_name']} {cand['last_name']}, phone {cand['phone']}")

            # 2. IDENTITY GATE — a disclosure read is refused before verification.
            blocked = await call(session, "get_patient_summary", {"patient_id": cand["patient_id"]})
            print(f"get_patient_summary BEFORE verify → {blocked['error']}  (identity gate holds)")

            # 3. VERIFY — name + dob.
            verified = await call(session, "verify_identity", {"patient_id": cand["patient_id"], "last_name": "Garcia", "dob": "1989-04-12"})
            print(f"verify_identity(Garcia, 1989-04-12) → verified={verified['verified']}")

            # 4. SUMMARY — Halo envelope; fetch only contact + insurance, never clinical.
            summary = await call(session, "get_patient_summary", {"patient_id": cand["patient_id"]})
            print(f"\nget_patient_summary → envelope refs: {', '.join(summary['refs'].keys())}")
            print(f"  {summary['summary']['name']} — recall_overdue={summary['summary']['recall_overdue']}, insurance={summary['summary']['insurance']}")
            drill = await call(session, "halo_fetch_many", {"handles": [summary["refs"]["contact"], summary["refs"]["insurance"]]})
            contact = drill[summary["refs"]["contact"]]
            print(f"  halo_fetch_many(contact, insurance) → phone {contact['phone']}; clinical handle left UNFETCHED")

            # 5. COVERAGE for a cleaning.
            cov = await call(session, "check_coverage", {"patient_id": cand["patient_id"], "descriptor_id": "appt_cleaning"})
            print(f"\ncheck_coverage(Cleaning) → {cov['eligibility']}, {cov['coverage_pct']}% covered, copay ${cov['copay_cents']/100:.2f} ({cov['carrier']})")

            # 6. SLOTS — derived (availabilities minus booked); envelope summary, then the slot list.
            to = (datetime.now(timezone.utc) + timedelta(days=9)).isoformat()
            slots = await call(session, "find_open_slots", {"descriptor_id": "appt_cleaning", "provider": "Dr. Nguyen", "time_of_day": "AM", "to": to})
            print(f"\nfind_open_slots(Cleaning, Dr. Nguyen, AM) → {slots['summary']['n_slots']} slots across {len(slots['summary']['by_day'])} days")
            all_slots = await call(session, "halo_fetch", {"handle": slots["refs"]["all_slots"]})
            chosen = all_slots[0]
            print(f"  first slot: {fmt(chosen['start_time'])} with {chosen['provider_name']} in {chosen['operatory_id']}")

            # 7. HOLD — agent-local, short TTL.
            hold = await call(session, "hold_slot", {
                "patient_id": cand["patient_id"], "descriptor_id": "appt_cleaning",
                "start_time": chosen["start_time"], "provider_id": chosen["provider_id"],
                "operatory_id": chosen["operatory_id"], "location_id": chosen["location_id"],
            })
            print(f"\nhold_slot → hold {hold['hold_id'][:8]} (status={hold['status']}, expires {fmt(hold['expires_at'])})")

            # 8. BOOK — human-gated; auto-approver stands in for the front desk.
            print("\nbook_appointment (HUMAN-GATED — agent blocks)…")
            _, booked = await asyncio.gather(auto_approve("book"), call(session, "book_appointment", {"hold_id": hold["hold_id"]}))
            print(f"  → committed={booked['committed']} appointment={booked.get('external_appt_id')} at {fmt(booked['start_time'])} by {booked.get('decided_by')}")

            # 9. NO DOUBLE-BOOK — a different patient holds the SAME chair+time, then books.
            print("\nattempting a conflicting book on the same chair+time (different patient)…")
            hold2 = await call(session, "hold_slot", {
                "patient_id": "pat_001", "descriptor_id": "appt_cleaning",
                "start_time": chosen["start_time"], "provider_id": chosen["provider_id"],
                "operatory_id": chosen["operatory_id"], "location_id": chosen["location_id"],
            })
            _, clash = await asyncio.gather(auto_approve("book"), call(session, "book_appointment", {"hold_id": hold2["hold_id"]}))
            print(f"  → committed={clash['committed']}, error={clash.get('error')}  (no_chair_overlap held the line)")

            # 10. SHOW the booking landed.
            appts = await call(session, "get_appointments", {"patient_id": cand["patient_id"]})
            nxt = ", ".join(f"{a['descriptor']} {fmt(a['start_time'])}" for a in appts["summary"]["upcoming"])
            print(f"\nget_appointments → {len(appts['summary']['upcoming'])} upcoming; next: {nxt}")

    print("\n=== demo complete: identity-gated → coverage → derived slots via Halo → held → booked (human-approved) → double-book blocked ===")


if __name__ == "__main__":
    asyncio.run(main())
