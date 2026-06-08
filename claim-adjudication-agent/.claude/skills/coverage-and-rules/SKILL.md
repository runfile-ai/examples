---
name: coverage-and-rules
description: >-
  For each line, gather the benefit rule, frequency history, accumulators, fee
  schedule, and network status, and judge which rules apply — including the edge
  cases the engine cannot resolve alone. Produces the `checks` that adjudicate_line
  consumes. Use after intake, before adjudicate.
allowed-tools:
  - mcp__claims__get_benefit_rules
  - mcp__claims__get_accumulators
  - mcp__claims__get_allowed_amount
  - mcp__claims__get_claim_history
  - mcp__claims__check_network
  - mcp__claims__lookup_reason_code
  - mcp__claims__halo_fetch
  - mcp__claims__halo_fetch_many
---

# Coverage and rules

For the codes on this claim (only those — not the whole plan):

1. **Benefit rules** — `get_benefit_rules(plan_id, codes)`: coverage %, category,
   `frequency_per_year`, `waiting_months`, `requires_preauth`, `covered`.

2. **Accumulators** — `get_accumulators(member_id, plan_year)`: deductible met,
   annual max used, OOP met. These drive the math.

3. **Allowed amounts** — `get_allowed_amount(plan_id, codes)` from the fee schedule.

4. **Judge the per-line `checks`** — this is the part the engine cannot do; you
   reason it out and pass booleans to `adjudicate_line`:
   - `within_frequency` — `get_claim_history(member_id, code=…, exclude_claim_id=THIS_CLAIM)`.
     Count prior occurrences in the window against `frequency_per_year`. **Always set
     `exclude_claim_id`** so the claim you are adjudicating is not counted as its own history.
   - `is_duplicate` — same code/tooth/date already adjudicated → duplicate (CARC 18).
   - `past_waiting` — months since `effective_date` ≥ `waiting_months`.
   - `preauth_on_file` — for `requires_preauth` codes, is the attachment/narrative present?
     `halo_fetch` the claim's `attachments` to check.
   - `missing_info` — required data absent.

5. **Reason codes** — when you need a human-readable code, `lookup_reason_code`.
   Select from the reference set; never invent one.

Hand the rule, accumulators, allowed amount, network flag, and `checks` per line to
**adjudicate**.
