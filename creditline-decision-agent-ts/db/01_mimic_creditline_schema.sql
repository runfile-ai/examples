-- ============================================================================
-- ENVIRONMENT DB  —  mimic_creditline
-- Mutable, Mimic-served simulated data. The agent (via the MCP server) reads
-- and writes here. This is the "world" the agent acts on.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()

-- Customers ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS customers (
    customer_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    full_name              text          NOT NULL,
    date_of_birth          date          NOT NULL,
    email                  text          NOT NULL,
    annual_income          numeric(14,2) NOT NULL,
    employment_status      text          NOT NULL,   -- employed | self_employed | retired | ...
    residential_status     text          NOT NULL,   -- owner | renter | other
    relationship_since     date,
    internal_risk_segment  text,                      -- A | B | C | D
    created_at             timestamptz   NOT NULL DEFAULT now()
);

-- Existing credit lines / accounts ------------------------------------------
CREATE TABLE IF NOT EXISTS credit_lines (
    line_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id     uuid NOT NULL REFERENCES customers(customer_id),
    product_type    text NOT NULL,                    -- revolving | overdraft | card
    current_limit   numeric(14,2) NOT NULL,
    current_balance numeric(14,2) NOT NULL DEFAULT 0,
    status          text NOT NULL DEFAULT 'active',   -- active | closed | suspended
    opened_at       timestamptz NOT NULL DEFAULT now()
);

-- Inbound requests the agent acts on ----------------------------------------
CREATE TABLE IF NOT EXISTS credit_line_requests (
    request_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id     uuid NOT NULL REFERENCES customers(customer_id),
    request_type    text NOT NULL,                    -- new | increase
    requested_limit numeric(14,2) NOT NULL,
    channel         text NOT NULL,                    -- web | app | branch
    status          text NOT NULL DEFAULT 'pending',  -- pending | approved | denied | escalated
    submitted_at    timestamptz NOT NULL DEFAULT now()
);

-- Simulated credit-bureau reports -------------------------------------------
CREATE TABLE IF NOT EXISTS bureau_reports (
    bureau_report_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id            uuid NOT NULL REFERENCES customers(customer_id),
    bureau_name            text NOT NULL,             -- experian_sim | equifax_sim
    report_version         text NOT NULL,
    credit_score           int  NOT NULL,             -- 300..850
    total_outstanding_debt numeric(14,2) NOT NULL,
    delinquencies_24m      int  NOT NULL DEFAULT 0,
    open_accounts          int  NOT NULL DEFAULT 0,
    hard_inquiries_6m      int  NOT NULL DEFAULT 0,
    pulled_at              timestamptz NOT NULL DEFAULT now()
);

-- Versioned decision policy (the retrieval node) ----------------------------
CREATE TABLE IF NOT EXISTS decision_policies (
    policy_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    version        text NOT NULL UNIQUE,              -- e.g. "2026.03-rev2"
    thresholds     jsonb NOT NULL,                    -- {min_credit_score, max_dti,
                                                       --  auto_approve_ceiling,
                                                       --  max_delinquencies_24m}
    narrative      text,
    effective_from timestamptz NOT NULL,
    effective_to   timestamptz                        -- NULL = currently active
);

-- Recorded decisions (written by the record-decision tool) ------------------
CREATE TABLE IF NOT EXISTS decisions (
    decision_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id              uuid NOT NULL UNIQUE REFERENCES credit_line_requests(request_id),
    customer_id             uuid NOT NULL REFERENCES customers(customer_id),
    outcome                 text NOT NULL,            -- approved | denied | escalated
    approved_limit          numeric(14,2),
    rationale               text NOT NULL,
    model_version           text NOT NULL,
    prompt_version_hash     text NOT NULL,
    policy_version          text NOT NULL REFERENCES decision_policies(version),
    bureau_report_id        uuid NOT NULL REFERENCES bureau_reports(bureau_report_id),
    requires_human_approval boolean NOT NULL DEFAULT false,
    decided_at              timestamptz NOT NULL DEFAULT now()
);

-- Human-in-the-loop records (written by the resolve-approval surface) --------
CREATE TABLE IF NOT EXISTS approvals (
    approval_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_id    uuid NOT NULL REFERENCES decisions(decision_id),
    approver_id    text,
    approver_role  text NOT NULL,                     -- lead_credit_officer
    status         text NOT NULL DEFAULT 'pending',   -- pending | confirmed | rejected | modified
    is_override    boolean NOT NULL DEFAULT false,
    modified_limit numeric(14,2),
    justification  text,
    requested_at   timestamptz NOT NULL DEFAULT now(),
    resolved_at    timestamptz
);

CREATE INDEX IF NOT EXISTS ix_requests_customer ON credit_line_requests(customer_id);
CREATE INDEX IF NOT EXISTS ix_bureau_customer   ON bureau_reports(customer_id);
CREATE INDEX IF NOT EXISTS ix_policy_active     ON decision_policies(effective_to) WHERE effective_to IS NULL;
