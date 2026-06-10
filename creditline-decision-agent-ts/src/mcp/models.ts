// Zod input contracts for the MCP tools (the TypeScript analogue of the Python
// build's Pydantic models). Validating inputs at the boundary keeps the tool
// surface domain-shaped and makes the resulting tool-call records semantic and
// control-mappable rather than opaque.
import { z } from "zod";

export const GetRequestIn = z.object({
  request_id: z.string(),
});

export const GetCustomerIn = z.object({
  customer_id: z.string(),
});

export const PullBureauIn = z.object({
  customer_id: z.string(),
  bureau_name: z.enum(["experian_sim", "equifax_sim"]).default("experian_sim"),
});

export const GetActivePolicyIn = z.object({});

export const RecordDecisionIn = z.object({
  request_id: z.string(),
  outcome: z.enum(["approved", "denied", "escalated"]),
  rationale: z.string().min(1),
  model_version: z.string(),
  prompt_version_hash: z.string(),
  policy_version: z.string(),
  bureau_report_id: z.string(),
  approved_limit: z.number().nullable().optional(),
});

export const RequestApprovalIn = z.object({
  decision_id: z.string(),
  summary: z.string(),
  approver_role: z.string().default("lead_credit_officer"),
});

export const NotifyCustomerIn = z.object({
  request_id: z.string(),
  outcome: z.enum(["approved", "denied", "escalated"]),
  idempotency_key: z.string(),
  approved_limit: z.number().nullable().optional(),
});
