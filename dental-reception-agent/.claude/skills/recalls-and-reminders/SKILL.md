---
name: recalls-and-reminders
description: >-
  The scheduled, admin-batch mode. Find patients overdue for a recall or with
  upcoming appointments needing confirmation, and propose outreach. Use for
  batch reception work rather than a single live caller.
allowed-tools:
  - mcp__dental__find_recalls
  - mcp__dental__get_appointments
  - mcp__dental__add_to_waitlist
  - mcp__dental__confirm_appointment
  - mcp__dental__halo_fetch
---

# Recalls and reminders

This is staff-side batch work, not a live caller, so it operates on outreach
lists rather than a single verified session.

1. **Pull the batch** — `find_recalls({ kind })`:
   - `kind: "recall"` — patients past their `recall_due` (due for a cleaning).
   - `kind: "reminder"` — booked appointments in the next few days that are not
     yet confirmed.
   Both return thin candidates with masked phones — enough to drive outreach,
   no clinical detail.

2. **Propose outreach** — for each, summarise who to contact and why (overdue
   since X / appointment on Y). Keep it a list the front desk can action.

3. **Low-risk follow-through** — when a reminder is acknowledged,
   `confirm_appointment(appointment_id)`. If a recall patient wants the next open
   cleaning but none suits, `add_to_waitlist(patient_id, descriptor_id, prefs)`.
   Both are direct, low-risk writes.

Anything that creates or moves an appointment still goes through the **booking**
or **reschedule-cancel** skills and their write gate.
