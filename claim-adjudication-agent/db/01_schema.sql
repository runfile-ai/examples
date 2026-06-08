-- ============================================================================
-- Insurance claim adjudication agent — one Postgres, two schemas.
--   ext.*   stands in for the payer's systems (claim, member, benefit,
--           accumulator, network, fee-schedule) and the X12 shapes (837 claim,
--           835 remittance, 270/271 eligibility, CARC/RARC reason codes).
--           Swapping to the real feeds later touches only the tool bodies.
--   agent.* is the agent's own state — sessions, the Halo store, approvals, and
--           the tamper-evident decision + evidence record — and never changes
--           when you integrate.
-- ============================================================================

-- ─────────────────────────────── ext schema ───────────────────────────────
create schema if not exists ext;

create table if not exists ext.plans (
  id                 text primary key,
  name               text,
  type               text,                  -- 'dental_ppo' | 'medical_hmo' | ...
  annual_max_cents   int,
  deductible_cents   int,
  oop_max_cents      int,
  coinsurance        jsonb                   -- { preventive: 100, basic: 80, major: 50 }
);

create table if not exists ext.members (
  id             text primary key,
  first_name     text, last_name text, dob date,
  plan_id        text references ext.plans(id),
  group_id       text,
  effective_date date, term_date date,
  status         text                        -- 'active' | 'termed'
);

create table if not exists ext.benefit_rules (    -- per procedure code, the plan's rule
  id                text primary key,
  plan_id           text references ext.plans(id),
  procedure_code    text not null,              -- CDT / CPT
  category          text,                       -- 'preventive' | 'basic' | 'major'
  covered           boolean,
  coverage_pct      int,
  frequency_limit   text,                       -- e.g. '2/year'
  frequency_per_year int,                       -- machine-checkable form of frequency_limit
  waiting_months    int,
  requires_preauth  boolean default false
);
create index if not exists ix_benefit_rules_plan_code on ext.benefit_rules (plan_id, procedure_code);

create table if not exists ext.accumulators (     -- running totals per member per plan year
  member_id             text references ext.members(id),
  plan_year             int,
  deductible_met_cents  int default 0,
  annual_max_used_cents int default 0,
  oop_met_cents         int default 0,
  primary key (member_id, plan_year)
);

create table if not exists ext.providers (
  id          text primary key,
  npi         text, name text, specialty text
);

create table if not exists ext.network (          -- provider in/out of network per plan
  plan_id     text references ext.plans(id),
  provider_id text references ext.providers(id),
  in_network  boolean,
  primary key (plan_id, provider_id)
);

create table if not exists ext.fee_schedule (     -- allowed amounts
  plan_id        text references ext.plans(id),
  procedure_code text,
  allowed_cents  int,
  primary key (plan_id, procedure_code)
);

create table if not exists ext.claims (           -- mirrors an 837 claim header
  id              text primary key,
  claim_number    text unique,
  member_id       text references ext.members(id),
  provider_id     text references ext.providers(id),
  date_received   date,
  place_of_service text,
  diagnosis_codes jsonb,                          -- ICD codes
  attachments     jsonb,                          -- refs to x-rays / notes
  total_charged_cents int,
  status          text default 'received'         -- received | adjudicated | pended | denied | paid
);

create table if not exists ext.claim_lines (      -- mirrors 837 service lines / 835 SVC
  id             text primary key,
  claim_id       text references ext.claims(id),
  line_number    int,
  procedure_code text not null,                   -- CDT / CPT
  tooth          text, surface text,              -- dental
  date_of_service date,
  units          int default 1,
  charged_cents  int,
  -- filled at adjudication (the 835 / EOB result):
  status         text default 'pending',          -- paid | denied | reduced | pended
  allowed_cents  int,
  plan_paid_cents int,
  patient_resp_cents int,
  carc           jsonb,                            -- [ { code, group, amount_cents } ]  group: PR|CO|OA|PI
  rarc           jsonb                             -- remark codes
);
create index if not exists ix_claim_lines_claim on ext.claim_lines (claim_id, line_number);

create table if not exists ext.reason_codes (     -- CARC / RARC reference; the agent selects, never invents
  code        text primary key,
  kind        text,                                -- 'CARC' | 'RARC'
  description text
);

-- ────────────────────────────── agent schema ──────────────────────────────
create schema if not exists agent;

create table if not exists agent.sessions (
  id         uuid primary key,
  claim_id   text,                                 -- the claim being adjudicated
  channel    text,                                 -- queue | batch
  started_at timestamptz default now(),
  ended_at   timestamptz,
  status     text default 'active'
);

create table if not exists agent.messages (
  id uuid primary key, session_id uuid references agent.sessions(id),
  role text, content jsonb, created_at timestamptz default now()   -- envelopes, never raw claims
);

create table if not exists agent.tool_calls (
  id uuid primary key, session_id uuid references agent.sessions(id),
  tool text, args jsonb, envelope_root text, latency_ms int, ok boolean, error text,
  created_at timestamptz default now()
);

create table if not exists agent.halo_nodes (     -- verifiable store: handle = sha256(content) = integrity
  handle text primary key, bytes bytea not null, created_at timestamptz default now()
);
create table if not exists agent.halo_maps (
  session_id uuid references agent.sessions(id), map_id text, root text, source jsonb,
  updated_at timestamptz default now(), primary key (session_id, map_id)
);

create table if not exists agent.approvals (      -- HITL for denials, reductions, pends, large amounts
  id uuid primary key, session_id uuid references agent.sessions(id),
  action text, payload jsonb, idempotency_key text unique,
  status text default 'pending', decided_by text,
  created_at timestamptz default now(), decided_at timestamptz
);

create table if not exists agent.decisions (      -- the audit and explainability record, one per line
  id            uuid primary key,
  session_id    uuid references agent.sessions(id),
  claim_id      text not null,
  line_number   int not null,
  decision      text not null,                    -- pay | deny | reduce | pend
  allowed_cents int, plan_paid_cents int, patient_resp_cents int,
  deductible_cents int, coinsurance_cents int, copay_cents int,
  carc          jsonb, rarc jsonb,
  rule_basis    jsonb,                             -- which benefit rules / checks fired
  evidence      jsonb,                             -- Halo handles of the exact data this rested on
  computed_by   text default 'engine',             -- 'engine' = deterministic, never the model
  status        text default 'proposed',           -- proposed | approved | final
  approver      text,
  created_at    timestamptz default now(),
  decided_at    timestamptz,
  unique (claim_id, line_number)                   -- idempotency: one final decision per line
);
