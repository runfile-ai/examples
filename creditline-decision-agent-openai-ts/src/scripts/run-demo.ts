// ============================================================================
// Deterministic end-to-end demo for the OpenAI Agents build: one escalated,
// human-overridden decision (no LLM / API key). Drives the SAME
// `mimic-creditline` MCP server (spawned from the sibling base project) over
// stdio through the full flow for the seeded escalation case, recording the
// OpenAI agent's provenance, then simulates the officer override.
//
//   npm run demo
//
// The live, model-driven version is `npm run agent`. The shared, interactive
// credit-officer console lives in the base project (`npm run officer` there);
// this demo inlines the same admin-side resolution for a hands-off run.
// ============================================================================
import { fileURLToPath } from "node:url";
import { dirname, join, resolve as pathResolve } from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import pg from "pg";
import { evaluate } from "../agent/decision.js";
import { MODEL_VERSION, PROMPT_VERSION_HASH } from "../agent/prompts.js";

try { process.loadEnvFile(); } catch { /* env may come from the shell */ }

pg.types.setTypeParser(1700, (v) => (v === null ? null : Number(v))); // numeric → number

const __dirname = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = pathResolve(__dirname, "..", "..");
const SIBLING = pathResolve(PROJECT_ROOT, "..", "creditline-decision-agent-ts");
const SERVER_PATH = join(SIBLING, "src", "mcp", "server.ts");
const TSX_BIN = join(PROJECT_ROOT, "node_modules", ".bin", "tsx");

const DEMO_REQUEST_ID = "11111111-1111-1111-1111-111111111111";
const AGENT_DSN =
  process.env.MIMIC_DB_DSN || "postgresql://creditline_agent:agent_demo_pw@localhost:5433/mimic_creditline";

function adminDsn(): string {
  const host = process.env.ADMIN_DB_HOST || "localhost";
  const port = process.env.ADMIN_DB_PORT || "5433";
  const user = process.env.ADMIN_DB_USER || "postgres";
  const pw = process.env.ADMIN_DB_PASSWORD || "postgres";
  return `postgresql://${user}:${pw}@${host}:${port}/mimic_creditline`;
}

async function call(client: Client, name: string, args: Record<string, unknown>): Promise<any> {
  const res: any = await client.callTool({ name, arguments: args });
  return JSON.parse(res.content?.[0]?.text ?? "{}");
}

// The officer's admin-side override (modify to a reduced approved limit).
async function officerModify(decisionId: string, modifiedLimit: number): Promise<void> {
  const c = new pg.Client({ connectionString: adminDsn() });
  await c.connect();
  try {
    let approvalId: string | undefined;
    for (let i = 0; i < 30 && !approvalId; i++) {
      const r = await c.query(
        `SELECT approval_id FROM approvals WHERE decision_id = $1 ORDER BY requested_at DESC LIMIT 1`,
        [decisionId],
      );
      if (r.rowCount) approvalId = r.rows[0].approval_id;
      else await new Promise((res) => setTimeout(res, 500));
    }
    if (!approvalId) throw new Error("approval row never appeared");
    await new Promise((res) => setTimeout(res, 1000));
    console.log("  [officer] reviewing… modifying to an approved limit of 12,000");
    const appr = (
      await c.query(`SELECT d.decision_id, d.request_id FROM approvals a
                       JOIN decisions d ON d.decision_id = a.decision_id
                      WHERE a.approval_id = $1`, [approvalId])
    ).rows[0];
    await c.query("BEGIN");
    await c.query(
      `UPDATE approvals SET status='modified', is_override=true, modified_limit=$2,
              approver_id='co-114-jmalik', justification=$3, resolved_at=now()
        WHERE approval_id=$1`,
      [approvalId, modifiedLimit, "Strong relationship, clean record; approve reduced 12,000 limit."],
    );
    await c.query(`UPDATE decisions SET outcome='approved', approved_limit=$2 WHERE decision_id=$1`, [
      appr.decision_id,
      modifiedLimit,
    ]);
    await c.query(`UPDATE credit_line_requests SET status='approved' WHERE request_id=$1`, [appr.request_id]);
    await c.query("COMMIT");
  } catch (err) {
    await c.query("ROLLBACK").catch(() => {});
    throw err;
  } finally {
    await c.end();
  }
}

async function main() {
  console.log("=== Credit-Line Decision Agent (OpenAI Agents SDK) — deterministic demo ===\n");

  const transport = new StdioClientTransport({
    command: TSX_BIN,
    args: [SERVER_PATH],
    cwd: SIBLING,
    env: {
      ...process.env,
      MIMIC_DB_DSN: AGENT_DSN,
      APPROVAL_POLL_SECONDS: "1",
      APPROVAL_TIMEOUT_SECONDS: "60",
    } as Record<string, string>,
  });
  const client = new Client({ name: "demo", version: "0.1.0" });
  await client.connect(transport);

  const req = await call(client, "creditline_get_request", { request_id: DEMO_REQUEST_ID });
  const cust = await call(client, "creditline_get_customer", { customer_id: req.customer_id });
  const bureau = await call(client, "creditline_pull_bureau", { customer_id: req.customer_id });
  const policy = await call(client, "creditline_get_active_policy", {});
  const customer = cust.customer;

  console.log(`Applicant        : ${customer.full_name}`);
  console.log(`Request          : ${req.request_type} → limit ${req.requested_limit}`);
  console.log(`Active policy    : ${policy.version}  thresholds ${JSON.stringify(policy.thresholds)}\n`);

  const rec = evaluate({
    requested_limit: Number(req.requested_limit),
    annual_income: Number(customer.annual_income),
    bureau,
    policy_thresholds: policy.thresholds,
  });
  console.log(`Computed DTI     : ${rec.dti.toFixed(3)}`);
  console.log(`Recommendation   : ${rec.outcome.toUpperCase()}`);
  for (const r of rec.reasons) console.log(`   • ${r}`);
  console.log();

  const decision = await call(client, "creditline_record_decision", {
    request_id: DEMO_REQUEST_ID,
    outcome: rec.outcome,
    rationale: rec.reasons.join("; "),
    model_version: MODEL_VERSION,
    prompt_version_hash: PROMPT_VERSION_HASH,
    policy_version: policy.version,
    bureau_report_id: bureau.bureau_report_id,
    approved_limit: rec.approved_limit,
  });
  console.log(`Recorded decision: ${decision.decision_id}  requires_human_approval=${decision.requires_human_approval}`);
  console.log(`   model_version       = ${MODEL_VERSION}`);
  console.log(`   prompt_version_hash = ${PROMPT_VERSION_HASH}\n`);

  if (!decision.requires_human_approval) {
    console.log("Auto-approved — no human gate. Done.");
    await client.close();
    return;
  }

  console.log("Opening human-in-the-loop approval gate (agent now blocks)…");
  const summary =
    `${customer.full_name} requests ${req.requested_limit}. Above ceiling and ` +
    `DTI ${rec.dti.toFixed(3)} over appetite. Recommend escalate.`;

  const gate = call(client, "creditline_request_approval", { decision_id: decision.decision_id, summary });
  const officer = officerModify(decision.decision_id, 12000);
  const [gateResult] = await Promise.all([gate, officer]);

  console.log("\nApproval resolved:");
  console.log(`   status        : ${gateResult.status}`);
  console.log(`   is_override   : ${gateResult.is_override}  ← Art.14 / SR 11-7 effective challenge`);
  console.log(`   approved limit: ${gateResult.modified_limit}`);
  console.log(`   approver      : ${gateResult.approver_id} (${gateResult.approver_role})`);

  await call(client, "creditline_notify_customer", {
    request_id: DEMO_REQUEST_ID,
    outcome: "approved",
    approved_limit: gateResult.modified_limit,
    idempotency_key: decision.decision_id,
  });

  await client.close();
  console.log("\n=== demo complete: escalated → human override → approved at 12,000 ===");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
