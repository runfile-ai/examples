---
name: scoring
description: >-
  Retrieve the active versioned policy and score a credit-line request against
  it: compute DTI and compare credit score, DTI, delinquencies, and requested
  limit to the policy thresholds. Use after intake, before deciding.
allowed-tools:
  - mcp__mimic-creditline__creditline_get_active_policy
---

# Scoring

1. **Retrieve policy** — `creditline_get_active_policy()`. This is the retrieval
   evidence node. Record the `version` and the `thresholds`:
   `min_credit_score`, `max_dti`, `auto_approve_ceiling`, `max_delinquencies_24m`.

2. **Compute, showing arithmetic:**

   ```
   score  = bureau.credit_score
   dti    = (bureau.total_outstanding_debt + requested_limit) / customer.annual_income
   delinq = bureau.delinquencies_24m
   ```

3. **Compare each signal to its threshold** and state pass/fail explicitly:
   - `score  >= min_credit_score`
   - `dti    <= max_dti`
   - `delinq <= max_delinquencies_24m`
   - `requested_limit <= auto_approve_ceiling`

Report the four comparisons and the exact numbers. Do not round away precision
on DTI — quote at least three decimals. The decisioning skill consumes these
results verbatim.
