-- ============================================================================
-- Dental reception agent — one Postgres, two schemas.
--   ext.*   stands in for a NexHealth-style synchronizer (patients, providers,
--           locations, operatories, appointment descriptors, appointments,
--           insurance) and is shaped like it. Swapping to the real API later
--           touches only the tool bodies, never these shapes.
--   agent.* is the agent's own state (sessions, halo store, approvals, holds,
--           bookings) and never changes when you integrate.
-- ============================================================================

-- Needed for the no-double-book exclusion constraint on ext.appointments.
create extension if not exists btree_gist;

-- ─────────────────────────────── ext schema ───────────────────────────────
create schema if not exists ext;

create table if not exists ext.locations (            -- NexHealth location (lid)
  id        text primary key,
  name      text not null,
  address   text,
  timezone  text not null,
  subdomain text
);

create table if not exists ext.providers (            -- NexHealth provider (pid)
  id          text primary key,
  name        text not null,
  specialty   text,
  npi         text,
  location_id text references ext.locations(id)
);

create table if not exists ext.operatories (          -- chairs / rooms (operatory_id)
  id          text primary key,
  name        text not null,
  location_id text references ext.locations(id)
);

create table if not exists ext.appointment_descriptors (   -- bookable appointment types
  id              text primary key,
  name            text not null,        -- 'Cleaning', 'New Patient Exam', 'Crown', ...
  duration_min    int not null,
  location_id     text references ext.locations(id),
  bookable_online boolean default true
);

create table if not exists ext.patients (             -- mirrors a NexHealth patient
  id            text primary key,
  foreign_id    text,                   -- id in the underlying PMS
  first_name    text not null,
  middle_name   text,
  last_name     text not null,
  dob           date,
  email         text,
  phone         text,
  address       text,
  balance_cents int default 0,
  recall_due    date,                   -- next cleaning due
  inactive      boolean default false,
  created_at    timestamptz default now(),
  updated_at    timestamptz default now()
);
create index if not exists ix_patients_phone on ext.patients (phone);
create index if not exists ix_patients_name_dob on ext.patients (lower(last_name), dob);

create table if not exists ext.insurance_coverages (
  id              text primary key,
  patient_id      text references ext.patients(id),
  carrier         text,
  plan_name       text,
  member_id       text,
  group_number    text,
  eligibility     text,                 -- 'active' | 'inactive' | 'unknown'
  coverage_pct    int,                  -- e.g. 100 for preventive
  copay_cents     int,
  verified_at     timestamptz
);
create index if not exists ix_coverage_patient on ext.insurance_coverages (patient_id);

create table if not exists ext.provider_availabilities (   -- working hours (NexHealth availabilities)
  id            text primary key,
  provider_id   text references ext.providers(id),
  location_id   text references ext.locations(id),
  operatory_id  text references ext.operatories(id),
  weekday       int,                    -- 0=Sunday .. 6=Saturday
  start_time    time not null,
  end_time      time not null
);

create table if not exists ext.appointments (
  id            text primary key,
  patient_id    text references ext.patients(id),
  provider_id   text references ext.providers(id),
  location_id   text references ext.locations(id),
  operatory_id  text references ext.operatories(id),
  descriptor_id text references ext.appointment_descriptors(id),
  start_time    timestamptz not null,
  end_time      timestamptz not null,
  status        text default 'booked',  -- booked | confirmed | cancelled | completed | no_show
  note          text,
  created_at    timestamptz default now()
);
create index if not exists ix_appts_patient on ext.appointments (patient_id, start_time desc);
create index if not exists ix_appts_op_time on ext.appointments (operatory_id, start_time);
-- prevent two appointments overlapping in the same chair (needs btree_gist).
-- Guarded by an existence check so re-applying the schema is idempotent (adding
-- the constraint again raises 42P07 on its backing index, not duplicate_object).
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'no_chair_overlap') then
    alter table ext.appointments
      add constraint no_chair_overlap
      exclude using gist (operatory_id with =, tstzrange(start_time, end_time) with &&)
      where (status in ('booked','confirmed'));
  end if;
end $$;

create table if not exists ext.waitlist (
  id            text primary key,
  patient_id    text references ext.patients(id),
  descriptor_id text references ext.appointment_descriptors(id),
  provider_pref text,
  window_pref   text,                   -- 'Tue/Thu PM', etc.
  created_at    timestamptz default now()
);

-- ────────────────────────────── agent schema ──────────────────────────────
create schema if not exists agent;

create table if not exists agent.sessions (
  id          uuid primary key,
  channel     text,                     -- voice | chat | batch
  patient_ref text,                     -- foreign id once identified, not PHI
  identity_ok boolean default false,    -- set true only after identity verification
  started_at  timestamptz default now(),
  ended_at    timestamptz,
  status      text default 'active'
);

create table if not exists agent.messages (
  id         uuid primary key,
  session_id uuid references agent.sessions(id),
  role       text not null,             -- user | assistant | tool
  content    jsonb not null,            -- envelopes, never raw records
  created_at timestamptz default now()
);

create table if not exists agent.tool_calls (         -- observability of the agent's own actions
  id            uuid primary key,
  session_id    uuid references agent.sessions(id),
  tool          text not null,
  args          jsonb,
  envelope_root text,                   -- halo root handle of the result
  latency_ms    int,
  ok            boolean,
  error         text,
  created_at    timestamptz default now()
);

create table if not exists agent.halo_nodes (         -- the Halo content-addressed store
  handle     text primary key,          -- h:sha256:...
  bytes      bytea not null,
  created_at timestamptz default now()
);

create table if not exists agent.halo_maps (          -- latest root per entity (e.g. patient id)
  session_id uuid references agent.sessions(id),
  map_id     text not null,
  root       text not null,
  source     jsonb,
  updated_at timestamptz default now(),
  primary key (session_id, map_id)
);

create table if not exists agent.approvals (          -- HITL for any schedule or record write
  id              uuid primary key,
  session_id      uuid references agent.sessions(id),
  action          text not null,        -- book | reschedule | cancel | update_contact
  payload         jsonb not null,
  idempotency_key text unique,
  status          text default 'pending', -- pending | approved | rejected
  decided_by      text,
  created_at      timestamptz default now(),
  decided_at      timestamptz
);

create table if not exists agent.booking_holds (      -- soft local hold, prevents double-book across the sync delay
  id              uuid primary key,
  session_id      uuid references agent.sessions(id),
  patient_ref     text not null,
  provider_ref    text not null,
  location_ref    text not null,
  operatory_ref   text not null,
  descriptor_ref  text not null,
  start_time      timestamptz not null,
  end_time        timestamptz not null,
  expires_at      timestamptz not null, -- TTL, released if not booked
  idempotency_key text unique,
  status          text default 'held',  -- held | committed | expired | released
  created_at      timestamptz default now()
);

create table if not exists agent.bookings (           -- the agent's record of what it created
  id               uuid primary key,
  hold_id          uuid references agent.booking_holds(id),
  external_appt_id text,                -- ext.appointments.id today, synchronizer id later
  status           text default 'confirmed',
  created_at       timestamptz default now()
);
