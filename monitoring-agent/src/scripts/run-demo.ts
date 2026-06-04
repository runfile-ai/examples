// ============================================================================
// Deterministic end-to-end demo (no LLM / API key). Drives the REAL monitoring
// MCP server over stdio: triage → Halo drill-down → diagnose → human-gated
// declare_incident (auto-approved here) → resolve. Proves the tool contract,
// the Halo envelope/fetch flow, and the approval gate.
//
//   npm run demo
// ============================================================================
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";
import { randomUUID } from "node:crypto";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import pg from "pg";

try { process.loadEnvFile(); } catch { /* env may come from the shell */ }

const __dirname = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = resolve(__dirname, "..", "..");
const SERVER_PATH = join(PROJECT_ROOT, "src", "mcp", "server.ts");
const TSX_BIN = join(PROJECT_ROOT, "node_modules", ".bin", "tsx");
const SESSION_ID = randomUUID();

const dsn =
  process.env.MONITORING_DB_DSN ||
  `postgresql://${process.env.ADMIN_DB_USER || "postgres"}:${process.env.ADMIN_DB_PASSWORD || "postgres"}@${process.env.ADMIN_DB_HOST || "localhost"}:${process.env.ADMIN_DB_PORT || "5433"}/${process.env.MONITORING_DB_NAME || "monitoring"}`;

async function call(client: Client, name: string, args: Record<string, unknown>): Promise<any> {
  const res: any = await client.callTool({ name, arguments: args });
  const text = res.content?.[0]?.text ?? "{}";
  return JSON.parse(text);
}

// Stand in for the on-call engineer: approve the next pending gated write.
async function autoApprove(action: string) {
  const c = new pg.Client({ connectionString: dsn });
  await c.connect();
  try {
    for (let i = 0; i < 60; i++) {
      const r = await c.query(
        `SELECT id, payload FROM agent.approvals WHERE action = $1 AND status = 'pending' ORDER BY created_at LIMIT 1`,
        [action],
      );
      if (r.rowCount) {
        await c.query(`UPDATE agent.approvals SET status='approved', decided_by='oncall-auto@demo', decided_at=now() WHERE id=$1`, [r.rows[0].id]);
        console.log(`  [on-call] APPROVED ${action} ${JSON.stringify(r.rows[0].payload)}`);
        return;
      }
      await new Promise((res) => setTimeout(res, 500));
    }
  } finally {
    await c.end();
  }
}

async function main() {
  console.log("=== Monitoring agent — deterministic demo (real MCP server over stdio) ===\n");

  const transport = new StdioClientTransport({
    command: TSX_BIN,
    args: [SERVER_PATH],
    env: { ...process.env, MONITORING_DB_DSN: dsn, AGENT_SESSION_ID: SESSION_ID, MONITORING_CHANNEL: "demo", APPROVAL_POLL_SECONDS: "1", APPROVAL_TIMEOUT_SECONDS: "60" } as Record<string, string>,
  });
  const client = new Client({ name: "demo", version: "0.1.0" });
  await client.connect(transport);

  const tools = await client.listTools();
  console.log(`MCP server exposes ${tools.tools.length} tools.\n`);

  // 1. TRIAGE — heavy list comes back as a Halo envelope (summary + handle).
  const issues = await call(client, "list_open_issues", { severity: ["error", "fatal"] });
  console.log("list_open_issues → envelope summary:");
  console.log(`  total=${issues.summary.total}  by_level=${JSON.stringify(issues.summary.by_level)}`);
  const top = issues.summary.top[0];
  console.log(`  top issue: ${top.short_id} "${top.title}" — ${top.user_count} users, ${top.times_seen} events`);
  console.log(`  full_list handle (not fetched): ${issues.refs.full_list}\n`);

  // 2. DIAGNOSE — detail envelope; fetch ONLY the stacktrace handle (Halo).
  const detail = await call(client, "get_issue_detail", { issue_id: top.issue_id });
  console.log(`get_issue_detail(${top.issue_id}) → envelope refs: ${Object.keys(detail.refs).join(", ")}`);
  console.log(`  latest exception: ${detail.summary.latest_event.exception_type}: ${detail.summary.latest_event.exception_value}`);
  const drill = await call(client, "halo_fetch_many", { handles: [detail.refs.stacktrace, detail.refs.breadcrumbs] });
  const stack = (drill[detail.refs.stacktrace] as any).frames as any[];
  const culpritFrame = stack.find((f) => f.in_app && f.context_line) || stack[1];
  console.log(`  halo_fetch_many → top in-app frame: ${culpritFrame.filename}:${culpritFrame.lineno} ${culpritFrame.function}()`);
  console.log(`     ${culpritFrame.context_line ?? ""}`);

  // 3. Correlate with logs — summary only, then fetch the error slice.
  const logs = await call(client, "search_logs", { service: "checkout-api", level: "error" });
  console.log(`\nsearch_logs(checkout-api, error) → ${logs.summary.error_count} errors in window ${logs.summary.window.from.slice(11, 19)}–${logs.summary.window.to.slice(11, 19)}`);
  const errs = (await call(client, "halo_fetch", { handle: logs.refs.errors })) as any[];
  console.log(`  halo_fetch errors → e.g. "${errs[0]?.message}"`);

  // 4. Record triage decision (low risk; direct write).
  await call(client, "triage_note", { issue_id: top.issue_id, decision: "declared", reason: "412 users; TypeError in checkout.completeOrder correlated with checkout-api error spike." });
  console.log(`\ntriage_note → ${top.short_id} marked 'declared'`);

  // 5. INCIDENT — human-gated declare; auto-approver stands in for on-call.
  console.log("\ndeclare_incident (HUMAN-GATED — agent blocks)…");
  const approver = autoApprove("declare_incident");
  const declared = await call(client, "declare_incident", {
    issue_id: top.issue_id,
    severity: "error",
    summary: `Checkout failures: ${top.title}`,
  });
  await approver;
  console.log(`  → committed=${declared.committed} incident=${declared.incident_id} (#${declared.incident_number}) by ${declared.decided_by}`);

  // 6. Resolve it (also gated).
  console.log("\nresolve_incident (HUMAN-GATED — agent blocks)…");
  const approver2 = autoApprove("resolve_incident");
  const resolved = await call(client, "resolve_incident", { incident_id: declared.incident_id, note: "Patched null session guard in completeOrder; deploy backend@2026.6.4." });
  await approver2;
  console.log(`  → committed=${resolved.committed} status=${resolved.status} by ${resolved.decided_by}`);

  await client.close();
  console.log("\n=== demo complete: triaged → diagnosed via Halo → declared → resolved (both human-approved) ===");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
