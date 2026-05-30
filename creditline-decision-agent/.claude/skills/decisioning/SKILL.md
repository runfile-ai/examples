---
name: decisioning
description: >-
  Apply the policy decision rules to the scoring result, record the decision
  with full provenance, and route any adverse or above-ceiling outcome through
  the human-in-the-loop approval gate. Use as the final step of every request.
allowed-tools:
  - mcp__mimic-creditline__creditline_get_agent_provenance
  - mcp__mimic-creditline__creditline_record_decision
  - mcp__mimic-creditline__creditline_request_approval
  - mcp__mimic-creditline__creditline_notify_customer
---

# Decisioning

## 1. Apply the rules (exactly)

```
AUTO-APPROVE  if  requested_limit <= auto_approve_ceiling
              and score  >= min_credit_score
              and dti    <= max_dti
              and delinq <= max_delinquencies_24m

ESCALATE      if  requested_limit > auto_approve_ceiling      (large exposure)
              or  any single threshold fails                  (borderline / adverse)

AUTO-DENY     never. You do not deny. Every adverse outcome ESCALATES so a human
              confirms it (GDPR Art. 22 / EU AI Act Art. 14).
```

The outcome you record is therefore always `approved` or `escalated`.

## 2. Record the decision

First call `creditline_get_agent_provenance` and keep its `prompt_version_hash`.
Then call `creditline_record_decision` with a precise `rationale` (cite the
numbers and which rule fired) and full provenance: `model_version`, that
`prompt_version_hash`, `policy_version`, `bureau_report_id`, and — for an
approval — `approved_limit`. The tool returns `requires_human_approval`.

## 3. Human-in-the-loop

If `requires_human_approval` is true, call `creditline_request_approval` with a
short `summary` of the case and your recommendation. **This call blocks** until a
credit officer resolves it. The result tells you:

- `status`: `confirmed` | `rejected` | `modified`
- `is_override`: true when the officer went against your recommendation
- `modified_limit`: the limit the officer approved, if they modified

Honour the officer's resolution as the final outcome. If `status` is `rejected`,
the final outcome is a denial; if `modified`, it is an approval at
`modified_limit`. Only after the human has confirmed may the customer be told an
adverse result. Optionally send the letter with `creditline_notify_customer`
(use a stable `idempotency_key` such as the decision id).
