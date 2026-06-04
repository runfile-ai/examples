---
name: halo-navigation
description: >-
  How to work with Halo envelopes efficiently: read the summary, fetch only what
  the step needs, batch drill-downs into one call, and never pull a patient's
  clinical detail or the whole slot grid. Applies to every heavy read.
allowed-tools:
  - mcp__dental__halo_fetch
  - mcp__dental__halo_fetch_many
---

# Halo navigation

Heavy reads (`get_patient_summary`, `find_open_slots`, `get_appointments`) return
an **envelope**, not the raw payload:

```
{ kind, summary, refs: { <name>: "h:sha256:..." }, map_root? }
```

Rules that keep a session cheap and safe:

1. **Read the summary first.** It is sized to let you decide. Most steps never
   need to fetch anything.
2. **Fetch only what the step needs.** `halo_fetch(handle)` returns the decoded
   content behind one handle — e.g. `contact` to read back a phone number.
3. **Never fetch clinical detail.** `get_patient_summary` exposes a `clinical`
   ref on purpose: reception work does not need it, so leave it unfetched. The
   minimum-necessary default for a medical record.
4. **Don't pull the whole grid.** `find_open_slots` gives `by_day` / `by_provider`
   counts and a `sample`. Reason on those and offer a slot; only `halo_fetch` the
   `all_slots` handle if the sample doesn't cover the caller's ask.
5. **Batch drill-downs.** Need several handles? Pass them together to
   `halo_fetch_many([h1, h2])` — one round trip instead of N.
6. **Reuse the patient map.** `get_patient_summary` and `get_appointments` fold
   into one growing map keyed by patient id (`map_root`); a handle seen earlier is
   still fetchable later — don't re-pull data you already hold a handle for.

The handles are stable content addresses, so the same data is never re-sent turn
after turn.
