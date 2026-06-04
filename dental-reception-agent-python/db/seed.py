"""Create the `dental` database, apply the schema, and seed ext.* with a small
but realistic practice: one location, two providers and their chairs, the usual
appointment types, a few dozen patients with insurance and recall dates, provider
availabilities, and an existing schedule. This is the whole "external system" —
it makes the agent runnable today with no credentials.

  python -m db.seed     (run from the project root)
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

load_dotenv()

ADMIN = {
    "host": os.environ.get("ADMIN_DB_HOST", "localhost"),
    "port": int(os.environ.get("ADMIN_DB_PORT", "5433")),
    "user": os.environ.get("ADMIN_DB_USER", "postgres"),
    "password": os.environ.get("ADMIN_DB_PASSWORD", "postgres"),
}
DB_NAME = os.environ.get("DENTAL_DB_NAME", "dental")
DENTAL_DSN = os.environ.get("DENTAL_DB_DSN") or (
    f"postgresql://{ADMIN['user']}:{ADMIN['password']}@{ADMIN['host']}:{ADMIN['port']}/{DB_NAME}"
)

SCHEMA_PATH = Path(__file__).resolve().parent / "01_schema.sql"

NOW = datetime.now(timezone.utc)
DAY = timedelta(days=1)


def at(days_from_now: int, hour: int, minute: int = 0) -> datetime:
    d = (NOW + days_from_now * DAY).date()
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=timezone.utc)


def next_weekday(weekday: int) -> date:
    """Next date (>= tomorrow) whose Sunday=0..Saturday=6 index matches weekday."""
    d = (NOW + DAY).date()
    while d.isoweekday() % 7 != weekday:
        d += DAY
    return d


FIRST = ["James", "Olivia", "Liam", "Emma", "Noah", "Ava", "Lucas", "Sophia", "Mason", "Isabella",
         "Ethan", "Mia", "Logan", "Amelia", "Jacob", "Harper", "Daniel", "Evelyn", "Henry", "Abigail"]
LAST = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Miller", "Davis", "Wilson", "Moore",
        "Taylor", "Anderson", "Thomas", "Jackson", "White", "Harris", "Martin", "Garcia", "Clark", "Lewis", "Walker"]


async def ensure_database() -> None:
    admin = await asyncpg.connect(**ADMIN, database="postgres")
    try:
        exists = await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", DB_NAME)
        if not exists:
            await admin.execute(f"CREATE DATABASE {DB_NAME}")
            print(f"created database {DB_NAME}")
    finally:
        await admin.close()


async def seed(conn: asyncpg.Connection) -> None:
    for t in [
        "agent.bookings", "agent.booking_holds", "agent.approvals", "agent.halo_maps", "agent.halo_nodes",
        "agent.tool_calls", "agent.messages", "agent.sessions",
        "ext.waitlist", "ext.appointments", "ext.provider_availabilities", "ext.insurance_coverages",
        "ext.appointment_descriptors", "ext.operatories", "ext.providers", "ext.patients", "ext.locations",
    ]:
        await conn.execute(f"DELETE FROM {t}")

    # ── Location, providers, chairs ───────────────────────────────────────────
    await conn.execute(
        "INSERT INTO ext.locations (id, name, address, timezone, subdomain) "
        "VALUES ('loc_main','BrightSmile Dental','100 Market St','America/New_York','brightsmile')"
    )
    await conn.execute(
        "INSERT INTO ext.providers (id, name, specialty, npi, location_id) VALUES "
        "('prov_nguyen','Dr. Alice Nguyen','General Dentistry','1003001','loc_main'),"
        "('prov_carter','Dr. Ben Carter','General Dentistry','1003002','loc_main'),"
        "('prov_patel','Dr. Riya Patel','Hygienist','1003003','loc_main')"
    )
    await conn.execute(
        "INSERT INTO ext.operatories (id, name, location_id) VALUES "
        "('op_1','Operatory 1','loc_main'),('op_2','Operatory 2','loc_main'),('op_3','Operatory 3','loc_main')"
    )

    # ── Appointment types ─────────────────────────────────────────────────────
    await conn.execute(
        "INSERT INTO ext.appointment_descriptors (id, name, duration_min, location_id, bookable_online) VALUES "
        "('appt_cleaning','Cleaning',60,'loc_main',true),"
        "('appt_newpatient','New Patient Exam',60,'loc_main',true),"
        "('appt_crown','Crown',90,'loc_main',false),"
        "('appt_emergency','Emergency Visit',30,'loc_main',true)"
    )

    # ── Availabilities: each provider works Mon–Fri 09:00–17:00 in one chair ───
    for prov, op, prefix in [("prov_nguyen", "op_1", "av_n"), ("prov_carter", "op_2", "av_c"), ("prov_patel", "op_3", "av_p")]:
        for wd in range(1, 6):
            await conn.execute(
                "INSERT INTO ext.provider_availabilities "
                "(id, provider_id, location_id, operatory_id, weekday, start_time, end_time) "
                "VALUES ($1,$2,'loc_main',$3,$4,$5,$6)",
                f"{prefix}_{wd}", prov, op, wd, time(9, 0), time(17, 0),
            )

    # ── Patients ──────────────────────────────────────────────────────────────
    # Hero: Maria Garcia — overdue cleaning, active Delta Dental ($0 preventive).
    await conn.execute(
        "INSERT INTO ext.patients (id, foreign_id, first_name, last_name, dob, email, phone, address, "
        "balance_cents, recall_due) VALUES ('pat_maria','pms_55012','Maria','Garcia',$1,"
        "'maria.garcia@example.com','+14155550142','22 Pine St, Brooklyn NY',0,$2)",
        date(1989, 4, 12), (NOW - 20 * DAY).date(),
    )
    await conn.execute(
        "INSERT INTO ext.insurance_coverages (id, patient_id, carrier, plan_name, member_id, group_number, "
        "eligibility, coverage_pct, copay_cents, verified_at) VALUES "
        "('cov_maria','pat_maria','Delta Dental','PPO Preventive','DG88123','GRP-4410','active',100,0, now())"
    )

    for i in range(30):
        first = FIRST[i % len(FIRST)]
        last = LAST[(i * 7 + 3) % len(LAST)]
        pid = f"pat_{i:03d}"
        phone = f"+1415555{2000 + i:04d}"
        if i % 3 == 0:
            recall: date | None = (NOW - (i % 30) * DAY).date()
        elif i % 3 == 1:
            recall = (NOW + (i % 40) * DAY).date()
        else:
            recall = None
        dob = date(1970 + (i % 30), (i % 9) + 1, 11 + (i % 8))
        await conn.execute(
            "INSERT INTO ext.patients (id, foreign_id, first_name, last_name, dob, email, phone, balance_cents, recall_due) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
            pid, f"pms_{10000 + i}", first, last, dob,
            f"{first.lower()}.{last.lower()}@example.com", phone, (i % 5) * 2500, recall,
        )
        if i % 4 == 0:
            await conn.execute(
                "INSERT INTO ext.insurance_coverages (id, patient_id, carrier, plan_name, eligibility, coverage_pct, copay_cents, verified_at) "
                "VALUES ($1,$2,$3,'PPO',$4,$5,$6, now())",
                f"cov_{pid}", pid, ["Cigna", "Aetna", "MetLife", "Guardian"][i % 4],
                "inactive" if i % 8 == 0 else "active", [80, 100, 50][i % 3], [1500, 0, 3000][i % 3],
            )

    # ── Existing schedule ─────────────────────────────────────────────────────
    # A past completed cleaning for Maria (so get_appointments shows history).
    await conn.execute(
        "INSERT INTO ext.appointments (id, patient_id, provider_id, location_id, operatory_id, descriptor_id, "
        "start_time, end_time, status) VALUES "
        "('appt_hist01','pat_maria','prov_nguyen','loc_main','op_1','appt_cleaning',$1,$2,'completed')",
        at(-180, 14), at(-180, 15),
    )

    # Booked appointments next week carve real gaps into the slot grid, incl. a
    # 09:00 with Dr. Nguyen on the next Tuesday (so the first AM slot is 10:00).
    tue = next_weekday(2)
    await conn.execute(
        "INSERT INTO ext.appointments (id, patient_id, provider_id, location_id, operatory_id, descriptor_id, "
        "start_time, end_time, status) VALUES "
        "('appt_busy01','pat_000','prov_nguyen','loc_main','op_1','appt_cleaning',$1,$2,'booked')",
        datetime(tue.year, tue.month, tue.day, 9, 0, tzinfo=timezone.utc),
        datetime(tue.year, tue.month, tue.day, 10, 0, tzinfo=timezone.utc),
    )
    await conn.execute(
        "INSERT INTO ext.appointments (id, patient_id, provider_id, location_id, operatory_id, descriptor_id, "
        "start_time, end_time, status) VALUES "
        "('appt_busy02','pat_004','prov_carter','loc_main','op_2','appt_crown',$1,$2,'confirmed')",
        datetime(tue.year, tue.month, tue.day, 11, 0, tzinfo=timezone.utc),
        datetime(tue.year, tue.month, tue.day, 12, 30, tzinfo=timezone.utc),
    )

    # Maria has an upcoming Crown consult (so reschedule/cancel have a target).
    thu = next_weekday(4)
    await conn.execute(
        "INSERT INTO ext.appointments (id, patient_id, provider_id, location_id, operatory_id, descriptor_id, "
        "start_time, end_time, status) VALUES "
        "('appt_maria_up','pat_maria','prov_carter','loc_main','op_2','appt_crown',$1,$2,'booked')",
        datetime(thu.year, thu.month, thu.day, 13, 0, tzinfo=timezone.utc),
        datetime(thu.year, thu.month, thu.day, 14, 30, tzinfo=timezone.utc),
    )

    counts = await conn.fetchrow(
        "SELECT (SELECT count(*) FROM ext.patients) patients, "
        "(SELECT count(*) FROM ext.insurance_coverages) coverages, "
        "(SELECT count(*) FROM ext.provider_availabilities) availabilities, "
        "(SELECT count(*) FROM ext.appointments) appointments"
    )
    print("seeded ext.*:", dict(counts))
    print("hero patient: pat_maria (Maria Garcia, dob 1989-04-12, +14155550142) — recall overdue, active Delta Dental")
    print(f"next bookable Tuesday: {tue.isoformat()} (09:00 with Dr. Nguyen already taken → first AM slot is 10:00)")


async def main() -> None:
    await ensure_database()
    conn = await asyncpg.connect(DENTAL_DSN)
    try:
        await conn.execute(SCHEMA_PATH.read_text())
        print("applied schema (ext + agent)")
        await seed(conn)
        print("done.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
