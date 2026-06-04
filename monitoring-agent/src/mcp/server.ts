// ============================================================================
// monitoring-mcp — stdio MCP server.
//
// Intent-shaped tools over the local Postgres. The tool CONTRACT is the swap
// point: today each body is SQL on ext.*; later it is an API call to Sentry /
// the logs backend / PagerDuty. Signatures and returned shapes do not change.
//
// Heavy reads return Halo envelopes (compact summary + handles); the agent
// drills in with halo_fetch / halo_fetch_many. Real-world writes
// (declare_incident, resolve_incident) route through agent.approvals and block
// until a human confirms. Every call is recorded in agent.tool_calls.
// ============================================================================
import { randomUUID } from "node:crypto";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  type Tool,
} from "@modelcontextprotocol/sdk/types.js";
import type { PoolClient } from "pg";
import { pool, withClient } from "./db.js";
import * as halo from "./halo.js";

// ── session (one agent run) ──────────────────────────────────────────────────
let SESSION_ID = process.env.AGENT_SESSION_ID || randomUUID();
const CHANNEL = process.env.MONITORING_CHANNEL || "cron";

async function ensureSession(): Promise<void> {
  await withClient((c) =>
    c.query(
      `INSERT INTO agent.sessions (id, channel, status) VALUES ($1, $2, 'active')
       ON CONFLICT (id) DO NOTHING`,
      [SESSION_ID, CHANNEL],
    ),
  );
}

// ── helpers ──────────────────────────────────────────────────────────────────
type Json = Record<string, unknown>;
const result = (obj: unknown) => ({ content: [{ type: "text" as const, text: JSON.stringify(obj) }] });
const urgencyFor = (sev: string) =>
  ["fatal", "error", "critical", "high"].includes((sev || "").toLowerCase()) ? "high" : "low";

async function recordToolCall(
  tool: string,
  args: unknown,
  envelopeRoot: string | null,
  latencyMs: number,
  ok: boolean,
  error: string | null,
): Promise<void> {
  try {
    await withClient((c) =>
      c.query(
        `INSERT INTO agent.tool_calls (id, session_id, tool, args, envelope_root, latency_ms, ok, error)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8)`,
        [randomUUID(), SESSION_ID, tool, args ?? {}, envelopeRoot, latencyMs, ok, error],
      ),
    );
  } catch {
    /* observability must never break a tool call */
  }
}

// Block until a pending approval is resolved (or times out). Polls with short
// connections so we never hold one open across the wait.
async function awaitApproval(
  action: string,
  payload: Json,
  idempotencyKey: string,
): Promise<{ status: string; decided_by: string | null; id: string }> {
  const timeoutS = Number(process.env.APPROVAL_TIMEOUT_SECONDS || "900");
  const pollS = Number(process.env.APPROVAL_POLL_SECONDS || "2");

  const existing = await withClient((c) =>
    c.query(`SELECT id, status, decided_by FROM agent.approvals WHERE idempotency_key = $1`, [
      idempotencyKey,
    ]),
  );
  let approvalId: string;
  if (existing.rowCount && existing.rows[0].status !== "pending") {
    return existing.rows[0]; // idempotent replay
  } else if (existing.rowCount) {
    approvalId = existing.rows[0].id;
  } else {
    approvalId = randomUUID();
    await withClient((c) =>
      c.query(
        `INSERT INTO agent.approvals (id, session_id, action, payload, idempotency_key, status)
         VALUES ($1,$2,$3,$4,$5,'pending') ON CONFLICT (idempotency_key) DO NOTHING`,
        [approvalId, SESSION_ID, action, payload, idempotencyKey],
      ),
    );
  }

  let waited = 0;
  while (waited < timeoutS) {
    const r = await withClient((c) =>
      c.query(`SELECT id, status, decided_by FROM agent.approvals WHERE id = $1`, [approvalId]),
    );
    if (r.rowCount && r.rows[0].status !== "pending") return r.rows[0];
    await new Promise((res) => setTimeout(res, pollS * 1000));
    waited += pollS;
  }
  return { id: approvalId, status: "timeout", decided_by: null };
}

// ── reads (Halo-encoded) ─────────────────────────────────────────────────────
async function listOpenIssues(args: Json) {
  const severity = args.severity as string[] | undefined;
  const since = args.since as string | undefined;
  const service = args.service as string | undefined;
  return withClient(async (c) => {
    const rows = (
      await c.query(
        `SELECT i.id, i.short_id, i.title, i.culprit, i.level, i.status, i.times_seen,
                i.user_count, i.last_seen, p.slug AS project
           FROM ext.issues i JOIN ext.projects p ON p.id = i.project_id
          WHERE i.status = 'unresolved'
            AND ($1::text[] IS NULL OR i.level = ANY($1))
            AND ($2::timestamptz IS NULL OR i.last_seen >= $2)
            AND ($3::text IS NULL OR p.slug = $3)
          ORDER BY i.user_count DESC, i.times_seen DESC
          LIMIT 100`,
        [severity ?? null, since ?? null, service ?? null],
      )
    ).rows;

    const byLevel: Record<string, number> = {};
    for (const r of rows) byLevel[r.level] = (byLevel[r.level] || 0) + 1;
    const top = rows.slice(0, 15).map((r) => ({
      issue_id: r.id,
      short_id: r.short_id,
      title: r.title,
      level: r.level,
      user_count: r.user_count,
      times_seen: r.times_seen,
      last_seen: r.last_seen,
      culprit: r.culprit,
    }));
    const env = await halo.encode(c, "open_issues", { total: rows.length, by_level: byLevel, top }, {
      full_list: rows,
    });
    return env;
  });
}

async function getIssueDetail(args: Json) {
  const issueId = String(args.issue_id);
  return withClient(async (c) => {
    const issue = (await c.query(`SELECT * FROM ext.issues WHERE id = $1`, [issueId])).rows[0];
    if (!issue) return { error: "issue_not_found", issue_id: issueId };
    const events = (
      await c.query(
        `SELECT id, timestamp, message, environment, release, server_name,
                exception, breadcrumbs, tags, contexts
           FROM ext.events WHERE issue_id = $1 ORDER BY timestamp DESC LIMIT 10`,
        [issueId],
      )
    ).rows;
    const latest = events[0];
    const exVal = latest?.exception?.values?.[0];
    const summary = {
      issue: {
        id: issue.id,
        short_id: issue.short_id,
        title: issue.title,
        culprit: issue.culprit,
        level: issue.level,
        status: issue.status,
        times_seen: issue.times_seen,
        user_count: issue.user_count,
        first_seen: issue.first_seen,
        last_seen: issue.last_seen,
      },
      latest_event: latest
        ? {
            id: latest.id,
            timestamp: latest.timestamp,
            environment: latest.environment,
            release: latest.release,
            exception_type: exVal?.type,
            exception_value: exVal?.value,
            frame_count: exVal?.stacktrace?.frames?.length ?? 0,
          }
        : null,
      n_events: events.length,
    };
    const env = await halo.encode(c, "issue_detail", summary, {
      stacktrace: exVal?.stacktrace ?? null,
      breadcrumbs: latest?.breadcrumbs ?? null,
      tags: latest?.tags ?? null,
      events,
    });
    // Argument-join: fold repeated lookups of this issue into one growing map.
    env.map_root = await halo.accumulate(c, SESSION_ID, issueId, env, { issue_id: issueId });
    return env;
  });
}

async function searchLogs(args: Json) {
  const query = (args.query as string | undefined) || null;
  const level = (args.level as string | undefined) || null;
  const service = (args.service as string | undefined) || null;
  return withClient(async (c) => {
    // Default window: the 6 hours ending at the latest log we have.
    const maxTs = (await c.query(`SELECT max(ts) AS m FROM ext.logs`)).rows[0].m;
    const to = (args.to as string | undefined) || maxTs;
    const from =
      (args.from as string | undefined) ||
      (to ? new Date(new Date(to).getTime() - 6 * 3600 * 1000).toISOString() : null);

    const rows = (
      await c.query(
        `SELECT ts, service, level, message, attributes, trace_id, span_id
           FROM ext.logs
          WHERE ts BETWEEN $1 AND $2
            AND ($3::text IS NULL OR level = $3)
            AND ($4::text IS NULL OR service = $4)
            AND ($5::text IS NULL OR message ILIKE '%' || $5 || '%')
          ORDER BY ts
          LIMIT 5000`,
        [from, to, level, service, query],
      )
    ).rows;

    const byService: Record<string, number> = {};
    let errorCount = 0;
    for (const r of rows) {
      byService[r.service] = (byService[r.service] || 0) + 1;
      if (r.level === "error") errorCount++;
    }
    const errors = rows.filter((r) => r.level === "error");
    const summary = {
      window: { from, to },
      total: rows.length,
      error_count: errorCount,
      by_service: byService,
      sample_errors: errors.slice(0, 5).map((r) => ({ ts: r.ts, service: r.service, message: r.message })),
    };
    return halo.encode(c, "logs", summary, { lines: rows, errors });
  });
}

async function listIncidents(args: Json) {
  const status = (args.status as string | undefined) || null;
  const service = (args.service as string | undefined) || null;
  return withClient(async (c) => {
    const rows = (
      await c.query(
        `SELECT id, incident_number, title, status, urgency, service_id, dedup_key,
                assigned_to, created_at, resolved_at
           FROM ext.incidents
          WHERE ($1::text IS NULL OR status = $1)
            AND ($2::text IS NULL OR service_id = $2)
          ORDER BY created_at DESC`,
        [status, service],
      )
    ).rows;
    return { incidents: rows, count: rows.length };
  });
}

// ── halo fetch ───────────────────────────────────────────────────────────────
const haloFetch = (args: Json) => withClient((c) => halo.getJson(c, String(args.handle)));
const haloFetchMany = (args: Json) =>
  withClient((c) => halo.getMany(c, (args.handles as string[]) || []));

// ── writes ───────────────────────────────────────────────────────────────────
async function triageNote(args: Json) {
  const issueId = String(args.issue_id);
  const decision = String(args.decision);
  const reason = String(args.reason ?? "");
  return withClient(async (c) => {
    await c.query(
      `INSERT INTO agent.triage_state (issue_id, last_seen_event, decision, reason, updated_at)
       VALUES ($1, now(), $2, $3, now())
       ON CONFLICT (issue_id) DO UPDATE SET decision = EXCLUDED.decision,
         reason = EXCLUDED.reason, updated_at = now()`,
      [issueId, decision, reason],
    );
    return { issue_id: issueId, decision, recorded: true };
  });
}

async function declareIncident(args: Json) {
  const issueId = String(args.issue_id);
  const severity = String(args.severity ?? "error");
  const summary = String(args.summary ?? "");
  const idempotencyKey = `declare:${issueId}`;
  const decision = await awaitApproval("declare_incident", { issue_id: issueId, severity, summary }, idempotencyKey);
  if (decision.status !== "approved") {
    return { committed: false, approval_status: decision.status, issue_id: issueId };
  }
  return withClient(async (c) => {
    const incidentId = "INC-" + randomUUID().slice(0, 8);
    const inc = (
      await c.query(
        `INSERT INTO ext.incidents (id, title, urgency, dedup_key)
         VALUES ($1, $2, $3, $4)
         ON CONFLICT (dedup_key) DO UPDATE SET updated_at = now()
         RETURNING id, incident_number, status, dedup_key`,
        [incidentId, summary || `Incident for ${issueId}`, urgencyFor(severity), issueId],
      )
    ).rows[0];
    await c.query(
      `INSERT INTO agent.incident_links (issue_id, dedup_key, incident_id, status)
       VALUES ($1, $2, $3, 'declared')
       ON CONFLICT (issue_id) DO UPDATE SET incident_id = EXCLUDED.incident_id, status = 'declared'`,
      [issueId, issueId, inc.id],
    );
    await c.query(
      `INSERT INTO agent.triage_state (issue_id, decision, reason, updated_at)
       VALUES ($1, 'declared', $2, now())
       ON CONFLICT (issue_id) DO UPDATE SET decision = 'declared', reason = EXCLUDED.reason, updated_at = now()`,
      [issueId, summary],
    );
    return {
      committed: true,
      approval_status: "approved",
      decided_by: decision.decided_by,
      incident_id: inc.id,
      incident_number: inc.incident_number,
      dedup_key: inc.dedup_key,
    };
  });
}

async function acknowledgeIncident(args: Json) {
  const incidentId = String(args.incident_id);
  return withClient(async (c) => {
    const r = await c.query(
      `UPDATE ext.incidents SET status = 'acknowledged', updated_at = now()
       WHERE id = $1 RETURNING id, status`,
      [incidentId],
    );
    return r.rowCount ? { ...r.rows[0], updated: true } : { error: "incident_not_found", incident_id: incidentId };
  });
}

async function resolveIncident(args: Json) {
  const incidentId = String(args.incident_id);
  const note = String(args.note ?? "");
  const idempotencyKey = `resolve:${incidentId}`;
  const decision = await awaitApproval("resolve_incident", { incident_id: incidentId, note }, idempotencyKey);
  if (decision.status !== "approved") {
    return { committed: false, approval_status: decision.status, incident_id: incidentId };
  }
  return withClient(async (c) => {
    const r = await c.query(
      `UPDATE ext.incidents SET status = 'resolved', resolved_at = now(), updated_at = now()
       WHERE id = $1 RETURNING id, status, resolved_at`,
      [incidentId],
    );
    if (!r.rowCount) return { error: "incident_not_found", incident_id: incidentId };
    await c.query(
      `INSERT INTO ext.incident_notes (id, incident_id, content, author)
       VALUES ($1, $2, $3, 'monitoring-agent')`,
      ["note-" + randomUUID().slice(0, 8), incidentId, note],
    );
    await c.query(`UPDATE agent.incident_links SET status = 'resolved' WHERE incident_id = $1`, [incidentId]);
    return { committed: true, approval_status: "approved", decided_by: decision.decided_by, ...r.rows[0] };
  });
}

async function assignIncident(args: Json) {
  const incidentId = String(args.incident_id);
  const user = String(args.user);
  return withClient(async (c) => {
    const r = await c.query(
      `UPDATE ext.incidents SET assigned_to = $2, updated_at = now() WHERE id = $1 RETURNING id, assigned_to`,
      [incidentId, user],
    );
    return r.rowCount ? { ...r.rows[0], updated: true } : { error: "incident_not_found", incident_id: incidentId };
  });
}

// ── tool catalogue (metadata + dispatch) ─────────────────────────────────────
type Handler = (args: Json) => Promise<unknown>;
const HANDLERS: Record<string, Handler> = {
  list_open_issues: listOpenIssues,
  get_issue_detail: getIssueDetail,
  search_logs: searchLogs,
  list_incidents: listIncidents,
  halo_fetch: haloFetch,
  halo_fetch_many: haloFetchMany,
  triage_note: triageNote,
  declare_incident: declareIncident,
  acknowledge_incident: acknowledgeIncident,
  resolve_incident: resolveIncident,
  assign_incident: assignIncident,
};

const TOOLS: Tool[] = [
  {
    name: "list_open_issues",
    description:
      "List unresolved issues ranked by users affected then event count (Sentry-shaped). Returns a Halo envelope: a compact summary + a `full_list` handle. Drill in with halo_fetch.",
    inputSchema: {
      type: "object",
      properties: {
        service: { type: "string", description: "project slug filter" },
        since: { type: "string", description: "ISO timestamp; only issues seen since" },
        severity: { type: "array", items: { type: "string" }, description: "levels, e.g. ['error','fatal']" },
      },
    },
    annotations: { readOnlyHint: true, openWorldHint: true },
  },
  {
    name: "get_issue_detail",
    description:
      "Fetch an issue plus its latest events (Sentry-shaped). Returns a Halo envelope whose `refs` carve out stacktrace / breadcrumbs / tags / events; keyed into the issue map for argument-join.",
    inputSchema: {
      type: "object",
      properties: { issue_id: { type: "string" } },
      required: ["issue_id"],
    },
    annotations: { readOnlyHint: true, openWorldHint: true },
  },
  {
    name: "search_logs",
    description:
      "Search logs in a time window (Datadog/Loki-shaped). Returns a Halo envelope: summary + `lines`/`errors` handles. The biggest payload — slice rather than pull the whole window.",
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string", description: "substring match on message" },
        service: { type: "string" },
        level: { type: "string", enum: ["error", "warn", "info", "debug"] },
        from: { type: "string", description: "ISO window start" },
        to: { type: "string", description: "ISO window end" },
      },
    },
    annotations: { readOnlyHint: true, openWorldHint: true },
  },
  {
    name: "list_incidents",
    description: "List incidents (PagerDuty-shaped), optionally filtered by status/service.",
    inputSchema: {
      type: "object",
      properties: {
        status: { type: "string", enum: ["triggered", "acknowledged", "resolved"] },
        service: { type: "string" },
      },
    },
    annotations: { readOnlyHint: true, openWorldHint: true },
  },
  {
    name: "halo_fetch",
    description: "Fetch the decoded content behind one Halo handle (h:sha256:...).",
    inputSchema: { type: "object", properties: { handle: { type: "string" } }, required: ["handle"] },
    annotations: { readOnlyHint: true },
  },
  {
    name: "halo_fetch_many",
    description: "Fetch many Halo handles in one round trip (batched drill-down).",
    inputSchema: {
      type: "object",
      properties: { handles: { type: "array", items: { type: "string" } } },
      required: ["handles"],
    },
    annotations: { readOnlyHint: true },
  },
  {
    name: "triage_note",
    description:
      "Record a triage decision for an issue (low risk; writes agent.triage_state directly). decision ∈ watch|declared|ignored|resolved.",
    inputSchema: {
      type: "object",
      properties: {
        issue_id: { type: "string" },
        decision: { type: "string", enum: ["watch", "declared", "ignored", "resolved"] },
        reason: { type: "string" },
      },
      required: ["issue_id", "decision", "reason"],
    },
    annotations: { readOnlyHint: false, idempotentHint: true },
  },
  {
    name: "declare_incident",
    description:
      "Declare a PagerDuty-shaped incident for an issue. HUMAN-GATED: proposes to agent.approvals and BLOCKS until a human confirms. Dedup_key = issue_id (one incident per issue).",
    inputSchema: {
      type: "object",
      properties: {
        issue_id: { type: "string" },
        severity: { type: "string" },
        summary: { type: "string" },
      },
      required: ["issue_id", "severity", "summary"],
    },
    annotations: { readOnlyHint: false, idempotentHint: true, openWorldHint: true },
  },
  {
    name: "acknowledge_incident",
    description: "Acknowledge an incident (low risk; direct write).",
    inputSchema: { type: "object", properties: { incident_id: { type: "string" } }, required: ["incident_id"] },
    annotations: { readOnlyHint: false, idempotentHint: true },
  },
  {
    name: "resolve_incident",
    description:
      "Resolve an incident and attach a note. HUMAN-GATED: proposes to agent.approvals and BLOCKS until a human confirms.",
    inputSchema: {
      type: "object",
      properties: { incident_id: { type: "string" }, note: { type: "string" } },
      required: ["incident_id", "note"],
    },
    annotations: { readOnlyHint: false, idempotentHint: true, openWorldHint: true },
  },
  {
    name: "assign_incident",
    description: "Assign an incident to a user (low risk; direct write).",
    inputSchema: {
      type: "object",
      properties: { incident_id: { type: "string" }, user: { type: "string" } },
      required: ["incident_id", "user"],
    },
    annotations: { readOnlyHint: false, idempotentHint: true },
  },
];

// ── server wiring ────────────────────────────────────────────────────────────
const server = new Server({ name: "monitoring-mcp", version: "0.1.0" }, { capabilities: { tools: {} } });

server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));

server.setRequestHandler(CallToolRequestSchema, async (req) => {
  const name = req.params.name;
  const args = (req.params.arguments ?? {}) as Json;
  const handler = HANDLERS[name];
  if (!handler) return { isError: true, content: [{ type: "text", text: `unknown tool: ${name}` }] };

  const started = Date.now();
  try {
    const out = await handler(args);
    const envelopeRoot =
      out && typeof out === "object" && "map_root" in (out as any)
        ? ((out as any).map_root as string)
        : out && typeof out === "object" && "refs" in (out as any)
          ? Object.values((out as any).refs)[0] ?? null
          : null;
    await recordToolCall(name, args, envelopeRoot as string | null, Date.now() - started, true, null);
    return result(out);
  } catch (err: any) {
    await recordToolCall(name, args, null, Date.now() - started, false, String(err?.message ?? err));
    return { isError: true, content: [{ type: "text", text: `error in ${name}: ${err?.message ?? err}` }] };
  }
});

async function main() {
  await ensureSession();
  await server.connect(new StdioServerTransport());
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
