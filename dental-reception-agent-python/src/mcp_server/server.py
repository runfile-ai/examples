"""dental-mcp — stdio MCP server (Python).

Intent-shaped tools over the local Postgres. The tool CONTRACT is the swap point:
today each body is SQL on ext.*; later it is a call to the NexHealth synchronizer.
Signatures and returned shapes do not change.

Two gates sit in front of sensitive work:
  • an IDENTITY gate — get_patient_summary / check_coverage / get_appointments
    refuse until the session is verified (verify_identity sets identity_ok);
  • a WRITE gate — book_appointment / reschedule / cancel / update_contact route
    through agent.approvals and BLOCK until a human confirms.

Heavy reads return Halo envelopes (compact summary + handles); the agent drills in
with halo_fetch / halo_fetch_many. Every call is recorded in agent.tool_calls.

Run as a module:  python -m src.mcp_server.server
"""
from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Awaitable, Callable

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from . import halo
from .db import dumps, get_pool

# ── session (one agent run / call) ────────────────────────────────────────────
SESSION_ID = uuid.UUID(os.environ.get("AGENT_SESSION_ID") or str(uuid.uuid4()))
CHANNEL = os.environ.get("DENTAL_CHANNEL", "voice")
HOLD_TTL_MIN = int(os.environ.get("HOLD_TTL_MINUTES", "15"))


class IdentityRequired(Exception):
    pass


def _parse_ts(s: Any) -> datetime:
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _mask_phone(p: str | None) -> str | None:
    if not p:
        return None
    return re.sub(r".(?=.{4})", "•", p)


async def ensure_session() -> None:
    pool = await get_pool()
    async with pool.acquire() as c:
        await c.execute(
            "INSERT INTO agent.sessions (id, channel, status) VALUES ($1, $2, 'active') "
            "ON CONFLICT (id) DO NOTHING",
            SESSION_ID,
            CHANNEL,
        )


async def _identity_ok() -> bool:
    pool = await get_pool()
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT identity_ok FROM agent.sessions WHERE id = $1", SESSION_ID)
    return bool(row and row["identity_ok"])


async def _require_identity() -> None:
    if not await _identity_ok():
        raise IdentityRequired()


async def _record_tool_call(tool: str, args: Any, env_root: str | None, latency_ms: int, ok: bool, error: str | None) -> None:
    try:
        pool = await get_pool()
        async with pool.acquire() as c:
            await c.execute(
                "INSERT INTO agent.tool_calls (id, session_id, tool, args, envelope_root, latency_ms, ok, error) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
                uuid.uuid4(),
                SESSION_ID,
                tool,
                args or {},
                env_root,
                latency_ms,
                ok,
                error,
            )
    except Exception:
        pass  # observability must never break a tool call


async def _await_approval(action: str, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
    """Block until a pending approval is resolved (or times out)."""
    timeout_s = int(os.environ.get("APPROVAL_TIMEOUT_SECONDS", "900"))
    poll_s = int(os.environ.get("APPROVAL_POLL_SECONDS", "2"))
    pool = await get_pool()

    async with pool.acquire() as c:
        existing = await c.fetchrow(
            "SELECT id, status, decided_by FROM agent.approvals WHERE idempotency_key = $1", idempotency_key
        )
        if existing and existing["status"] != "pending":
            return dict(existing)  # idempotent replay
        if existing:
            approval_id = existing["id"]
        else:
            approval_id = uuid.uuid4()
            await c.execute(
                "INSERT INTO agent.approvals (id, session_id, action, payload, idempotency_key, status) "
                "VALUES ($1,$2,$3,$4,$5,'pending') ON CONFLICT (idempotency_key) DO NOTHING",
                approval_id,
                SESSION_ID,
                action,
                payload,
                idempotency_key,
            )

    waited = 0
    while waited < timeout_s:
        async with pool.acquire() as c:
            r = await c.fetchrow("SELECT id, status, decided_by FROM agent.approvals WHERE id = $1", approval_id)
        if r and r["status"] != "pending":
            return dict(r)
        await asyncio.sleep(poll_s)
        waited += poll_s
    return {"id": approval_id, "status": "timeout", "decided_by": None}


# ── reads ─────────────────────────────────────────────────────────────────────
async def find_patient(args: dict[str, Any]) -> dict[str, Any]:
    query = args.get("query")
    last_name = args.get("last_name")
    dob = args.get("dob")
    email = args.get("email")
    phone = args.get("phone") or query
    pool = await get_pool()
    async with pool.acquire() as c:
        rows = await c.fetch(
            r"""SELECT id, first_name, last_name, phone
                   FROM ext.patients
                  WHERE inactive = false
                    AND ( ($1::text IS NOT NULL AND length(regexp_replace($1,'\D','','g')) >= 7
                           AND right(regexp_replace(phone,'\D','','g'), 10) = right(regexp_replace($1,'\D','','g'), 10))
                       OR ($2::text IS NOT NULL AND lower(last_name) = lower($2) AND ($3::date IS NULL OR dob = $3))
                       OR ($4::text IS NOT NULL AND lower(email) = lower($4))
                       OR ($5::text IS NOT NULL AND lower(email) = lower($5)) )
                  ORDER BY last_name, first_name
                  LIMIT 5""",
            phone,
            last_name,
            date.fromisoformat(dob) if dob else None,
            email,
            query,
        )
    return {
        "candidates": [
            {"patient_id": r["id"], "first_name": r["first_name"], "last_name": r["last_name"], "phone": _mask_phone(r["phone"])}
            for r in rows
        ],
        "count": len(rows),
        "note": "Verify with verify_identity(patient_id, last_name, dob) before disclosing any detail.",
    }


async def verify_identity(args: dict[str, Any]) -> dict[str, Any]:
    patient_id = str(args["patient_id"])
    last_name = str(args.get("last_name", ""))
    dob = str(args.get("dob", ""))
    pool = await get_pool()
    async with pool.acquire() as c:
        r = await c.fetchrow(
            "SELECT id, foreign_id FROM ext.patients "
            "WHERE id = $1 AND lower(last_name) = lower($2) AND dob = $3 AND inactive = false",
            patient_id,
            last_name,
            date.fromisoformat(dob) if dob else None,
        )
        if not r:
            return {"verified": False, "reason": "name_or_dob_mismatch"}
        await c.execute(
            "UPDATE agent.sessions SET identity_ok = true, patient_ref = $2 WHERE id = $1",
            SESSION_ID,
            r["foreign_id"] or patient_id,
        )
    return {"verified": True, "patient_id": patient_id}


async def get_patient_summary(args: dict[str, Any]) -> halo.Envelope:
    await _require_identity()
    patient_id = str(args["patient_id"])
    pool = await get_pool()
    async with pool.acquire() as c:
        p = await c.fetchrow("SELECT * FROM ext.patients WHERE id = $1", patient_id)
        if not p:
            return halo.Envelope(kind="error", summary={"error": "patient_not_found", "patient_id": patient_id})
        coverages = [dict(x) for x in await c.fetch("SELECT * FROM ext.insurance_coverages WHERE patient_id = $1", patient_id)]
        upcoming = await c.fetchval(
            "SELECT count(*)::int FROM ext.appointments "
            "WHERE patient_id = $1 AND status IN ('booked','confirmed') AND start_time >= now()",
            patient_id,
        )
        active_ins = next((x for x in coverages if x["eligibility"] == "active"), None)
        recall_due = p["recall_due"]
        summary = {
            "patient_id": p["id"],
            "name": f"{p['first_name']} {p['last_name']}",
            "recall_due": recall_due,
            "recall_overdue": (recall_due < date.today()) if recall_due else False,
            "balance_cents": p["balance_cents"],
            "insurance": {"carrier": active_ins["carrier"], "eligibility": active_ins["eligibility"]}
            if active_ins
            else {"eligibility": coverages[0]["eligibility"] if coverages else "unknown"},
            "upcoming_appointments": upcoming,
        }
        env = await halo.encode(
            c,
            "patient_summary",
            summary,
            {
                "contact": {"email": p["email"], "phone": p["phone"], "address": p["address"]},
                "insurance": coverages,
                "clinical": {"note": "clinical chart withheld — not needed for reception", "chart_ref": p["foreign_id"]},
            },
        )
        env.map_root = await halo.accumulate(c, SESSION_ID, patient_id, env, {"patient_id": patient_id})
    return env


async def check_coverage(args: dict[str, Any]) -> dict[str, Any]:
    await _require_identity()
    patient_id = str(args["patient_id"])
    descriptor_id = str(args["descriptor_id"])
    pool = await get_pool()
    async with pool.acquire() as c:
        cov = await c.fetchrow(
            "SELECT eligibility, coverage_pct, copay_cents, carrier, plan_name "
            "FROM ext.insurance_coverages WHERE patient_id = $1 "
            "ORDER BY (eligibility = 'active') DESC LIMIT 1",
            patient_id,
        )
        descriptor = await c.fetchrow("SELECT name FROM ext.appointment_descriptors WHERE id = $1", descriptor_id)
    name = descriptor["name"] if descriptor else descriptor_id
    if not cov:
        return {"patient_id": patient_id, "eligibility": "unknown", "descriptor": name}
    return {
        "patient_id": patient_id,
        "descriptor": name,
        "eligibility": cov["eligibility"],
        "coverage_pct": cov["coverage_pct"],
        "copay_cents": cov["copay_cents"],
        "carrier": cov["carrier"],
        "plan_name": cov["plan_name"],
    }


async def get_appointments(args: dict[str, Any]) -> halo.Envelope:
    await _require_identity()
    patient_id = str(args["patient_id"])
    pool = await get_pool()
    async with pool.acquire() as c:
        rows = [
            dict(r)
            for r in await c.fetch(
                """SELECT a.id, a.start_time, a.end_time, a.status, a.note,
                          d.name AS descriptor, pr.name AS provider, o.name AS operatory
                     FROM ext.appointments a
                     LEFT JOIN ext.appointment_descriptors d ON d.id = a.descriptor_id
                     LEFT JOIN ext.providers pr ON pr.id = a.provider_id
                     LEFT JOIN ext.operatories o ON o.id = a.operatory_id
                    WHERE a.patient_id = $1
                    ORDER BY a.start_time DESC
                    LIMIT 50""",
                patient_id,
            )
        ]
        upcoming = [r for r in rows if r["status"] in ("booked", "confirmed")]
        summary = {
            "patient_id": patient_id,
            "total": len(rows),
            "upcoming": [
                {
                    "appointment_id": r["id"],
                    "start_time": r["start_time"],
                    "descriptor": r["descriptor"],
                    "provider": r["provider"],
                    "status": r["status"],
                }
                for r in upcoming[:5]
            ],
        }
        env = await halo.encode(c, "appointments", summary, {"all": rows})
        env.map_root = await halo.accumulate(c, SESSION_ID, patient_id, env, {"patient_id": patient_id})
    return env


async def list_appointment_types(args: dict[str, Any]) -> dict[str, Any]:
    location = args.get("location_id")
    pool = await get_pool()
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT id, name, duration_min, bookable_online, location_id "
            "FROM ext.appointment_descriptors WHERE ($1::text IS NULL OR location_id = $1) ORDER BY name",
            location,
        )
    return {"appointment_types": [dict(r) for r in rows], "count": len(rows)}


async def find_open_slots(args: dict[str, Any]) -> halo.Envelope | dict[str, Any]:
    descriptor_id = str(args["descriptor_id"])
    provider_pref = args.get("provider")
    time_of_day = (args.get("time_of_day") or "").upper() or None
    pool = await get_pool()
    async with pool.acquire() as c:
        descriptor = await c.fetchrow("SELECT * FROM ext.appointment_descriptors WHERE id = $1", descriptor_id)
        if not descriptor:
            return {"error": "descriptor_not_found", "descriptor_id": descriptor_id}
        dur = timedelta(minutes=descriptor["duration_min"])

        frm = _parse_ts(args["from"]) if args.get("from") else datetime.now(timezone.utc)
        to = _parse_ts(args["to"]) if args.get("to") else frm + timedelta(days=14)

        all_avails = [
            dict(r)
            for r in await c.fetch(
                """SELECT pa.*, pr.name AS provider_name, o.name AS operatory_name
                     FROM ext.provider_availabilities pa
                     JOIN ext.providers pr ON pr.id = pa.provider_id
                     JOIN ext.operatories o ON o.id = pa.operatory_id
                    WHERE pa.location_id = $1""",
                descriptor["location_id"],
            )
        ]
        booked = [
            (r["operatory_id"], _parse_ts(r["start_time"]), _parse_ts(r["end_time"]))
            for r in await c.fetch(
                "SELECT operatory_id, start_time, end_time FROM ext.appointments "
                "WHERE status IN ('booked','confirmed') AND start_time < $2 AND end_time > $1",
                frm,
                to,
            )
        ]

    # Forgiving provider match: id, or every significant token of a spoken name
    # appears in the provider's full name ("Dr. Nguyen" matches "Dr. Alice Nguyen").
    stop = {"dr", "doctor", "mr", "mrs", "ms", "the"}
    pref_tokens = [t for t in re.sub(r"\.", "", (provider_pref or "").lower()).split() if t and t not in stop]

    def matches(av: dict[str, Any]) -> bool:
        if not provider_pref:
            return True
        if av["provider_id"] == provider_pref:
            return True
        name = av["provider_name"].lower()
        return len(pref_tokens) > 0 and all(t in name for t in pref_tokens)

    avails = [a for a in all_avails if matches(a)]

    def overlaps(op: str, s: datetime, e: datetime) -> bool:
        return any(b_op == op and s < b_e and e > b_s for (b_op, b_s, b_e) in booked)

    slots: list[dict[str, Any]] = []
    d = frm.date()
    while datetime(d.year, d.month, d.day, tzinfo=timezone.utc) <= to and len(slots) < 300:
        weekday = d.isoweekday() % 7  # Sunday=0 .. Saturday=6 (matches the seed)
        for av in avails:
            if av["weekday"] != weekday:
                continue
            start_t: dtime = av["start_time"]
            end_t: dtime = av["end_time"]
            t = datetime.combine(d, start_t, tzinfo=timezone.utc)
            limit = datetime.combine(d, end_t, tzinfo=timezone.utc)
            while t + dur <= limit:
                if t < frm:
                    t += dur
                    continue
                hour = t.hour
                if time_of_day == "AM" and hour >= 12:
                    t += dur
                    continue
                if time_of_day == "PM" and hour < 12:
                    t += dur
                    continue
                if not overlaps(av["operatory_id"], t, t + dur):
                    slots.append(
                        {
                            "start_time": t.isoformat(),
                            "end_time": (t + dur).isoformat(),
                            "provider_id": av["provider_id"],
                            "provider_name": av["provider_name"],
                            "operatory_id": av["operatory_id"],
                            "location_id": descriptor["location_id"],
                        }
                    )
                t += dur
        d = d + timedelta(days=1)

    slots.sort(key=lambda s: s["start_time"])
    by_day: dict[str, int] = {}
    by_provider: dict[str, int] = {}
    for s in slots:
        day = s["start_time"][:10]
        by_day[day] = by_day.get(day, 0) + 1
        by_provider[s["provider_name"]] = by_provider.get(s["provider_name"], 0) + 1
    summary = {
        "descriptor": descriptor["name"],
        "duration_min": descriptor["duration_min"],
        "window": {"from": frm.isoformat(), "to": to.isoformat()},
        "n_slots": len(slots),
        "by_day": by_day,
        "by_provider": by_provider,
        "sample": slots[:6],
    }
    pool = await get_pool()
    async with pool.acquire() as c:
        return await halo.encode(c, "open_slots", summary, {"all_slots": slots})


async def find_recalls(args: dict[str, Any]) -> dict[str, Any]:
    kind = args.get("kind", "recall")
    pool = await get_pool()
    async with pool.acquire() as c:
        if kind == "reminder":
            rows = await c.fetch(
                """SELECT a.id AS appointment_id, a.start_time, p.id AS patient_id,
                          p.first_name, p.last_name, p.phone
                     FROM ext.appointments a JOIN ext.patients p ON p.id = a.patient_id
                    WHERE a.status = 'booked' AND a.start_time BETWEEN now() AND now() + interval '3 days'
                    ORDER BY a.start_time LIMIT 100"""
            )
        else:
            rows = await c.fetch(
                """SELECT id AS patient_id, first_name, last_name, phone, recall_due
                     FROM ext.patients
                    WHERE inactive = false AND recall_due IS NOT NULL AND recall_due <= current_date
                    ORDER BY recall_due LIMIT 100"""
            )
    items = []
    for r in rows:
        d = dict(r)
        d["phone"] = _mask_phone(d.get("phone"))
        items.append(d)
    return {"kind": kind, "count": len(items), "items": items}


# ── halo fetch ────────────────────────────────────────────────────────────────
async def halo_fetch(args: dict[str, Any]) -> Any:
    pool = await get_pool()
    async with pool.acquire() as c:
        return await halo.get_json(c, str(args["handle"]))


async def halo_fetch_many(args: dict[str, Any]) -> Any:
    pool = await get_pool()
    async with pool.acquire() as c:
        return await halo.get_many(c, list(args.get("handles", [])))


# ── writes ────────────────────────────────────────────────────────────────────
async def hold_slot(args: dict[str, Any]) -> dict[str, Any]:
    patient_id = str(args["patient_id"])
    descriptor_id = str(args["descriptor_id"])
    start_time = _parse_ts(args["start_time"])
    provider_id = str(args["provider_id"])
    operatory_id = str(args["operatory_id"])
    location_id = str(args.get("location_id") or "")
    pool = await get_pool()
    async with pool.acquire() as c:
        descriptor = await c.fetchrow("SELECT duration_min, location_id FROM ext.appointment_descriptors WHERE id = $1", descriptor_id)
        if not descriptor:
            return {"error": "descriptor_not_found", "descriptor_id": descriptor_id}
        end_time = start_time + timedelta(minutes=descriptor["duration_min"])
        idempotency_key = f"hold:{patient_id}:{operatory_id}:{start_time.isoformat()}"
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=HOLD_TTL_MIN)
        existing = await c.fetchrow(
            "SELECT id, status, expires_at FROM agent.booking_holds WHERE idempotency_key = $1", idempotency_key
        )
        if existing and existing["status"] == "held":
            return {"hold_id": str(existing["id"]), "status": "held", "expires_at": existing["expires_at"], "idempotent": True}
        hold_id = uuid.uuid4()
        await c.execute(
            """INSERT INTO agent.booking_holds
                 (id, session_id, patient_ref, provider_ref, location_ref, operatory_ref, descriptor_ref,
                  start_time, end_time, expires_at, idempotency_key, status)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'held')
               ON CONFLICT (idempotency_key) DO NOTHING""",
            hold_id,
            SESSION_ID,
            patient_id,
            provider_id,
            location_id or descriptor["location_id"],
            operatory_id,
            descriptor_id,
            start_time,
            end_time,
            expires_at,
            idempotency_key,
        )
    return {"hold_id": str(hold_id), "status": "held", "start_time": start_time, "end_time": end_time, "expires_at": expires_at}


async def book_appointment(args: dict[str, Any]) -> dict[str, Any]:
    hold_id = str(args["hold_id"])
    pool = await get_pool()
    async with pool.acquire() as c:
        hold = await c.fetchrow("SELECT * FROM agent.booking_holds WHERE id = $1", uuid.UUID(hold_id))
    if not hold:
        return {"committed": False, "error": "hold_not_found", "hold_id": hold_id}
    if hold["status"] != "held":
        return {"committed": False, "error": f"hold_{hold['status']}", "hold_id": hold_id}
    if _parse_ts(hold["expires_at"]) < datetime.now(timezone.utc):
        return {"committed": False, "error": "hold_expired", "hold_id": hold_id}

    decision = await _await_approval(
        "book",
        {"patient_ref": hold["patient_ref"], "provider_ref": hold["provider_ref"], "start_time": hold["start_time"].isoformat()},
        f"book:{hold_id}",
    )
    if decision["status"] != "approved":
        return {"committed": False, "approval_status": decision["status"], "hold_id": hold_id}

    appt_id = "appt_" + uuid.uuid4().hex[:8]
    async with pool.acquire() as c:
        try:
            await c.execute(
                """INSERT INTO ext.appointments
                     (id, patient_id, provider_id, location_id, operatory_id, descriptor_id, start_time, end_time, status)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'booked')""",
                appt_id,
                hold["patient_ref"],
                hold["provider_ref"],
                hold["location_ref"],
                hold["operatory_ref"],
                hold["descriptor_ref"],
                hold["start_time"],
                hold["end_time"],
            )
        except Exception as err:  # exclusion constraint → the chair was taken
            if "no_chair_overlap" in str(err):
                await c.execute("UPDATE agent.booking_holds SET status='released' WHERE id=$1", uuid.UUID(hold_id))
                return {"committed": False, "error": "slot_taken", "hold_id": hold_id}
            raise
        await c.execute("UPDATE agent.booking_holds SET status='committed' WHERE id=$1", uuid.UUID(hold_id))
        await c.execute(
            "INSERT INTO agent.bookings (id, hold_id, external_appt_id, status) VALUES ($1,$2,$3,'confirmed')",
            uuid.uuid4(),
            uuid.UUID(hold_id),
            appt_id,
        )
    return {
        "committed": True,
        "approval_status": "approved",
        "decided_by": decision["decided_by"],
        "external_appt_id": appt_id,
        "start_time": hold["start_time"],
    }


async def reschedule(args: dict[str, Any]) -> dict[str, Any]:
    appt_id = str(args["appointment_id"])
    slot = args.get("new_slot") or args
    new_start = _parse_ts(slot["start_time"])
    pool = await get_pool()
    async with pool.acquire() as c:
        appt = await c.fetchrow(
            "SELECT a.*, d.duration_min FROM ext.appointments a "
            "LEFT JOIN ext.appointment_descriptors d ON d.id = a.descriptor_id WHERE a.id = $1",
            appt_id,
        )
    if not appt:
        return {"committed": False, "error": "appointment_not_found", "appointment_id": appt_id}
    dur = timedelta(minutes=appt["duration_min"] or 60)
    new_end = _parse_ts(slot["end_time"]) if slot.get("end_time") else new_start + dur
    new_operatory = slot.get("operatory_id") or appt["operatory_id"]

    decision = await _await_approval(
        "reschedule",
        {"appointment_id": appt_id, "from": appt["start_time"].isoformat(), "to": new_start.isoformat()},
        f"reschedule:{appt_id}:{new_start.isoformat()}",
    )
    if decision["status"] != "approved":
        return {"committed": False, "approval_status": decision["status"], "appointment_id": appt_id}
    async with pool.acquire() as c:
        try:
            r = await c.fetchrow(
                "UPDATE ext.appointments SET start_time=$2, end_time=$3, operatory_id=$4 WHERE id=$1 "
                "RETURNING id, start_time, end_time, operatory_id",
                appt_id,
                new_start,
                new_end,
                new_operatory,
            )
        except Exception as err:
            if "no_chair_overlap" in str(err):
                return {"committed": False, "error": "slot_taken", "appointment_id": appt_id}
            raise
    return {"committed": True, "approval_status": "approved", "decided_by": decision["decided_by"], **dict(r)}


async def cancel(args: dict[str, Any]) -> dict[str, Any]:
    appt_id = str(args["appointment_id"])
    reason = str(args.get("reason", ""))
    decision = await _await_approval("cancel", {"appointment_id": appt_id, "reason": reason}, f"cancel:{appt_id}")
    if decision["status"] != "approved":
        return {"committed": False, "approval_status": decision["status"], "appointment_id": appt_id}
    pool = await get_pool()
    async with pool.acquire() as c:
        r = await c.fetchrow(
            "UPDATE ext.appointments SET status='cancelled', note=$2 WHERE id=$1 RETURNING id, status", appt_id, reason
        )
    if r:
        return {"committed": True, "approval_status": "approved", "decided_by": decision["decided_by"], **dict(r)}
    return {"committed": False, "error": "appointment_not_found", "appointment_id": appt_id}


async def update_contact(args: dict[str, Any]) -> dict[str, Any]:
    await _require_identity()
    patient_id = str(args["patient_id"])
    fields = args.get("fields") or {}
    allowed = ("phone", "email", "address")
    sets, vals = [], [patient_id]
    for k in allowed:
        if fields.get(k) is not None:
            vals.append(fields[k])
            sets.append(f"{k} = ${len(vals)}")
    if not sets:
        return {"committed": False, "error": "no_updatable_fields"}
    decision = await _await_approval(
        "update_contact", {"patient_id": patient_id, "fields": fields}, f"contact:{patient_id}:{dumps(fields)}"
    )
    if decision["status"] != "approved":
        return {"committed": False, "approval_status": decision["status"], "patient_id": patient_id}
    pool = await get_pool()
    async with pool.acquire() as c:
        r = await c.fetchrow(
            f"UPDATE ext.patients SET {', '.join(sets)}, updated_at = now() WHERE id = $1 RETURNING id", *vals
        )
    if r:
        return {
            "committed": True,
            "approval_status": "approved",
            "decided_by": decision["decided_by"],
            "patient_id": patient_id,
            "updated_fields": list(fields.keys()),
        }
    return {"committed": False, "error": "patient_not_found", "patient_id": patient_id}


async def add_to_waitlist(args: dict[str, Any]) -> dict[str, Any]:
    patient_id = str(args["patient_id"])
    descriptor_id = str(args["descriptor_id"])
    pool = await get_pool()
    async with pool.acquire() as c:
        wl_id = "wl_" + uuid.uuid4().hex[:8]
        await c.execute(
            "INSERT INTO ext.waitlist (id, patient_id, descriptor_id, provider_pref, window_pref) VALUES ($1,$2,$3,$4,$5)",
            wl_id,
            patient_id,
            descriptor_id,
            args.get("provider_pref"),
            args.get("window_pref"),
        )
    return {"waitlist_id": wl_id, "added": True}


async def confirm_appointment(args: dict[str, Any]) -> dict[str, Any]:
    appt_id = str(args["appointment_id"])
    pool = await get_pool()
    async with pool.acquire() as c:
        r = await c.fetchrow(
            "UPDATE ext.appointments SET status='confirmed' WHERE id=$1 AND status='booked' RETURNING id, status", appt_id
        )
    if r:
        return {**dict(r), "confirmed": True}
    return {"error": "not_confirmable", "appointment_id": appt_id}


# ── tool catalogue (metadata + dispatch) ──────────────────────────────────────
Handler = Callable[[dict[str, Any]], Awaitable[Any]]
HANDLERS: dict[str, Handler] = {
    "find_patient": find_patient,
    "verify_identity": verify_identity,
    "get_patient_summary": get_patient_summary,
    "check_coverage": check_coverage,
    "get_appointments": get_appointments,
    "list_appointment_types": list_appointment_types,
    "find_open_slots": find_open_slots,
    "find_recalls": find_recalls,
    "halo_fetch": halo_fetch,
    "halo_fetch_many": halo_fetch_many,
    "hold_slot": hold_slot,
    "book_appointment": book_appointment,
    "reschedule": reschedule,
    "cancel": cancel,
    "update_contact": update_contact,
    "add_to_waitlist": add_to_waitlist,
    "confirm_appointment": confirm_appointment,
}


def _ann(**kw: bool) -> types.ToolAnnotations:
    return types.ToolAnnotations(**kw)


TOOLS: list[types.Tool] = [
    types.Tool(
        name="find_patient",
        description="Find a patient by phone, name+dob, or email. Returns only THIN candidates (name + masked phone) — enough to start identity verification, no PHI. Pass {query} for a phone/email, or {last_name, dob}.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "phone number or email"},
                "last_name": {"type": "string"},
                "dob": {"type": "string", "description": "YYYY-MM-DD"},
                "email": {"type": "string"},
                "phone": {"type": "string"},
            },
        },
        annotations=_ann(readOnlyHint=True, openWorldHint=True),
    ),
    types.Tool(
        name="verify_identity",
        description="IDENTITY GATE. Confirm a caller's last_name + dob against the record and mark the session verified. Returns only a boolean — no PHI. Must succeed before get_patient_summary / check_coverage / get_appointments will return anything.",
        inputSchema={
            "type": "object",
            "properties": {"patient_id": {"type": "string"}, "last_name": {"type": "string"}, "dob": {"type": "string", "description": "YYYY-MM-DD"}},
            "required": ["patient_id", "last_name", "dob"],
        },
        annotations=_ann(idempotentHint=True),
    ),
    types.Tool(
        name="get_patient_summary",
        description="Fetch a patient's reception-relevant record (contact, balance, recall, insurance status). REQUIRES a verified session. Returns a Halo envelope; `refs` carve out contact / insurance / clinical — never fetch `clinical` for reception work. Keyed into the patient map.",
        inputSchema={"type": "object", "properties": {"patient_id": {"type": "string"}}, "required": ["patient_id"]},
        annotations=_ann(readOnlyHint=True),
    ),
    types.Tool(
        name="check_coverage",
        description="Eligibility + copay for one appointment type (hides the insurance lookup). REQUIRES a verified session.",
        inputSchema={
            "type": "object",
            "properties": {"patient_id": {"type": "string"}, "descriptor_id": {"type": "string"}},
            "required": ["patient_id", "descriptor_id"],
        },
        annotations=_ann(readOnlyHint=True),
    ),
    types.Tool(
        name="get_appointments",
        description="A patient's appointments (Halo envelope: summary of upcoming + `all` handle). REQUIRES a verified session.",
        inputSchema={"type": "object", "properties": {"patient_id": {"type": "string"}}, "required": ["patient_id"]},
        annotations=_ann(readOnlyHint=True),
    ),
    types.Tool(
        name="list_appointment_types",
        description='List the practice\'s bookable appointment types (id, name, duration, bookable_online). Call this to resolve a spoken type like "cleaning" to the descriptor_id that check_coverage / find_open_slots / hold_slot expect.',
        inputSchema={"type": "object", "properties": {"location_id": {"type": "string"}}},
        annotations=_ann(readOnlyHint=True, openWorldHint=True),
    ),
    types.Tool(
        name="find_open_slots",
        description="Derive open slots for an appointment type (availabilities minus booked appointments — mirrors NexHealth appointment_slots). Returns a Halo envelope: summary with `by_day` / `by_provider` counts + a sample, and an `all_slots` handle. Walk to the day/provider the caller wants; don't fetch the whole grid.",
        inputSchema={
            "type": "object",
            "properties": {
                "descriptor_id": {"type": "string", "description": "appointment type to book"},
                "from": {"type": "string", "description": "ISO window start (default: now)"},
                "to": {"type": "string", "description": "ISO window end (default: +14 days)"},
                "provider": {"type": "string", "description": "provider id or name preference"},
                "time_of_day": {"type": "string", "enum": ["AM", "PM"]},
            },
            "required": ["descriptor_id"],
        },
        annotations=_ann(readOnlyHint=True, openWorldHint=True),
    ),
    types.Tool(
        name="find_recalls",
        description="Batch/admin read for recalls-and-reminders: patients overdue for recall (kind='recall') or upcoming appointments needing confirmation (kind='reminder'). Returns thin outreach candidates with masked phones.",
        inputSchema={"type": "object", "properties": {"kind": {"type": "string", "enum": ["recall", "reminder"]}}},
        annotations=_ann(readOnlyHint=True, openWorldHint=True),
    ),
    types.Tool(
        name="halo_fetch",
        description="Fetch the decoded content behind one Halo handle (h:sha256:...).",
        inputSchema={"type": "object", "properties": {"handle": {"type": "string"}}, "required": ["handle"]},
        annotations=_ann(readOnlyHint=True),
    ),
    types.Tool(
        name="halo_fetch_many",
        description="Fetch many Halo handles in one round trip (batched drill-down).",
        inputSchema={"type": "object", "properties": {"handles": {"type": "array", "items": {"type": "string"}}}, "required": ["handles"]},
        annotations=_ann(readOnlyHint=True),
    ),
    types.Tool(
        name="hold_slot",
        description="Reserve a slot with a short-TTL agent-local hold (no external write; the double-book defense). Idempotent on (patient, operatory, start). Returns a hold_id to pass to book_appointment.",
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "descriptor_id": {"type": "string"},
                "start_time": {"type": "string", "description": "ISO start of the chosen slot"},
                "provider_id": {"type": "string"},
                "operatory_id": {"type": "string"},
                "location_id": {"type": "string"},
            },
            "required": ["patient_id", "descriptor_id", "start_time", "provider_id", "operatory_id"],
        },
        annotations=_ann(idempotentHint=True),
    ),
    types.Tool(
        name="book_appointment",
        description="Commit a held slot into the schedule. HUMAN-GATED: proposes to agent.approvals and BLOCKS until a human confirms. The DB exclusion constraint guarantees no double-book; on a lost race returns committed=false, error='slot_taken'.",
        inputSchema={"type": "object", "properties": {"hold_id": {"type": "string"}}, "required": ["hold_id"]},
        annotations=_ann(idempotentHint=True, openWorldHint=True),
    ),
    types.Tool(
        name="reschedule",
        description="Move an appointment to a new slot. HUMAN-GATED. Pass {appointment_id, new_slot:{start_time, operatory_id?}}.",
        inputSchema={
            "type": "object",
            "properties": {
                "appointment_id": {"type": "string"},
                "new_slot": {
                    "type": "object",
                    "properties": {"start_time": {"type": "string"}, "end_time": {"type": "string"}, "operatory_id": {"type": "string"}},
                    "required": ["start_time"],
                },
            },
            "required": ["appointment_id", "new_slot"],
        },
        annotations=_ann(idempotentHint=True, openWorldHint=True),
    ),
    types.Tool(
        name="cancel",
        description="Cancel an appointment with a reason. HUMAN-GATED.",
        inputSchema={
            "type": "object",
            "properties": {"appointment_id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["appointment_id", "reason"],
        },
        annotations=_ann(idempotentHint=True, openWorldHint=True),
    ),
    types.Tool(
        name="update_contact",
        description="Update a patient's contact details (phone / email / address). REQUIRES a verified session and is HUMAN-GATED.",
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "fields": {
                    "type": "object",
                    "properties": {"phone": {"type": "string"}, "email": {"type": "string"}, "address": {"type": "string"}},
                },
            },
            "required": ["patient_id", "fields"],
        },
        annotations=_ann(idempotentHint=True, openWorldHint=True),
    ),
    types.Tool(
        name="add_to_waitlist",
        description="Add a patient to the waitlist for an appointment type (low risk; direct write).",
        inputSchema={
            "type": "object",
            "properties": {
                "patient_id": {"type": "string"},
                "descriptor_id": {"type": "string"},
                "provider_pref": {"type": "string"},
                "window_pref": {"type": "string", "description": "e.g. 'Tue/Thu PM'"},
            },
            "required": ["patient_id", "descriptor_id"],
        },
        annotations=_ann(idempotentHint=True),
    ),
    types.Tool(
        name="confirm_appointment",
        description="Mark a booked appointment confirmed (patient confirming a reminder; low risk, direct).",
        inputSchema={"type": "object", "properties": {"appointment_id": {"type": "string"}}, "required": ["appointment_id"]},
        annotations=_ann(idempotentHint=True),
    ),
]

# ── server wiring ─────────────────────────────────────────────────────────────
server: Server = Server("dental-mcp")


@server.list_tools()
async def _list_tools() -> list[types.Tool]:
    return TOOLS


@server.call_tool()
async def _call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    handler = HANDLERS.get(name)
    if handler is None:
        return [types.TextContent(type="text", text=f"unknown tool: {name}")]
    started = time.monotonic()
    try:
        out = await handler(arguments)
        if isinstance(out, halo.Envelope):
            payload: Any = out.to_dict()
            env_root = out.map_root or next(iter(out.refs.values()), None)
        elif isinstance(out, dict) and "refs" in out:
            payload = out
            env_root = next(iter(out["refs"].values()), None)
        else:
            payload = out
            env_root = None
        await _record_tool_call(name, arguments, env_root, int((time.monotonic() - started) * 1000), True, None)
        return [types.TextContent(type="text", text=dumps(payload))]
    except IdentityRequired:
        await _record_tool_call(name, arguments, None, int((time.monotonic() - started) * 1000), False, "identity_required")
        return [
            types.TextContent(
                type="text",
                text=dumps({"error": "identity_required", "note": "Call verify_identity(patient_id, last_name, dob) first."}),
            )
        ]
    except Exception as err:  # noqa: BLE001
        await _record_tool_call(name, arguments, None, int((time.monotonic() - started) * 1000), False, str(err))
        return [types.TextContent(type="text", text=f"error in {name}: {err}")]


async def main() -> None:
    await ensure_session()
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
