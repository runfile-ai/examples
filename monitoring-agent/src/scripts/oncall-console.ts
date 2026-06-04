// On-call console — the human side of the write gate. Lists pending approvals
// (declare_incident / resolve_incident) and lets the on-call engineer approve or
// reject. The agent's gated tool call is blocked until this resolves it.
//
//   npm run oncall            interactive
//   npm run oncall -- auto    unattended: approve the next pending one (demos)
import { createInterface } from "node:readline/promises";
import { stdin, stdout } from "node:process";
import pg from "pg";

try { process.loadEnvFile(); } catch { /* env may come from the shell */ }

const dsn =
  process.env.MONITORING_DB_DSN ||
  `postgresql://${process.env.ADMIN_DB_USER || "postgres"}:${process.env.ADMIN_DB_PASSWORD || "postgres"}@${process.env.ADMIN_DB_HOST || "localhost"}:${process.env.ADMIN_DB_PORT || "5433"}/${process.env.MONITORING_DB_NAME || "monitoring"}`;

async function pending(client: pg.Client) {
  return (
    await client.query(
      `SELECT id, action, payload, idempotency_key, created_at
         FROM agent.approvals WHERE status = 'pending' ORDER BY created_at`,
    )
  ).rows;
}

export async function resolve(client: pg.Client, id: string, status: "approved" | "rejected", decidedBy: string) {
  await client.query(
    `UPDATE agent.approvals SET status = $1, decided_by = $2, decided_at = now() WHERE id = $3`,
    [status, decidedBy, id],
  );
}

async function auto() {
  const client = new pg.Client({ connectionString: dsn });
  await client.connect();
  try {
    console.log("[oncall:auto] waiting for a pending approval…");
    for (;;) {
      const rows = await pending(client);
      if (rows.length) {
        const a = rows[0];
        await resolve(client, a.id, "approved", "oncall-auto@demo");
        console.log(`[oncall:auto] APPROVED ${a.action} ${JSON.stringify(a.payload)}`);
        return;
      }
      await new Promise((r) => setTimeout(r, 1000));
    }
  } finally {
    await client.end();
  }
}

async function interactive() {
  const client = new pg.Client({ connectionString: dsn });
  await client.connect();
  try {
    const rows = await pending(client);
    if (!rows.length) {
      console.log("No pending approvals.");
      return;
    }
    const rl = createInterface({ input: stdin, output: stdout });
    for (const a of rows) {
      console.log(`\nApproval ${a.id}`);
      console.log(`  action : ${a.action}`);
      console.log(`  payload: ${JSON.stringify(a.payload)}`);
      const ans = (await rl.question("  approve / reject / skip? ")).trim().toLowerCase();
      if (ans === "approve" || ans === "a") {
        const who = (await rl.question("  your on-call id: ")).trim() || "oncall-unknown";
        await resolve(client, a.id, "approved", who);
        console.log("  → approved");
      } else if (ans === "reject" || ans === "r") {
        const who = (await rl.question("  your on-call id: ")).trim() || "oncall-unknown";
        await resolve(client, a.id, "rejected", who);
        console.log("  → rejected");
      } else {
        console.log("  → skipped");
      }
    }
    rl.close();
  } finally {
    await client.end();
  }
}

if (process.argv[2] === "auto") auto();
else interactive();
