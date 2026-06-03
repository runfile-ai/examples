# Runfile examples — the Credit-Line Decision Agent

One human-in-the-loop credit-line decision agent, built three times on three
different runtimes, all acting through the **same** `mimic-creditline` MCP server
and the **same** `mimic_creditline` Postgres environment. Only the agent runtime
changes; the simulated world it acts on is shared.

Each build decides whether to **approve, deny, or escalate** a customer's
credit-line request, and routes every adverse or above-ceiling outcome to a human
credit officer for confirmation or override — the EU AI Act Art. 14 / SR 11-7
§5.2 "effective challenge" node.

## The three builds

| Example | Runtime | Runfile audit |
|---------|---------|---------------|
| [`creditline-decision-agent`](creditline-decision-agent) | Claude Agent SDK | ✅ `observe_query()` via `runfile_ai.integrations.anthropic` |
| [`creditline-decision-agent-langgraph`](creditline-decision-agent-langgraph) | LangGraph (`create_react_agent`) | ✅ `instrument()` via `runfile_ai.integrations.langgraph` |
| [`creditline-decision-agent-openai`](creditline-decision-agent-openai) | OpenAI Agents SDK | — |

The `creditline-decision-agent` (Claude Agent SDK) build is the **base project**:
it owns the MCP server, the Postgres schema + seed, and the officer console that
the other builds reuse. Start there.

## Audit capture with Runfile

The two integrated builds observe their agent through the **Runfile** Python SDK
([`runfile-ai`](https://pypi.org/project/runfile-ai/)), installed **from PyPI**
through each project's `runfile-ai[...]` extra — never a local path or editable
build. Runfile translates framework-native signals (tool calls, model activity,
the approval gate) into tamper-evident audit events under a stable, version-pinned
agent identity.

Capture is **opt-in and transparent**: with no `RUNFILE_API_KEY` set the adapters
pass straight through, so every example runs identically with or without Runfile.
Set `RUNFILE_API_KEY` (and optionally `RUNFILE_BASE_URL` for a local or
self-hosted ingest endpoint) to capture a run.

See each build's README for the per-runtime wiring:
- [Claude Agent SDK — Audit capture with Runfile](creditline-decision-agent/README.md#audit-capture-with-runfile)
- [LangGraph — Audit capture with Runfile](creditline-decision-agent-langgraph/README.md#audit-capture-with-runfile)

## Quick start

Each project has its own README with a full quick start. In short:

```bash
# Base project: Postgres + MCP server + seed data + officer console
cd creditline-decision-agent
docker compose up -d
uv venv --python 3.11 && source .venv/bin/activate && uv pip install -e .
cp .env.example .env && set -a && . ./.env && set +a && bash scripts/init_db.sh
python -m agent.main 11111111-1111-1111-1111-111111111111
```

The seeded escalation case (**Dana Whitfield**, request
`11111111-1111-1111-1111-111111111111`) asks for 25,000 — above the 15,000
ceiling and pushing DTI past 0.45 — so the agent escalates and the officer
modifies it to an approved 12,000 limit (`is_override = true`).
