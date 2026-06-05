---
name: intake-and-validate
description: >-
  The clean-claim gate. Parse the claim, confirm member eligibility and effective
  dates, confirm the provider network, and check the claim is complete. Missing
  required data pends with CARC 16 rather than guessing. Use first, before any
  line is adjudicated.
allowed-tools:
  - mcp__claims__get_claim
  - mcp__claims__get_member_coverage
  - mcp__claims__check_network
  - mcp__claims__pend_claim
  - mcp__claims__halo_fetch
  - mcp__claims__halo_fetch_many
---

# Intake and validate

1. **Read the claim** — `get_claim(claim_id)`. The envelope summary has the header
   and the line codes/amounts; `refs` hold `full_lines`, `diagnosis_codes`, and
   `attachments`. Don't fetch attachments yet — only when a specific line needs
   clinical review.

2. **Eligibility** — `get_member_coverage(member_id)`. Confirm `eligible` is true
   and the dates of service fall within `effective_date`..`term_date`. A termed or
   not-yet-effective member's services pend or deny (CARC 26/27 family) — route to
   a human, do not auto-decide.

3. **Network** — `check_network(provider_id, plan_id)`. Out-of-network is not an
   auto-deny; it changes the math (balance billing) and routes to review.

4. **Completeness** — every line needs a procedure code, date of service, and a
   charge; a major/pre-auth service needs its attachment. If something required is
   missing, `pend_claim(claim_id, reason)` with CARC 16 (missing information)
   rather than guessing. This is the clean-claim gate.

Hand a complete, eligible claim to **coverage-and-rules**.
