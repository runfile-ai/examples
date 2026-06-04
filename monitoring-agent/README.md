# Production Monitoring Agent (TypeScript · Claude Agent SDK)

A self-contained monitoring agent that triages **Sentry-shaped** issues,
diagnoses them against **Datadog/Loki-shaped** logs, and declares/resolves
**PagerDuty-shaped** incidents — all through a human-gated, **Halo**-backed tool
layer over a local Postgres. No external credentials required.

Built on `@anthropic-ai/claude-agent-sdk` (TypeScript) with Skills, and a custom
stdio MCP server.

## The guiding idea

The **tool contract is the swap point.** Today each tool body is SQL on `ext.*`;
later it is an API call to Sentry / the logs backend / PagerDuty. Because the
local `ext.*` tables are shaped like the external systems, that swap is
mechanical — the skills, the approval gate, and Halo are untouched.

```
 schedule / webhook ─▶ orchestrator (Claude Agent SDK) ◀── skills (triage, diagnose,
                              │                              incident-response, halo-navigation)
                              │ mcp tool call
                              ▼
                     monitoring MCP server ─▶ local Postgres  (ext.* mirrors Sentry + logs + PagerDuty)
                              │  (Halo envelopes at the tool-result boundary)
                              ▼
                     write gate ─▶ agent.approvals ─▶ human confirm ─▶ commit (ext.incidents)
```

One Postgres, two schemas: `ext.*` stands in for the external systems and is
shaped like them; `agent.*` is the agent's own state (sessions, the Halo store,
approvals, triage) and never changes when you integrate.

## Halo (the part that matters)

Heavy tool results are not returned raw. The heavy parts are written to
`agent.halo_nodes` keyed by a content handle (`h:sha256:...`), and the tool
returns a compact **envelope** — `{ summary, refs: { name: handle } }`. The model
reasons on the summary and fetches only the handles a step needs
(`halo_fetch` / `halo_fetch_many`). The store is persistent, so a handle seen
early in a long run is fetchable late, and `get_issue_detail` folds repeated
lookups of one issue into a growing map (argument-join). See
`src/mcp/halo.ts` and the **halo-navigation** skill.

## Tools (`src/mcp/server.ts`)

| Tool | Kind | Notes |
|------|------|-------|
| `list_open_issues` | read | ranked, Halo envelope (`full_list` handle) |
| `get_issue_detail` | read | Halo refs: stacktrace / breadcrumbs / tags / events; keyed into the issue map |
| `search_logs` | read | windowed; Halo refs: `lines` / `errors` |
| `list_incidents` | read | lightweight |
| `halo_fetch` / `halo_fetch_many` | read | drill into handles |
| `triage_note` | write | direct, low risk |
| `declare_incident` | write | **human-gated**; dedup_key = issue_id |
| `resolve_incident` | write | **human-gated** |
| `acknowledge_incident` / `assign_incident` | write | direct, low risk |

Every call is recorded in `agent.tool_calls` (tool, args, result root handle,
latency, outcome) — the eval/observability trail.

## Quick start

```bash
# 1. Postgres (reuses the shared local instance on :5433)
#    From elsewhere in this repo: docker compose -f ../creditline-decision-agent/docker-compose.yml up -d
#    …or any Postgres reachable via MONITORING_DB_DSN.

# 2. Install + configure
npm install
cp .env.example .env

# 3. Create the `monitoring` database, apply schema, seed ext.*
npm run initdb

# 4a. Deterministic demo (no model key) — drives the real MCP server over stdio
npm run demo

# 4b. Live agent (Claude Agent SDK; uses your `claude` login or ANTHROPIC_API_KEY)
npm run agent
#     …in a second terminal, act as on-call to resolve the gated writes:
npm run oncall            # interactive
#   (or, unattended for a hands-off run:)  npm run oncall -- auto
```

The seeded **hero issue** `4502913` (BACKEND-12A) — a `TypeError` in
`checkout.completeOrder` affecting 412 users, with a matching error spike on
`checkout-api` — is the case the agent triages, diagnoses, and declares.

## The swap to real systems, later

You touch only the tool bodies in `src/mcp/server.ts`:
`list_open_issues` / `get_issue_detail` → Sentry issues/events endpoints;
`search_logs` → the logs backend query API; `declare_incident` /
`resolve_incident` → PagerDuty Events API v2 passing `dedup_key = issue_id`.
Nothing else moves — `ext.*` was built to the external systems' shape.
