---
name: reschedule-cancel
description: >-
  Move or cancel an existing appointment. Find the appointment, propose the
  change, and commit it through the human write gate. Use when a caller wants to
  change or drop an appointment they already have.
allowed-tools:
  - mcp__dental__find_patient
  - mcp__dental__verify_identity
  - mcp__dental__get_appointments
  - mcp__dental__find_open_slots
  - mcp__dental__reschedule
  - mcp__dental__cancel
  - mcp__dental__halo_fetch
---

# Reschedule / cancel

1. **Identify and verify** — same identity gate as booking
   (`find_patient` → `verify_identity`). Required before you can list their
   appointments.

2. **Find the appointment** — `get_appointments(patient_id)`; the envelope
   summary lists upcoming appointments with their ids. Confirm which one the
   caller means (descriptor, provider, time).

3. **Reschedule** — if they want a new time, `find_open_slots` for the same
   descriptor to offer a slot, then
   `reschedule(appointment_id, { start_time, operatory_id? })`. **Gated** — it
   blocks for a human confirm. A clash returns `committed: false,
   error: "slot_taken"`; pick another slot.

4. **Cancel** — `cancel(appointment_id, reason)`. **Gated.** Capture a short
   reason for the record.

For either, only tell the caller the change is done once the gated call returns
`committed: true`.
