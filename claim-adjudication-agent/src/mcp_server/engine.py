"""The deterministic adjudication engine.

This module computes the money. It is pure functions — no LLM, no database, no
network, no clock. Given the same inputs it always returns the same numbers and
the same suggested reason codes. The model NEVER does this arithmetic and never
invents a code; it gathers the inputs, judges which rules apply (the `checks`
below), and selects/confirms reason codes from ext.reason_codes.

The split that matters: `checks` are the LLM's judgement (is this a duplicate? is
the member past the waiting period? is a pre-auth on file?), passed in as booleans.
Everything downstream of `checks` — deductible, coinsurance, annual max, OOP cap,
network reduction, the CARC groups and amounts, and the resulting disposition — is
fixed code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Carc:
    code: str
    group: str  # PR (patient responsibility) | CO (contractual) | OA | PI
    amount_cents: int

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "group": self.group, "amount_cents": self.amount_cents}


@dataclass
class LineResult:
    decision: str  # pay | deny | reduce | pend
    allowed_cents: int = 0
    plan_paid_cents: int = 0
    patient_resp_cents: int = 0
    deductible_cents: int = 0
    coinsurance_cents: int = 0
    copay_cents: int = 0
    suggested_carc: list[Carc] = field(default_factory=list)
    suggested_rarc: list[str] = field(default_factory=list)
    rule_basis: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "allowed_cents": self.allowed_cents,
            "plan_paid_cents": self.plan_paid_cents,
            "patient_resp_cents": self.patient_resp_cents,
            "deductible_cents": self.deductible_cents,
            "coinsurance_cents": self.coinsurance_cents,
            "copay_cents": self.copay_cents,
            "suggested_carc": [c.to_dict() for c in self.suggested_carc],
            "suggested_rarc": self.suggested_rarc,
            "rule_basis": self.rule_basis,
            "computed_by": "engine",
        }


def _i(v: Any, default: int = 0) -> int:
    return int(v) if v is not None else default


def adjudicate_line(
    line: dict[str, Any],
    rule: dict[str, Any] | None,
    accumulators: dict[str, Any],
    allowed_cents: int | None,
    in_network: bool,
    plan: dict[str, Any],
    checks: dict[str, Any] | None = None,
) -> LineResult:
    """Adjudicate one service line. See module docstring for the LLM/engine split.

    `checks` (all optional, default to the clean path):
      missing_info, is_duplicate, within_frequency, past_waiting, preauth_on_file
    """
    checks = checks or {}
    charged = _i(line.get("charged_cents"))
    basis: dict[str, Any] = {
        "procedure_code": line.get("procedure_code"),
        "in_network": in_network,
        "checks": {
            "missing_info": bool(checks.get("missing_info", False)),
            "is_duplicate": bool(checks.get("is_duplicate", False)),
            "within_frequency": bool(checks.get("within_frequency", True)),
            "past_waiting": bool(checks.get("past_waiting", True)),
            "preauth_on_file": bool(checks.get("preauth_on_file", True)),
        },
    }

    # ── Gate 1: things that PEND for a human before any money is computed ───────
    if checks.get("missing_info"):
        return LineResult("pend", suggested_carc=[Carc("16", "CO", charged)], suggested_rarc=["N706"], rule_basis={**basis, "fired": "missing_information"})
    if checks.get("is_duplicate"):
        return LineResult("pend", suggested_carc=[Carc("18", "OA", charged)], rule_basis={**basis, "fired": "duplicate_claim"})

    # ── Gate 2: coverage ────────────────────────────────────────────────────────
    if rule is None or not rule.get("covered", False):
        basis["category"] = rule.get("category") if rule else None
        return LineResult("deny", allowed_cents=0, plan_paid_cents=0, patient_resp_cents=charged,
                          suggested_carc=[Carc("96", "PR", charged)], rule_basis={**basis, "fired": "non_covered"})

    basis["category"] = rule.get("category")
    basis["coverage_pct"] = rule.get("coverage_pct")

    if rule.get("requires_preauth") and not checks.get("preauth_on_file", True):
        return LineResult("pend", suggested_carc=[Carc("197", "CO", charged)], rule_basis={**basis, "fired": "preauth_required"})
    if not checks.get("past_waiting", True):
        return LineResult("pend", suggested_carc=[Carc("26", "PR", charged)], rule_basis={**basis, "fired": "waiting_period"})
    if not checks.get("within_frequency", True):
        return LineResult("pend", suggested_carc=[Carc("119", "PR", charged)], rule_basis={**basis, "fired": "frequency_limit"})

    # ── The money (deterministic from here) ─────────────────────────────────────
    allowed = min(charged, _i(allowed_cents, charged)) if allowed_cents is not None else charged
    fee_writeoff = max(0, charged - allowed) if in_network else 0   # CO 45, provider writes off
    oon_balance = max(0, charged - allowed) if not in_network else 0  # PR 242, member may be balance-billed

    ded_remaining = max(0, _i(plan.get("deductible_cents")) - _i(accumulators.get("deductible_met_cents")))
    ded_applied = min(ded_remaining, allowed)
    after_ded = allowed - ded_applied

    pct = _i(rule.get("coverage_pct"))
    plan_share = round(after_ded * pct / 100)
    member_coins = after_ded - plan_share

    max_remaining = max(0, _i(plan.get("annual_max_cents")) - _i(accumulators.get("annual_max_used_cents")))
    plan_paid = min(plan_share, max_remaining)
    over_max = plan_share - plan_paid   # member owes the part the annual max would not cover

    carc: list[Carc] = []
    if fee_writeoff > 0:
        carc.append(Carc("45", "CO", fee_writeoff))
    if ded_applied > 0:
        carc.append(Carc("1", "PR", ded_applied))
    if member_coins > 0:
        carc.append(Carc("2", "PR", member_coins))
    if over_max > 0:
        carc.append(Carc("119", "PR", over_max))
    if oon_balance > 0:
        carc.append(Carc("242", "PR", oon_balance))

    patient_resp = ded_applied + member_coins + over_max + oon_balance

    # ── OOP cap: deductible + coinsurance count toward the out-of-pocket max ────
    oop_remaining = max(0, _i(plan.get("oop_max_cents")) - _i(accumulators.get("oop_met_cents")))
    pr_toward_oop = ded_applied + member_coins
    if oop_remaining and pr_toward_oop > oop_remaining:
        shift = pr_toward_oop - oop_remaining
        plan_paid += shift
        patient_resp -= shift
        basis["oop_cap_applied_cents"] = shift

    # ── Disposition (deterministic) ─────────────────────────────────────────────
    if over_max > 0 or oon_balance > 0:
        decision = "reduce"   # paying less than the plan would otherwise pay → human
    else:
        decision = "pay"

    basis["fired"] = "adjudicated"
    return LineResult(
        decision=decision,
        allowed_cents=allowed,
        plan_paid_cents=plan_paid,
        patient_resp_cents=patient_resp,
        deductible_cents=ded_applied,
        coinsurance_cents=member_coins,
        copay_cents=0,
        suggested_carc=carc,
        suggested_rarc=[],
        rule_basis=basis,
    )
