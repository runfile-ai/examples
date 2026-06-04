---
name: booking
description: >-
  Book a new appointment end to end: identify and verify the patient, check
  coverage if relevant, find slots matching their stated preferences, hold a
  slot, and commit it through the human write gate. Use whenever a caller wants
  a new appointment.
allowed-tools:
  - mcp__dental__find_patient
  - mcp__dental__verify_identity
  - mcp__dental__get_patient_summary
  - mcp__dental__check_coverage
  - mcp__dental__list_appointment_types
  - mcp__dental__find_open_slots
  - mcp__dental__hold_slot
  - mcp__dental__book_appointment
  - mcp__dental__halo_fetch
  - mcp__dental__halo_fetch_many
---

# Booking

1. **Identify** — `find_patient` by phone, email, or name+dob. You get only thin
   candidates (name + masked phone). If several match, disambiguate by something
   the caller can confirm — do **not** read out details.

2. **Verify (identity gate)** — `verify_identity(patient_id, last_name, dob)`.
   Until this returns `verified: true`, the disclosure reads refuse. Never reveal
   anything about the patient before it passes.

3. **Resolve the type** — the caller speaks a type ("a cleaning"), not an id.
   Call `list_appointment_types` and pick the matching `descriptor_id`; the other
   schedule tools (`check_coverage`, `find_open_slots`, `hold_slot`) all key on
   that id, so never guess it.

4. **Coverage (if relevant)** — `check_coverage(patient_id, descriptor_id)` for
   the appointment type, so you can quote eligibility and copay.

5. **Find slots** — `find_open_slots(descriptor_id, …)` with the caller's stated
   preferences (`provider`, `time_of_day: AM|PM`, `from`/`to` window). Reason on
   the envelope summary (`by_day`, `by_provider`, `sample`); `halo_fetch` the
   `all_slots` handle only if you need more than the sample. Offer one concrete
   slot: date, time, provider.

6. **Hold** — once the caller picks, `hold_slot(patient_id, descriptor_id,
   start_time, provider_id, operatory_id, location_id)`. This is an agent-local,
   short-TTL reservation — the double-book defense — not yet a real booking.

7. **Book (write gate)** — `book_appointment(hold_id)`. This is **gated**: it
   proposes to the approval queue and BLOCKS until a human confirms. Check the
   result: `committed: true` → state the confirmed appointment id and time;
   `committed: false` with `error: "slot_taken"` → the chair was taken in a race,
   so find another slot; any other non-commit → do **not** tell the caller it was
   booked.
