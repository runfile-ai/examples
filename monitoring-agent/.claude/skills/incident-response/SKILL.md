---
name: incident-response
description: >-
  Propose declaring or resolving an incident, and acknowledge, assign, or
  comment. All real-world writes route through the human approval gate. Use once
  a high-impact issue is diagnosed.
allowed-tools:
  - mcp__monitoring__declare_incident
  - mcp__monitoring__resolve_incident
  - mcp__monitoring__acknowledge_incident
  - mcp__monitoring__assign_incident
  - mcp__monitoring__list_incidents
---

# Incident response

Real-world writes page real people once integrated, so the high-risk ones are
**gated**: the tool proposes to the approval queue and BLOCKS until a human
confirms or rejects. Honour the result.

- **declare_incident(issue_id, severity, summary)** — gated. Declare only for a
  genuine, high-impact problem. Dedup is automatic (one incident per issue via
  `dedup_key = issue_id`), so calling twice will not double-page. On return,
  check `committed`: if `false` (rejected/timed out), do **not** claim an
  incident exists.
- **resolve_incident(incident_id, note)** — gated. Resolve when the problem is
  handled; the `note` is attached to the incident.
- **acknowledge_incident(incident_id)** — direct, low risk.
- **assign_incident(incident_id, user)** — direct, low risk.
- **list_incidents(status?, service?)** — to see current state before acting.

After a gated write resolves, state the final outcome plainly: incident id,
status, and who approved it. Never announce an incident the human rejected.
