# Credit-Line Decision Agent (TypeScript · OpenAI Agents SDK)

The **OpenAI Agents SDK** build of the credit-line decision agent — the same
human-in-the-loop agent as the Claude and LangGraph builds, on a different
runtime. It connects to the **same** `mimic-creditline` MCP server (spawned from
the sibling base project over stdio) and runs one request end to end.

This is the TypeScript port of the Python `creditline-decision-agent-openai`.
Only the agent runtime changes; the simulated world — the MCP server and the
`mimic_creditline` Postgres — is shared and owned by the base project.

> The reusable environment (MCP server, schema, seed, officer console) lives in
> [`../creditline-decision-agent-ts`](../creditline-decision-agent-ts). Set that
> up first. (The optional Runfile audit capture from the Python build is
> intentionally not wired here.)

## How it's wired

```
@openai/agents  Agent + run ──┐
   (model + MCP tools)        │  MCPServerStdio
                              ▼
        mimic-creditline MCP server  ─▶  Postgres mimic_creditline
        (spawned from the sibling base project)
```

- `MCPServerStdio` launches the base project's `src/mcp/server.ts` over stdio and
  exposes its 8 tools to the agent. `clientSessionTimeoutSeconds` is set to
  outlast the blocking approval gate.
- `new Agent({ instructions, model, mcpServers })` + `run(agent, prompt)` runs
  intake → bureau → policy → score → decide → record → human-approval, honouring
  the officer's resolution.

The decision logic (`src/agent/decision.ts`) is identical to the Claude build —
the rules belong to the domain, not the SDK.

## Quick start

```bash
# 0. Base project: bring up Postgres, install, seed (one time)
cd ../creditline-decision-agent-ts && npm install && cp .env.example .env && npm run initdb && cd -

# 1. Install + configure this build
npm install
cp .env.example .env

# 2a. Deterministic demo (no API key) — drives the shared MCP server over stdio
npm run demo

# 2b. Live, model-driven OpenAI Agents run (needs OPENAI_API_KEY)
npm run agent 11111111-1111-1111-1111-111111111111
#   …in a second terminal, resolve the blocking gate from the BASE project:
cd ../creditline-decision-agent-ts && npm run officer -- auto
```

The seeded escalation case (**Dana Whitfield**) asks for 25,000 — above the
15,000 ceiling and pushing DTI to 0.458 — so the agent escalates and the officer
modifies it to an approved 12,000 limit (`is_override = true`).

## Layout

```
src/agent/
  main.ts        OpenAI Agents run over the shared MCP tools
  decision.ts    reference §3 scorer (identical to the Claude build)
  prompts.ts     instructions + model/prompt provenance hash
src/scripts/
  run-demo.ts    deterministic end-to-end demo (shared MCP server over stdio)
```
