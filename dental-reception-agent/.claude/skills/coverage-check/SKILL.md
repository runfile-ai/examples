---
name: coverage-check
description: >-
  Answer a standalone insurance question — eligibility and copay for an
  appointment type — without booking anything. Use when a caller only wants to
  know what something will cost or whether they're covered.
allowed-tools:
  - mcp__dental__find_patient
  - mcp__dental__verify_identity
  - mcp__dental__list_appointment_types
  - mcp__dental__check_coverage
---

# Coverage check

1. **Identify and verify** — `find_patient` → `verify_identity`. Insurance is
   PHI; the identity gate applies.

2. **Check** — resolve the spoken type with `list_appointment_types`, then
   `check_coverage(patient_id, descriptor_id)` for the appointment type in question. Report `eligibility`, `coverage_pct`, and the `copay_cents`
   in plain language (e.g. "active Delta Dental, preventive covered 100%, $0
   copay").

3. If `eligibility` is `inactive` or `unknown`, say so plainly and suggest the
   patient confirm with their carrier — do not guess a copay.

This skill never books, holds, or changes anything.
