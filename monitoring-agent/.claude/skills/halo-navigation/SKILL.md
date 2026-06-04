---
name: halo-navigation
description: >-
  How to work with Halo envelopes efficiently: read the summary, fetch only what
  the step needs, batch drill-downs into one call, and slice logs rather than
  pulling the whole window. Applies to every heavy read.
allowed-tools:
  - mcp__monitoring__halo_fetch
  - mcp__monitoring__halo_fetch_many
---

# Halo navigation

Heavy reads (`list_open_issues`, `get_issue_detail`, `search_logs`) return an
**envelope**, not the raw payload:

```
{ kind, summary, refs: { <name>: "h:sha256:..." }, map_root? }
```

Rules that keep a long run cheap:

1. **Read the summary first.** It is sized to let you triage and decide. Most
   steps never need to fetch anything.
2. **Fetch only what the step needs.** `halo_fetch(handle)` returns the decoded
   content behind one handle. Never fetch `full_list` or a whole log window just
   to "look" — fetch the specific ref (e.g. `stacktrace`, `errors`).
3. **Batch drill-downs.** When you need several handles, pass them together to
   `halo_fetch_many([h1, h2])` — one round trip instead of N.
4. **Slice logs.** Prefer the `errors` ref or a narrower `search_logs` window
   over the full `lines` payload.
5. **Reuse the map.** `get_issue_detail` folds repeated lookups of the same
   issue into one growing map (`map_root`); a handle seen earlier in the run is
   still fetchable later — don't re-pull data you already have a handle for.

The handles are stable content addresses, so the same data is never re-sent
turn after turn. That compounding saving is the whole point.
