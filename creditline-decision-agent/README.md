# Credit-Line Decision Agent

A human-in-the-loop decision agent built on the **Claude Agent SDK**. It decides
whether to **approve, deny, or escalate** a customer's credit-line request, acting
only through a **custom MCP server** (`mimic-creditline`) backed by Postgres.

All customer/bureau/policy data is simulated. Every adverse or above-ceiling
outcome is routed to a human credit officer for confirmation or override — the
EU AI Act Art. 14 / SR 11-7 §5.2 "effective challenge" node.

> Scope of this example: the **agent** and the **MCP server** it acts through.
> The out-of-band audit "runfile" described in the original concept is **not**
> included here.

## Architecture

```
┌─────────────────────────────────────────────┐
│  AGENT  (Claude Agent SDK)                    │
│  Skills: intake → scoring → decisioning       │
│  Reasoning + tool calls + the approval gate   │
└───────────────┬───────────────────────────────┘
                │ MCP (stdio, domain tools)
                ▼
┌──────────────────────────────────────────────┐
│  mimic-creditline  (custom MCP server)         │
│  7 domain-shaped tools, Pydantic-validated     │
│  → Postgres: mimic_creditline (least-priv role)│
└──────────────────────────────────────────────┘
```

Two layers, kept separate:

- **Agent** (`agent/`) — the SDK runtime, the system prompt + provenance, the
  reference decision logic, and the three Skills under `.claude/skills/`.
- **Environment** (`mcp_servers/mimic_creditline/`) — the MCP server wrapping the
  simulated world in semantic tools (never raw SQL), so tool calls are
  control-mappable.

The MCP server connects to Postgres as the **least-privilege `creditline_agent`
role**. Database creation and seeding use a separate admin connection that is
never handed to the agent.

## The 7 MCP tools

| Tool | Purpose | readOnly | destructive | idempotent |
|------|---------|:--------:|:-----------:|:----------:|
| `creditline_get_request`       | Fetch the inbound request               | ✅ | ❌ | ✅ |
| `creditline_get_customer`      | Customer profile + existing lines       | ✅ | ❌ | ✅ |
| `creditline_pull_bureau`       | Pull simulated bureau report            | ✅ | ❌ | ❌ |
| `creditline_get_active_policy` | Retrieve active versioned policy        | ✅ | ❌ | ✅ |
| `creditline_record_decision`   | Persist outcome + provenance            | ❌ | ❌ | ✅ (by request_id) |
| `creditline_request_approval`  | Open HITL gate; **blocks** until resolved | ❌ | ❌ | ✅ (by decision_id) |
| `creditline_notify_customer`   | Send decision letter (simulated)        | ❌ | ✅ | ✅ (by idempotency key) |

## Decision logic (policy-driven, §3)

```
score  = bureau.credit_score
dti    = (bureau.total_outstanding_debt + requested_limit) / customer.annual_income
delinq = bureau.delinquencies_24m

AUTO-APPROVE  if requested_limit <= auto_approve_ceiling
              and score >= min_credit_score and dti <= max_dti
              and delinq <= max_delinquencies_24m
AUTO-DENY     never — every adverse outcome ESCALATES (GDPR Art. 22 / Art. 14)
ESCALATE      if requested_limit > auto_approve_ceiling, or any threshold fails
```

The rules live in the **policy row** (`decision_policies.thresholds`), so a
decision can change by versioning the policy rather than the code. The canonical
scorer is `agent/decision.py`; the live agent applies the same rules via the
`decisioning` skill.

## Quick start

```bash
# 1. Postgres (single engine; database mimic_creditline)
docker compose up -d

# 2. Python deps
uv venv --python 3.11 && source .venv/bin/activate
uv pip install -e .

# 3. Config
cp .env.example .env            # defaults match docker-compose

# 4. Create schema + agent role + seed data
set -a && . ./.env && set +a
bash scripts/init_db.sh

# 5a. Deterministic demo (no API key) — drives the real MCP tools end to end
python -m scripts.run_demo

# 5b. Live, model-driven agent (needs ANTHROPIC_API_KEY)
python -m agent.main 11111111-1111-1111-1111-111111111111
#   …in a second terminal, resolve the blocking approval gate:
python -m scripts.officer_console
```

The seeded escalation case (**Dana Whitfield**, request
`11111111-1111-1111-1111-111111111111`) asks for 25,000 — above the 15,000
ceiling **and** pushing DTI to 0.458 (> 0.45). The agent escalates; the officer
modifies it to an approved 12,000 limit (`is_override = true`).

## Human-in-the-loop

`creditline_request_approval` creates a pending `approvals` row and **blocks the
run** (polling) until a credit officer resolves it out of band via
`scripts/officer_console.py`. The officer can **confirm**, **reject**, or
**modify** (approve a lower limit). A reject/modify against the agent's
recommendation sets `is_override = true`.

## Layout

```
agent/                      Claude Agent SDK runtime
  main.py                   registers the MCP server, loads skills, runs a request
  decision.py               reference §3 scorer (deterministic)
  prompts.py                system prompt + model/prompt provenance hash
.claude/skills/             Skills the SDK loads (intake, scoring, decisioning)
mcp_servers/mimic_creditline/
  server.py                 the 7 FastMCP tools
  models.py                 Pydantic tool contracts
  db.py                     asyncpg pool (least-privilege agent role)
db/                         schema, role/grants, seed
scripts/                    init_db.sh, run_demo.py, officer_console.py
```
