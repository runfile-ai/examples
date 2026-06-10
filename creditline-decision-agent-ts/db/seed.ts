// ============================================================================
// Initialise the mimic_creditline database: create it (if absent), apply the
// schema, create the least-privilege `creditline_agent` role + grants, then
// seed the simulated world. Uses the ADMIN_* connection; the agent role is
// never used here.
//
//   npm run initdb     (or: tsx db/seed.ts)
//
// Seeds a versioned policy (v1 archived + v2 active), three applicants, their
// lines/bureau reports, and inbound requests. One applicant — Dana Whitfield,
// request id 1111…1111 — is engineered to ESCALATE (asks above the auto-approve
// ceiling AND trips the DTI threshold): the human-in-the-loop / override case.
// ============================================================================
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import pg from "pg";

try { process.loadEnvFile(); } catch { /* env may come from the shell */ }

const __dirname = dirname(fileURLToPath(import.meta.url));

const ADMIN = {
  host: process.env.ADMIN_DB_HOST || "localhost",
  port: Number(process.env.ADMIN_DB_PORT || "5433"),
  user: process.env.ADMIN_DB_USER || "postgres",
  password: process.env.ADMIN_DB_PASSWORD || "postgres",
};
const DB_NAME = "mimic_creditline";

// Deterministic ids so the demo can reference the escalation case directly.
const DEMO_CUSTOMER_ID = "22222222-2222-2222-2222-222222222222";
const DEMO_REQUEST_ID = "11111111-1111-1111-1111-111111111111";
const DEMO_BUREAU_ID = "33333333-3333-3333-3333-333333333333";

async function ensureDatabase() {
  const admin = new pg.Client({ ...ADMIN, database: "postgres" });
  await admin.connect();
  try {
    const exists = await admin.query("SELECT 1 FROM pg_database WHERE datname = $1", [DB_NAME]);
    if (exists.rowCount === 0) {
      await admin.query(`CREATE DATABASE ${DB_NAME}`);
      console.log(`created database ${DB_NAME}`);
    }
  } finally {
    await admin.end();
  }
}

async function seed(client: pg.Client) {
  // Idempotent reseed: clear in FK-safe order.
  for (const tbl of [
    "approvals",
    "decisions",
    "bureau_reports",
    "credit_line_requests",
    "credit_lines",
    "customers",
    "decision_policies",
  ]) {
    await client.query(`DELETE FROM ${tbl}`);
  }

  // ── Policies ────────────────────────────────────────────────────────────────
  await client.query(
    `INSERT INTO decision_policies (version, thresholds, narrative, effective_from, effective_to)
     VALUES ($1, $2::jsonb, $3, now() - interval '120 days', now() - interval '30 days')`,
    [
      "2026.01-rev1",
      JSON.stringify({ min_credit_score: 700, max_dti: 0.4, auto_approve_ceiling: 10000, max_delinquencies_24m: 0 }),
      "Conservative launch policy.",
    ],
  );
  await client.query(
    `INSERT INTO decision_policies (version, thresholds, narrative, effective_from, effective_to)
     VALUES ($1, $2::jsonb, $3, now() - interval '30 days', NULL)`,
    [
      "2026.03-rev2",
      JSON.stringify({ min_credit_score: 680, max_dti: 0.45, auto_approve_ceiling: 15000, max_delinquencies_24m: 1 }),
      "Q1-2026 revision: ceiling raised to 15k, DTI tolerance to 0.45.",
    ],
  );

  // ── Demo applicant: Dana Whitfield (escalation / override case) ─────────────
  await client.query(
    `INSERT INTO customers
       (customer_id, full_name, date_of_birth, email, annual_income,
        employment_status, residential_status, relationship_since, internal_risk_segment)
     VALUES ($1,'Dana Whitfield','1987-04-02','dana.whitfield@example.com',72000,
             'employed','renter','2019-06-01','B')`,
    [DEMO_CUSTOMER_ID],
  );
  await client.query(
    `INSERT INTO credit_lines (customer_id, product_type, current_limit, current_balance, status)
     VALUES ($1, 'card', 6000, 2200, 'active')`,
    [DEMO_CUSTOMER_ID],
  );
  await client.query(
    `INSERT INTO bureau_reports
       (bureau_report_id, customer_id, bureau_name, report_version, credit_score,
        total_outstanding_debt, delinquencies_24m, open_accounts, hard_inquiries_6m)
     VALUES ($1,$2,'experian_sim','2026-03-A',712, 8000, 0, 4, 1)`,
    [DEMO_BUREAU_ID, DEMO_CUSTOMER_ID],
  );
  // Requests 25,000 → above the 15,000 ceiling (large exposure) AND
  // dti = (8000 + 25000) / 72000 = 0.458 > 0.45 → escalate.
  await client.query(
    `INSERT INTO credit_line_requests
       (request_id, customer_id, request_type, requested_limit, channel, status)
     VALUES ($1,$2,'increase',25000,'app','pending')`,
    [DEMO_REQUEST_ID, DEMO_CUSTOMER_ID],
  );

  // ── A clean auto-approve applicant (Marco Reyes) ────────────────────────────
  const marco = (
    await client.query(
      `INSERT INTO customers
         (full_name, date_of_birth, email, annual_income, employment_status,
          residential_status, relationship_since, internal_risk_segment)
       VALUES ('Marco Reyes','1979-11-20','marco.reyes@example.com',98000,'employed','owner','2015-02-01','A')
       RETURNING customer_id`,
    )
  ).rows[0].customer_id;
  await client.query(
    `INSERT INTO bureau_reports
       (customer_id, bureau_name, report_version, credit_score,
        total_outstanding_debt, delinquencies_24m, open_accounts, hard_inquiries_6m)
     VALUES ($1,'experian_sim','2026-03-A',775, 5000, 0, 6, 0)`,
    [marco],
  );
  await client.query(
    `INSERT INTO credit_line_requests
       (customer_id, request_type, requested_limit, channel, status)
     VALUES ($1,'new',8000,'web','pending')`,
    [marco],
  );

  // ── A borderline adverse applicant (single-threshold fail → escalate) ───────
  const priya = (
    await client.query(
      `INSERT INTO customers
         (full_name, date_of_birth, email, annual_income, employment_status,
          residential_status, relationship_since, internal_risk_segment)
       VALUES ('Priya Nair','1992-07-09','priya.nair@example.com',54000,'self_employed','renter','2021-09-01','C')
       RETURNING customer_id`,
    )
  ).rows[0].customer_id;
  await client.query(
    `INSERT INTO bureau_reports
       (customer_id, bureau_name, report_version, credit_score,
        total_outstanding_debt, delinquencies_24m, open_accounts, hard_inquiries_6m)
     VALUES ($1,'experian_sim','2026-03-A',664, 9000, 2, 5, 3)`,
    [priya],
  );
  await client.query(
    `INSERT INTO credit_line_requests
       (customer_id, request_type, requested_limit, channel, status)
     VALUES ($1,'increase',6000,'branch','pending')`,
    [priya],
  );

  console.log("Seeded mimic_creditline:");
  console.log(`  demo escalation request_id = ${DEMO_REQUEST_ID} (Dana Whitfield)`);
  console.log("  + 1 auto-approve applicant (Marco Reyes)");
  console.log("  + 1 borderline applicant (Priya Nair)");
  console.log("  policies: 2026.01-rev1 (archived), 2026.03-rev2 (active)");
}

async function main() {
  await ensureDatabase();
  const dsn = `postgresql://${ADMIN.user}:${ADMIN.password}@${ADMIN.host}:${ADMIN.port}/${DB_NAME}`;
  const client = new pg.Client({ connectionString: dsn });
  await client.connect();
  try {
    await client.query(readFileSync(join(__dirname, "01_mimic_creditline_schema.sql"), "utf8"));
    console.log("applied schema");
    await client.query(readFileSync(join(__dirname, "03_roles_and_grants.sql"), "utf8"));
    console.log("created role + grants");
    await seed(client);
    console.log("done.");
  } finally {
    await client.end();
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
