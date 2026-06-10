// ============================================================================
// mimic-creditline — stdio MCP server (TypeScript).
//
// Eight domain-shaped tools over the simulated `mimic_creditline` Postgres.
// Transport: stdio (local demo) — run with `tsx src/mcp/server.ts`. Inputs are
// validated with Zod; tools return JSON-safe objects so downstream records are
// semantic, not opaque SQL.
//
// The agent is given ONLY these tools. Nothing here can rewrite history; the
// HITL gate (creditline_request_approval) blocks the run until a human resolves
// the pending approval row out of band (see src/scripts/officer-console.ts).
// ============================================================================
import { randomUUID, createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  type Tool,
} from "@modelcontextprotocol/sdk/types.js";
import type { PoolClient } from "pg";
import { withClient } from "./db.js";
import {
  GetActivePolicyIn,
  GetCustomerIn,
  GetRequestIn,
  NotifyCustomerIn,
  PullBureauIn,
  RecordDecisionIn,
  RequestApprovalIn,
} from "./models.js";

type Json = Record<string, unknown>;
const result = (obj: unknown) => ({ content: [{ type: "text" as const, text: JSON.stringify(obj) }] });

// ── canonical prompt provenance ──────────────────────────────────────────────
// CLAUDE.md governs the agent in every runtime, so the canonical
// prompt_version_hash is a sha256 over its raw bytes.
const __dirname = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = resolve(__dirname, "..", "..");
const CLAUDE_MD = join(PROJECT_ROOT, "CLAUDE.md");
const FALLBACK_PROMPT =
  "You are the Credit-Line Decision Agent. Act only through the mimic-creditline " +
  "MCP tools; intake -> bureau -> policy -> score -> decide -> record -> human " +
  "approval. Never auto-deny; every adverse outcome escalates to a human.";

function promptVersionHash(): string {
  let data: Buffer | string;
  try {
    data = readFileSync(CLAUDE_MD);
  } catch {
    data = FALLBACK_PROMPT;
  }
  return "sha256:" + createHash("sha256").update(data).digest("hex");
}

// ── 0. get_agent_provenance ──────────────────────────────────────────────────
async function getAgentProvenance(_args: Json) {
  return {
    prompt_version_hash: promptVersionHash(),
    prompt_source: "CLAUDE.md",
    agent_id: "creditline-decision-agent",
  };
}

// ── 1. get_request ───────────────────────────────────────────────────────────
async function getRequest(args: Json) {
  const { request_id } = GetRequestIn.parse(args);
  return withClient(async (c) => {
    const rec = (await c.query(`SELECT * FROM credit_line_requests WHERE request_id = $1`, [request_id])).rows[0];
    return rec ?? { error: "request_not_found", request_id };
  });
}

// ── 2. get_customer ──────────────────────────────────────────────────────────
async function getCustomer(args: Json) {
  const { customer_id } = GetCustomerIn.parse(args);
  return withClient(async (c) => {
    const cust = (await c.query(`SELECT * FROM customers WHERE customer_id = $1`, [customer_id])).rows[0];
    if (!cust) return { error: "customer_not_found", customer_id };
    const lines = (
      await c.query(`SELECT * FROM credit_lines WHERE customer_id = $1 ORDER BY opened_at`, [customer_id])
    ).rows;
    return { customer: cust, credit_lines: lines };
  });
}

// ── 3. pull_bureau ───────────────────────────────────────────────────────────
async function pullBureau(args: Json) {
  const { customer_id, bureau_name } = PullBureauIn.parse(args);
  return withClient(async (c) => {
    const rec = (
      await c.query(
        `SELECT * FROM bureau_reports
          WHERE customer_id = $1 AND bureau_name = $2
          ORDER BY pulled_at DESC LIMIT 1`,
        [customer_id, bureau_name],
      )
    ).rows[0];
    return rec ?? { error: "no_bureau_report", customer_id, bureau: bureau_name };
  });
}

// ── 4. get_active_policy ─────────────────────────────────────────────────────
async function getActivePolicy(args: Json) {
  GetActivePolicyIn.parse(args);
  return withClient(async (c) => {
    const rec = (
      await c.query(
        `SELECT * FROM decision_policies WHERE effective_to IS NULL
          ORDER BY effective_from DESC LIMIT 1`,
      )
    ).rows[0];
    return rec ?? { error: "no_active_policy" };
  });
}

// ── 5. record_decision ───────────────────────────────────────────────────────
async function recordDecision(args: Json) {
  const a = RecordDecisionIn.parse(args);
  return withClient(async (c) => {
    try {
      await c.query("BEGIN");
      const req = (
        await c.query(`SELECT customer_id, requested_limit FROM credit_line_requests WHERE request_id = $1`, [
          a.request_id,
        ])
      ).rows[0];
      if (!req) {
        await c.query("ROLLBACK");
        return { error: "request_not_found", request_id: a.request_id };
      }

      // Idempotency: return the existing decision if one already exists.
      const existing = (
        await c.query(`SELECT decision_id, requires_human_approval FROM decisions WHERE request_id = $1`, [
          a.request_id,
        ])
      ).rows[0];
      if (existing) {
        await c.query("ROLLBACK");
        return {
          decision_id: existing.decision_id,
          requires_human_approval: existing.requires_human_approval,
          idempotent_replay: true,
        };
      }

      const ceiling = (
        await c.query(
          `SELECT (thresholds->>'auto_approve_ceiling')::numeric AS ceiling
             FROM decision_policies WHERE version = $1`,
          [a.policy_version],
        )
      ).rows[0]?.ceiling as number | null;
      const aboveCeiling =
        a.outcome === "approved" && a.approved_limit != null && ceiling != null && a.approved_limit > ceiling;
      const requiresHuman = a.outcome !== "approved" || !!aboveCeiling;

      const decisionId = randomUUID();
      await c.query(
        `INSERT INTO decisions
           (decision_id, request_id, customer_id, outcome, approved_limit,
            rationale, model_version, prompt_version_hash, policy_version,
            bureau_report_id, requires_human_approval)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)`,
        [
          decisionId,
          a.request_id,
          req.customer_id,
          a.outcome,
          a.approved_limit ?? null,
          a.rationale,
          a.model_version,
          a.prompt_version_hash,
          a.policy_version,
          a.bureau_report_id,
          requiresHuman,
        ],
      );
      // Reflect the outcome onto the request row.
      await c.query(`UPDATE credit_line_requests SET status = $1 WHERE request_id = $2`, [a.outcome, a.request_id]);
      await c.query("COMMIT");

      return { decision_id: decisionId, requires_human_approval: requiresHuman, idempotent_replay: false };
    } catch (err) {
      await c.query("ROLLBACK").catch(() => {});
      throw err;
    }
  });
}

// ── 6. request_approval (HITL interrupt) ─────────────────────────────────────
async function approvalResult(c: PoolClient, approvalId: string) {
  const r = (await c.query(`SELECT * FROM approvals WHERE approval_id = $1`, [approvalId])).rows[0];
  return {
    approval_id: r.approval_id,
    decision_id: r.decision_id,
    status: r.status,
    is_override: r.is_override,
    modified_limit: r.modified_limit ?? null,
    approver_id: r.approver_id,
    approver_role: r.approver_role,
    justification: r.justification,
    resolved_at: r.resolved_at ?? null,
  };
}

async function requestApproval(args: Json) {
  const a = RequestApprovalIn.parse(args);
  const timeoutS = Number(process.env.APPROVAL_TIMEOUT_SECONDS || "900");
  const pollS = Number(process.env.APPROVAL_POLL_SECONDS || "2");

  const approvalId = await withClient(async (c) => {
    const existing = (
      await c.query(
        `SELECT approval_id, status FROM approvals WHERE decision_id = $1 ORDER BY requested_at DESC LIMIT 1`,
        [a.decision_id],
      )
    ).rows[0];
    if (existing && existing.status === "pending") return existing.approval_id as string;
    if (existing && existing.status !== "pending") return existing.approval_id as string; // resolved → replay below
    const id = randomUUID();
    await c.query(
      `INSERT INTO approvals (approval_id, decision_id, approver_role, status, justification)
       VALUES ($1, $2, $3, 'pending', $4)`,
      [id, a.decision_id, a.approver_role, a.summary],
    );
    return id;
  });

  // If the approval already existed and was resolved, return it immediately.
  const initial = await withClient((c) =>
    c.query(`SELECT status FROM approvals WHERE approval_id = $1`, [approvalId]),
  );
  if (initial.rows[0]?.status && initial.rows[0].status !== "pending") {
    return withClient((c) => approvalResult(c, approvalId));
  }

  let waited = 0;
  while (waited < timeoutS) {
    const r = await withClient((c) => c.query(`SELECT status FROM approvals WHERE approval_id = $1`, [approvalId]));
    if (r.rows[0]?.status && r.rows[0].status !== "pending") {
      return withClient((c) => approvalResult(c, approvalId));
    }
    await new Promise((res) => setTimeout(res, pollS * 1000));
    waited += pollS;
  }
  return { status: "timeout", approval_id: approvalId, waited_seconds: waited };
}

// ── 7. notify_customer ───────────────────────────────────────────────────────
async function notifyCustomer(args: Json) {
  const a = NotifyCustomerIn.parse(args);
  // No outbound side effect in the simulation — return a deterministic receipt.
  return {
    delivered: true,
    channel: "letter_sim",
    request_id: a.request_id,
    outcome: a.outcome,
    approved_limit: a.approved_limit ?? null,
    idempotency_key: a.idempotency_key,
  };
}

// ── tool catalogue (metadata + dispatch) ─────────────────────────────────────
type Handler = (args: Json) => Promise<unknown>;
const HANDLERS: Record<string, Handler> = {
  creditline_get_agent_provenance: getAgentProvenance,
  creditline_get_request: getRequest,
  creditline_get_customer: getCustomer,
  creditline_pull_bureau: pullBureau,
  creditline_get_active_policy: getActivePolicy,
  creditline_record_decision: recordDecision,
  creditline_request_approval: requestApproval,
  creditline_notify_customer: notifyCustomer,
};

const TOOLS: Tool[] = [
  {
    name: "creditline_get_agent_provenance",
    description:
      "Return the canonical prompt_version_hash (sha256 over CLAUDE.md). Call before recording a decision and pass the hash into creditline_record_decision so the recorded provenance pins the exact governing instructions.",
    inputSchema: { type: "object", properties: {} },
    annotations: { title: "Get canonical agent prompt provenance", readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  },
  {
    name: "creditline_get_request",
    description: "Fetch the inbound credit-line request by id.",
    inputSchema: { type: "object", properties: { request_id: { type: "string" } }, required: ["request_id"] },
    annotations: { title: "Get credit-line request", readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  },
  {
    name: "creditline_get_customer",
    description: "Fetch the customer profile together with their existing credit lines.",
    inputSchema: { type: "object", properties: { customer_id: { type: "string" } }, required: ["customer_id"] },
    annotations: { title: "Get customer profile + existing lines", readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  },
  {
    name: "creditline_pull_bureau",
    description:
      "Pull the most recent simulated bureau report for the customer. Models an external bureau call; seeded data is deterministic so the decision is reproducible for audit.",
    inputSchema: {
      type: "object",
      properties: {
        customer_id: { type: "string" },
        bureau_name: { type: "string", enum: ["experian_sim", "equifax_sim"] },
      },
      required: ["customer_id"],
    },
    annotations: { title: "Pull simulated credit-bureau report", readOnlyHint: true, destructiveHint: false, idempotentHint: false, openWorldHint: true },
  },
  {
    name: "creditline_get_active_policy",
    description: "Return the currently active policy (effective_to IS NULL): the retrieval node.",
    inputSchema: { type: "object", properties: {} },
    annotations: { title: "Retrieve active versioned decision policy", readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  },
  {
    name: "creditline_record_decision",
    description:
      "Persist the decision + full provenance. Idempotent by request_id. requires_human_approval is computed server-side from the active policy: any non-approved outcome, or an approval above the auto-approve ceiling, must be confirmed by a human.",
    inputSchema: {
      type: "object",
      properties: {
        request_id: { type: "string" },
        outcome: { type: "string", enum: ["approved", "denied", "escalated"] },
        rationale: { type: "string" },
        model_version: { type: "string" },
        prompt_version_hash: { type: "string" },
        policy_version: { type: "string" },
        bureau_report_id: { type: "string" },
        approved_limit: { type: "number" },
      },
      required: ["request_id", "outcome", "rationale", "model_version", "prompt_version_hash", "policy_version", "bureau_report_id"],
    },
    annotations: { title: "Persist decision outcome + provenance", readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  },
  {
    name: "creditline_request_approval",
    description:
      "Open the human-in-the-loop gate and BLOCK until a credit officer resolves it. Creates (or reuses) a pending approvals row, then polls until its status leaves 'pending'. On resolution returns who/what/why; is_override is true when the human went against the agent's recommendation.",
    inputSchema: {
      type: "object",
      properties: {
        decision_id: { type: "string" },
        summary: { type: "string" },
        approver_role: { type: "string" },
      },
      required: ["decision_id", "summary"],
    },
    annotations: { title: "Open human-in-the-loop approval gate (blocks until resolved)", readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: true },
  },
  {
    name: "creditline_notify_customer",
    description: "Simulate sending the decision letter to the customer. Idempotent by idempotency_key.",
    inputSchema: {
      type: "object",
      properties: {
        request_id: { type: "string" },
        outcome: { type: "string", enum: ["approved", "denied", "escalated"] },
        idempotency_key: { type: "string" },
        approved_limit: { type: "number" },
      },
      required: ["request_id", "outcome", "idempotency_key"],
    },
    annotations: { title: "Send decision letter (simulated)", readOnlyHint: false, destructiveHint: true, idempotentHint: true, openWorldHint: true },
  },
];

// ── server wiring ────────────────────────────────────────────────────────────
const server = new Server({ name: "mimic-creditline", version: "0.1.0" }, { capabilities: { tools: {} } });

server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));

server.setRequestHandler(CallToolRequestSchema, async (req) => {
  const name = req.params.name;
  const args = (req.params.arguments ?? {}) as Json;
  const handler = HANDLERS[name];
  if (!handler) return { isError: true, content: [{ type: "text" as const, text: `unknown tool: ${name}` }] };
  try {
    return result(await handler(args));
  } catch (err: any) {
    return { isError: true, content: [{ type: "text" as const, text: `error in ${name}: ${err?.message ?? err}` }] };
  }
});

async function main() {
  await server.connect(new StdioServerTransport());
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
