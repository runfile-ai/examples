# Credit-Line Decision Agent — LangGraph

The **same** human-in-the-loop credit-line decision agent as
[`../creditline-decision-agent`](../creditline-decision-agent) (Claude Agent SDK)
and [`../creditline-decision-agent-openai`](../creditline-decision-agent-openai)
(OpenAI Agents SDK), rebuilt on **LangGraph**.

Built on the current 1.x stack: `langgraph` 1.x, `langchain` 1.x,
`langchain-mcp-adapters` 0.2.x, `langchain-anthropic` 1.x.

As with the other builds, only the agent *runtime* changes. The environment —
the `mimic-creditline` MCP server and the `mimic_creditline` Postgres database —
is reused as-is.

```
┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐
│ Claude Agent SDK │   │ OpenAI Agents    │   │ LangGraph        │
│                  │   │ SDK              │   │ (this folder)    │
└────────┬─────────┘   └────────┬─────────┘   └────────┬─────────┘
         │           all speak MCP (stdio)             │
         └─────────────────────┬──────────────────────┘
                               ▼
            ┌───────────────────────────────────────┐
            │  mimic-creditline MCP server (shared)   │
            │  → Postgres mimic_creditline (shared)   │
            └───────────────────────────────────────┘
```

## How this build works

- **MCP tools → LangChain tools:** `langchain_mcp_adapters.MultiServerMCPClient`
  launches the shared MCP server over stdio and exposes its 8 tools via
  `await client.get_tools()`.
- **Agent graph:** `langgraph.prebuilt.create_react_agent(model, tools,
  prompt=INSTRUCTIONS)` builds the ReAct loop. Run with
  `await agent.ainvoke({"messages": [...]})`.
- **Model:** any LangChain chat model via `init_chat_model` — defaults to
  `anthropic:claude-sonnet-4-6` (set `AGENT_MODEL` / `AGENT_MODEL_PROVIDER`;
  `openai` works too).
- **Blocking HITL gate:** the block lives in the MCP `creditline_request_approval`
  tool, so it works unchanged. The stdio connection sets a long
  `session_kwargs.read_timeout_seconds` so the MCP session outlasts the wait.
- **Provenance:** the `prompt_version_hash` is computed locally from the
  instructions and passed to `creditline_record_decision` — same contract as the
  other builds.
- **Runfile audit (opt-in):** one line instruments the compiled graph — see
  [Audit capture with Runfile](#audit-capture-with-runfile).

## Audit capture with Runfile

Like the Claude Agent SDK build, this build can observe the agent through the
**Runfile** Python SDK ([`runfile-ai`](https://pypi.org/project/runfile-ai/),
installed **from PyPI** via the `runfile-ai[langgraph]` extra — never a local
path). `agent/main.py` wraps the compiled graph with
`runfile_ai.integrations.langgraph.instrument(...)` under a stable, version-pinned
identity (`did:web:runfile.ai:agents:creditline-decision-agent-langgraph:0.1.0`),
translating LangGraph's tool and model signals into tamper-evident audit events.

Capture is **opt-in and transparent**: the wrapper is a no-op unless
`RUNFILE_API_KEY` is set (and degrades to a pass-through if `runfile-ai` isn't
installed), so the example runs identically without Runfile.

```bash
export RUNFILE_API_KEY=rk_...           # enable capture
export RUNFILE_BASE_URL=http://localhost:8787   # optional: local/self-hosted ingest
python -m agent.main 11111111-1111-1111-1111-111111111111
```

## What's here vs. reused

| Component | Where |
|-----------|-------|
| Agent runtime (`create_react_agent` + `MultiServerMCPClient`) | `agent/main.py` (this folder) |
| Instructions + prompt-hash provenance | `agent/prompts.py` (this folder) |
| §3 decision scorer (framework-agnostic) | `agent/decision.py` (copy) |
| **MCP server `mimic-creditline`** | `../creditline-decision-agent/mcp_servers/` (reused) |
| **Postgres schema + seed** | `../creditline-decision-agent/db/` (reused) |
| **Human officer console (HITL)** | `../creditline-decision-agent/scripts/officer_console.py` (reused) |

## Quick start

```bash
# 0. Bring up Postgres and seed it using the base project (one-time):
cd ../creditline-decision-agent
docker compose up -d
uv venv --python 3.11 && source .venv/bin/activate && uv pip install -e .
cp .env.example .env && set -a && . ./.env && set +a && bash scripts/init_db.sh
cd ../creditline-decision-agent-langgraph

# 1. This project's deps
uv venv --python 3.11 && source .venv/bin/activate && uv pip install -e .
cp .env.example .env

# 2a. Deterministic demo (no model key) — drives the shared MCP tools
set -a && . ./.env && set +a
python -m scripts.run_demo

# 2b. Live, model-driven agent (needs ANTHROPIC_API_KEY for the default provider)
python -m agent.main 11111111-1111-1111-1111-111111111111
#   …in a second terminal, resolve the blocking gate (from the base project):
cd ../creditline-decision-agent && python -m scripts.officer_console
```

The seeded escalation case (Dana Whitfield, request
`11111111-1111-1111-1111-111111111111`) asks for 25,000 — above the 15,000
ceiling and pushing DTI past 0.45 — so the agent escalates; the officer modifies
it to an approved 12,000 limit (`is_override = true`).
