---
name: diagnose
description: >-
  For a chosen issue, fetch the stacktrace and the breadcrumb near the error,
  slice logs around the spike, and form a hypothesis grounded in the data. Use
  after triage, before proposing an incident.
allowed-tools:
  - mcp__monitoring__get_issue_detail
  - mcp__monitoring__search_logs
  - mcp__monitoring__halo_fetch
  - mcp__monitoring__halo_fetch_many
---

# Diagnose

1. **Detail** — `get_issue_detail(issue_id)`. The envelope `summary` gives the
   issue core + the latest exception type/value; `refs` carve out `stacktrace`,
   `breadcrumbs`, `tags`, and `events`.

2. **Drill in (batched)** — fetch only what you need, and batch it:
   `halo_fetch_many([refs.stacktrace, refs.breadcrumbs])`. Find the top **in-app**
   frame (`in_app: true`, has a `context_line`) — that is the culprit. Read the
   breadcrumb immediately before the error for what happened just prior.

3. **Correlate with logs** — `search_logs` scoped to the relevant service/level
   and window (e.g. `{ service, level: "error" }`). Reason on the summary
   (`error_count`, `by_service`, `window`); `halo_fetch(refs.errors)` for the
   error slice only. Do not pull the whole window.

4. **Hypothesis** — state the likely cause in one or two sentences, citing the
   frame, the breadcrumb, and the correlated log spike. This feeds the
   **incident-response** skill.
