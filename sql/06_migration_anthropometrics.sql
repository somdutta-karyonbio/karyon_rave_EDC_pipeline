-- ============================================================
-- 06_migration_anthropometrics.sql
-- Adds anthropometric columns to existing staging and clinical
-- tables. Safe to run on a live database — uses IF NOT EXISTS.
-- ============================================================

-- ── staging.demographics ─────────────────────────────────────
ALTER TABLE staging.demographics ADD COLUMN IF NOT EXISTS weight_kg   TEXT;
ALTER TABLE staging.demographics ADD COLUMN IF NOT EXISTS height_cm   TEXT;
ALTER TABLE staging.demographics ADD COLUMN IF NOT EXISTS bmi         TEXT;
ALTER TABLE staging.demographics ADD COLUMN IF NOT EXISTS waist_cm    TEXT;
ALTER TABLE staging.demographics ADD COLUMN IF NOT EXISTS hip_cm      TEXT;

-- ── clinical.subjects ────────────────────────────────────────
ALTER TABLE clinical.subjects ADD COLUMN IF NOT EXISTS weight_kg       NUMERIC(5,1);
ALTER TABLE clinical.subjects ADD COLUMN IF NOT EXISTS height_cm       NUMERIC(5,1);
ALTER TABLE clinical.subjects ADD COLUMN IF NOT EXISTS bmi             NUMERIC(5,2);
ALTER TABLE clinical.subjects ADD COLUMN IF NOT EXISTS waist_cm        NUMERIC(5,1);
ALTER TABLE clinical.subjects ADD COLUMN IF NOT EXISTS hip_cm          NUMERIC(5,1);
ALTER TABLE clinical.subjects ADD COLUMN IF NOT EXISTS waist_hip_ratio NUMERIC(4,3);
