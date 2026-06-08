"""Create the `claims` database, apply the schema, and seed ext.* with a small
payer: one PPO plan, a couple of providers (one in-network, one out), a fee
schedule, benefit rules, members with accumulators, reason codes, and claims —
including a rich hero claim that exercises every disposition and a clean claim
that auto-finalizes. This is the whole "external system"; no credentials needed.

  python db/seed.py     (run from the project root)
"""
from __future__ import annotations

import asyncio
import os
import json
from datetime import date, timedelta
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

load_dotenv()

ADMIN = {
    "host": os.environ.get("ADMIN_DB_HOST", "localhost"),
    "port": int(os.environ.get("ADMIN_DB_PORT", "5433")),
    "user": os.environ.get("ADMIN_DB_USER", "postgres"),
    "password": os.environ.get("ADMIN_DB_PASSWORD", "postgres"),
}
DB_NAME = os.environ.get("CLAIMS_DB_NAME", "claims")
CLAIMS_DSN = os.environ.get("CLAIMS_DB_DSN") or (
    f"postgresql://{ADMIN['user']}:{ADMIN['password']}@{ADMIN['host']}:{ADMIN['port']}/{DB_NAME}"
)
SCHEMA_PATH = Path(__file__).resolve().parent / "01_schema.sql"

TODAY = date.today()
YEAR = TODAY.year


def days_ago(n: int) -> date:
    return TODAY - timedelta(days=n)


async def ensure_database() -> None:
    admin = await asyncpg.connect(**ADMIN, database="postgres")
    try:
        if not await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", DB_NAME):
            await admin.execute(f"CREATE DATABASE {DB_NAME}")
            print(f"created database {DB_NAME}")
    finally:
        await admin.close()


async def seed(c: asyncpg.Connection) -> None:
    for t in [
        "agent.decisions", "agent.approvals", "agent.halo_maps", "agent.halo_nodes",
        "agent.tool_calls", "agent.messages", "agent.sessions",
        "ext.claim_lines", "ext.claims", "ext.reason_codes", "ext.fee_schedule", "ext.network",
        "ext.accumulators", "ext.benefit_rules", "ext.providers", "ext.members", "ext.plans",
    ]:
        await c.execute(f"DELETE FROM {t}")

    # ── Plan ────────────────────────────────────────────────────────────────────
    await c.execute(
        "INSERT INTO ext.plans (id, name, type, annual_max_cents, deductible_cents, oop_max_cents, coinsurance) "
        "VALUES ('plan_ppo','BrightCare Dental PPO','dental_ppo',150000,5000,300000,$1)",
        {"preventive": 100, "basic": 80, "major": 50},
    )

    # ── Providers + network ─────────────────────────────────────────────────────
    await c.execute(
        "INSERT INTO ext.providers (id, npi, name, specialty) VALUES "
        "('prov_in','1902001','Dr. Susan Park DDS','General Dentistry'),"
        "('prov_out','1902002','Dr. Mark Hill DDS','General Dentistry')"
    )
    await c.execute(
        "INSERT INTO ext.network (plan_id, provider_id, in_network) VALUES "
        "('plan_ppo','prov_in',true),('plan_ppo','prov_out',false)"
    )

    # ── Fee schedule (allowed amounts) ──────────────────────────────────────────
    for code, allowed in [("D1110", 9000), ("D0274", 5000), ("D2391", 18000), ("D2740", 90000), ("D4341", 25000), ("D9110", 8000)]:
        await c.execute("INSERT INTO ext.fee_schedule (plan_id, procedure_code, allowed_cents) VALUES ('plan_ppo',$1,$2)", code, allowed)

    # ── Benefit rules ───────────────────────────────────────────────────────────
    rules = [
        # id, code, category, covered, pct, freq_text, freq_per_year, waiting_months, preauth
        ("br_1110", "D1110", "preventive", True, 100, "2/year", 2, 0, False),
        ("br_0274", "D0274", "preventive", True, 100, "1/year", 1, 0, False),
        ("br_2391", "D2391", "basic", True, 80, None, None, 0, False),
        ("br_2740", "D2740", "major", True, 50, None, None, 12, True),
        ("br_4341", "D4341", "basic", True, 80, None, None, 0, False),
        ("br_9110", "D9110", "basic", False, 0, None, None, 0, False),  # palliative — not covered on this plan
    ]
    for r in rules:
        await c.execute(
            "INSERT INTO ext.benefit_rules (id, plan_id, procedure_code, category, covered, coverage_pct, "
            "frequency_limit, frequency_per_year, waiting_months, requires_preauth) "
            "VALUES ($1,'plan_ppo',$2,$3,$4,$5,$6,$7,$8,$9)",
            *r,
        )

    # ── Members + accumulators ──────────────────────────────────────────────────
    # Hero: effective 2y ago (past the crown's 12-month wait); deductible already
    # met; $1200 of the $1500 annual max already used (so the crown hits the max).
    await c.execute(
        "INSERT INTO ext.members (id, first_name, last_name, dob, plan_id, group_id, effective_date, term_date, status) "
        "VALUES ('mem_hero','Robert','Lee',$1,'plan_ppo','GRP-100',$2,NULL,'active')",
        date(1979, 7, 3), days_ago(730),
    )
    await c.execute(
        "INSERT INTO ext.accumulators (member_id, plan_year, deductible_met_cents, annual_max_used_cents, oop_met_cents) "
        "VALUES ('mem_hero',$1,5000,120000,40000)", YEAR,
    )
    # Clean member: deductible met, almost no annual max used (clean auto-finalize).
    await c.execute(
        "INSERT INTO ext.members (id, first_name, last_name, dob, plan_id, group_id, effective_date, term_date, status) "
        "VALUES ('mem_clean','Dana','Cruz',$1,'plan_ppo','GRP-100',$2,NULL,'active')",
        date(1990, 1, 20), days_ago(400),
    )
    await c.execute(
        "INSERT INTO ext.accumulators (member_id, plan_year, deductible_met_cents, annual_max_used_cents, oop_met_cents) "
        "VALUES ('mem_clean',$1,5000,0,0)", YEAR,
    )

    # ── Reason codes (the agent selects from these) ─────────────────────────────
    reason_codes = [
        ("1", "CARC", "Deductible amount"),
        ("2", "CARC", "Coinsurance amount"),
        ("3", "CARC", "Co-payment amount"),
        ("16", "CARC", "Claim/service lacks information or has submission/billing error(s)"),
        ("18", "CARC", "Exact duplicate claim/service"),
        ("26", "CARC", "Expenses incurred prior to coverage / waiting period not met"),
        ("45", "CARC", "Charge exceeds fee schedule/maximum allowable amount"),
        ("96", "CARC", "Non-covered charge(s)"),
        ("119", "CARC", "Benefit maximum for this time period or occurrence has been reached"),
        ("197", "CARC", "Precertification/authorization/notification absent"),
        ("242", "CARC", "Services not provided by network/primary care providers"),
        ("N706", "RARC", "Missing documentation"),
    ]
    for code, kind, desc in reason_codes:
        await c.execute("INSERT INTO ext.reason_codes (code, kind, description) VALUES ($1,$2,$3)", code, kind, desc)

    # ── Prior history (for frequency / duplicate checks) ────────────────────────
    # A previously-adjudicated claim: a bitewing set 90 days ago (D0274 is 1/year,
    # so a new D0274 this year exceeds frequency) and a cleaning 200 days ago.
    await c.execute(
        "INSERT INTO ext.claims (id, claim_number, member_id, provider_id, date_received, place_of_service, "
        "total_charged_cents, status) VALUES ('clm_0900','CLM-0900','mem_hero','prov_in',$1,'11',11000,'paid')",
        days_ago(200),
    )
    await c.execute(
        "INSERT INTO ext.claim_lines (id, claim_id, line_number, procedure_code, date_of_service, units, charged_cents, "
        "status, allowed_cents, plan_paid_cents, patient_resp_cents) VALUES "
        "('cl_0900_1','clm_0900',1,'D1110',$1,1,11000,'paid',9000,9000,0)", days_ago(200),
    )
    await c.execute(
        "INSERT INTO ext.claims (id, claim_number, member_id, provider_id, date_received, place_of_service, "
        "total_charged_cents, status) VALUES ('clm_0901','CLM-0901','mem_hero','prov_in',$1,'11',6000,'paid')",
        days_ago(90),
    )
    await c.execute(
        "INSERT INTO ext.claim_lines (id, claim_id, line_number, procedure_code, date_of_service, units, charged_cents, "
        "status, allowed_cents, plan_paid_cents, patient_resp_cents) VALUES "
        "('cl_0901_1','clm_0901',1,'D0274',$1,1,6000,'paid',5000,5000,0)", days_ago(90),
    )

    # ── Hero claim CLM-1001 (in-network) — pay / pend / reduce / deny ────────────
    await c.execute(
        "INSERT INTO ext.claims (id, claim_number, member_id, provider_id, date_received, place_of_service, "
        "diagnosis_codes, attachments, total_charged_cents, status) "
        "VALUES ('clm_1001','CLM-1001','mem_hero','prov_in',$1,'11',$2,$3,136000,'received')",
        days_ago(5), ["K02.9", "K08.1"],
        [{"line": 3, "type": "xray", "ref": "img://crown-preop-12.png"}, {"line": 3, "type": "narrative", "ref": "doc://crown-note.txt"}],
    )
    hero_lines = [
        # line, code, tooth, charged
        (1, "D1110", None, 12000),    # cleaning — pay (preventive 100%)
        (2, "D0274", None, 6000),     # bitewings — pend (1/year frequency already used)
        (3, "D2740", "14", 110000),   # crown — reduce (50% major, hits annual max)
        (4, "D9110", None, 8000),     # palliative — deny (non-covered)
    ]
    for ln, code, tooth, charged in hero_lines:
        await c.execute(
            "INSERT INTO ext.claim_lines (id, claim_id, line_number, procedure_code, tooth, date_of_service, units, charged_cents, status) "
            "VALUES ($1,'clm_1001',$2,$3,$4,$5,1,$6,'pending')",
            f"cl_1001_{ln}", ln, code, tooth, days_ago(6), charged,
        )

    # ── Clean claim CLM-1002 (in-network) — a single preventive line, auto-finalize ─
    await c.execute(
        "INSERT INTO ext.claims (id, claim_number, member_id, provider_id, date_received, place_of_service, "
        "total_charged_cents, status) VALUES ('clm_1002','CLM-1002','mem_clean','prov_in',$1,'11',12000,'received')",
        days_ago(3),
    )
    await c.execute(
        "INSERT INTO ext.claim_lines (id, claim_id, line_number, procedure_code, date_of_service, units, charged_cents, status) "
        "VALUES ('cl_1002_1','clm_1002',1,'D1110',$1,1,12000,'pending')", days_ago(3),
    )

    # ── Out-of-network claim CLM-1003 — for the OON path (used by the live agent) ─
    await c.execute(
        "INSERT INTO ext.claims (id, claim_number, member_id, provider_id, date_received, place_of_service, "
        "total_charged_cents, status) VALUES ('clm_1003','CLM-1003','mem_hero','prov_out',$1,'11',30000,'received')",
        days_ago(2),
    )
    await c.execute(
        "INSERT INTO ext.claim_lines (id, claim_id, line_number, procedure_code, date_of_service, units, charged_cents, status) "
        "VALUES ('cl_1003_1','clm_1003',1,'D4341',$1,1,30000,'pending')", days_ago(2),
    )

    counts = await c.fetchrow(
        "SELECT (SELECT count(*) FROM ext.claims) claims, (SELECT count(*) FROM ext.claim_lines) lines, "
        "(SELECT count(*) FROM ext.benefit_rules) rules, (SELECT count(*) FROM ext.reason_codes) reason_codes"
    )
    print("seeded ext.*:", dict(counts))
    print("hero claim: CLM-1001 (mem_hero Robert Lee) — 4 lines: D1110 pay · D0274 pend(freq) · D2740 reduce(annual max) · D9110 deny(non-covered)")
    print("clean claim: CLM-1002 (mem_clean) — single preventive line, auto-finalizes under the ceiling")
    print("oon claim:  CLM-1003 (out-of-network provider) — for the balance-bill / reduce path")


async def main() -> None:
    await ensure_database()
    conn = await asyncpg.connect(CLAIMS_DSN)
    try:
        for typ in ("jsonb", "json"):
            await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
        await conn.execute(SCHEMA_PATH.read_text())
        print("applied schema (ext + agent)")
        await seed(conn)
        print("done.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
