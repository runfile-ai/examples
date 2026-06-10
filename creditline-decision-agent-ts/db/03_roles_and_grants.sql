-- ============================================================================
-- LEAST-PRIVILEGE AGENT ROLE
-- The MCP server connects as this role — it can read/write the simulated world
-- but holds no superuser rights. Seeding/initialisation use a separate admin
-- DSN that is never handed to the agent.
-- Run while connected to the mimic_creditline database.
-- ============================================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'creditline_agent') THEN
        CREATE ROLE creditline_agent LOGIN PASSWORD 'agent_demo_pw';
    END IF;
END $$;

GRANT USAGE ON SCHEMA public TO creditline_agent;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO creditline_agent;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO creditline_agent;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE ON TABLES TO creditline_agent;
