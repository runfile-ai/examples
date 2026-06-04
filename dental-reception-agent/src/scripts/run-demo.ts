// ============================================================================
// Deterministic end-to-end demo (no LLM / API key). Drives the REAL dental MCP
// server over stdio: identity gate → verify → Halo patient summary → coverage →
// derive open slots → hold → human-gated book (auto-approved here) → prove the
// no-double-book constraint → show the booking. Exercises both gates and the
// Halo envelope/fetch flow.
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
  process.env.DENTAL_DB_DSN ||
  `postgresql://${process.env.ADMIN_DB_USER || "postgres"}:${process.env.ADMIN_DB_PASSWORD || "postgres"}@${process.env.ADMIN_DB_HOST || "localhost"}:${process.env.ADMIN_DB_PORT || "5433"}/${process.env.DENTAL_DB_NAME || "dental"}`;

async function call(client: Client, name: string, args: Record<string, unknown>): Promise<any> {
  const res: any = await client.callTool({ name, arguments: args });
  const text = res.content?.[0]?.text ?? "{}";
  return JSON.parse(text);
}

// Stand in for the front-desk: approve the next pending gated write.
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
        await c.query(`UPDATE agent.approvals SET status='approved', decided_by='frontdesk-auto@demo', decided_at=now() WHERE id=$1`, [r.rows[0].id]);
        console.log(`  [front-desk] APPROVED ${action} ${JSON.stringify(r.rows[0].payload)}`);
        return;
      }
      await new Promise((res) => setTimeout(res, 500));
    }
  } finally {
    await c.end();
  }
}

const fmt = (iso: string) => new Date(iso).toISOString().replace("T", " ").slice(0, 16) + "Z";

async function main() {
  console.log("=== Dental reception agent — deterministic demo (real MCP server over stdio) ===\n");

  const transport = new StdioClientTransport({
    command: TSX_BIN,
    args: [SERVER_PATH],
    env: { ...process.env, DENTAL_DB_DSN: dsn, AGENT_SESSION_ID: SESSION_ID, DENTAL_CHANNEL: "demo", APPROVAL_POLL_SECONDS: "1", APPROVAL_TIMEOUT_SECONDS: "60" } as Record<string, string>,
  });
  const client = new Client({ name: "demo", version: "0.1.0" });
  await client.connect(transport);

  const tools = await client.listTools();
  console.log(`MCP server exposes ${tools.tools.length} tools.\n`);

  // 1. LOOKUP — thin candidates only (no PHI).
  const found = await call(client, "find_patient", { query: "+14155550142" });
  const cand = found.candidates[0];
  console.log(`find_patient("+14155550142") → ${found.count} candidate: ${cand.first_name} ${cand.last_name}, phone ${cand.phone}`);

  // 2. IDENTITY GATE — a disclosure read is refused before verification.
  const blocked = await call(client, "get_patient_summary", { patient_id: cand.patient_id });
  console.log(`get_patient_summary BEFORE verify → ${blocked.error}  (identity gate holds)`);

  // 3. VERIFY — name + dob.
  const verified = await call(client, "verify_identity", { patient_id: cand.patient_id, last_name: "Garcia", dob: "1989-04-12" });
  console.log(`verify_identity(Garcia, 1989-04-12) → verified=${verified.verified}`);

  // 4. SUMMARY — Halo envelope; fetch only contact + insurance, never clinical.
  const summary = await call(client, "get_patient_summary", { patient_id: cand.patient_id });
  console.log(`\nget_patient_summary → envelope refs: ${Object.keys(summary.refs).join(", ")}`);
  console.log(`  ${summary.summary.name} — recall_overdue=${summary.summary.recall_overdue}, insurance=${JSON.stringify(summary.summary.insurance)}`);
  const drill = await call(client, "halo_fetch_many", { handles: [summary.refs.contact, summary.refs.insurance] });
  const contact = drill[summary.refs.contact];
  console.log(`  halo_fetch_many(contact, insurance) → phone ${contact.phone}; clinical handle left UNFETCHED`);

  // 5. COVERAGE for a cleaning.
  const cov = await call(client, "check_coverage", { patient_id: cand.patient_id, descriptor_id: "appt_cleaning" });
  console.log(`\ncheck_coverage(Cleaning) → ${cov.eligibility}, ${cov.coverage_pct}% covered, copay $${(cov.copay_cents / 100).toFixed(2)} (${cov.carrier})`);

  // 6. SLOTS — derived (availabilities minus booked); envelope summary, then the slot list.
  const slots = await call(client, "find_open_slots", { descriptor_id: "appt_cleaning", provider: "prov_nguyen", time_of_day: "AM", to: new Date(Date.now() + 9 * 864e5).toISOString() });
  console.log(`\nfind_open_slots(Cleaning, Dr. Nguyen, AM) → ${slots.summary.n_slots} slots across ${Object.keys(slots.summary.by_day).length} days`);
  const allSlots = await call(client, "halo_fetch", { handle: slots.refs.all_slots });
  const chosen = allSlots[0];
  console.log(`  first slot: ${fmt(chosen.start_time)} with ${chosen.provider_name} in ${chosen.operatory_id}`);

  // 7. HOLD — agent-local, short TTL.
  const hold = await call(client, "hold_slot", {
    patient_id: cand.patient_id, descriptor_id: "appt_cleaning",
    start_time: chosen.start_time, provider_id: chosen.provider_id, operatory_id: chosen.operatory_id, location_id: chosen.location_id,
  });
  console.log(`\nhold_slot → hold ${hold.hold_id.slice(0, 8)} (status=${hold.status}, expires ${fmt(hold.expires_at)})`);

  // 8. BOOK — human-gated; auto-approver stands in for the front desk.
  console.log("\nbook_appointment (HUMAN-GATED — agent blocks)…");
  const approver = autoApprove("book");
  const booked = await call(client, "book_appointment", { hold_id: hold.hold_id });
  await approver;
  console.log(`  → committed=${booked.committed} appointment=${booked.external_appt_id} at ${fmt(booked.start_time)} by ${booked.decided_by}`);

  // 9. NO DOUBLE-BOOK — a different patient holds the SAME chair+time, then books.
  console.log("\nattempting a conflicting book on the same chair+time (different patient)…");
  const hold2 = await call(client, "hold_slot", {
    patient_id: "pat_001", descriptor_id: "appt_cleaning",
    start_time: chosen.start_time, provider_id: chosen.provider_id, operatory_id: chosen.operatory_id, location_id: chosen.location_id,
  });
  const approver2 = autoApprove("book");
  const clash = await call(client, "book_appointment", { hold_id: hold2.hold_id });
  await approver2;
  console.log(`  → committed=${clash.committed}, error=${clash.error}  (no_chair_overlap held the line)`);

  // 10. SHOW the booking landed.
  const appts = await call(client, "get_appointments", { patient_id: cand.patient_id });
  console.log(`\nget_appointments → ${appts.summary.upcoming.length} upcoming; next: ${appts.summary.upcoming.map((a: any) => `${a.descriptor} ${fmt(a.start_time)}`).join(", ")}`);

  await client.close();
  console.log("\n=== demo complete: identity-gated → coverage → derived slots via Halo → held → booked (human-approved) → double-book blocked ===");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
