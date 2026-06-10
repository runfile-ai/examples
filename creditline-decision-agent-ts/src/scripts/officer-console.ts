// ============================================================================
// Credit-officer console — the human side of the HITL gate.
//
// Lists pending approvals with full decision context and lets a credit officer
// resolve them. A *reject* or *modify* against the agent's recommendation is the
// EU AI Act Art. 14 / SR 11-7 §5.2 effective-challenge / override event.
//
//   npm run officer            interactive
//   npm run officer -- auto    unattended: resolve the next pending one (demos)
//
// Uses the admin DSN: the human officer is a distinct actor from the agent, and
// must be able to write the final disposition the agent's role could record only
// as a recommendation.
// ============================================================================
import { createInterface } from "node:readline/promises";
import { stdin, stdout } from "node:process";
import pg from "pg";

try { process.loadEnvFile(); } catch { /* env may come from the shell */ }

pg.types.setTypeParser(1700, (v) => (v === null ? null : Number(v))); // numeric → number

export function adminDsn(): string {
  const host = process.env.ADMIN_DB_HOST || "localhost";
  const port = process.env.ADMIN_DB_PORT || "5433";
  const user = process.env.ADMIN_DB_USER || "postgres";
  const pw = process.env.ADMIN_DB_PASSWORD || "postgres";
  return `postgresql://${user}:${pw}@${host}:${port}/mimic_creditline`;
}

export async function pending(client: pg.Client) {
  return (
    await client.query(
      `SELECT a.approval_id, a.decision_id, a.approver_role, a.justification AS summary,
              d.outcome, d.rationale, d.approved_limit, d.policy_version,
              r.request_id, r.requested_limit, c.full_name, c.annual_income
         FROM approvals a
         JOIN decisions d ON d.decision_id = a.decision_id
         JOIN credit_line_requests r ON r.request_id = d.request_id
         JOIN customers c ON c.customer_id = d.customer_id
        WHERE a.status = 'pending'
        ORDER BY a.requested_at`,
    )
  ).rows;
}

export type Action = "confirm" | "reject" | "modify";

// Programmatic resolver (used by the console and by the demo).
export async function resolve(opts: {
  approvalId: string;
  action: Action;
  approverId: string;
  justification: string;
  modifiedLimit?: number | null;
}): Promise<void> {
  const client = new pg.Client({ connectionString: adminDsn() });
  await client.connect();
  try {
    const appr = (
      await client.query(
        `SELECT a.decision_id, d.request_id, r.requested_limit
           FROM approvals a
           JOIN decisions d ON d.decision_id = a.decision_id
           JOIN credit_line_requests r ON r.request_id = d.request_id
          WHERE a.approval_id = $1`,
        [opts.approvalId],
      )
    ).rows[0];
    if (!appr) throw new Error(`approval ${opts.approvalId} not found`);

    let status: string;
    let isOverride: boolean;
    let finalOutcome: string;
    let finalLimit: number | null;
    if (opts.action === "confirm") {
      status = "confirmed";
      isOverride = false;
      finalOutcome = "approved";
      finalLimit = appr.requested_limit;
    } else if (opts.action === "reject") {
      status = "rejected";
      isOverride = true;
      finalOutcome = "denied";
      finalLimit = null;
    } else if (opts.action === "modify") {
      status = "modified";
      isOverride = true;
      finalOutcome = "approved";
      finalLimit = opts.modifiedLimit ?? null;
    } else {
      throw new Error(`unknown action ${opts.action}`);
    }

    await client.query("BEGIN");
    await client.query(
      `UPDATE approvals
          SET status = $1, is_override = $2, modified_limit = $3,
              approver_id = $4, justification = $5, resolved_at = now()
        WHERE approval_id = $6`,
      [status, isOverride, opts.modifiedLimit ?? null, opts.approverId, opts.justification, opts.approvalId],
    );
    // Write the human's final disposition onto the decision + request.
    await client.query(`UPDATE decisions SET outcome = $1, approved_limit = $2 WHERE decision_id = $3`, [
      finalOutcome,
      finalLimit,
      appr.decision_id,
    ]);
    await client.query(`UPDATE credit_line_requests SET status = $1 WHERE request_id = $2`, [
      finalOutcome,
      appr.request_id,
    ]);
    await client.query("COMMIT");
  } catch (err) {
    await client.query("ROLLBACK").catch(() => {});
    throw err;
  } finally {
    await client.end();
  }
}

async function interactive(): Promise<void> {
  const client = new pg.Client({ connectionString: adminDsn() });
  await client.connect();
  let rows: any[];
  try {
    rows = await pending(client);
  } finally {
    await client.end();
  }

  if (!rows.length) {
    console.log("No pending approvals.");
    return;
  }

  console.log("Pending approvals:\n");
  rows.forEach((r, i) => {
    console.log(`[${i}] approval ${r.approval_id}`);
    console.log(`    applicant   : ${r.full_name} (income ${r.annual_income})`);
    console.log(`    request      : ${r.requested_limit} | agent outcome: ${r.outcome}`);
    console.log(`    policy       : ${r.policy_version}`);
    console.log(`    agent summary: ${r.summary}`);
    console.log(`    rationale    : ${r.rationale}\n`);
  });

  const rl = createInterface({ input: stdin, output: stdout });
  const idx = Number((await rl.question("Select approval index: ")).trim());
  const chosen = rows[idx];
  const action = (await rl.question("Action [confirm/reject/modify]: ")).trim().toLowerCase() as Action;
  const approverId = (await rl.question("Your officer id: ")).trim() || "officer-unknown";
  let modifiedLimit: number | null = null;
  if (action === "modify") modifiedLimit = Number((await rl.question("Modified (approved) limit: ")).trim());
  const justification = (await rl.question("Justification: ")).trim();
  rl.close();

  await resolve({ approvalId: chosen.approval_id, action, approverId, justification, modifiedLimit });
  console.log(`\nResolved approval ${chosen.approval_id} as '${action}'.`);
}

// Unattended resolver: wait for the next pending approval and resolve it. For
// hands-off demos of the live agent — stands in for the human officer so the
// blocking gate gets unblocked without an interactive console.
async function autoResolve(action: Action = "modify", modifiedLimit = 12000): Promise<void> {
  console.log(`[auto-officer] waiting for a pending approval (will ${action})…`);
  for (;;) {
    const client = new pg.Client({ connectionString: adminDsn() });
    await client.connect();
    let rows: any[];
    try {
      rows = await pending(client);
    } finally {
      await client.end();
    }
    if (rows.length) {
      const chosen = rows[0];
      await resolve({
        approvalId: chosen.approval_id,
        action,
        approverId: "co-114-jmalik",
        justification:
          "Auto-officer: strong relationship and clean delinquency record; " +
          `approving a reduced ${modifiedLimit} limit to keep DTI in appetite.`,
        modifiedLimit: action === "modify" ? modifiedLimit : null,
      });
      console.log(`[auto-officer] resolved ${chosen.approval_id} as '${action}'`);
      return;
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
}

// Run directly (not when imported by the demo).
const isMain = process.argv[1] && import.meta.url === `file://${process.argv[1]}`;
if (isMain) {
  if (process.argv[2] === "auto") autoResolve().catch((e) => { console.error(e); process.exit(1); });
  else interactive().catch((e) => { console.error(e); process.exit(1); });
}
