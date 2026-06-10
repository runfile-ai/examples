---
name: intake
description: >-
  Gather everything needed to assess a credit-line request: the request itself,
  the customer profile and existing lines, and a fresh bureau report. Use at the
  start of every credit-line decision before any scoring.
allowed-tools:
  - mcp__mimic-creditline__creditline_get_request
  - mcp__mimic-creditline__creditline_get_customer
  - mcp__mimic-creditline__creditline_pull_bureau
---

# Intake

Assemble the full evidence set for a credit-line request, in order:

1. **Request** — `creditline_get_request(request_id)`. Note `request_type`
   (new vs increase), `requested_limit`, and `customer_id`.
2. **Customer** — `creditline_get_customer(customer_id)`. Capture
   `annual_income`, `employment_status`, `residential_status`, and any existing
   `credit_lines` (current limits/balances matter for exposure).
3. **Bureau** — `creditline_pull_bureau(customer_id)`. Record the
   `bureau_report_id`, `credit_score`, `total_outstanding_debt`,
   `delinquencies_24m`, and `hard_inquiries_6m`.

Carry forward `bureau_report_id` — it is required provenance when the decision
is recorded. Do not proceed to scoring until all three are retrieved. Never
fabricate values; if a tool returns an error, stop and report it.
