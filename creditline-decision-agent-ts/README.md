# Credit-Line Decision Agent (TypeScript · Claude Agent SDK)

A human-in-the-loop decision agent built on the **Claude Agent SDK** (TypeScript).
It decides whether to **approve, deny, or escalate** a customer's credit-line
request, acting only through a **custom MCP server** (`mimic-creditline`) backed
by Postgres.

This is the **TypeScript port** of the Python `creditline-decision-agent`. All
customer/bureau/policy data is simulated. Every adverse or above-ceiling outcome
is routed to a human credit officer for confirmation or override — the EU AI Act
Art. 14 / SR 11-7 §5.2 "effective challenge" node.

> This port covers the **agent**, the **MCP server** it acts through, the
> Postgres schema/seed, and the officer console. It is the **base project**: the
> `-langgraph-ts` and `-openai-ts` builds reuse this MCP server and database.
> (The optional Runfile audit capture from the Python build is intentionally not
> wired here.)

## Architecture

```
┌─────────────────────────────────────────────┐
│  AGENT  (Claude Agent SDK, TypeScript)        │
│  Skills: intake → scoring → decisioning       │
│  Reasoning + tool calls + the approval gate   │
└───────────────┬───────────────────────────────┘
                │ MCP (stdio, domain tools)
                ▼
┌──────────────────────────────────────────────┐
│  mimic-creditline  (custom MCP server)         │
│  8 domain-shaped tools, Zod-validated          │
│  → Postgres: mimic_creditline (least-priv role)│
└──────────────────────────────────────────────┘
```

- **Agent** (`src/agent/`) — the SDK runtime, the system prompt + provenance, the
  reference decision logic, and the three Skills under `.claude/skills/`.
- **Environment** (`src/mcp/`) — the MCP server wrapping the simulated world in
  semantic tools (never raw SQL), so tool calls are control-mappable.

The MCP server connects to Postgres as the **least-privilege `creditline_agent`
role**. Database creation and seeding use a separate admin connection that is
never handed to the agent.

## The 8 MCP tools

| Tool | Purpose | readOnly | destructive | idempotent |
|------|---------|:--------:|:-----------:|:----------:|
| `creditline_get_agent_provenance` | Canonical `prompt_version_hash` (sha256 over CLAUDE.md) | ✅ | ❌ | ✅ |
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
scorer is `src/agent/decision.ts`; the live agent applies the same rules via the
`decisioning` skill.

## Quick start

```bash
# 1. Postgres (single engine; database mimic_creditline). Reuses the shared
#    local instance on :5433.
docker compose up -d

# 2. Install + configure
npm install
cp .env.example .env            # defaults match docker-compose

# 3. Create database + schema + agent role + seed data
npm run initdb

# 4a. Deterministic demo (no API key) — drives the real MCP server over stdio
npm run demo

# 4b. Live, model-driven agent via the Claude Agent SDK
npm run agent 11111111-1111-1111-1111-111111111111
#   …in a second terminal, resolve the blocking approval gate:
npm run officer            # interactive
#   (or, unattended for a hands-off run:)  npm run officer -- auto
```

The seeded escalation case (**Dana Whitfield**, request
`11111111-1111-1111-1111-111111111111`) asks for 25,000 — above the 15,000
ceiling **and** pushing DTI to 0.458 (> 0.45) — so the agent escalates and the
officer modifies it to an approved 12,000 limit (`is_override = true`).

### Run it via the Claude Code runtime

The project ships a native Claude Code setup, so you can drive the same agent
straight from the `claude` CLI — no Node entrypoint needed:

- `.mcp.json` registers the `mimic-creditline` MCP server (`npx tsx src/mcp/server.ts`),
- `CLAUDE.md` is the agent's operating manual,
- `.claude/settings.json` enables the server and allow-lists its tools,
- `.claude/skills/` are loaded as Skills automatically.

```bash
set -a && . ./.env && set +a            # so the MCP server can reach Postgres

# one terminal: stand in for the credit officer
npm run officer -- auto

# another terminal: run the agent
claude -p "Process credit-line request 11111111-1111-1111-1111-111111111111 \
  end to end; when you escalate, open the approval gate, wait for the officer, \
  then state the final outcome." --model claude-sonnet-4-6
```

## Human-in-the-loop

`creditline_request_approval` creates a pending `approvals` row and **blocks the
run** (polling) until a credit officer resolves it out of band via
`src/scripts/officer-console.ts`. The officer can **confirm**, **reject**, or
**modify** (approve a lower limit). A reject/modify against the agent's
recommendation sets `is_override = true`.

## Layout

```
src/agent/                  Claude Agent SDK runtime
  main.ts                   registers the MCP server, loads skills, runs a request
  decision.ts               reference §3 scorer (deterministic)
  prompts.ts                system prompt + model/prompt provenance hash
.claude/skills/             Skills the SDK loads (intake, scoring, decisioning)
src/mcp/
  server.ts                 the 8 MCP tools
  models.ts                 Zod tool contracts
  db.ts                     pg pool (least-privilege agent role)
db/                         schema, role/grants, seed.ts
src/scripts/                run-demo.ts, officer-console.ts
```
