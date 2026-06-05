"""Deterministic end-to-end demo (no LLM / API key). Drives the REAL claims MCP
server over stdio for the hero claim CLM-1001:

  get_claim → coverage → network → per line (rules, accumulators, allowed,
  history) → adjudicate_line (deterministic engine) → record_decision with
  evidence handles → halo_verify the evidence → post_adjudication (HUMAN-GATED,
  auto-approved here) → show the 835/EOB lines.

Then CLM-1002 to show a clean claim auto-finalizing with no human.

  python -m src.scripts.run_demo
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import asyncpg
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SESSION_ID = str(uuid.uuid4())
DSN = os.environ.get("CLAIMS_DB_DSN") or (
    f"postgresql://{os.environ.get('ADMIN_DB_USER','postgres')}:{os.environ.get('ADMIN_DB_PASSWORD','postgres')}"
    f"@{os.environ.get('ADMIN_DB_HOST','localhost')}:{os.environ.get('ADMIN_DB_PORT','5433')}/{os.environ.get('CLAIMS_DB_NAME','claims')}"
)

USD = lambda cents: f"${(cents or 0)/100:,.2f}"


async def call(session: ClientSession, name: str, args: dict[str, Any]) -> Any:
    res = await session.call_tool(name, args)
    return json.loads(res.content[0].text)


async def auto_approve(action: str) -> None:
    """Stand in for the human reviewer: approve the next pending gated write."""
    c = await asyncpg.connect(DSN)
    try:
        for _ in range(60):
            row = await c.fetchrow(
                "SELECT id, payload FROM agent.approvals WHERE action=$1 AND status='pending' ORDER BY created_at LIMIT 1", action)
            if row:
                await c.execute(
                    "UPDATE agent.approvals SET status='approved', decided_by='reviewer-auto@demo', decided_at=now() WHERE id=$1", row["id"])
                print(f"  [reviewer] APPROVED {action} {row['payload']}")
                return
            await asyncio.sleep(0.5)
    finally:
        await c.close()


def carc_str(carc: list[dict[str, Any]] | None) -> str:
    return ", ".join(f"{x['code']}/{x['group']} {USD(x['amount_cents'])}" for x in (carc or [])) or "—"


async def main() -> None:
    print("=== Claim adjudication agent — deterministic demo (real MCP server over stdio) ===\n")
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "src.mcp_server.server"], cwd=str(PROJECT_ROOT),
        env={**os.environ, "CLAIMS_DB_DSN": DSN, "AGENT_SESSION_ID": SESSION_ID, "CLAIMS_CHANNEL": "demo",
             "APPROVAL_POLL_SECONDS": "1", "APPROVAL_TIMEOUT_SECONDS": "60", "PYTHONPATH": str(PROJECT_ROOT)},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print(f"MCP server exposes {len(tools.tools)} tools.\n")

            # ── Hero claim CLM-1001 ────────────────────────────────────────────
            claim = await call(session, "get_claim", {"claim_id": "clm_1001"})
            s = claim["summary"]
            print(f"get_claim(CLM-1001) → {s['n_lines']} lines, charged {USD(s['total_charged_cents'])}, member {s['member_id']}")
            print(f"  envelope refs (heavy, unfetched): {', '.join(claim['refs'].keys())}")
            evidence = [claim["map_root"], claim["refs"]["full_lines"]]

            cov = await call(session, "get_member_coverage", {"member_id": s["member_id"]})
            plan = cov["plan"]
            print(f"get_member_coverage → {cov['name']}, eligible={cov['eligible']}, plan {plan['name']} "
                  f"(annual max {USD(plan['annual_max_cents'])}, deductible {USD(plan['deductible_cents'])})")

            net = await call(session, "check_network", {"provider_id": claim["summary"]["provider_id"], "plan_id": plan["id"]})
            print(f"check_network → {net['provider']['name']} in_network={net['in_network']}")

            codes = [ln["procedure_code"] for ln in s["lines"]]
            rules = {r["procedure_code"]: r for r in (await call(session, "get_benefit_rules", {"plan_id": plan["id"], "procedure_codes": codes}))["rules"]}
            accum = await call(session, "get_accumulators", {"member_id": s["member_id"]})
            allowed = (await call(session, "get_allowed_amount", {"plan_id": plan["id"], "procedure_codes": codes}))["allowed"]
            print(f"\naccumulators → deductible met {USD(accum['deductible_met_cents'])}, annual max used {USD(accum['annual_max_used_cents'])} "
                  f"(of {USD(plan['annual_max_cents'])})")

            # frequency history for the bitewing (D0274 is 1/year)
            hist = await call(session, "get_claim_history", {"member_id": s["member_id"], "code": "D0274", "exclude_claim_id": "clm_1001"})
            evidence.append(hist["map_root"])
            d0274_prior = hist["summary"]["by_code"].get("D0274", 0)
            print(f"get_claim_history(D0274) → {d0274_prior} prior in window (limit 1/year)\n")

            # The crown line needs clinical review → pull its attachments (Halo).
            atts = await call(session, "halo_fetch", {"handle": claim["refs"]["attachments"]})
            print(f"halo_fetch(attachments) → crown line has {len(atts)} attachment(s): {', '.join(a['type'] for a in atts)} (pre-auth on file)\n")

            decisions = []
            for ln in s["lines"]:
                code = ln["procedure_code"]
                rule = rules.get(code)
                # The model's JUDGEMENT, expressed as checks the engine consumes:
                checks: dict[str, Any] = {}
                if code == "D0274":
                    checks["within_frequency"] = d0274_prior < 1   # 1/year already used → False
                if code == "D2740":
                    checks["preauth_on_file"] = len(atts) > 0       # narrative + x-ray present → True
                res = await call(session, "adjudicate_line", {
                    "line": ln, "rule": rule, "accumulators": accum, "allowed_cents": allowed.get(code),
                    "in_network": net["in_network"], "plan": plan, "checks": checks,
                })
                print(f"  line {ln['line_number']} {code:6} → {res['decision']:6} "
                      f"allowed {USD(res['allowed_cents'])} · plan {USD(res['plan_paid_cents'])} · patient {USD(res['patient_resp_cents'])}  "
                      f"CARC[{carc_str(res['suggested_carc'])}]")
                decisions.append({
                    "line_number": ln["line_number"], "decision": res["decision"],
                    "allowed_cents": res["allowed_cents"], "plan_paid_cents": res["plan_paid_cents"],
                    "patient_resp_cents": res["patient_resp_cents"], "deductible_cents": res["deductible_cents"],
                    "coinsurance_cents": res["coinsurance_cents"], "copay_cents": res["copay_cents"],
                    "carc": res["suggested_carc"], "rarc": res["suggested_rarc"],
                    "rule_basis": res["rule_basis"], "evidence": evidence,
                })

            rec = await call(session, "record_decision", {"claim_id": "clm_1001", "lines": decisions})
            print(f"\nrecord_decision → {rec['count']} proposed decisions, each with {len(evidence)} evidence handles")

            ver = await call(session, "halo_verify", {"handles": evidence})
            print(f"halo_verify(evidence) → all_intact={ver['all_intact']} ({ver['checked']} handles re-hashed to their content)")

            print("\npost_adjudication (DECISION GATE — has deny/reduce/pend → blocks on a human)…")
            _, posted = await asyncio.gather(auto_approve("post_adjudication"), call(session, "post_adjudication", {"claim_id": "clm_1001"}))
            print(f"  → committed={posted['committed']} auto_finalized={posted['auto_finalized']} approver={posted.get('approver')} "
                  f"plan_paid total {USD(posted.get('total_plan_paid_cents'))}")

            # ── Show the 835/EOB result ────────────────────────────────────────
            print("\n835/EOB — ext.claim_lines after posting:")
            cdb = await asyncpg.connect(DSN)
            await cdb.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
            try:
                rows = await cdb.fetch("SELECT line_number, procedure_code, status, allowed_cents, plan_paid_cents, patient_resp_cents, carc FROM ext.claim_lines WHERE claim_id='clm_1001' ORDER BY line_number")
                for r in rows:
                    print(f"  L{r['line_number']} {r['procedure_code']:6} {r['status']:7} allowed {USD(r['allowed_cents'])} · plan {USD(r['plan_paid_cents'])} · patient {USD(r['patient_resp_cents'])}  CARC[{carc_str(r['carc'])}]")
            finally:
                await cdb.close()

            # ── Clean claim CLM-1002 — auto-finalize, no human ─────────────────
            print("\n— clean claim CLM-1002 (single preventive line) —")
            c2 = await call(session, "get_claim", {"claim_id": "clm_1002"})
            s2 = c2["summary"]
            cov2 = await call(session, "get_member_coverage", {"member_id": s2["member_id"]})
            accum2 = await call(session, "get_accumulators", {"member_id": s2["member_id"]})
            net2 = await call(session, "check_network", {"provider_id": s2["provider_id"], "plan_id": cov2["plan"]["id"]})
            allowed2 = (await call(session, "get_allowed_amount", {"plan_id": cov2["plan"]["id"], "procedure_codes": ["D1110"]}))["allowed"]
            rule2 = (await call(session, "get_benefit_rules", {"plan_id": cov2["plan"]["id"], "procedure_codes": ["D1110"]}))["rules"][0]
            ln2 = s2["lines"][0]
            res2 = await call(session, "adjudicate_line", {"line": ln2, "rule": rule2, "accumulators": accum2, "allowed_cents": allowed2.get("D1110"), "in_network": net2["in_network"], "plan": cov2["plan"], "checks": {}})
            print(f"  line 1 D1110 → {res2['decision']} plan {USD(res2['plan_paid_cents'])} patient {USD(res2['patient_resp_cents'])}")
            await call(session, "record_decision", {"claim_id": "clm_1002", "lines": [{
                "line_number": 1, "decision": res2["decision"], "allowed_cents": res2["allowed_cents"],
                "plan_paid_cents": res2["plan_paid_cents"], "patient_resp_cents": res2["patient_resp_cents"],
                "deductible_cents": res2["deductible_cents"], "coinsurance_cents": res2["coinsurance_cents"],
                "copay_cents": res2["copay_cents"], "carc": res2["suggested_carc"], "rarc": res2["suggested_rarc"],
                "rule_basis": res2["rule_basis"], "evidence": [c2["map_root"]],
            }]})
            posted2 = await call(session, "post_adjudication", {"claim_id": "clm_1002"})
            print(f"  post_adjudication → committed={posted2['committed']} auto_finalized={posted2['auto_finalized']} (no human needed) approver={posted2.get('approver')}")

    print("\n=== demo complete: deterministic engine computed the money · evidence tamper-evident · human owned the deny/reduce/pend · clean claim auto-finalized ===")


if __name__ == "__main__":
    asyncio.run(main())
