// ============================================================================
// dental-mcp — stdio MCP server.
//
// Intent-shaped tools over the local Postgres. The tool CONTRACT is the swap
// point: today each body is SQL on ext.*; later it is a call to the NexHealth
// synchronizer. Signatures and returned shapes do not change.
//
// Two gates sit in front of sensitive work:
//   • an IDENTITY gate — get_patient_summary / check_coverage / get_appointments
//     refuse until the session is verified (verify_identity sets identity_ok);
//   • a WRITE gate — book_appointment / reschedule / cancel / update_contact
//     route through agent.approvals and BLOCK until a human confirms.
//
// Heavy reads return Halo envelopes (compact summary + handles); the agent
// drills in with halo_fetch / halo_fetch_many. Every call is recorded in
// agent.tool_calls.
// ============================================================================
import { randomUUID } from "node:crypto";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  type Tool,
} from "@modelcontextprotocol/sdk/types.js";
import { withClient } from "./db.js";
import * as halo from "./halo.js";

// ── session (one agent run / call) ───────────────────────────────────────────
const SESSION_ID = process.env.AGENT_SESSION_ID || randomUUID();
const CHANNEL = process.env.DENTAL_CHANNEL || "voice";
const HOLD_TTL_MIN = Number(process.env.HOLD_TTL_MINUTES || "15");

async function ensureSession(): Promise<void> {
  await withClient((c) =>
    c.query(
      `INSERT INTO agent.sessions (id, channel, status) VALUES ($1, $2, 'active')
       ON CONFLICT (id) DO NOTHING`,
      [SESSION_ID, CHANNEL],
    ),
  );
}

// ── helpers ──────────────────────────────────────────────────────────────────
type Json = Record<string, unknown>;
const result = (obj: unknown) => ({ content: [{ type: "text" as const, text: JSON.stringify(obj) }] });
const maskPhone = (p: string | null) => (p ? p.replace(/.(?=.{4})/g, "•") : null);

class IdentityRequired extends Error {
  constructor() {
    super("identity_required");
  }
}

async function identityOk(): Promise<boolean> {
  const r = await withClient((c) =>
    c.query(`SELECT identity_ok FROM agent.sessions WHERE id = $1`, [SESSION_ID]),
  );
  return !!r.rows[0]?.identity_ok;
}

async function requireIdentity(): Promise<void> {
  if (!(await identityOk())) throw new IdentityRequired();
}

async function recordToolCall(
  tool: string,
  args: unknown,
  envelopeRoot: string | null,
  latencyMs: number,
  ok: boolean,
  error: string | null,
): Promise<void> {
  try {
    await withClient((c) =>
      c.query(
        `INSERT INTO agent.tool_calls (id, session_id, tool, args, envelope_root, latency_ms, ok, error)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8)`,
        [randomUUID(), SESSION_ID, tool, args ?? {}, envelopeRoot, latencyMs, ok, error],
      ),
    );
  } catch {
    /* observability must never break a tool call */
  }
}

// Block until a pending approval is resolved (or times out). Polls with short
// connections so we never hold one open across the wait.
async function awaitApproval(
  action: string,
  payload: Json,
  idempotencyKey: string,
): Promise<{ status: string; decided_by: string | null; id: string }> {
  const timeoutS = Number(process.env.APPROVAL_TIMEOUT_SECONDS || "900");
  const pollS = Number(process.env.APPROVAL_POLL_SECONDS || "2");

  const existing = await withClient((c) =>
    c.query(`SELECT id, status, decided_by FROM agent.approvals WHERE idempotency_key = $1`, [
      idempotencyKey,
    ]),
  );
  let approvalId: string;
  if (existing.rowCount && existing.rows[0].status !== "pending") {
    return existing.rows[0]; // idempotent replay
  } else if (existing.rowCount) {
    approvalId = existing.rows[0].id;
  } else {
    approvalId = randomUUID();
    await withClient((c) =>
      c.query(
        `INSERT INTO agent.approvals (id, session_id, action, payload, idempotency_key, status)
         VALUES ($1,$2,$3,$4,$5,'pending') ON CONFLICT (idempotency_key) DO NOTHING`,
        [approvalId, SESSION_ID, action, payload, idempotencyKey],
      ),
    );
  }

  let waited = 0;
  while (waited < timeoutS) {
    const r = await withClient((c) =>
      c.query(`SELECT id, status, decided_by FROM agent.approvals WHERE id = $1`, [approvalId]),
    );
    if (r.rowCount && r.rows[0].status !== "pending") return r.rows[0];
    await new Promise((res) => setTimeout(res, pollS * 1000));
    waited += pollS;
  }
  return { id: approvalId, status: "timeout", decided_by: null };
}

// ── reads ────────────────────────────────────────────────────────────────────

// Thin candidate list to START identity verification — by phone, name+dob, or
// email. Deliberately returns no PHI beyond a name and a masked phone.
async function findPatient(args: Json) {
  const query = (args.query as string | undefined) ?? null;
  const lastName = (args.last_name as string | undefined) ?? null;
  const dob = (args.dob as string | undefined) ?? null;
  const email = (args.email as string | undefined) ?? null;
  const phone = (args.phone as string | undefined) ?? query;
  return withClient(async (c) => {
    const rows = (
      await c.query(
        `SELECT id, first_name, last_name, phone
           FROM ext.patients
          WHERE inactive = false
            AND ( ($1::text IS NOT NULL AND phone = $1)
               OR ($2::text IS NOT NULL AND lower(last_name) = lower($2) AND ($3::date IS NULL OR dob = $3))
               OR ($4::text IS NOT NULL AND lower(email) = lower($4))
               OR ($5::text IS NOT NULL AND lower(email) = lower($5)) )
          ORDER BY last_name, first_name
          LIMIT 5`,
        [phone, lastName, dob, email, query],
      )
    ).rows;
    return {
      candidates: rows.map((r) => ({
        patient_id: r.id,
        first_name: r.first_name,
        last_name: r.last_name,
        phone: maskPhone(r.phone),
      })),
      count: rows.length,
      note: "Verify with verify_identity(patient_id, last_name, dob) before disclosing any detail.",
    };
  });
}

// Identity gate: confirm name + dob against the record, then mark the session
// verified. No PHI is returned; only a boolean.
async function verifyIdentity(args: Json) {
  const patientId = String(args.patient_id);
  const lastName = String(args.last_name ?? "");
  const dob = String(args.dob ?? "");
  return withClient(async (c) => {
    const r = await c.query(
      `SELECT id, foreign_id FROM ext.patients
        WHERE id = $1 AND lower(last_name) = lower($2) AND dob = $3 AND inactive = false`,
      [patientId, lastName, dob],
    );
    if (!r.rowCount) return { verified: false, reason: "name_or_dob_mismatch" };
    await c.query(
      `UPDATE agent.sessions SET identity_ok = true, patient_ref = $2 WHERE id = $1`,
      [SESSION_ID, r.rows[0].foreign_id ?? patientId],
    );
    return { verified: true, patient_id: patientId };
  });
}

// Fat patient record, Halo-encoded. Reception work needs contact, balance,
// recall, and insurance status — never the clinical detail, which stays behind
// an unfetched handle. Keyed into the patient map for argument-join.
async function getPatientSummary(args: Json) {
  await requireIdentity();
  const patientId = String(args.patient_id);
  return withClient(async (c) => {
    const p = (await c.query(`SELECT * FROM ext.patients WHERE id = $1`, [patientId])).rows[0];
    if (!p) return { error: "patient_not_found", patient_id: patientId };
    const coverages = (
      await c.query(`SELECT * FROM ext.insurance_coverages WHERE patient_id = $1`, [patientId])
    ).rows;
    const upcoming = (
      await c.query(
        `SELECT count(*)::int AS n FROM ext.appointments
          WHERE patient_id = $1 AND status IN ('booked','confirmed') AND start_time >= now()`,
        [patientId],
      )
    ).rows[0].n;
    const activeIns = coverages.find((x) => x.eligibility === "active");
    const summary = {
      patient_id: p.id,
      name: `${p.first_name} ${p.last_name}`,
      recall_due: p.recall_due,
      recall_overdue: p.recall_due ? new Date(p.recall_due) < new Date() : false,
      balance_cents: p.balance_cents,
      insurance: activeIns
        ? { carrier: activeIns.carrier, eligibility: activeIns.eligibility }
        : { eligibility: coverages[0]?.eligibility ?? "unknown" },
      upcoming_appointments: upcoming,
    };
    const env = await halo.encode(c, "patient_summary", summary, {
      contact: { email: p.email, phone: p.phone, address: p.address },
      insurance: coverages,
      // Present but intentionally never fetched for reception work.
      clinical: { note: "clinical chart withheld — not needed for reception", chart_ref: p.foreign_id },
    });
    env.map_root = await halo.accumulate(c, SESSION_ID, patientId, env, { patient_id: patientId });
    return env;
  });
}

// Turn the insurance record into eligibility + copay for one appointment type.
async function checkCoverage(args: Json) {
  await requireIdentity();
  const patientId = String(args.patient_id);
  const descriptorId = String(args.descriptor_id);
  return withClient(async (c) => {
    const cov = (
      await c.query(
        `SELECT eligibility, coverage_pct, copay_cents, carrier, plan_name
           FROM ext.insurance_coverages WHERE patient_id = $1
          ORDER BY (eligibility = 'active') DESC LIMIT 1`,
        [patientId],
      )
    ).rows[0];
    const descriptor = (
      await c.query(`SELECT name FROM ext.appointment_descriptors WHERE id = $1`, [descriptorId])
    ).rows[0];
    if (!cov) return { patient_id: patientId, eligibility: "unknown", descriptor: descriptor?.name };
    return {
      patient_id: patientId,
      descriptor: descriptor?.name ?? descriptorId,
      eligibility: cov.eligibility,
      coverage_pct: cov.coverage_pct,
      copay_cents: cov.copay_cents,
      carrier: cov.carrier,
      plan_name: cov.plan_name,
    };
  });
}

async function getAppointments(args: Json) {
  await requireIdentity();
  const patientId = String(args.patient_id);
  return withClient(async (c) => {
    const rows = (
      await c.query(
        `SELECT a.id, a.start_time, a.end_time, a.status, a.note,
                d.name AS descriptor, pr.name AS provider, o.name AS operatory
           FROM ext.appointments a
           LEFT JOIN ext.appointment_descriptors d ON d.id = a.descriptor_id
           LEFT JOIN ext.providers pr ON pr.id = a.provider_id
           LEFT JOIN ext.operatories o ON o.id = a.operatory_id
          WHERE a.patient_id = $1
          ORDER BY a.start_time DESC
          LIMIT 50`,
        [patientId],
      )
    ).rows;
    const upcoming = rows.filter((r) => r.status === "booked" || r.status === "confirmed");
    const summary = {
      patient_id: patientId,
      total: rows.length,
      upcoming: upcoming.slice(0, 5).map((r) => ({
        appointment_id: r.id,
        start_time: r.start_time,
        descriptor: r.descriptor,
        provider: r.provider,
        status: r.status,
      })),
    };
    const env = await halo.encode(c, "appointments", summary, { all: rows });
    env.map_root = await halo.accumulate(c, SESSION_ID, patientId, env, { patient_id: patientId });
    return env;
  });
}

// Discovery: the bookable appointment types and their ids. The model needs this
// to resolve a spoken "cleaning" to the descriptor_id the schedule tools expect.
async function listAppointmentTypes(args: Json) {
  const location = (args.location_id as string | undefined) ?? null;
  return withClient(async (c) => {
    const rows = (
      await c.query(
        `SELECT id, name, duration_min, bookable_online, location_id
           FROM ext.appointment_descriptors
          WHERE ($1::text IS NULL OR location_id = $1)
          ORDER BY name`,
        [location],
      )
    ).rows;
    return { appointment_types: rows, count: rows.length };
  });
}

// Open slots are not a table: derive them from provider_availabilities minus
// booked appointments, mirroring how the synchronizer derives appointment_slots.
async function findOpenSlots(args: Json) {
  const descriptorId = String(args.descriptor_id);
  const providerPref = (args.provider as string | undefined) ?? null;
  const timeOfDay = (args.time_of_day as string | undefined)?.toUpperCase() ?? null; // AM | PM
  return withClient(async (c) => {
    const descriptor = (
      await c.query(`SELECT * FROM ext.appointment_descriptors WHERE id = $1`, [descriptorId])
    ).rows[0];
    if (!descriptor) return { error: "descriptor_not_found", descriptor_id: descriptorId };
    const durMs = descriptor.duration_min * 60_000;

    const from = args.from ? new Date(String(args.from)) : new Date();
    const to = args.to ? new Date(String(args.to)) : new Date(from.getTime() + 14 * 24 * 3600 * 1000);

    const avails = (
      await c.query(
        `SELECT pa.*, pr.name AS provider_name, o.name AS operatory_name
           FROM ext.provider_availabilities pa
           JOIN ext.providers pr ON pr.id = pa.provider_id
           JOIN ext.operatories o ON o.id = pa.operatory_id
          WHERE pa.location_id = $1
            AND ($2::text IS NULL OR pa.provider_id = $2 OR pr.name ILIKE '%'||$2||'%')`,
        [descriptor.location_id, providerPref],
      )
    ).rows;

    const booked = (
      await c.query(
        `SELECT operatory_id, start_time, end_time FROM ext.appointments
          WHERE status IN ('booked','confirmed')
            AND start_time < $2 AND end_time > $1`,
        [from.toISOString(), to.toISOString()],
      )
    ).rows.map((r) => ({ op: r.operatory_id, s: new Date(r.start_time).getTime(), e: new Date(r.end_time).getTime() }));

    const overlaps = (op: string, s: number, e: number) =>
      booked.some((b) => b.op === op && s < b.e && e > b.s);

    const slots: any[] = [];
    const dayStart = new Date(Date.UTC(from.getUTCFullYear(), from.getUTCMonth(), from.getUTCDate()));
    for (let d = new Date(dayStart); d <= to && slots.length < 300; d.setUTCDate(d.getUTCDate() + 1)) {
      const weekday = d.getUTCDay();
      const dateStr = d.toISOString().slice(0, 10);
      for (const av of avails) {
        if (av.weekday !== weekday) continue;
        let t = new Date(`${dateStr}T${av.start_time}Z`).getTime();
        const limit = new Date(`${dateStr}T${av.end_time}Z`).getTime();
        for (; t + durMs <= limit; t += durMs) {
          if (t < from.getTime()) continue;
          const hour = new Date(t).getUTCHours();
          if (timeOfDay === "AM" && hour >= 12) continue;
          if (timeOfDay === "PM" && hour < 12) continue;
          if (overlaps(av.operatory_id, t, t + durMs)) continue;
          slots.push({
            start_time: new Date(t).toISOString(),
            end_time: new Date(t + durMs).toISOString(),
            provider_id: av.provider_id,
            provider_name: av.provider_name,
            operatory_id: av.operatory_id,
            location_id: descriptor.location_id,
          });
        }
      }
    }
    slots.sort((a, b) => a.start_time.localeCompare(b.start_time));

    const byDay: Record<string, number> = {};
    const byProvider: Record<string, number> = {};
    for (const s of slots) {
      const day = s.start_time.slice(0, 10);
      byDay[day] = (byDay[day] || 0) + 1;
      byProvider[s.provider_name] = (byProvider[s.provider_name] || 0) + 1;
    }
    const summary = {
      descriptor: descriptor.name,
      duration_min: descriptor.duration_min,
      window: { from: from.toISOString(), to: to.toISOString() },
      n_slots: slots.length,
      by_day: byDay,
      by_provider: byProvider,
      sample: slots.slice(0, 6),
    };
    return halo.encode(c, "open_slots", summary, { all_slots: slots });
  });
}

// ── halo fetch ───────────────────────────────────────────────────────────────
const haloFetch = (args: Json) => withClient((c) => halo.getJson(c, String(args.handle)));
const haloFetchMany = (args: Json) =>
  withClient((c) => halo.getMany(c, (args.handles as string[]) || []));

// ── writes ───────────────────────────────────────────────────────────────────

// Agent-local soft hold. No external write — this is the agent's own double-book
// defense, idempotent on (patient, operatory, start) with a short TTL.
async function holdSlot(args: Json) {
  const patientId = String(args.patient_id);
  const descriptorId = String(args.descriptor_id);
  const startTime = String(args.start_time);
  const providerId = String(args.provider_id);
  const operatoryId = String(args.operatory_id);
  const locationId = String(args.location_id ?? "");
  return withClient(async (c) => {
    const descriptor = (
      await c.query(`SELECT duration_min, location_id FROM ext.appointment_descriptors WHERE id = $1`, [descriptorId])
    ).rows[0];
    if (!descriptor) return { error: "descriptor_not_found", descriptor_id: descriptorId };
    const endTime = new Date(new Date(startTime).getTime() + descriptor.duration_min * 60_000).toISOString();
    const idempotencyKey = `hold:${patientId}:${operatoryId}:${startTime}`;
    const expiresAt = new Date(Date.now() + HOLD_TTL_MIN * 60_000).toISOString();
    const existing = await c.query(
      `SELECT id, status, expires_at FROM agent.booking_holds WHERE idempotency_key = $1`,
      [idempotencyKey],
    );
    if (existing.rowCount && existing.rows[0].status === "held") {
      return { hold_id: existing.rows[0].id, status: "held", expires_at: existing.rows[0].expires_at, idempotent: true };
    }
    const holdId = randomUUID();
    await c.query(
      `INSERT INTO agent.booking_holds
         (id, session_id, patient_ref, provider_ref, location_ref, operatory_ref, descriptor_ref,
          start_time, end_time, expires_at, idempotency_key, status)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'held')
       ON CONFLICT (idempotency_key) DO NOTHING`,
      [holdId, SESSION_ID, patientId, providerId, locationId || descriptor.location_id, operatoryId,
       descriptorId, startTime, endTime, expiresAt, idempotencyKey],
    );
    return { hold_id: holdId, status: "held", start_time: startTime, end_time: endTime, expires_at: expiresAt };
  });
}

// HUMAN-GATED. Commit a held slot into ext.appointments. The no_chair_overlap
// exclusion constraint makes a double-book impossible even under a race.
async function bookAppointment(args: Json) {
  const holdId = String(args.hold_id);
  const hold = await withClient((c) =>
    c.query(`SELECT * FROM agent.booking_holds WHERE id = $1`, [holdId]),
  ).then((r) => r.rows[0]);
  if (!hold) return { committed: false, error: "hold_not_found", hold_id: holdId };
  if (hold.status !== "held") return { committed: false, error: `hold_${hold.status}`, hold_id: holdId };
  if (new Date(hold.expires_at) < new Date()) return { committed: false, error: "hold_expired", hold_id: holdId };

  const idempotencyKey = `book:${holdId}`;
  const decision = await awaitApproval(
    "book",
    { patient_ref: hold.patient_ref, provider_ref: hold.provider_ref, start_time: hold.start_time },
    idempotencyKey,
  );
  if (decision.status !== "approved") {
    return { committed: false, approval_status: decision.status, hold_id: holdId };
  }
  return withClient(async (c) => {
    const apptId = "appt_" + randomUUID().slice(0, 8);
    try {
      await c.query(
        `INSERT INTO ext.appointments
           (id, patient_id, provider_id, location_id, operatory_id, descriptor_id, start_time, end_time, status)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'booked')`,
        [apptId, hold.patient_ref, hold.provider_ref, hold.location_ref, hold.operatory_ref,
         hold.descriptor_ref, hold.start_time, hold.end_time],
      );
    } catch (err: any) {
      if (String(err?.constraint) === "no_chair_overlap" || /no_chair_overlap/.test(String(err?.message))) {
        await c.query(`UPDATE agent.booking_holds SET status='released' WHERE id=$1`, [holdId]);
        return { committed: false, error: "slot_taken", hold_id: holdId };
      }
      throw err;
    }
    await c.query(`UPDATE agent.booking_holds SET status='committed' WHERE id=$1`, [holdId]);
    await c.query(
      `INSERT INTO agent.bookings (id, hold_id, external_appt_id, status) VALUES ($1,$2,$3,'confirmed')`,
      [randomUUID(), holdId, apptId],
    );
    return {
      committed: true,
      approval_status: "approved",
      decided_by: decision.decided_by,
      external_appt_id: apptId,
      start_time: hold.start_time,
    };
  });
}

// HUMAN-GATED.
async function reschedule(args: Json) {
  const apptId = String(args.appointment_id);
  const slot = (args.new_slot as Json) ?? args;
  const newStart = String(slot.start_time);
  return withClient(async (c) => {
    const appt = (await c.query(
      `SELECT a.*, d.duration_min FROM ext.appointments a
         LEFT JOIN ext.appointment_descriptors d ON d.id = a.descriptor_id WHERE a.id = $1`,
      [apptId],
    )).rows[0];
    if (!appt) return { committed: false, error: "appointment_not_found", appointment_id: apptId };
    const durMs = (appt.duration_min ?? 60) * 60_000;
    const newEnd = slot.end_time ? String(slot.end_time) : new Date(new Date(newStart).getTime() + durMs).toISOString();
    const newOperatory = (slot.operatory_id as string | undefined) ?? appt.operatory_id;

    const decision = await awaitApproval(
      "reschedule",
      { appointment_id: apptId, from: appt.start_time, to: newStart },
      `reschedule:${apptId}:${newStart}`,
    );
    if (decision.status !== "approved") return { committed: false, approval_status: decision.status, appointment_id: apptId };

    try {
      const r = await c.query(
        `UPDATE ext.appointments SET start_time=$2, end_time=$3, operatory_id=$4 WHERE id=$1
         RETURNING id, start_time, end_time, operatory_id`,
        [apptId, newStart, newEnd, newOperatory],
      );
      return { committed: true, approval_status: "approved", decided_by: decision.decided_by, ...r.rows[0] };
    } catch (err: any) {
      if (/no_chair_overlap/.test(String(err?.message)) || String(err?.constraint) === "no_chair_overlap") {
        return { committed: false, error: "slot_taken", appointment_id: apptId };
      }
      throw err;
    }
  });
}

// HUMAN-GATED.
async function cancel(args: Json) {
  const apptId = String(args.appointment_id);
  const reason = String(args.reason ?? "");
  const decision = await awaitApproval("cancel", { appointment_id: apptId, reason }, `cancel:${apptId}`);
  if (decision.status !== "approved") return { committed: false, approval_status: decision.status, appointment_id: apptId };
  return withClient(async (c) => {
    const r = await c.query(
      `UPDATE ext.appointments SET status='cancelled', note=$2 WHERE id=$1 RETURNING id, status`,
      [apptId, reason],
    );
    return r.rowCount
      ? { committed: true, approval_status: "approved", decided_by: decision.decided_by, ...r.rows[0] }
      : { committed: false, error: "appointment_not_found", appointment_id: apptId };
  });
}

// HUMAN-GATED.
async function updateContact(args: Json) {
  await requireIdentity();
  const patientId = String(args.patient_id);
  const fields = (args.fields as Json) ?? {};
  const allowed = ["phone", "email", "address"] as const;
  const sets: string[] = [];
  const vals: unknown[] = [patientId];
  for (const k of allowed) {
    if (fields[k] !== undefined) {
      vals.push(fields[k]);
      sets.push(`${k} = $${vals.length}`);
    }
  }
  if (!sets.length) return { committed: false, error: "no_updatable_fields" };
  const decision = await awaitApproval("update_contact", { patient_id: patientId, fields }, `contact:${patientId}:${JSON.stringify(fields)}`);
  if (decision.status !== "approved") return { committed: false, approval_status: decision.status, patient_id: patientId };
  return withClient(async (c) => {
    const r = await c.query(
      `UPDATE ext.patients SET ${sets.join(", ")}, updated_at = now() WHERE id = $1 RETURNING id`,
      vals,
    );
    return r.rowCount
      ? { committed: true, approval_status: "approved", decided_by: decision.decided_by, patient_id: patientId, updated_fields: Object.keys(fields) }
      : { committed: false, error: "patient_not_found", patient_id: patientId };
  });
}

// ── low-risk writes ───────────────────────────────────────────────────────────
async function addToWaitlist(args: Json) {
  const patientId = String(args.patient_id);
  const descriptorId = String(args.descriptor_id);
  return withClient(async (c) => {
    const id = "wl_" + randomUUID().slice(0, 8);
    await c.query(
      `INSERT INTO ext.waitlist (id, patient_id, descriptor_id, provider_pref, window_pref)
       VALUES ($1,$2,$3,$4,$5)`,
      [id, patientId, descriptorId, args.provider_pref ?? null, args.window_pref ?? null],
    );
    return { waitlist_id: id, added: true };
  });
}

async function confirmAppointment(args: Json) {
  const apptId = String(args.appointment_id);
  return withClient(async (c) => {
    const r = await c.query(
      `UPDATE ext.appointments SET status='confirmed' WHERE id=$1 AND status='booked' RETURNING id, status`,
      [apptId],
    );
    return r.rowCount ? { ...r.rows[0], confirmed: true } : { error: "not_confirmable", appointment_id: apptId };
  });
}

// Batch/admin read for the recalls-and-reminders skill: patients overdue for a
// recall, or upcoming appointments that still need confirmation.
async function findRecalls(args: Json) {
  const kind = (args.kind as string | undefined) ?? "recall"; // recall | reminder
  return withClient(async (c) => {
    if (kind === "reminder") {
      const rows = (
        await c.query(
          `SELECT a.id AS appointment_id, a.start_time, p.id AS patient_id,
                  p.first_name, p.last_name, p.phone
             FROM ext.appointments a JOIN ext.patients p ON p.id = a.patient_id
            WHERE a.status = 'booked' AND a.start_time BETWEEN now() AND now() + interval '3 days'
            ORDER BY a.start_time LIMIT 100`,
        )
      ).rows;
      return { kind, count: rows.length, items: rows.map((r) => ({ ...r, phone: maskPhone(r.phone) })) };
    }
    const rows = (
      await c.query(
        `SELECT id AS patient_id, first_name, last_name, phone, recall_due
           FROM ext.patients
          WHERE inactive = false AND recall_due IS NOT NULL AND recall_due <= current_date
          ORDER BY recall_due LIMIT 100`,
      )
    ).rows;
    return { kind, count: rows.length, items: rows.map((r) => ({ ...r, phone: maskPhone(r.phone) })) };
  });
}

// ── tool catalogue (metadata + dispatch) ─────────────────────────────────────
type Handler = (args: Json) => Promise<unknown>;
const HANDLERS: Record<string, Handler> = {
  find_patient: findPatient,
  verify_identity: verifyIdentity,
  get_patient_summary: getPatientSummary,
  check_coverage: checkCoverage,
  get_appointments: getAppointments,
  list_appointment_types: listAppointmentTypes,
  find_open_slots: findOpenSlots,
  find_recalls: findRecalls,
  halo_fetch: haloFetch,
  halo_fetch_many: haloFetchMany,
  hold_slot: holdSlot,
  book_appointment: bookAppointment,
  reschedule: reschedule,
  cancel: cancel,
  update_contact: updateContact,
  add_to_waitlist: addToWaitlist,
  confirm_appointment: confirmAppointment,
};

const TOOLS: Tool[] = [
  {
    name: "find_patient",
    description:
      "Find a patient by phone, name+dob, or email. Returns only THIN candidates (name + masked phone) — enough to start identity verification, no PHI. Pass {query} for a phone/email, or {last_name, dob}.",
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string", description: "phone number or email" },
        last_name: { type: "string" },
        dob: { type: "string", description: "YYYY-MM-DD" },
        email: { type: "string" },
        phone: { type: "string" },
      },
    },
    annotations: { readOnlyHint: true, openWorldHint: true },
  },
  {
    name: "verify_identity",
    description:
      "IDENTITY GATE. Confirm a caller's last_name + dob against the record and mark the session verified. Returns only a boolean — no PHI. Must succeed before get_patient_summary / check_coverage / get_appointments will return anything.",
    inputSchema: {
      type: "object",
      properties: {
        patient_id: { type: "string" },
        last_name: { type: "string" },
        dob: { type: "string", description: "YYYY-MM-DD" },
      },
      required: ["patient_id", "last_name", "dob"],
    },
    annotations: { readOnlyHint: false, idempotentHint: true },
  },
  {
    name: "get_patient_summary",
    description:
      "Fetch a patient's reception-relevant record (contact, balance, recall, insurance status). REQUIRES a verified session. Returns a Halo envelope; `refs` carve out contact / insurance / clinical — never fetch `clinical` for reception work. Keyed into the patient map.",
    inputSchema: { type: "object", properties: { patient_id: { type: "string" } }, required: ["patient_id"] },
    annotations: { readOnlyHint: true },
  },
  {
    name: "check_coverage",
    description:
      "Eligibility + copay for one appointment type (hides the insurance lookup). REQUIRES a verified session.",
    inputSchema: {
      type: "object",
      properties: { patient_id: { type: "string" }, descriptor_id: { type: "string" } },
      required: ["patient_id", "descriptor_id"],
    },
    annotations: { readOnlyHint: true },
  },
  {
    name: "get_appointments",
    description:
      "A patient's appointments (Halo envelope: summary of upcoming + `all` handle). REQUIRES a verified session.",
    inputSchema: { type: "object", properties: { patient_id: { type: "string" } }, required: ["patient_id"] },
    annotations: { readOnlyHint: true },
  },
  {
    name: "list_appointment_types",
    description:
      "List the practice's bookable appointment types (id, name, duration, bookable_online). Call this to resolve a spoken type like \"cleaning\" to the descriptor_id that check_coverage / find_open_slots / hold_slot expect.",
    inputSchema: {
      type: "object",
      properties: { location_id: { type: "string" } },
    },
    annotations: { readOnlyHint: true, openWorldHint: true },
  },
  {
    name: "find_open_slots",
    description:
      "Derive open slots for an appointment type (availabilities minus booked appointments — mirrors NexHealth appointment_slots). Returns a Halo envelope: summary with `by_day` / `by_provider` counts + a sample, and an `all_slots` handle. Walk to the day/provider the caller wants; don't fetch the whole grid.",
    inputSchema: {
      type: "object",
      properties: {
        descriptor_id: { type: "string", description: "appointment type to book" },
        from: { type: "string", description: "ISO window start (default: now)" },
        to: { type: "string", description: "ISO window end (default: +14 days)" },
        provider: { type: "string", description: "provider id or name preference" },
        time_of_day: { type: "string", enum: ["AM", "PM"] },
      },
      required: ["descriptor_id"],
    },
    annotations: { readOnlyHint: true, openWorldHint: true },
  },
  {
    name: "find_recalls",
    description:
      "Batch/admin read for recalls-and-reminders: patients overdue for recall (kind='recall') or upcoming appointments needing confirmation (kind='reminder'). Returns thin outreach candidates with masked phones.",
    inputSchema: {
      type: "object",
      properties: { kind: { type: "string", enum: ["recall", "reminder"] } },
    },
    annotations: { readOnlyHint: true, openWorldHint: true },
  },
  {
    name: "halo_fetch",
    description: "Fetch the decoded content behind one Halo handle (h:sha256:...).",
    inputSchema: { type: "object", properties: { handle: { type: "string" } }, required: ["handle"] },
    annotations: { readOnlyHint: true },
  },
  {
    name: "halo_fetch_many",
    description: "Fetch many Halo handles in one round trip (batched drill-down).",
    inputSchema: {
      type: "object",
      properties: { handles: { type: "array", items: { type: "string" } } },
      required: ["handles"],
    },
    annotations: { readOnlyHint: true },
  },
  {
    name: "hold_slot",
    description:
      "Reserve a slot with a short-TTL agent-local hold (no external write; the double-book defense). Idempotent on (patient, operatory, start). Returns a hold_id to pass to book_appointment.",
    inputSchema: {
      type: "object",
      properties: {
        patient_id: { type: "string" },
        descriptor_id: { type: "string" },
        start_time: { type: "string", description: "ISO start of the chosen slot" },
        provider_id: { type: "string" },
        operatory_id: { type: "string" },
        location_id: { type: "string" },
      },
      required: ["patient_id", "descriptor_id", "start_time", "provider_id", "operatory_id"],
    },
    annotations: { readOnlyHint: false, idempotentHint: true },
  },
  {
    name: "book_appointment",
    description:
      "Commit a held slot into the schedule. HUMAN-GATED: proposes to agent.approvals and BLOCKS until a human confirms. The DB exclusion constraint guarantees no double-book; on a lost race returns committed=false, error='slot_taken'.",
    inputSchema: { type: "object", properties: { hold_id: { type: "string" } }, required: ["hold_id"] },
    annotations: { readOnlyHint: false, idempotentHint: true, openWorldHint: true },
  },
  {
    name: "reschedule",
    description:
      "Move an appointment to a new slot. HUMAN-GATED. Pass {appointment_id, new_slot:{start_time, operatory_id?}}.",
    inputSchema: {
      type: "object",
      properties: {
        appointment_id: { type: "string" },
        new_slot: {
          type: "object",
          properties: { start_time: { type: "string" }, end_time: { type: "string" }, operatory_id: { type: "string" } },
          required: ["start_time"],
        },
      },
      required: ["appointment_id", "new_slot"],
    },
    annotations: { readOnlyHint: false, idempotentHint: true, openWorldHint: true },
  },
  {
    name: "cancel",
    description: "Cancel an appointment with a reason. HUMAN-GATED.",
    inputSchema: {
      type: "object",
      properties: { appointment_id: { type: "string" }, reason: { type: "string" } },
      required: ["appointment_id", "reason"],
    },
    annotations: { readOnlyHint: false, idempotentHint: true, openWorldHint: true },
  },
  {
    name: "update_contact",
    description:
      "Update a patient's contact details (phone / email / address). REQUIRES a verified session and is HUMAN-GATED.",
    inputSchema: {
      type: "object",
      properties: {
        patient_id: { type: "string" },
        fields: {
          type: "object",
          properties: { phone: { type: "string" }, email: { type: "string" }, address: { type: "string" } },
        },
      },
      required: ["patient_id", "fields"],
    },
    annotations: { readOnlyHint: false, idempotentHint: true, openWorldHint: true },
  },
  {
    name: "add_to_waitlist",
    description: "Add a patient to the waitlist for an appointment type (low risk; direct write).",
    inputSchema: {
      type: "object",
      properties: {
        patient_id: { type: "string" },
        descriptor_id: { type: "string" },
        provider_pref: { type: "string" },
        window_pref: { type: "string", description: "e.g. 'Tue/Thu PM'" },
      },
      required: ["patient_id", "descriptor_id"],
    },
    annotations: { readOnlyHint: false, idempotentHint: true },
  },
  {
    name: "confirm_appointment",
    description: "Mark a booked appointment confirmed (patient confirming a reminder; low risk, direct).",
    inputSchema: { type: "object", properties: { appointment_id: { type: "string" } }, required: ["appointment_id"] },
    annotations: { readOnlyHint: false, idempotentHint: true },
  },
];

// ── server wiring ────────────────────────────────────────────────────────────
const server = new Server({ name: "dental-mcp", version: "0.1.0" }, { capabilities: { tools: {} } });

server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));

server.setRequestHandler(CallToolRequestSchema, async (req) => {
  const name = req.params.name;
  const args = (req.params.arguments ?? {}) as Json;
  const handler = HANDLERS[name];
  if (!handler) return { isError: true, content: [{ type: "text", text: `unknown tool: ${name}` }] };

  const started = Date.now();
  try {
    const out = await handler(args);
    const envelopeRoot =
      out && typeof out === "object" && "map_root" in (out as any)
        ? ((out as any).map_root as string)
        : out && typeof out === "object" && "refs" in (out as any)
          ? Object.values((out as any).refs)[0] ?? null
          : null;
    await recordToolCall(name, args, envelopeRoot as string | null, Date.now() - started, true, null);
    return result(out);
  } catch (err: any) {
    const msg = err instanceof IdentityRequired ? "identity_required" : String(err?.message ?? err);
    await recordToolCall(name, args, null, Date.now() - started, false, msg);
    if (err instanceof IdentityRequired) {
      return result({ error: "identity_required", note: "Call verify_identity(patient_id, last_name, dob) first." });
    }
    return { isError: true, content: [{ type: "text", text: `error in ${name}: ${msg}` }] };
  }
});

async function main() {
  await ensureSession();
  await server.connect(new StdioServerTransport());
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
