-- ============================================================
-- 04_audit.sql
-- Audit trail, data queries, protocol deviations,
-- sanitization log, and consent compliance tracking.
-- ============================================================

-- ── Sanitization Issues ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit.sanitization_log (
    issue_id        SERIAL PRIMARY KEY,
    run_id          INT,
    rave_subject_id TEXT,
    site_id         TEXT,
    domain          TEXT NOT NULL,  -- DEMOGRAPHICS | LABS | FIBROSCAN | etc.
    field_name      TEXT,
    field_value     TEXT,
    issue_type      TEXT NOT NULL,  -- MISSING | OUT_OF_RANGE | INVALID_DATE | DUPLICATE | FORMAT_ERROR
    severity        TEXT NOT NULL DEFAULT 'WARNING',  -- ERROR | WARNING | INFO
    message         TEXT NOT NULL,
    resolved        BOOLEAN DEFAULT FALSE,
    resolved_at     TIMESTAMPTZ,
    resolved_by     TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_san_subject ON audit.sanitization_log(rave_subject_id);
CREATE INDEX IF NOT EXISTS idx_san_domain  ON audit.sanitization_log(domain);
CREATE INDEX IF NOT EXISTS idx_san_run     ON audit.sanitization_log(run_id);

-- ── Protocol Deviations ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit.protocol_deviations (
    pd_id           SERIAL PRIMARY KEY,
    run_id          INT,
    rave_subject_id TEXT NOT NULL,
    site_id         TEXT,
    visit_name      TEXT,
    deviation_type  TEXT NOT NULL,
    -- VISIT_WINDOW | MISSING_ASSESSMENT | ELIGIBILITY_VIOLATION |
    -- DOSE_DEVIATION | PROHIBITED_MED | CONSENT_VIOLATION
    description     TEXT NOT NULL,
    detected_date   DATE,
    deviation_date  DATE,
    severity        TEXT DEFAULT 'MAJOR',  -- MAJOR | MINOR | ADMINISTRATIVE
    impact_on_safety    BOOLEAN DEFAULT FALSE,
    impact_on_efficacy  BOOLEAN DEFAULT FALSE,
    status          TEXT DEFAULT 'OPEN',  -- OPEN | CLOSED | WAIVED
    reported_to_sponsor BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pd_subject ON audit.protocol_deviations(rave_subject_id);
CREATE INDEX IF NOT EXISTS idx_pd_status  ON audit.protocol_deviations(status);

-- ── Data Queries (DCF-style) ─────────────────────────────────
CREATE TABLE IF NOT EXISTS audit.data_queries (
    query_id        SERIAL PRIMARY KEY,
    run_id          INT,
    rave_subject_id TEXT NOT NULL,
    site_id         TEXT,
    domain          TEXT NOT NULL,
    visit_name      TEXT,
    field_name      TEXT,
    query_text      TEXT NOT NULL,
    query_type      TEXT,           -- MISSING_DATA | DISCREPANCY | CLARIFICATION | OUT_OF_RANGE
    status          TEXT DEFAULT 'OPEN',  -- OPEN | ANSWERED | CLOSED | CANCELLED
    opened_date     DATE DEFAULT CURRENT_DATE,
    answered_date   DATE,
    answered_by     TEXT,
    answer_text     TEXT,
    closed_date     DATE,
    closed_by       TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dq_subject ON audit.data_queries(rave_subject_id);
CREATE INDEX IF NOT EXISTS idx_dq_status  ON audit.data_queries(status);

-- ── Consent Compliance ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit.consent_compliance (
    cc_id               SERIAL PRIMARY KEY,
    run_id              INT,
    rave_subject_id     TEXT NOT NULL,
    site_id             TEXT,
    check_name          TEXT NOT NULL,
    -- CONSENT_BEFORE_PROCEDURES | ICF_VERSION_CURRENT |
    -- RECONSENT_REQUIRED | WITHDRAWAL_DOCUMENTED
    passed              BOOLEAN NOT NULL,
    detail              TEXT,
    checked_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cc_subject ON audit.consent_compliance(rave_subject_id);
CREATE INDEX IF NOT EXISTS idx_cc_check   ON audit.consent_compliance(check_name);

-- ── Full Audit Trail ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit.audit_trail (
    trail_id        BIGSERIAL PRIMARY KEY,
    run_id          INT,
    rave_subject_id TEXT,
    schema_name     TEXT NOT NULL,
    table_name      TEXT NOT NULL,
    record_id       INT,
    operation       TEXT NOT NULL CHECK (operation IN ('INSERT','UPDATE','DELETE')),
    changed_by      TEXT DEFAULT current_user,
    changed_at      TIMESTAMPTZ DEFAULT NOW(),
    old_values      JSONB,
    new_values      JSONB
);

CREATE INDEX IF NOT EXISTS idx_trail_subject ON audit.audit_trail(rave_subject_id);
CREATE INDEX IF NOT EXISTS idx_trail_table   ON audit.audit_trail(table_name);
CREATE INDEX IF NOT EXISTS idx_trail_time    ON audit.audit_trail(changed_at);

-- ── Audit trigger function ───────────────────────────────────
CREATE OR REPLACE FUNCTION audit.log_change()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO audit.audit_trail(schema_name, table_name, record_id, operation, old_values, new_values)
    VALUES (
        TG_TABLE_SCHEMA,
        TG_TABLE_NAME,
        CASE WHEN TG_OP = 'DELETE' THEN OLD.subject_id ELSE NEW.subject_id END,
        TG_OP,
        CASE WHEN TG_OP IN ('UPDATE','DELETE') THEN row_to_json(OLD)::JSONB ELSE NULL END,
        CASE WHEN TG_OP IN ('INSERT','UPDATE') THEN row_to_json(NEW)::JSONB ELSE NULL END
    );
    RETURN NULL;
END;
$$;

-- Attach trigger to all clinical tables
DO $$
DECLARE tbl TEXT;
BEGIN
    FOR tbl IN SELECT unnest(ARRAY['subjects','consent_forms','visits','labs','fibroscan','urinalysis','treatment','adverse_events'])
    LOOP
        EXECUTE format(
            'CREATE OR REPLACE TRIGGER trg_audit_%s
             AFTER INSERT OR UPDATE OR DELETE ON clinical.%I
             FOR EACH ROW EXECUTE FUNCTION audit.log_change()',
            tbl, tbl
        );
    END LOOP;
END;
$$;
