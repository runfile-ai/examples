---
name: triage
description: >-
  Pull open issues, rank by users affected, event count, severity, and recency,
  and pick the few that matter. Record each decision with triage_note. Use at
  the start of every monitoring run.
allowed-tools:
  - mcp__monitoring__list_open_issues
  - mcp__monitoring__halo_fetch
  - mcp__monitoring__triage_note
---

# Triage

1. **Pull** — `list_open_issues` (optionally filter `severity: ["error","fatal"]`).
   You get a Halo envelope: a `summary` with `total`, `by_level`, and a `top`
   ranked table. Reason on the summary; do **not** fetch `refs.full_list` unless
   the top table is insufficient.

2. **Rank** the candidates by, in order: users affected (`user_count`), event
   count (`times_seen`), severity (`fatal` > `error` > `warning` > `info`), and
   recency (`last_seen`). Pick the few that genuinely matter — usually 1–3.

3. **Record** a decision for each one you considered with `triage_note`:
   - `declared` — high impact, will become an incident (hand to diagnose).
   - `watch` — real but not yet actionable.
   - `ignored` — noise / known / low impact.
   - `resolved` — already handled.

`triage_note` is a direct, low-risk write and dedups across runs (the agent
won't re-surface an issue it already decided). Hand the `declared` issue(s) to
the **diagnose** skill.
