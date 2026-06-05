---
name: adjudicate
description: >-
  Call adjudicate_line per line for the deterministic numbers, attach the reason
  codes it selected from the reference set, record the decisions with evidence, and
  route any deny/reduce/pend through post_adjudication's human gate. Use after
  coverage-and-rules.
allowed-tools:
  - mcp__claims__adjudicate_line
  - mcp__claims__record_decision
  - mcp__claims__post_adjudication
  - mcp__claims__pend_claim
  - mcp__claims__lookup_reason_code
  - mcp__claims__halo_verify
---

# Adjudicate

1. **Per line, call the engine** — `adjudicate_line(line, rule, accumulators,
   allowed_cents, in_network, plan, checks)`. It returns the exact money
   (`allowed_cents`, `plan_paid_cents`, `patient_resp_cents`, `deductible_cents`,
   `coinsurance_cents`) and `suggested_carc` / `suggested_rarc`, plus a deterministic
   `decision` (pay | deny | reduce | pend). **Do not compute any of these yourself**
   and **do not change the amounts**.

2. **Reason codes** — use the engine's `suggested_carc`/`suggested_rarc`. Confirm
   them against `lookup_reason_code` if you want the wording; never invent a code.

3. **Record with evidence** — `record_decision(claim_id, lines[])`, and for each
   line attach `evidence`: the Halo handles the decision rested on (the claim map
   root, the history map root, the line/attachment handles). This is the
   tamper-evident audit record; `halo_verify` can re-hash it.

4. **Post through the gate** — `post_adjudication(claim_id)`. Any deny / reduce /
   pend line, or a claim total over the auto-finalize ceiling, **BLOCKS** until a
   human reviewer confirms. A clean, all-pay, within-ceiling claim auto-finalizes.
   Check the result: `committed: true` → state the outcome; otherwise the claim was
   **not** posted — say so, don't claim it paid.

Anything genuinely ambiguous or needing clinical eyes: `pend_claim` instead of
forcing a decision.
