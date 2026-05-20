-- ============================================================
-- 03_clinical.sql
-- Typed, validated clinical tables populated from staging.
-- These are the source-of-truth tables for analysis and reporting.
-- ============================================================

-- ── Subjects (Demographics) ──────────────────────────────────
CREATE TABLE IF NOT EXISTS clinical.subjects (
    subject_id          SERIAL PRIMARY KEY,
    rave_subject_id     TEXT NOT NULL UNIQUE,
    site_id             TEXT NOT NULL,
    subject_number      TEXT NOT NULL,
    date_of_birth       DATE,
    sex                 TEXT CHECK (sex IN ('M','F','U')),
    race                TEXT,
    ethnicity           TEXT,
    country             TEXT,
    enrollment_date     DATE,
    randomization_date  DATE,
    treatment_arm       TEXT,
    rave_status         TEXT,
    -- ── Anthropometrics ──────────────────────────────────────
    weight_kg           NUMERIC(5,1),
    height_cm           NUMERIC(5,1),
    bmi                 NUMERIC(5,2),
    waist_cm            NUMERIC(5,1),
    hip_cm              NUMERIC(5,1),
    waist_hip_ratio     NUMERIC(4,3),   -- derived: waist / hip
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

-- ── Consent Forms ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS clinical.consent_forms (
    consent_id          SERIAL PRIMARY KEY,
    subject_id          INT NOT NULL REFERENCES clinical.subjects(subject_id),
    rave_subject_id     TEXT NOT NULL,
    icf_version         TEXT NOT NULL,
    consent_date        DATE NOT NULL,
    consent_obtained_by TEXT,
    re_consent_required BOOLEAN DEFAULT FALSE,
    re_consent_date     DATE,
    re_consent_version  TEXT,
    withdrawal_date     DATE,
    withdrawal_reason   TEXT,
    consent_status      TEXT NOT NULL DEFAULT 'ACTIVE',  -- ACTIVE | WITHDRAWN | PENDING_RE_CONSENT
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

-- ── Visits ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS clinical.visits (
    visit_id            SERIAL PRIMARY KEY,
    subject_id          INT NOT NULL REFERENCES clinical.subjects(subject_id),
    rave_subject_id     TEXT NOT NULL,
    visit_name          TEXT NOT NULL,
    visit_date          DATE NOT NULL,
    visit_status        TEXT CHECK (visit_status IN ('COMPLETED','MISSED','UNSCHEDULED','PENDING')),
    days_from_baseline  INT,
    window_compliant    BOOLEAN,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (subject_id, visit_name, visit_date)
);

-- ── Laboratory Results ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS clinical.labs (
    lab_id              SERIAL PRIMARY KEY,
    subject_id          INT NOT NULL REFERENCES clinical.subjects(subject_id),
    rave_subject_id     TEXT NOT NULL,
    visit_id            INT REFERENCES clinical.visits(visit_id),
    visit_name          TEXT,
    lab_date            DATE NOT NULL,
    lab_test            TEXT NOT NULL,
    lab_value           NUMERIC,
    lab_value_raw       TEXT,
    lab_unit            TEXT,
    lab_normal_low      NUMERIC,
    lab_normal_high     NUMERIC,
    lab_flag            TEXT,
    x_uln               NUMERIC,      -- value / ULN ratio
    graded_toxicity     INT,          -- NCI CTCAE grade 1-5, NULL if normal
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (subject_id, visit_name, lab_date, lab_test)
);

-- ── FibroScan ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS clinical.fibroscan (
    fibroscan_id        SERIAL PRIMARY KEY,
    subject_id          INT NOT NULL REFERENCES clinical.subjects(subject_id),
    rave_subject_id     TEXT NOT NULL,
    visit_id            INT REFERENCES clinical.visits(visit_id),
    visit_name          TEXT,
    scan_date           DATE NOT NULL,
    lsm_kpa             NUMERIC(6,2),
    lsm_iqr             NUMERIC(6,2),
    lsm_success_rate    NUMERIC(5,2),
    cap_score           NUMERIC(6,2),
    fibrosis_stage      TEXT,         -- F0 | F1 | F2 | F3 | F4 (derived)
    steatosis_grade     TEXT,         -- S0 | S1 | S2 | S3 (derived from CAP)
    operator_id         TEXT,
    device_serial       TEXT,
    quality_adequate    BOOLEAN,      -- IQR/median ≤ 30% and success rate ≥ 60%
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (subject_id, visit_name, scan_date)
);

-- ── Urinalysis ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS clinical.urinalysis (
    urine_id            SERIAL PRIMARY KEY,
    subject_id          INT NOT NULL REFERENCES clinical.subjects(subject_id),
    rave_subject_id     TEXT NOT NULL,
    visit_id            INT REFERENCES clinical.visits(visit_id),
    visit_name          TEXT,
    urine_date          DATE NOT NULL,
    specific_gravity    NUMERIC(6,4),
    ph                  NUMERIC(4,1),
    protein             TEXT,         -- NEG | TRACE | 1+ | 2+ | 3+ | 4+
    glucose             TEXT,
    ketones             TEXT,
    blood               TEXT,
    leukocyte_esterase  TEXT,
    nitrites            TEXT,
    microscopy_rbc      NUMERIC,
    microscopy_wbc      NUMERIC,
    microscopy_casts    TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (subject_id, visit_name, urine_date)
);

-- ── Treatment ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS clinical.treatment (
    treatment_id        SERIAL PRIMARY KEY,
    subject_id          INT NOT NULL REFERENCES clinical.subjects(subject_id),
    rave_subject_id     TEXT NOT NULL,
    drug_name           TEXT NOT NULL,
    dose                NUMERIC,
    dose_unit           TEXT,
    frequency           TEXT,
    route               TEXT,
    start_date          DATE,
    end_date            DATE,
    ongoing             BOOLEAN,
    indication          TEXT,
    treatment_type      TEXT CHECK (treatment_type IN ('STUDY_DRUG','CONMED','PRIOR')),
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- ── Adverse Events ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS clinical.adverse_events (
    ae_id               SERIAL PRIMARY KEY,
    subject_id          INT NOT NULL REFERENCES clinical.subjects(subject_id),
    rave_subject_id     TEXT NOT NULL,
    ae_term             TEXT NOT NULL,
    ae_start_date       DATE,
    ae_end_date         DATE,
    ae_ongoing          BOOLEAN,
    severity            TEXT CHECK (severity IN ('MILD','MODERATE','SEVERE','LIFE_THREATENING','FATAL')),
    serious             BOOLEAN,
    seriousness_criteria TEXT,
    relationship        TEXT,
    outcome             TEXT,
    action_taken        TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);
