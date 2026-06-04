---
name: patient-admin
description: >-
  Basic patient admin: update contact details and manage the waitlist. Use for
  housekeeping changes that are not appointments.
allowed-tools:
  - mcp__dental__find_patient
  - mcp__dental__verify_identity
  - mcp__dental__update_contact
  - mcp__dental__add_to_waitlist
---

# Patient admin

1. **Identify and verify** — `find_patient` → `verify_identity`. Required before
   any change to a patient record.

2. **Update contact** — `update_contact(patient_id, { phone?, email?, address? })`.
   **Gated** — it proposes to the approval queue and blocks for a human confirm,
   since it changes the record. Only the three contact fields are updatable here;
   confirm the new value back to the caller once `committed: true`.

3. **Waitlist** — `add_to_waitlist(patient_id, descriptor_id, { provider_pref,
   window_pref })` for a patient who wants an earlier slot than is currently
   open. Low risk, direct write.

Keep changes to exactly what the caller asked for; never edit fields they did not
mention.
