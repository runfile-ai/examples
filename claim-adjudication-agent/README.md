# Insurance Claim Adjudication Agent (Python · Claude Agent SDK)

A self-contained claim adjudication agent that takes a submitted claim and decides
each service line — **pay, deny, reduce, or pend** — with the patient
responsibility and the standard **CARC/RARC** reason codes, through a custom stdio
MCP server over a local Postgres. No external credentials required.

The database mirrors a payer's systems and the **X12** shapes (837 claim in, 835
remittance out, 270/271 eligibility, CARC/RARC codes), so integrating later swaps
each read tool from SQL to the real feeds and nothing above them changes. Built on
`claude-agent-sdk`, the `mcp` package, and `asyncpg`. Same family as the
`monitoring-agent` and `dental-reception-agent` examples.

## The one rule that shapes the architecture

> **The model orchestrates and reasons. A deterministic engine computes the money.
> A human owns every denial or reduction.**

The LLM gathers inputs, judges which rules apply, handles edge cases, and *selects*
reason codes from the standard set. It does **not** do benefit arithmetic and it
does **not** invent codes. The amounts come from `src/mcp_server/engine.py` —
pure, reproducible code — and anything that pays less than billed goes to a human.

```
 claim ─▶ orchestrator (Claude Agent SDK) ◀── skills (intake-and-validate, coverage-and-rules,
              │                                adjudicate, explain-decision, halo-navigation)
              ▼
        read tools ─▶ local Postgres (ext.* mirrors payer systems / X12)
        adjudicate_line  ── DETERMINISTIC engine, not an LLM call
              │  (Halo envelopes at the tool boundary; verifiable layer ON)
              ▼
        decision gate ─▶ agent.approvals ─▶ human reviewer ─▶ post (ext.claim_lines = the 835/EOB)
              ▼
        agent.* state (sessions, halo, decisions + tamper-evident evidence)
```

One Postgres, two schemas: `ext.*` stands in for the payer's claim/member/benefit/
accumulator/network/fee-schedule systems and the X12 shapes; `agent.*` is the
agent's own state — including the decision + evidence record — and never changes
when you integrate.

## Deterministic money, never the model

`adjudicate_line` (`src/mcp_server/engine.py`) is pure functions — no LLM, no DB,
no clock. Given a line, its benefit rule, the accumulators, the allowed amount,
network status, the plan, and the LLM's judged `checks` (duplicate? within
frequency? past waiting? pre-auth on file? missing info?), it applies deductible,
coinsurance, annual max, OOP cap, and network reduction, and returns the exact
money plus the suggested CARC/RARC. Same inputs → same numbers, every time. The
model passes inputs and picks codes; it never does the arithmetic.

## Humans own denials — the decision gate

`post_adjudication` commits decisions to `ext.claim_lines` (the 835/EOB). Any line
that **denies, reduces, or pends**, or any claim whose total plan-paid exceeds the
auto-finalize ceiling, **blocks** on a human reviewer via `agent.approvals`. Only a
clean, all-pay, within-ceiling claim auto-finalizes — and even that is recorded
with full evidence. `agent.decisions` is unique per claim line and
`post_adjudication` is idempotent, so a retry cannot pay twice.

## Verifiable evidence (the point of this agent)

Halo's handles are content addresses: `handle = sha256(content)`. The verifiable
layer is on, and `agent.decisions.evidence` records the exact handles each decision
rested on. Because a handle both *addresses* and *integrity-checks* the bytes, that
audit trail is **tamper-evident** — `halo_verify` re-hashes the stored nodes and
confirms they still match. When a denial is appealed or a regulator asks how a
decision was made, the answer is the specific data the engine adjudicated on, with
proof it was not altered after the fact.

## Tools (`src/mcp_server/server.py`)

| Tool | Kind | Notes |
|------|------|-------|
| `get_claim` | read | 837-shaped header + lines; Halo refs: full_lines / diagnosis / attachments |
| `get_member_coverage` | read | eligibility + plan terms (270/271 later) |
| `get_benefit_rules` | read | per-code coverage %, frequency, waiting, preauth |
| `get_accumulators` | read | deductible / annual-max / OOP met |
| `get_claim_history` | read | prior lines for frequency/duplicate; Halo; `exclude_claim_id` |
| `check_network` | read | in/out of network |
| `get_allowed_amount` | read | fee-schedule allowed amounts |
| `lookup_reason_code` | read | CARC/RARC reference (select, never invent) |
| `adjudicate_line` | **engine** | deterministic money + suggested codes — not a model call |
| `record_decision` | write | proposed decisions + evidence handles |
| `pend_claim` | write | route to a human reviewer |
| `post_adjudication` | write | **decision gate**; idempotent per line |
| `halo_fetch` / `halo_fetch_many` | read | drill into handles |
| `halo_verify` | read | re-hash evidence → tamper check |

Every call is recorded in `agent.tool_calls`.

## Quick start

```bash
# 1. Postgres (reuses the shared local instance on :5433)
#    From elsewhere in this repo: docker compose -f ../creditline-decision-agent/docker-compose.yml up -d
#    …or any Postgres reachable via CLAIMS_DB_DSN.

# 2. Install + configure
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

# 3. Create the `claims` database, apply schema, seed ext.*
python db/seed.py

# 4a. Deterministic demo (no model key) — drives the real MCP server over stdio
python -m src.scripts.run_demo

# 4b. Live agent (Claude Agent SDK; uses your `claude` login or ANTHROPIC_API_KEY)
python -m src.agent.main clm_1001
#     …in a second terminal, act as the reviewer to confirm the gated decisions:
python -m src.scripts.reviewer_console          # interactive
#   (or, unattended for a hands-off run:)  python -m src.scripts.reviewer_console auto
```

The seeded **hero claim CLM-1001** (member Robert Lee) has four lines that hit
every path: `D1110` cleaning → **pay**, `D0274` bitewings → **pend** (1/year
frequency already used), `D2740` crown → **reduce** (50% major, annual max
reached), `D9110` palliative → **deny** (non-covered). **CLM-1002** is a clean
single preventive line that **auto-finalizes** with no human.

## The swap to real systems, later

You touch only the tool bodies in `src/mcp_server/server.py`: `get_claim` /
`get_claim_history` → the claims platform / 837 intake; `get_member_coverage` →
270/271 eligibility; `get_benefit_rules` / `get_allowed_amount` / `check_network` /
`get_accumulators` → the plan, fee-schedule, network, and accumulator systems;
`post_adjudication` → 835 remittance / EOB. **`adjudicate_line` does not swap** —
it is your own deterministic engine and stays whatever the data source is, which is
the point: the arithmetic is never delegated to an API or a model. Everything above
the tools — the skills, the decision gate, `agent.*`, and Halo — is untouched,
because `ext.*` was built to the payer's and X12 shapes.
