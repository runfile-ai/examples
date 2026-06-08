---
name: halo-navigation
description: >-
  How to work with Halo envelopes efficiently and verifiably: read the summary,
  fetch only what each check needs, batch the fetches, pull clinical attachments
  only when a line needs them, and use the handles as tamper-evident evidence.
  Applies to every heavy read.
allowed-tools:
  - mcp__claims__halo_fetch
  - mcp__claims__halo_fetch_many
  - mcp__claims__halo_verify
---

# Halo navigation

Heavy reads (`get_claim`, `get_claim_history`) return an **envelope**, not the raw
payload:

```
{ kind, summary, refs: { <name>: "h:sha256:..." }, map_root? }
```

Rules that keep adjudication cheap and the record verifiable:

1. **Read the summary first.** `get_claim`'s summary already has the line codes,
   amounts, and statuses — enough to drive most of adjudication.
2. **Fetch only what a check needs.** `halo_fetch(handle)` for one, and pull a
   claim's `attachments` only when a line needs clinical review (a major service,
   a pre-auth check) — not by default.
3. **Slice history.** `get_claim_history` to the `code` and `window_months` a
   frequency/duplicate check needs; reason on `by_code` before fetching `lines`.
4. **Batch drill-downs.** Several handles → `halo_fetch_many([h1, h2])`, one round trip.
5. **Reuse the map.** `get_claim` and `get_claim_history` fold into one
   claim-and-member map (`map_root`) keyed by claim/member id; a handle seen earlier
   is still fetchable later.
6. **Handles are evidence.** A handle is the sha256 of its content, so recording it
   in a decision's `evidence` both points at the data and proves integrity.
   `halo_verify(handles)` re-hashes and confirms nothing was altered — the
   tamper-evidence an appeal or audit relies on.
