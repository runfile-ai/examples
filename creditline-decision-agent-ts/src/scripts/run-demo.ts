// ============================================================================
// Deterministic end-to-end demo: one escalated, human-overridden credit-line
// decision (no LLM / API key). Drives the REAL `mimic-creditline` MCP server
// over stdio through the full flow for the seeded escalation case, then
// simulates the credit officer modifying the agent's recommendation — the
// Art. 14 / SR 11-7 effective-challenge event. A deterministic smoke test of the
// environment + MCP server.
//
//   npm run demo
//
// The live, model-driven version is `npm run agent`.
// ============================================================================
import { fileURLToPath } from "node:url";
import { dirname, join, resolve as pathResolve } from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import pg from "pg";
import { evaluate } from "../agent/decision.js";
import { MODEL_VERSION, PROMPT_VERSION_HASH } from "../agent/prompts.js";
import { adminDsn, resolve } from "./officer-console.js";

try { process.loadEnvFile(); } catch { /* env may come from the shell */ }

const __dirname = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = pathResolve(__dirname, "..", "..");
const SERVER_PATH = join(PROJECT_ROOT, "src", "mcp", "server.ts");
const TSX_BIN = join(PROJECT_ROOT, "node_modules", ".bin", "tsx");

const DEMO_REQUEST_ID = "11111111-1111-1111-1111-111111111111";

const AGENT_DSN =
  process.env.MIMIC_DB_DSN || "postgresql://creditline_agent:agent_demo_pw@localhost:5433/mimic_creditline";

async function call(client: Client, name: string, args: Record<string, unknown>): Promise<any> {
  const res: any = await client.callTool({ name, arguments: args });
  return JSON.parse(res.content?.[0]?.text ?? "{}");
}

// Wait until the approval row for a decision appears, then return its id.
async function approvalIdFor(decisionId: string): Promise<string> {
  const c = new pg.Client({ connectionString: adminDsn() });
  await c.connect();
  try {
    for (let i = 0; i < 30; i++) {
      const r = await c.query(
        `SELECT approval_id FROM approvals WHERE decision_id = $1 ORDER BY requested_at DESC LIMIT 1`,
        [decisionId],
      );
      if (r.rowCount) return r.rows[0].approval_id;
      await new Promise((res) => setTimeout(res, 500));
    }
  } finally {
    await c.end();
  }
  throw new Error("approval row never appeared");
}

async function main() {
  console.log("=== Credit-Line Decision Agent — deterministic demo (real MCP server over stdio) ===\n");

  const transport = new StdioClientTransport({
    command: TSX_BIN,
    args: [SERVER_PATH],
    env: {
      ...process.env,
      MIMIC_DB_DSN: AGENT_DSN,
      APPROVAL_POLL_SECONDS: "1",
      APPROVAL_TIMEOUT_SECONDS: "60",
    } as Record<string, string>,
  });
  const client = new Client({ name: "demo", version: "0.1.0" });
  await client.connect(transport);

  // 1. Intake
  const req = await call(client, "creditline_get_request", { request_id: DEMO_REQUEST_ID });
  const cust = await call(client, "creditline_get_customer", { customer_id: req.customer_id });
  const bureau = await call(client, "creditline_pull_bureau", { customer_id: req.customer_id });
  const policy = await call(client, "creditline_get_active_policy", {});
  const customer = cust.customer;

  console.log(`Applicant        : ${customer.full_name}`);
  console.log(`Request          : ${req.request_type} → limit ${req.requested_limit}`);
  console.log(`Bureau score     : ${bureau.credit_score}  debt ${bureau.total_outstanding_debt}  delinq ${bureau.delinquencies_24m}`);
  console.log(`Active policy    : ${policy.version}  thresholds ${JSON.stringify(policy.thresholds)}\n`);

  // 2. Score + decide
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

  // 3. Record decision (with full provenance)
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
  console.log(`Recorded decision: ${decision.decision_id}  requires_human_approval=${decision.requires_human_approval}\n`);

  if (!decision.requires_human_approval) {
    console.log("Auto-approved — no human gate. Done.");
    await client.close();
    return;
  }

  // 4. Human-in-the-loop: the gate blocks; the officer resolves out of band.
  console.log("Opening human-in-the-loop approval gate (agent now blocks)…");
  const summary =
    `${customer.full_name} requests ${req.requested_limit}. Above 15k ceiling and ` +
    `DTI ${rec.dti.toFixed(3)} > 0.45. Recommend escalate; suggest approving a reduced limit.`;

  const gate = call(client, "creditline_request_approval", {
    decision_id: decision.decision_id,
    summary,
  });

  const officer = (async () => {
    const approvalId = await approvalIdFor(decision.decision_id);
    await new Promise((res) => setTimeout(res, 1000));
    console.log("  [officer] reviewing… modifying to an approved limit of 12,000");
    await resolve({
      approvalId,
      action: "modify",
      approverId: "co-114-jmalik",
      justification:
        "Strong 7-yr relationship and clean delinquency record. Approve a reduced 12,000 limit " +
        "to keep DTI within appetite; full 25,000 declined.",
      modifiedLimit: 12000,
    });
  })();

  const [gateResult] = await Promise.all([gate, officer]);

  console.log("\nApproval resolved:");
  console.log(`   status        : ${gateResult.status}`);
  console.log(`   is_override   : ${gateResult.is_override}  ← Art.14 / SR 11-7 effective challenge`);
  console.log(`   approved limit: ${gateResult.modified_limit}`);
  console.log(`   approver      : ${gateResult.approver_id} (${gateResult.approver_role})`);
  console.log(`   justification : ${gateResult.justification}`);

  // 5. Side effect
  const note = await call(client, "creditline_notify_customer", {
    request_id: DEMO_REQUEST_ID,
    outcome: "approved",
    approved_limit: gateResult.modified_limit,
    idempotency_key: decision.decision_id,
  });
  console.log(`\nCustomer notified: ${note.delivered} via ${note.channel}`);

  await client.close();
  console.log("\n=== demo complete: escalated → human override → approved at 12,000 ===");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
