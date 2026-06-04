-- ============================================================================
-- Monitoring agent — one Postgres, two schemas.
--   ext.*   stands in for the external systems (Sentry issues/events, logs,
--           PagerDuty incidents) and is shaped like them. Swapping to the real
--           APIs later touches only the tool bodies, never these shapes.
--   agent.* is the agent's own state (sessions, halo store, approvals, triage)
--           and never changes when you integrate.
-- ============================================================================

-- ─────────────────────────────── ext schema ───────────────────────────────
create schema if not exists ext;

create table if not exists ext.projects (
  id        text primary key,
  slug      text unique not null,
  name      text,
  platform  text
);

create table if not exists ext.issues (              -- mirrors a Sentry issue (group)
  id          text primary key,
  short_id    text,
  project_id  text references ext.projects(id),
  title       text not null,
  culprit     text,
  level       text not null,                          -- error | warning | info | fatal
  status      text not null default 'unresolved',     -- unresolved | resolved | ignored
  times_seen  int  not null default 0,
  user_count  int  not null default 0,
  first_seen  timestamptz,
  last_seen   timestamptz,
  metadata    jsonb,
  permalink   text
);
create index if not exists ix_issues_status_seen on ext.issues (status, last_seen desc);

create table if not exists ext.events (              -- mirrors a Sentry event; heavy payload
  id           text primary key,
  issue_id     text references ext.issues(id),
  timestamp    timestamptz not null,
  message      text,
  platform     text,
  environment  text,
  release      text,
  server_name  text,
  exception    jsonb,                                 -- { values:[{ type, value, stacktrace:{frames:[...]} }] }
  breadcrumbs  jsonb,                                 -- [ { timestamp, category, message, level } ]
  tags         jsonb,                                 -- { key: value }
  contexts     jsonb
);
create index if not exists ix_events_issue_ts on ext.events (issue_id, timestamp desc);

create table if not exists ext.logs (                -- Datadog/Loki shaped; the biggest payload
  id          bigserial primary key,
  ts          timestamptz not null,
  service     text,
  level       text,                                  -- error | warn | info | debug
  message     text,
  attributes  jsonb,
  trace_id    text,
  span_id     text
);
create index if not exists ix_logs_ts on ext.logs (ts desc);
create index if not exists ix_logs_svc_lvl_ts on ext.logs (service, level, ts desc);

create table if not exists ext.services (
  id    text primary key,
  name  text not null
);

create table if not exists ext.incidents (          -- mirrors a PagerDuty incident
  id              text primary key,
  incident_number int generated always as identity,
  title           text not null,
  description     text,
  status          text not null default 'triggered',  -- triggered | acknowledged | resolved
  urgency         text not null default 'high',        -- high | low
  service_id      text references ext.services(id),
  dedup_key       text unique,                          -- one incident per key (PD dedup)
  assigned_to     text,
  created_at      timestamptz default now(),
  updated_at      timestamptz default now(),
  resolved_at     timestamptz
);

create table if not exists ext.incident_alerts (
  id           text primary key,
  incident_id  text references ext.incidents(id),
  severity     text,
  summary      text,
  source       text,
  status       text default 'triggered',
  created_at   timestamptz default now(),
  body         jsonb
);

create table if not exists ext.incident_notes (
  id           text primary key,
  incident_id  text references ext.incidents(id),
  content      text not null,
  author       text,
  created_at   timestamptz default now()
);

-- ────────────────────────────── agent schema ──────────────────────────────
create schema if not exists agent;

create table if not exists agent.sessions (
  id         uuid primary key,
  channel    text,                                   -- cron | webhook
  started_at timestamptz default now(),
  ended_at   timestamptz,
  status     text default 'active'
);

create table if not exists agent.messages (
  id         uuid primary key,
  session_id uuid references agent.sessions(id),
  role       text not null,                          -- user | assistant | tool
  content    jsonb not null,
  created_at timestamptz default now()
);

create table if not exists agent.tool_calls (        -- observability of the agent's own actions
  id            uuid primary key,
  session_id    uuid references agent.sessions(id),
  tool          text not null,
  args          jsonb,
  envelope_root text,                                 -- halo root handle of the result
  latency_ms    int,
  ok            boolean,
  error         text,
  created_at    timestamptz default now()
);

create table if not exists agent.halo_nodes (        -- the Halo content-addressed store
  handle     text primary key,                       -- h:sha256:...
  bytes      bytea not null,
  created_at timestamptz default now()
);

create table if not exists agent.halo_maps (         -- latest root per entity (e.g. issue id)
  session_id uuid references agent.sessions(id),
  map_id     text not null,
  root       text not null,
  source     jsonb,
  updated_at timestamptz default now(),
  primary key (session_id, map_id)
);

create table if not exists agent.approvals (         -- human-in-the-loop for real-world writes
  id              uuid primary key,
  session_id      uuid references agent.sessions(id),
  action          text not null,                     -- declare_incident | resolve_incident | ...
  payload         jsonb not null,
  idempotency_key text unique,
  status          text default 'pending',            -- pending | approved | rejected
  decided_by      text,
  created_at      timestamptz default now(),
  decided_at      timestamptz
);

create table if not exists agent.triage_state (      -- dedups triage across runs
  issue_id        text primary key,
  last_seen_event timestamptz,
  decision        text,                               -- watch | declared | ignored | resolved
  reason          text,
  updated_at      timestamptz default now()
);

create table if not exists agent.incident_links (    -- ties an issue to the incident the agent declared
  issue_id    text primary key,
  dedup_key   text not null,
  incident_id text,
  status      text default 'declared',
  created_at  timestamptz default now()
);
