-- ============================================================
-- 02_staging.sql
-- Raw landing tables for RAVE API extracts.
-- All columns TEXT — no type coercion at this layer.
-- Append-only; pipeline_run_id ties each row to an extract run.
-- ============================================================

-- ── Extract run log ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS staging.pipeline_run (
    run_id          SERIAL PRIMARY KEY,
    run_started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_finished_at TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'RUNNING',  -- RUNNING | SUCCESS | FAILED
    triggered_by    TEXT,
    rave_env        TEXT,
    notes           TEXT
);

-- ── Demographics ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS staging.demographics (
    stg_id          SERIAL PRIMARY KEY,
    run_id          INT  REFERENCES staging.pipeline_run(run_id),
    rave_subject_id TEXT,
    site_id         TEXT,
    subject_number  TEXT,
    initials        TEXT,
    date_of_birth   TEXT,
    sex             TEXT,
    race            TEXT,
    ethnicity       TEXT,
    country         TEXT,
    enrollment_date TEXT,
    randomization_date TEXT,
    treatment_arm   TEXT,
    rave_status     TEXT,
    -- ── Anthropometrics ──────────────────────────────────────
    weight_kg       TEXT,
    height_cm       TEXT,
    bmi             TEXT,
    waist_cm        TEXT,
    hip_cm          TEXT,
    loaded_at       TIMESTAMPTZ DEFAULT NOW()
);

-- ── Informed Consent ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS staging.consent (
    stg_id          SERIAL PRIMARY KEY,
    run_id          INT  REFERENCES staging.pipeline_run(run_id),
    rave_subject_id TEXT,
    site_id         TEXT,
    icf_version     TEXT,
    consent_date    TEXT,
    consent_obtained_by TEXT,
    re_consent_required TEXT,
    re_consent_date TEXT,
    re_consent_version  TEXT,
    withdrawal_date TEXT,
    withdrawal_reason   TEXT,
    loaded_at       TIMESTAMPTZ DEFAULT NOW()
);

-- ── Visit Schedule ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS staging.visits (
    stg_id          SERIAL PRIMARY KEY,
    run_id          INT  REFERENCES staging.pipeline_run(run_id),
    rave_subject_id TEXT,
    site_id         TEXT,
    visit_name      TEXT,
    visit_date      TEXT,
    visit_status    TEXT,   -- COMPLETED | MISSED | UNSCHEDULED
    loaded_at       TIMESTAMPTZ DEFAULT NOW()
);

-- ── Laboratory Results ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS staging.labs (
    stg_id          SERIAL PRIMARY KEY,
    run_id          INT  REFERENCES staging.pipeline_run(run_id),
    rave_subject_id TEXT,
    site_id         TEXT,
    visit_name      TEXT,
    lab_date        TEXT,
    lab_test        TEXT,
    lab_value       TEXT,
    lab_unit        TEXT,
    lab_normal_low  TEXT,
    lab_normal_high TEXT,
    lab_flag        TEXT,   -- H | L | HH | LL | blank
    local_lab_id    TEXT,
    loaded_at       TIMESTAMPTZ DEFAULT NOW()
);

-- ── FibroScan ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS staging.fibroscan (
    stg_id          SERIAL PRIMARY KEY,
    run_id          INT  REFERENCES staging.pipeline_run(run_id),
    rave_subject_id TEXT,
    site_id         TEXT,
    visit_name      TEXT,
    scan_date       TEXT,
    lsm_kpa         TEXT,   -- liver stiffness measurement
    lsm_iqr         TEXT,   -- interquartile range
    lsm_success_rate TEXT,
    cap_score       TEXT,   -- controlled attenuation parameter dB/m
    operator_id     TEXT,
    device_serial   TEXT,
    loaded_at       TIMESTAMPTZ DEFAULT NOW()
);

-- ── Urinalysis ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS staging.urinalysis (
    stg_id          SERIAL PRIMARY KEY,
    run_id          INT  REFERENCES staging.pipeline_run(run_id),
    rave_subject_id TEXT,
    site_id         TEXT,
    visit_name      TEXT,
    urine_date      TEXT,
    specific_gravity TEXT,
    ph              TEXT,
    protein         TEXT,
    glucose         TEXT,
    ketones         TEXT,
    blood           TEXT,
    leukocyte_esterase TEXT,
    nitrites        TEXT,
    microscopy_rbc  TEXT,
    microscopy_wbc  TEXT,
    microscopy_casts TEXT,
    loaded_at       TIMESTAMPTZ DEFAULT NOW()
);

-- ── Treatment History ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS staging.treatment (
    stg_id          SERIAL PRIMARY KEY,
    run_id          INT  REFERENCES staging.pipeline_run(run_id),
    rave_subject_id TEXT,
    site_id         TEXT,
    drug_name       TEXT,
    dose            TEXT,
    dose_unit       TEXT,
    frequency       TEXT,
    route           TEXT,
    start_date      TEXT,
    end_date        TEXT,
    ongoing         TEXT,
    indication      TEXT,
    treatment_type  TEXT,   -- STUDY_DRUG | CONMED | PRIOR
    loaded_at       TIMESTAMPTZ DEFAULT NOW()
);

-- ── Adverse Events ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS staging.adverse_events (
    stg_id          SERIAL PRIMARY KEY,
    run_id          INT  REFERENCES staging.pipeline_run(run_id),
    rave_subject_id TEXT,
    site_id         TEXT,
    ae_term         TEXT,
    ae_start_date   TEXT,
    ae_end_date     TEXT,
    ae_ongoing      TEXT,
    severity        TEXT,   -- MILD | MODERATE | SEVERE | LIFE_THREATENING | FATAL
    serious         TEXT,   -- Y | N
    seriousness_criteria TEXT,
    relationship    TEXT,   -- RELATED | NOT_RELATED | POSSIBLY_RELATED
    outcome         TEXT,
    action_taken    TEXT,
    loaded_at       TIMESTAMPTZ DEFAULT NOW()
);
