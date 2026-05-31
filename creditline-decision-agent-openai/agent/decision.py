"""Reference implementation of the §3 decision rule set (framework-agnostic).

Identical to the Claude version's scorer — the decision logic belongs to the
domain, not the agent SDK. Used by the deterministic demo; the live agent
applies the same rules via its instructions.

Invariant: AUTO-DENY never happens. Every adverse outcome ESCALATES to a human
(GDPR Art. 22 / EU AI Act Art. 14).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass
class Recommendation:
    outcome: str                      # "approved" | "escalated"
    requires_human_approval: bool
    approved_limit: Decimal | None
    score: int
    dti: float
    delinquencies_24m: int
    reasons: list[str] = field(default_factory=list)
    failed_thresholds: list[str] = field(default_factory=list)


def evaluate(
    *,
    requested_limit: Decimal,
    annual_income: Decimal,
    bureau: dict[str, Any],
    policy_thresholds: dict[str, Any],
) -> Recommendation:
    score = int(bureau["credit_score"])
    outstanding = Decimal(str(bureau["total_outstanding_debt"]))
    delinq = int(bureau["delinquencies_24m"])

    dti = float((outstanding + requested_limit) / annual_income)

    min_score = int(policy_thresholds["min_credit_score"])
    max_dti = float(policy_thresholds["max_dti"])
    ceiling = Decimal(str(policy_thresholds["auto_approve_ceiling"]))
    max_delinq = int(policy_thresholds["max_delinquencies_24m"])

    failed: list[str] = []
    if score < min_score:
        failed.append(f"credit_score {score} < min_credit_score {min_score}")
    if dti > max_dti:
        failed.append(f"dti {dti:.3f} > max_dti {max_dti}")
    if delinq > max_delinq:
        failed.append(f"delinquencies_24m {delinq} > max {max_delinq}")

    above_ceiling = requested_limit > ceiling

    if not above_ceiling and not failed:
        return Recommendation(
            outcome="approved",
            requires_human_approval=False,
            approved_limit=requested_limit,
            score=score,
            dti=dti,
            delinquencies_24m=delinq,
            reasons=[
                f"requested_limit {requested_limit} <= auto_approve_ceiling {ceiling}",
                "all thresholds satisfied",
            ],
        )

    reasons: list[str] = []
    if above_ceiling:
        reasons.append(
            f"requested_limit {requested_limit} > auto_approve_ceiling {ceiling} (large exposure)"
        )
    reasons.extend(failed)

    return Recommendation(
        outcome="escalated",
        requires_human_approval=True,
        approved_limit=None,
        score=score,
        dti=dti,
        delinquencies_24m=delinq,
        reasons=reasons,
        failed_thresholds=failed,
    )
