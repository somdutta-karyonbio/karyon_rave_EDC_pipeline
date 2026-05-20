-- ============================================================
-- 01_schemas.sql
-- Creates the three logical schemas for the pipeline.
-- Run once on a fresh database.
-- ============================================================

CREATE SCHEMA IF NOT EXISTS staging;   -- raw data from RAVE (untouched)
CREATE SCHEMA IF NOT EXISTS clinical;  -- validated, query-resolved data
CREATE SCHEMA IF NOT EXISTS audit;     -- queries, deviations, consent log, trail

-- Pipeline service user — restrict to these schemas only
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pipeline_user') THEN
    CREATE ROLE pipeline_user WITH LOGIN PASSWORD 'CHANGE_ME';
  END IF;
END
$$;

GRANT USAGE ON SCHEMA staging  TO pipeline_user;
GRANT USAGE ON SCHEMA clinical TO pipeline_user;
GRANT USAGE ON SCHEMA audit    TO pipeline_user;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES    IN SCHEMA staging  TO pipeline_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES    IN SCHEMA clinical TO pipeline_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES    IN SCHEMA audit    TO pipeline_user;

GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA staging  TO pipeline_user;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA clinical TO pipeline_user;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA audit    TO pipeline_user;

ALTER DEFAULT PRIVILEGES IN SCHEMA staging  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES    TO pipeline_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA clinical GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES    TO pipeline_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA audit    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES    TO pipeline_user;

ALTER DEFAULT PRIVILEGES IN SCHEMA staging  GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO pipeline_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA clinical GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO pipeline_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA audit    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO pipeline_user;
