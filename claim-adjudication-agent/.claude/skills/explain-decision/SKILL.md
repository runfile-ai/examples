---
name: explain-decision
description: >-
  Produce the EOB-style rationale for each line — the decision, the amounts, the
  reason codes, the rule basis, and the evidence it rested on. Use at the end, once
  decisions are recorded/posted, especially for any denied or reduced line.
allowed-tools:
  - mcp__claims__lookup_reason_code
  - mcp__claims__halo_fetch
  - mcp__claims__halo_fetch_many
  - mcp__claims__halo_verify
---

# Explain decision

For each line, give the member/provider-facing explanation an EOB needs:

1. **Decision and money** — pay / deny / reduce / pend, with `allowed`,
   `plan_paid`, and `patient_resp` exactly as the engine computed them (cite cents
   as dollars).

2. **Why** — the reason codes in plain language (`lookup_reason_code` for the text)
   and the rule basis: which benefit rule, which check fired (frequency limit hit,
   annual max reached, non-covered, out-of-network, pre-auth absent…).

3. **Evidence** — name the data the decision rested on, and that it is verifiable:
   the `evidence` handles are content hashes, so `halo_verify` proves the bytes
   behind the decision were not altered after the fact. This is the answer to "how
   was this decided?" on an appeal or a regulatory review.

Be precise and neutral. For a denial or reduction, make the path to appeal clear:
the specific rule and the specific evidence.
