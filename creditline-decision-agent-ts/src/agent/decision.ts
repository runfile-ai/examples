// Reference implementation of the §3 decision rule set.
//
// Policy-driven so a decision can change by versioning the *policy*, not the
// code. This module is the canonical scorer: the deterministic demo
// (src/scripts/run-demo.ts) calls it directly, and the live agent's
// `decisioning` skill instructs the model to apply exactly these rules to the
// retrieved policy.
//
// Key invariant: AUTO-DENY never happens. Every adverse outcome ESCALATES to a
// human (GDPR Art. 22 / EU AI Act Art. 14).

export interface Bureau {
  credit_score: number;
  total_outstanding_debt: number;
  delinquencies_24m: number;
}

export interface PolicyThresholds {
  min_credit_score: number;
  max_dti: number;
  auto_approve_ceiling: number;
  max_delinquencies_24m: number;
}

export interface Recommendation {
  outcome: "approved" | "escalated";
  requires_human_approval: boolean;
  approved_limit: number | null;
  score: number;
  dti: number;
  delinquencies_24m: number;
  reasons: string[];
  failed_thresholds: string[];
}

export function evaluate(params: {
  requested_limit: number;
  annual_income: number;
  bureau: Bureau;
  policy_thresholds: PolicyThresholds;
}): Recommendation {
  const { requested_limit, annual_income, bureau, policy_thresholds } = params;

  const score = Number(bureau.credit_score);
  const outstanding = Number(bureau.total_outstanding_debt);
  const delinq = Number(bureau.delinquencies_24m);

  const dti = (outstanding + requested_limit) / annual_income;

  const minScore = Number(policy_thresholds.min_credit_score);
  const maxDti = Number(policy_thresholds.max_dti);
  const ceiling = Number(policy_thresholds.auto_approve_ceiling);
  const maxDelinq = Number(policy_thresholds.max_delinquencies_24m);

  const failed: string[] = [];
  if (score < minScore) failed.push(`credit_score ${score} < min_credit_score ${minScore}`);
  if (dti > maxDti) failed.push(`dti ${dti.toFixed(3)} > max_dti ${maxDti}`);
  if (delinq > maxDelinq) failed.push(`delinquencies_24m ${delinq} > max ${maxDelinq}`);

  const aboveCeiling = requested_limit > ceiling;

  if (!aboveCeiling && failed.length === 0) {
    return {
      outcome: "approved",
      requires_human_approval: false,
      approved_limit: requested_limit,
      score,
      dti,
      delinquencies_24m: delinq,
      reasons: [
        `requested_limit ${requested_limit} <= auto_approve_ceiling ${ceiling}`,
        "all thresholds satisfied",
      ],
      failed_thresholds: [],
    };
  }

  const reasons: string[] = [];
  if (aboveCeiling) {
    reasons.push(`requested_limit ${requested_limit} > auto_approve_ceiling ${ceiling} (large exposure)`);
  }
  reasons.push(...failed);

  return {
    outcome: "escalated",
    requires_human_approval: true,
    approved_limit: null,
    score,
    dti,
    delinquencies_24m: delinq,
    reasons,
    failed_thresholds: failed,
  };
}
