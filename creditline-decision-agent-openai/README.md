# Credit-Line Decision Agent — OpenAI Agents SDK

The **same** human-in-the-loop credit-line decision agent as
[`../creditline-decision-agent`](../creditline-decision-agent), rebuilt on the
**OpenAI Agents SDK** instead of the Claude Agent SDK.

The point of this folder: the agent *runtime* is the only thing that changes.
The environment — the `mimic-creditline` MCP server and the `mimic_creditline`
Postgres database — is reused as-is. MCP makes the domain tools portable across
agent frameworks.

```
┌──────────────────────────────┐        ┌──────────────────────────────┐
│  Claude Agent SDK runtime     │        │  OpenAI Agents SDK runtime     │
│  ../creditline-decision-agent │        │  this folder                   │
└───────────────┬───────────────┘        └───────────────┬───────────────┘
                │           both speak MCP (stdio)        │
                └──────────────────┬──────────────────────┘
                                   ▼
                 ┌───────────────────────────────────────┐
                 │  mimic-creditline MCP server (shared)   │
                 │  → Postgres mimic_creditline (shared)   │
                 └───────────────────────────────────────┘
```

## What's here vs. reused

| Component | Where |
|-----------|-------|
| Agent runtime (`agents.Agent` + `Runner` + `MCPServerStdio`) | `agent/main.py` (this folder) |
| Instructions + prompt-hash provenance | `agent/prompts.py` (this folder) |
| §3 decision scorer (framework-agnostic) | `agent/decision.py` (copy) |
| **MCP server `mimic-creditline`** | `../creditline-decision-agent/mcp_servers/` (reused) |
| **Postgres schema + seed** | `../creditline-decision-agent/db/` (reused) |
| **Human officer console (HITL)** | `../creditline-decision-agent/scripts/officer_console.py` (reused) |

## How it differs from the Claude version

- **SDK:** `openai-agents` (`Agent`, `Runner.run`, `agents.mcp.MCPServerStdio`)
  instead of `claude-agent-sdk` (`query`, `ClaudeAgentOptions`).
- **Model:** OpenAI (`gpt-4.1` by default) instead of Claude.
- **MCP wiring:** the server is attached via `Agent(mcp_servers=[server])`; the
  blocking approval gate works unchanged because the block lives in the MCP tool,
  so `client_session_timeout_seconds` is raised to outlast it.
- **Provenance:** the prompt hash is computed locally from the instructions
  (`PROMPT_VERSION_HASH`) and passed to `creditline_record_decision`, rather than
  fetched from the `creditline_get_agent_provenance` tool. Same contract.

Everything else — the seven domain tools, the policy-driven rules, the
escalate-never-deny invariant, the override semantics — is identical because it
lives in the shared MCP server and database.

## Quick start

```bash
# 0. Bring up Postgres and seed it using the SIBLING project (one-time):
cd ../creditline-decision-agent
docker compose up -d
uv venv --python 3.11 && source .venv/bin/activate && uv pip install -e .
cp .env.example .env && set -a && . ./.env && set +a && bash scripts/init_db.sh
cd ../creditline-decision-agent-openai

# 1. This project's deps
uv venv --python 3.11 && source .venv/bin/activate && uv pip install -e .
cp .env.example .env

# 2a. Deterministic demo (no OpenAI key) — drives the shared MCP tools
set -a && . ./.env && set +a
python -m scripts.run_demo

# 2b. Live, model-driven agent (needs OPENAI_API_KEY)
python -m agent.main 11111111-1111-1111-1111-111111111111
#   …in a second terminal, resolve the blocking gate (from the sibling project):
cd ../creditline-decision-agent && python -m scripts.officer_console
```

The seeded escalation case (Dana Whitfield, request
`11111111-1111-1111-1111-111111111111`) asks for 25,000 — above the 15,000
ceiling and pushing DTI past 0.45 — so the agent escalates; the officer modifies
it to an approved 12,000 limit (`is_override = true`).
