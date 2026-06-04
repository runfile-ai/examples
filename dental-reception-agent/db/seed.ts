// Create the `dental` database, apply the schema, and seed ext.* with a small
// but realistic practice: one location, two providers and their chairs, the
// usual appointment types, a few dozen patients with insurance and recall
// dates, provider availabilities, and an existing schedule. This is the whole
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
const DB_NAME = process.env.DENTAL_DB_NAME || "dental";
const dentalDsn =
  process.env.DENTAL_DB_DSN ||
  `postgresql://${ADMIN.user}:${ADMIN.password}@${ADMIN.host}:${ADMIN.port}/${DB_NAME}`;

const DAY = 24 * 3600 * 1000;
const pad = (n: number) => String(n).padStart(2, "0");
const dateOnly = (d: Date) => `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
// An ISO timestamp at a given UTC hour, N days from today.
const at = (daysFromNow: number, hour: number, min = 0) => {
  const d = new Date(Date.now() + daysFromNow * DAY);
  return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), hour, min)).toISOString();
};
// The next date (>= tomorrow) that falls on the given weekday (0=Sun..6=Sat).
function nextWeekday(weekday: number): Date {
  const d = new Date(Date.now() + DAY);
  while (d.getUTCDay() !== weekday) d.setUTCDate(d.getUTCDate() + 1);
  return d;
}

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

const FIRST = ["James", "Olivia", "Liam", "Emma", "Noah", "Ava", "Lucas", "Sophia", "Mason", "Isabella",
  "Ethan", "Mia", "Logan", "Amelia", "Jacob", "Harper", "Daniel", "Evelyn", "Henry", "Abigail"];
const LAST = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Miller", "Davis", "Wilson", "Moore",
  "Taylor", "Anderson", "Thomas", "Jackson", "White", "Harris", "Martin", "Garcia", "Clark", "Lewis", "Walker"];

async function seed(client: pg.Client) {
  for (const t of [
    "agent.bookings", "agent.booking_holds", "agent.approvals", "agent.halo_maps", "agent.halo_nodes",
    "agent.tool_calls", "agent.messages", "agent.sessions",
    "ext.waitlist", "ext.appointments", "ext.provider_availabilities", "ext.insurance_coverages",
    "ext.appointment_descriptors", "ext.operatories", "ext.providers", "ext.patients", "ext.locations",
  ]) {
    await client.query(`DELETE FROM ${t}`);
  }

  // ── Location, providers, chairs ─────────────────────────────────────────────
  await client.query(
    `INSERT INTO ext.locations (id, name, address, timezone, subdomain)
     VALUES ('loc_main','BrightSmile Dental','100 Market St','America/New_York','brightsmile')`,
  );
  await client.query(
    `INSERT INTO ext.providers (id, name, specialty, npi, location_id) VALUES
      ('prov_nguyen','Dr. Alice Nguyen','General Dentistry','1003001','loc_main'),
      ('prov_carter','Dr. Ben Carter','General Dentistry','1003002','loc_main'),
      ('prov_patel','Dr. Riya Patel','Hygienist','1003003','loc_main')`,
  );
  await client.query(
    `INSERT INTO ext.operatories (id, name, location_id) VALUES
      ('op_1','Operatory 1','loc_main'),
      ('op_2','Operatory 2','loc_main'),
      ('op_3','Operatory 3','loc_main')`,
  );

  // ── Appointment types ───────────────────────────────────────────────────────
  await client.query(
    `INSERT INTO ext.appointment_descriptors (id, name, duration_min, location_id, bookable_online) VALUES
      ('appt_cleaning','Cleaning',60,'loc_main',true),
      ('appt_newpatient','New Patient Exam',60,'loc_main',true),
      ('appt_crown','Crown',90,'loc_main',false),
      ('appt_emergency','Emergency Visit',30,'loc_main',true)`,
  );

  // ── Availabilities: each provider works Mon–Fri 09:00–17:00 in one chair ─────
  const avails: [string, string, string][] = [
    ["prov_nguyen", "op_1", "av_n"],
    ["prov_carter", "op_2", "av_c"],
    ["prov_patel", "op_3", "av_p"],
  ];
  for (const [prov, op, prefix] of avails) {
    for (let wd = 1; wd <= 5; wd++) {
      await client.query(
        `INSERT INTO ext.provider_availabilities
           (id, provider_id, location_id, operatory_id, weekday, start_time, end_time)
         VALUES ($1,$2,'loc_main',$3,$4,'09:00:00','17:00:00')`,
        [`${prefix}_${wd}`, prov, op, wd],
      );
    }
  }

  // ── Patients ────────────────────────────────────────────────────────────────
  // Hero: Maria Garcia — overdue for a cleaning, active Delta Dental (preventive
  // covered 100%, $0 copay). The booking demo case.
  await client.query(
    `INSERT INTO ext.patients (id, foreign_id, first_name, last_name, dob, email, phone, address,
       balance_cents, recall_due)
     VALUES ('pat_maria','pms_55012','Maria','Garcia','1989-04-12','maria.garcia@example.com',
       '+14155550142','22 Pine St, Brooklyn NY', 0, $1)`,
    [dateOnly(new Date(Date.now() - 20 * DAY))], // recall 20 days overdue
  );
  await client.query(
    `INSERT INTO ext.insurance_coverages (id, patient_id, carrier, plan_name, member_id, group_number,
       eligibility, coverage_pct, copay_cents, verified_at)
     VALUES ('cov_maria','pat_maria','Delta Dental','PPO Preventive','DG88123','GRP-4410',
       'active',100,0, now())`,
  );

  // A spread of other patients (some insured, some not, varied recall dates).
  let inserted = 0;
  for (let i = 0; i < 30; i++) {
    const first = FIRST[i % FIRST.length];
    const last = LAST[(i * 7 + 3) % LAST.length];
    const id = `pat_${String(i).padStart(3, "0")}`;
    const phone = `+1415555${String(2000 + i).padStart(4, "0")}`;
    const recall = i % 3 === 0 ? dateOnly(new Date(Date.now() - (i % 30) * DAY)) // overdue
      : i % 3 === 1 ? dateOnly(new Date(Date.now() + (i % 40) * DAY)) // upcoming
      : null;
    await client.query(
      `INSERT INTO ext.patients (id, foreign_id, first_name, last_name, dob, email, phone, balance_cents, recall_due)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)`,
      [id, `pms_${10000 + i}`, first, last, `19${70 + (i % 30)}-0${(i % 9) + 1}-1${(i % 8) + 1}`,
       `${first.toLowerCase()}.${last.toLowerCase()}@example.com`, phone, (i % 5) * 2500, recall],
    );
    if (i % 4 === 0) {
      await client.query(
        `INSERT INTO ext.insurance_coverages (id, patient_id, carrier, plan_name, eligibility, coverage_pct, copay_cents, verified_at)
         VALUES ($1,$2,$3,'PPO',$4,$5,$6, now())`,
        [`cov_${id}`, id, ["Cigna", "Aetna", "MetLife", "Guardian"][i % 4],
         i % 8 === 0 ? "inactive" : "active", [80, 100, 50][i % 3], [1500, 0, 3000][i % 3]],
      );
    }
    inserted++;
  }

  // ── Existing schedule ───────────────────────────────────────────────────────
  // A past completed cleaning for Maria (so get_appointments shows history).
  await client.query(
    `INSERT INTO ext.appointments (id, patient_id, provider_id, location_id, operatory_id, descriptor_id,
       start_time, end_time, status)
     VALUES ('appt_hist01','pat_maria','prov_nguyen','loc_main','op_1','appt_cleaning',$1,$2,'completed')`,
    [at(-180, 14), at(-180, 15)],
  );

  // Some booked appointments next week that carve real gaps into the slot grid,
  // including a 09:00 booking with Dr. Nguyen on the next Tuesday (so the first
  // AM slot the demo finds is 10:00, proving the derivation respects the diary).
  const tue = nextWeekday(2);
  const tueStr = dateOnly(tue);
  await client.query(
    `INSERT INTO ext.appointments (id, patient_id, provider_id, location_id, operatory_id, descriptor_id,
       start_time, end_time, status)
     VALUES ('appt_busy01','pat_000','prov_nguyen','loc_main','op_1','appt_cleaning',$1,$2,'booked')`,
    [`${tueStr}T09:00:00Z`, `${tueStr}T10:00:00Z`],
  );
  await client.query(
    `INSERT INTO ext.appointments (id, patient_id, provider_id, location_id, operatory_id, descriptor_id,
       start_time, end_time, status)
     VALUES ('appt_busy02','pat_004','prov_carter','loc_main','op_2','appt_crown',$1,$2,'confirmed')`,
    [`${tueStr}T11:00:00Z`, `${tueStr}T12:30:00Z`],
  );

  // Maria has an upcoming Crown consult (so reschedule/cancel have a target).
  const thu = nextWeekday(4);
  const thuStr = dateOnly(thu);
  await client.query(
    `INSERT INTO ext.appointments (id, patient_id, provider_id, location_id, operatory_id, descriptor_id,
       start_time, end_time, status)
     VALUES ('appt_maria_up','pat_maria','prov_carter','loc_main','op_2','appt_crown',$1,$2,'booked')`,
    [`${thuStr}T13:00:00Z`, `${thuStr}T14:30:00Z`],
  );

  const counts = await client.query(`SELECT
    (SELECT count(*) FROM ext.patients) patients,
    (SELECT count(*) FROM ext.insurance_coverages) coverages,
    (SELECT count(*) FROM ext.provider_availabilities) availabilities,
    (SELECT count(*) FROM ext.appointments) appointments`);
  console.log("seeded ext.*:", counts.rows[0]);
  console.log(`hero patient: pat_maria (Maria Garcia, dob 1989-04-12, +14155550142) — recall overdue, active Delta Dental`);
  console.log(`next bookable Tuesday: ${tueStr} (09:00 with Dr. Nguyen already taken → first AM slot is 10:00)`);
}

async function main() {
  await ensureDatabase();
  const client = new pg.Client({ connectionString: dentalDsn });
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
