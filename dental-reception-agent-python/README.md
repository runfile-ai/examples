# Dental Reception Agent (Python · Claude Agent SDK)

A self-contained dental front-desk agent that handles reception work — patient
lookup, booking, rescheduling, cancellations, coverage checks, recalls and
reminders, and basic patient admin — all through an **identity gate**, a
**human write gate**, and a **Halo**-backed tool layer over a local Postgres.
No external credentials required.

This is the **Python** port of `dental-reception-agent` (TypeScript) — same
architecture, same database, same six skills, line-for-line behaviour. It builds
on the `claude-agent-sdk` Python package, the `mcp` package for a custom stdio
MCP server, and `asyncpg`.

The database deliberately mirrors a **NexHealth-style synchronizer** (patients,
providers, locations, operatories, appointment descriptors, appointments,
insurance), so integrating later swaps each tool body from SQL to an API call
and nothing above the tools changes.

## The guiding idea

The **tool contract is the swap point.** Today each tool body is SQL on `ext.*`;
later it is a synchronizer call. Because the local `ext.*` tables are shaped like
the external system, that swap is mechanical — the skills, both gates, and Halo
are untouched.

```
 call / chat ─▶ orchestrator (Claude Agent SDK) ◀── skills (booking, reschedule-cancel,
                     │                                coverage-check, recalls-and-reminders,
                     │ mcp tool call                  patient-admin, halo-navigation)
                     ▼
              identity gate ─▶ dental MCP server ─▶ local Postgres (ext.* mirrors NexHealth)
                     │              │  (Halo envelopes at the tool-result boundary)
                     ▼              ▼
              write gate ─▶ agent.approvals ─▶ human confirm ─▶ commit (ext.appointments)
```

One Postgres, two schemas: `ext.*` stands in for the synchronizer and is shaped
like it; `agent.*` is the agent's own state (sessions, the Halo store, approvals,
holds, bookings) and never changes when you integrate.

## Two gates

- **Identity before disclosure.** `find_patient` returns only thin candidates
  (name + masked phone). `get_patient_summary`, `check_coverage`, and
  `get_appointments` refuse until `verify_identity(patient_id, last_name, dob)`
  has marked the session verified.
- **Writes are human-gated.** `book_appointment`, `reschedule`, `cancel`, and
  `update_contact` propose to `agent.approvals` and **block** until a human
  confirms. `hold_slot`, `add_to_waitlist`, and `confirm_appointment` are low
  risk and commit directly.

## No double-booking

`hold_slot` takes a short-TTL agent-local hold; `book_appointment` commits only
against a live hold; and a `no_chair_overlap` **exclusion constraint** on
`ext.appointments` makes an overlapping booking impossible at the database, even
under a race. The demo proves it: a second patient trying the same chair+time
gets `committed: false, error: "slot_taken"`.

## Halo (the part that matters)

Heavy tool results are not returned raw. The heavy parts are written to
`agent.halo_nodes` keyed by a content handle (`h:sha256:...`), and the tool
returns a compact **envelope** — `{ summary, refs: { name: handle } }`. The model
reasons on the summary and fetches only the handles a step needs
(`halo_fetch` / `halo_fetch_many`). `get_patient_summary` exposes `contact` /
`insurance` / `clinical` as separate handles — reception fetches contact +
insurance and **never pulls clinical detail** into context; `find_open_slots`
returns counts + a sample rather than the whole grid. See
`src/mcp_server/halo.py` and the **halo-navigation** skill.

## Tools (`src/mcp_server/server.py`)

| Tool | Kind | Notes |
|------|------|-------|
| `find_patient` | read | thin candidates only (name + masked phone) |
| `verify_identity` | gate | sets `identity_ok` on the session |
| `get_patient_summary` | read | **needs identity**; Halo refs: contact / insurance / clinical |
| `check_coverage` | read | **needs identity**; eligibility + copay |
| `get_appointments` | read | **needs identity**; Halo envelope |
| `list_appointment_types` | read | resolve a spoken type → `descriptor_id` |
| `find_open_slots` | read | derived (availabilities − booked); Halo envelope |
| `find_recalls` | read | batch outreach list (recall / reminder) |
| `halo_fetch` / `halo_fetch_many` | read | drill into handles |
| `hold_slot` | write | agent-local, short TTL, idempotent — no external write |
| `book_appointment` | write | **human-gated**; DB constraint guarantees no double-book |
| `reschedule` / `cancel` | write | **human-gated** |
| `update_contact` | write | **needs identity** + **human-gated** |
| `add_to_waitlist` / `confirm_appointment` | write | direct, low risk |

Every call is recorded in `agent.tool_calls` (tool, args, result root handle,
latency, outcome) — the eval/observability trail.

## Quick start

```bash
# 1. Postgres (reuses the shared local instance on :5433)
#    From elsewhere in this repo: docker compose -f ../creditline-decision-agent/docker-compose.yml up -d
#    …or any Postgres reachable via DENTAL_DB_DSN. Needs the btree_gist extension
#    (bundled with standard Postgres; the schema enables it).

# 2. Install + configure
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

# 3. Create the `dental` database, apply schema, seed ext.*
python db/seed.py

# 4a. Deterministic demo (no model key) — drives the real MCP server over stdio
python -m src.scripts.run_demo

# 4b. Live agent (Claude Agent SDK; uses your `claude` login or ANTHROPIC_API_KEY)
python -m src.agent.main
#     …in a second terminal, act as front-desk to confirm the gated writes:
python -m src.scripts.frontdesk_console          # interactive
#   (or, unattended for a hands-off run:)  python -m src.scripts.frontdesk_console auto
```

The seeded **hero patient** `pat_maria` (Maria Garcia, dob 1989-04-12,
+1 415-555-0142) — overdue for a cleaning, active Delta Dental (preventive
covered 100%, $0 copay) — is the case the agent verifies, quotes coverage for,
finds a morning slot for, holds, and books.

## The swap to real systems, later

You touch only the tool bodies in `src/mcp_server/server.py`:
`find_patient` / `get_patient_summary` / `get_appointments` → NexHealth patients
and appointments endpoints; `check_coverage` → the insurance coverage endpoints;
`find_open_slots` → the `appointment_slots` endpoint; `book_appointment` → POST
`/appointments`; `reschedule` / `cancel` → the appointment update endpoints;
`update_contact` → PATCH `/patients/{id}`. `hold_slot` stays agent-local on
purpose. Everything else — the skills, both gates, `agent.*`, and Halo — is
untouched, because `ext.*` was built to the synchronizer's shape.
