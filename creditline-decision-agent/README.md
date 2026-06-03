# Credit-Line Decision Agent

A human-in-the-loop decision agent built on the **Claude Agent SDK**. It decides
whether to **approve, deny, or escalate** a customer's credit-line request, acting
only through a **custom MCP server** (`mimic-creditline`) backed by Postgres.

All customer/bureau/policy data is simulated. Every adverse or above-ceiling
outcome is routed to a human credit officer for confirmation or override — the
EU AI Act Art. 14 / SR 11-7 §5.2 "effective challenge" node.

> Scope of this example: the **agent**, the **MCP server** it acts through, and
> an opt-in **Runfile** audit trail. Capture is wired through the Claude Agent
> SDK adapter and is a no-op unless `RUNFILE_API_KEY` is set, so the example runs
> identically with or without Runfile. See [Audit capture with Runfile](#audit-capture-with-runfile).

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

# 5b. Live, model-driven agent via the Claude Agent SDK
python -m agent.main 11111111-1111-1111-1111-111111111111
#   …in a second terminal, resolve the blocking approval gate:
python -m scripts.officer_console
```

### Run it via the Claude Code runtime

The project ships a native Claude Code setup, so you can drive the same agent
straight from the `claude` CLI — no Python entrypoint needed:

- `.mcp.json` registers the `mimic-creditline` MCP server,
- `CLAUDE.md` is the agent's operating manual,
- `.claude/settings.json` enables the server and allow-lists its tools,
- `.claude/skills/` are loaded as Skills automatically.

```bash
set -a && . ./.env && set +a            # so the MCP server can reach Postgres

# one terminal: stand in for the credit officer (or use the interactive console)
python -m scripts.officer_console auto

# another terminal: run the agent
claude -p "Process credit-line request 11111111-1111-1111-1111-111111111111 \
  end to end; when you escalate, open the approval gate, wait for the officer, \
  then state the final outcome." --model claude-sonnet-4-6
```

This path is verified end to end: the model runs intake → bureau → policy →
score → escalate → record → HITL gate → override, and the officer modifies the
recommendation to an approved 12,000 limit (`is_override = true`).

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

## Audit capture with Runfile

This build observes the agent through the **Runfile** Python SDK
([`runfile-ai`](https://pypi.org/project/runfile-ai/), installed from PyPI via
the `runfile-ai[anthropic]` dependency — never a local path). `agent/main.py`
swaps the SDK's `query()` for `observe_query()` from
`runfile_ai.integrations.anthropic`: it owns the run lifecycle and translates the
agent's tool calls and model activity into tamper-evident audit events, then
`flush()` drains them before exit. The agent runs under a stable, version-pinned
identity (`did:web:runfile.ai:agents:creditline-decision-agent:0.1.0`), and
`thinking` is set to `summarized` so the decision's reasoning lands in the trail
rather than being discarded.

Capture is **opt-in and transparent**: with no `RUNFILE_API_KEY` set,
`observe_query()` passes straight through and the run is byte-for-byte identical
to plain `query()`.

```bash
export RUNFILE_API_KEY=rk_...           # enable capture
export RUNFILE_BASE_URL=http://localhost:8787   # optional: local/self-hosted ingest
python -m agent.main 11111111-1111-1111-1111-111111111111
```

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
