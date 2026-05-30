"""Seed the mimic_creditline database.

Creates a versioned policy (v1 archived + v2 active), a handful of customers,
their existing lines and bureau reports, and inbound requests. One applicant —
Dana Whitfield, request id 1111…1111 — is engineered to ESCALATE (asks above
the auto-approve ceiling AND trips the DTI threshold), which is the human-in-
the-loop / override demo case.

Run via the admin DSN (never the agent role):
    python -m db.seed.seed_mimic
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os

import asyncpg

# Deterministic ids so the demo can reference the escalation case directly.
DEMO_CUSTOMER_ID = "22222222-2222-2222-2222-222222222222"
DEMO_REQUEST_ID = "11111111-1111-1111-1111-111111111111"
DEMO_BUREAU_ID = "33333333-3333-3333-3333-333333333333"


def _admin_dsn() -> str:
    host = os.environ.get("ADMIN_DB_HOST", "localhost")
    port = os.environ.get("ADMIN_DB_PORT", "5433")
    user = os.environ.get("ADMIN_DB_USER", "postgres")
    pw = os.environ.get("ADMIN_DB_PASSWORD", "postgres")
    return f"postgresql://{user}:{pw}@{host}:{port}/mimic_creditline"


async def seed() -> None:
    conn = await asyncpg.connect(_admin_dsn())
    try:
        # Idempotent reseed: clear in FK-safe order.
        for tbl in (
            "approvals",
            "decisions",
            "bureau_reports",
            "credit_line_requests",
            "credit_lines",
            "customers",
            "decision_policies",
        ):
            await conn.execute(f"DELETE FROM {tbl}")

        # ── Policies ──────────────────────────────────────────────────────────
        now = dt.datetime.now(dt.timezone.utc)
        await conn.execute(
            """
            INSERT INTO decision_policies (version, thresholds, narrative, effective_from, effective_to)
            VALUES ($1, $2::jsonb, $3, $4, $5)
            """,
            "2026.01-rev1",
            json.dumps(
                {
                    "min_credit_score": 700,
                    "max_dti": 0.40,
                    "auto_approve_ceiling": 10000,
                    "max_delinquencies_24m": 0,
                }
            ),
            "Conservative launch policy.",
            now - dt.timedelta(days=120),
            now - dt.timedelta(days=30),  # archived
        )
        await conn.execute(
            """
            INSERT INTO decision_policies (version, thresholds, narrative, effective_from, effective_to)
            VALUES ($1, $2::jsonb, $3, $4, NULL)
            """,
            "2026.03-rev2",
            json.dumps(
                {
                    "min_credit_score": 680,
                    "max_dti": 0.45,
                    "auto_approve_ceiling": 15000,
                    "max_delinquencies_24m": 1,
                }
            ),
            "Q1-2026 revision: ceiling raised to 15k, DTI tolerance to 0.45.",
            now - dt.timedelta(days=30),
        )

        # ── Demo applicant: Dana Whitfield (escalation / override case) ───────
        await conn.execute(
            """
            INSERT INTO customers
                (customer_id, full_name, date_of_birth, email, annual_income,
                 employment_status, residential_status, relationship_since, internal_risk_segment)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            """,
            DEMO_CUSTOMER_ID,
            "Dana Whitfield",
            dt.date(1987, 4, 2),
            "dana.whitfield@example.com",
            72000,
            "employed",
            "renter",
            dt.date(2019, 6, 1),
            "B",
        )
        await conn.execute(
            """
            INSERT INTO credit_lines (customer_id, product_type, current_limit, current_balance, status)
            VALUES ($1, 'card', 6000, 2200, 'active')
            """,
            DEMO_CUSTOMER_ID,
        )
        await conn.execute(
            """
            INSERT INTO bureau_reports
                (bureau_report_id, customer_id, bureau_name, report_version, credit_score,
                 total_outstanding_debt, delinquencies_24m, open_accounts, hard_inquiries_6m)
            VALUES ($1,$2,'experian_sim','2026-03-A',712, 8000, 0, 4, 1)
            """,
            DEMO_BUREAU_ID,
            DEMO_CUSTOMER_ID,
        )
        # Requests 25,000 → above the 15,000 ceiling (large exposure) AND
        # dti = (8000 + 25000) / 72000 = 0.458 > 0.45 → escalate.
        await conn.execute(
            """
            INSERT INTO credit_line_requests
                (request_id, customer_id, request_type, requested_limit, channel, status)
            VALUES ($1,$2,'increase',25000,'app','pending')
            """,
            DEMO_REQUEST_ID,
            DEMO_CUSTOMER_ID,
        )

        # ── A clean auto-approve applicant ───────────────────────────────────
        marco = await conn.fetchval(
            """
            INSERT INTO customers
                (full_name, date_of_birth, email, annual_income, employment_status,
                 residential_status, relationship_since, internal_risk_segment)
            VALUES ('Marco Reyes','1979-11-20','marco.reyes@example.com',98000,'employed','owner','2015-02-01','A')
            RETURNING customer_id
            """
        )
        await conn.execute(
            """
            INSERT INTO bureau_reports
                (customer_id, bureau_name, report_version, credit_score,
                 total_outstanding_debt, delinquencies_24m, open_accounts, hard_inquiries_6m)
            VALUES ($1,'experian_sim','2026-03-A',775, 5000, 0, 6, 0)
            """,
            marco,
        )
        await conn.execute(
            """
            INSERT INTO credit_line_requests
                (customer_id, request_type, requested_limit, channel, status)
            VALUES ($1,'new',8000,'web','pending')
            """,
            marco,
        )

        # ── A borderline adverse applicant (single-threshold fail → escalate) ─
        priya = await conn.fetchval(
            """
            INSERT INTO customers
                (full_name, date_of_birth, email, annual_income, employment_status,
                 residential_status, relationship_since, internal_risk_segment)
            VALUES ('Priya Nair','1992-07-09','priya.nair@example.com',54000,'self_employed','renter','2021-09-01','C')
            RETURNING customer_id
            """
        )
        await conn.execute(
            """
            INSERT INTO bureau_reports
                (customer_id, bureau_name, report_version, credit_score,
                 total_outstanding_debt, delinquencies_24m, open_accounts, hard_inquiries_6m)
            VALUES ($1,'experian_sim','2026-03-A',664, 9000, 2, 5, 3)
            """,
            priya,
        )
        await conn.execute(
            """
            INSERT INTO credit_line_requests
                (customer_id, request_type, requested_limit, channel, status)
            VALUES ($1,'increase',6000,'branch','pending')
            """,
            priya,
        )

        print("Seeded mimic_creditline:")
        print(f"  demo escalation request_id = {DEMO_REQUEST_ID} (Dana Whitfield)")
        print("  + 1 auto-approve applicant (Marco Reyes)")
        print("  + 1 borderline applicant (Priya Nair)")
        print("  policies: 2026.01-rev1 (archived), 2026.03-rev2 (active)")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(seed())
