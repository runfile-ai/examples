// Create the `monitoring` database, apply the schema, and seed ext.* with
// realistic data: projects/services, a few dozen issues with heavy events, a
// stream of logs (with an error spike), and an open incident. This is the whole
// "external system" — it makes the agent runnable today with no credentials.
//
//   npm run initdb     (or: tsx db/seed.ts)
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
const DB_NAME = process.env.MONITORING_DB_NAME || "monitoring";
const monitoringDsn =
  process.env.MONITORING_DB_DSN ||
  `postgresql://${ADMIN.user}:${ADMIN.password}@${ADMIN.host}:${ADMIN.port}/${DB_NAME}`;

const now = Date.now();
const iso = (msAgo: number) => new Date(now - msAgo).toISOString();
const MIN = 60_000;
const HOUR = 60 * MIN;

async function ensureDatabase() {
  const admin = new pg.Client({ ...ADMIN, database: "postgres" });
  await admin.connect();
  const exists = await admin.query("SELECT 1 FROM pg_database WHERE datname = $1", [DB_NAME]);
  if (exists.rowCount === 0) {
    await admin.query(`CREATE DATABASE ${DB_NAME}`);
    console.log(`created database ${DB_NAME}`);
  }
  await admin.end();
}

function heroStacktrace() {
  return {
    values: [
      {
        type: "TypeError",
        value: "Cannot read properties of undefined (reading 'id')",
        stacktrace: {
          frames: [
            { filename: "app/server.js", function: "handleRequest", lineno: 142, in_app: true },
            { filename: "app/routes/checkout.js", function: "completeOrder", lineno: 88, in_app: true,
              context_line: "  const customerId = session.user.id;" },
            { filename: "app/services/payments.js", function: "charge", lineno: 51, in_app: true },
            { filename: "node_modules/pg/lib/client.js", function: "query", lineno: 526, in_app: false },
          ],
        },
      },
    ],
  };
}

const breadcrumbs = (base: number) => [
  { timestamp: iso(base + 5000), category: "http", message: "POST /api/checkout/complete", level: "info" },
  { timestamp: iso(base + 3000), category: "db", message: "SELECT carts WHERE id=$1", level: "info" },
  { timestamp: iso(base + 1500), category: "auth", message: "session resolved: anonymous", level: "warning" },
  { timestamp: iso(base + 200), category: "error", message: "completeOrder threw", level: "error" },
];

async function seed(client: pg.Client) {
  for (const t of [
    "agent.incident_links", "agent.triage_state", "agent.approvals", "agent.halo_maps",
    "agent.halo_nodes", "agent.tool_calls", "agent.messages", "agent.sessions",
    "ext.incident_notes", "ext.incident_alerts", "ext.incidents", "ext.events",
    "ext.logs", "ext.issues", "ext.services", "ext.projects",
  ]) {
    await client.query(`DELETE FROM ${t}`);
  }

  // Projects + services
  await client.query(`INSERT INTO ext.projects (id, slug, name, platform) VALUES
    ('p_backend','backend','Backend API','node'),
    ('p_web','web','Web App','javascript')`);
  await client.query(`INSERT INTO ext.services (id, name) VALUES
    ('svc_checkout','checkout-api'), ('svc_web','web-frontend'), ('svc_payments','payments-worker')`);

  // ── The hero issue (high impact; the triage/declare demo case) ──────────────
  await client.query(
    `INSERT INTO ext.issues (id, short_id, project_id, title, culprit, level, status,
       times_seen, user_count, first_seen, last_seen, metadata, permalink)
     VALUES ('4502913','BACKEND-12A','p_backend',
       'TypeError: Cannot read properties of undefined (reading ''id'')',
       'checkout.completeOrder','error','unresolved',1843,412,$1,$2,
       $3,'https://sentry.io/backend/issues/4502913')`,
    [iso(3 * HOUR), iso(4 * MIN),
     JSON.stringify({ type: "TypeError", value: "Cannot read properties of undefined (reading 'id')", filename: "app/routes/checkout.js" })],
  );
  for (let i = 0; i < 6; i++) {
    const base = i * 9 * MIN + 4 * MIN;
    await client.query(
      `INSERT INTO ext.events (id, issue_id, timestamp, message, platform, environment, release,
         server_name, exception, breadcrumbs, tags, contexts)
       VALUES ($1,'4502913',$2,'completeOrder failed','node','production','backend@2026.6.3',
         $3,$4,$5,$6,$7)`,
      [
        `evt_hero_${i}`, iso(base), `web-${(i % 3) + 1}`,
        JSON.stringify(heroStacktrace()), JSON.stringify(breadcrumbs(base)),
        JSON.stringify({ environment: "production", release: "backend@2026.6.3", transaction: "POST /api/checkout/complete", level: "error" }),
        JSON.stringify({ runtime: { name: "node", version: "22.2.0" }, os: { name: "linux" } }),
      ],
    );
  }

  // ── A spread of other issues (triage noise) ────────────────────────────────
  const others = [
    ["4502880", "WEB-3F", "p_web", "Unhandled promise rejection in Cart.tsx", "Cart.render", "warning", 220, 95],
    ["4502901", "BACKEND-12B", "p_backend", "ECONNRESET: payments upstream", "payments.charge", "error", 540, 70],
    ["4502777", "WEB-21", "p_web", "Hydration mismatch on /account", "Account.page", "warning", 88, 33],
    ["4502810", "BACKEND-09", "p_backend", "Slow query: orders join", "orders.list", "info", 1200, 5],
    ["4502999", "BACKEND-14", "p_backend", "OOM in image resize", "media.resize", "fatal", 60, 60],
  ];
  for (const [id, shortId, proj, title, culprit, level, seen, users] of others) {
    await client.query(
      `INSERT INTO ext.issues (id, short_id, project_id, title, culprit, level, status,
         times_seen, user_count, first_seen, last_seen)
       VALUES ($1,$2,$3,$4,$5,$6,'unresolved',$7,$8,$9,$10)`,
      [id, shortId, proj, title, culprit, level, seen, users, iso(8 * HOUR), iso(20 * MIN)],
    );
    await client.query(
      `INSERT INTO ext.events (id, issue_id, timestamp, message, platform, environment, release, exception, tags)
       VALUES ($1,$2,$3,$4,'node','production','backend@2026.6.3',$5,$6)`,
      [
        `evt_${id}`, id, iso(20 * MIN), title,
        JSON.stringify({ values: [{ type: String(title).split(":")[0], value: String(title), stacktrace: { frames: [{ filename: `app/${culprit}.js`, function: String(culprit), lineno: 33, in_app: true }] } }] }),
        JSON.stringify({ environment: "production", level }),
      ],
    );
  }

  // ── Logs: a baseline stream plus an error spike on checkout-api ─────────────
  const services = ["checkout-api", "web-frontend", "payments-worker"];
  const levels = ["info", "info", "info", "warn", "debug"];
  let n = 0;
  for (let m = 360; m >= 0; m--) {
    // ~3 baseline lines per minute across services
    for (let k = 0; k < 3; k++) {
      const svc = services[(m + k) % services.length];
      await client.query(
        `INSERT INTO ext.logs (ts, service, level, message, attributes, trace_id)
         VALUES ($1,$2,$3,$4,$5,$6)`,
        [iso(m * MIN), svc, levels[(m + k) % levels.length],
         `${svc} handled request rid=${n}`, JSON.stringify({ rid: n, path: "/healthz" }), `trace_${n}`],
      );
      n++;
    }
    // error spike on checkout-api in the 4–25 minute-ago window (matches the hero issue)
    if (m <= 25 && m >= 4 && m % 2 === 0) {
      for (let k = 0; k < 4; k++) {
        await client.query(
          `INSERT INTO ext.logs (ts, service, level, message, attributes, trace_id)
           VALUES ($1,'checkout-api','error',$2,$3,$4)`,
          [iso(m * MIN - k * 5000),
           "completeOrder failed: TypeError reading 'id' of undefined",
           JSON.stringify({ rid: n, route: "POST /api/checkout/complete", err: "TypeError" }), `trace_${n}`],
        );
        n++;
      }
    }
  }

  // ── One pre-existing open incident (unrelated) ─────────────────────────────
  await client.query(
    `INSERT INTO ext.incidents (id, title, status, urgency, service_id, dedup_key)
     VALUES ('INC-legacy01','Elevated 5xx on web-frontend','triggered','low','svc_web','web-5xx-legacy')`,
  );

  const counts = await client.query(`SELECT
    (SELECT count(*) FROM ext.issues) issues,
    (SELECT count(*) FROM ext.events) events,
    (SELECT count(*) FROM ext.logs) logs,
    (SELECT count(*) FROM ext.incidents) incidents`);
  console.log("seeded ext.*:", counts.rows[0]);
  console.log("hero issue: 4502913 (BACKEND-12A) — 412 users, error, checkout.completeOrder");
}

async function main() {
  await ensureDatabase();
  const client = new pg.Client({ connectionString: monitoringDsn });
  await client.connect();
  try {
    const schema = readFileSync(join(__dirname, "01_schema.sql"), "utf8");
    await client.query(schema);
    console.log("applied schema (ext + agent)");
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
