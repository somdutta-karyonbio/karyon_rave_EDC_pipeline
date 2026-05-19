-- ============================================================
-- 05_views.sql
-- Reporting views — query these for dashboards and exports.
-- ============================================================

-- ── Subject Listing (SDTM DM-style) ─────────────────────────
CREATE OR REPLACE VIEW clinical.v_subject_listing AS
SELECT
    s.rave_subject_id,
    s.site_id,
    s.subject_number,
    s.sex,
    DATE_PART('year', AGE(s.date_of_birth))::INT AS age_years,
    s.race,
    s.ethnicity,
    s.country,
    s.enrollment_date,
    s.randomization_date,
    s.treatment_arm,
    s.rave_status,
    cf.icf_version,
    cf.consent_date,
    cf.consent_status,
    cf.withdrawal_date
FROM clinical.subjects s
LEFT JOIN clinical.consent_forms cf
       ON cf.subject_id = s.subject_id
      AND cf.consent_status = 'ACTIVE';

-- ── Lab Out-of-Range Summary ─────────────────────────────────
CREATE OR REPLACE VIEW clinical.v_labs_oor AS
SELECT
    s.site_id,
    l.rave_subject_id,
    l.visit_name,
    l.lab_date,
    l.lab_test,
    l.lab_value,
    l.lab_unit,
    l.lab_normal_low,
    l.lab_normal_high,
    l.lab_flag,
    l.x_uln,
    l.graded_toxicity
FROM clinical.labs l
JOIN clinical.subjects s USING (subject_id)
WHERE l.lab_flag IS NOT NULL AND l.lab_flag <> ''
ORDER BY l.lab_date DESC, l.lab_test;

-- ── FibroScan Staging Summary ────────────────────────────────
CREATE OR REPLACE VIEW clinical.v_fibroscan_summary AS
SELECT
    s.site_id,
    f.rave_subject_id,
    f.visit_name,
    f.scan_date,
    f.lsm_kpa,
    f.lsm_iqr,
    f.lsm_success_rate,
    f.cap_score,
    f.fibrosis_stage,
    f.steatosis_grade,
    f.quality_adequate
FROM clinical.fibroscan f
JOIN clinical.subjects s USING (subject_id)
ORDER BY f.scan_date DESC;

-- ── Visit Compliance View ────────────────────────────────────
CREATE OR REPLACE VIEW clinical.v_visit_compliance AS
SELECT
    s.site_id,
    v.rave_subject_id,
    v.visit_name,
    v.visit_date,
    v.days_from_baseline,
    v.visit_status,
    v.window_compliant,
    CASE WHEN v.window_compliant = FALSE THEN 'OUT_OF_WINDOW'
         WHEN v.visit_status = 'MISSED'  THEN 'MISSED'
         ELSE 'OK'
    END AS compliance_flag
FROM clinical.visits v
JOIN clinical.subjects s USING (subject_id);

-- ── Open Data Queries ────────────────────────────────────────
CREATE OR REPLACE VIEW audit.v_open_queries AS
SELECT
    dq.query_id,
    dq.site_id,
    dq.rave_subject_id,
    dq.domain,
    dq.visit_name,
    dq.field_name,
    dq.query_text,
    dq.query_type,
    dq.status,
    dq.opened_date,
    NOW()::DATE - dq.opened_date AS days_open
FROM audit.data_queries dq
WHERE dq.status = 'OPEN'
ORDER BY days_open DESC;

-- ── Protocol Deviation Summary ───────────────────────────────
CREATE OR REPLACE VIEW audit.v_protocol_deviations AS
SELECT
    pd.pd_id,
    pd.site_id,
    pd.rave_subject_id,
    pd.visit_name,
    pd.deviation_type,
    pd.description,
    pd.deviation_date,
    pd.severity,
    pd.impact_on_safety,
    pd.impact_on_efficacy,
    pd.status,
    pd.reported_to_sponsor
FROM audit.protocol_deviations pd
ORDER BY pd.deviation_date DESC;

-- ── Consent Compliance Status ────────────────────────────────
CREATE OR REPLACE VIEW audit.v_consent_status AS
SELECT
    s.site_id,
    s.rave_subject_id,
    s.subject_number,
    cf.icf_version,
    cf.consent_date,
    cf.consent_status,
    cf.re_consent_required,
    cf.re_consent_date,
    cf.re_consent_version,
    cf.withdrawal_date,
    CASE
        WHEN cf.consent_id IS NULL         THEN 'MISSING'
        WHEN cf.consent_status = 'WITHDRAWN' THEN 'WITHDRAWN'
        WHEN cf.re_consent_required = TRUE
         AND cf.re_consent_date IS NULL    THEN 'RE_CONSENT_PENDING'
        ELSE 'OK'
    END AS consent_flag
FROM clinical.subjects s
LEFT JOIN clinical.consent_forms cf
       ON cf.subject_id = s.subject_id
      AND cf.consent_status != 'WITHDRAWN';

-- ── Site-Level Data Completeness Dashboard ────────────────────
CREATE OR REPLACE VIEW audit.v_site_dashboard AS
SELECT
    s.site_id,
    COUNT(DISTINCT s.subject_id)                        AS enrolled_subjects,
    COUNT(DISTINCT v.visit_id)                          AS total_visits,
    SUM(CASE WHEN v.window_compliant = FALSE THEN 1 ELSE 0 END) AS out_of_window_visits,
    COUNT(DISTINCT dq.query_id) FILTER (WHERE dq.status = 'OPEN') AS open_queries,
    COUNT(DISTINCT pd.pd_id)    FILTER (WHERE pd.status = 'OPEN') AS open_deviations,
    COUNT(DISTINCT CASE WHEN cf.consent_id IS NULL THEN s.subject_id END) AS missing_consent
FROM clinical.subjects s
LEFT JOIN clinical.visits v          USING (subject_id)
LEFT JOIN audit.data_queries dq      ON dq.rave_subject_id = s.rave_subject_id
LEFT JOIN audit.protocol_deviations pd ON pd.rave_subject_id = s.rave_subject_id
LEFT JOIN clinical.consent_forms cf  USING (subject_id)
GROUP BY s.site_id
ORDER BY s.site_id;

-- ── Sanitization Error Summary ───────────────────────────────
CREATE OR REPLACE VIEW audit.v_sanitization_summary AS
SELECT
    domain,
    issue_type,
    severity,
    COUNT(*) AS issue_count,
    COUNT(*) FILTER (WHERE resolved = FALSE) AS unresolved_count
FROM audit.sanitization_log
GROUP BY domain, issue_type, severity
ORDER BY severity DESC, issue_count DESC;
