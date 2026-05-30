"""Pydantic input/output contracts for the MCP tools.

Validating inputs at the boundary keeps the tool surface domain-shaped and makes
the resulting tool-call records semantic and control-mappable rather than opaque.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ── Inputs ───────────────────────────────────────────────────────────────────
class GetRequestIn(BaseModel):
    request_id: str


class GetCustomerIn(BaseModel):
    customer_id: str


class PullBureauIn(BaseModel):
    customer_id: str
    bureau_name: Literal["experian_sim", "equifax_sim"] = "experian_sim"


class GetActivePolicyIn(BaseModel):
    # No args: returns whichever policy row has effective_to IS NULL.
    pass


class RecordDecisionIn(BaseModel):
    request_id: str
    outcome: Literal["approved", "denied", "escalated"]
    rationale: str = Field(min_length=1)
    model_version: str
    prompt_version_hash: str
    policy_version: str
    bureau_report_id: str
    approved_limit: Optional[Decimal] = None


class RequestApprovalIn(BaseModel):
    decision_id: str
    approver_role: str = "lead_credit_officer"
    summary: str


class NotifyCustomerIn(BaseModel):
    request_id: str
    outcome: Literal["approved", "denied", "escalated"]
    approved_limit: Optional[Decimal] = None
    idempotency_key: str
