"""
Mock data generator for pipeline end-to-end testing.

Inserts synthetic patient records directly into staging tables,
simulating a RAVE extraction without a live RAVE connection.

9 subjects covering all Metavir fibrosis stages (F0–F4/Cirrhosis):
  KRY-001 / KRY-002  — clean, all checks pass                   F2
  KRY-003            — missing consent date                      F3
  KRY-004            — outdated ICF version                      F3
  KRY-005            — out-of-window visit (Week 12 + 15 days)   F3
  KRY-006            — critical ALT (≥ 5× ULN), dyslipidaemia   F4 (Cirrhosis)
  KRY-007            — missing FibroScan at Baseline             F2
  KRY-008            — invalid date of birth (blocked)           F2
  KRY-009            — SCREEN FAILURE: F1 (6.5 kPa < 7.1 min)   F1

Lab panels included per subject per visit:
  ▸ Liver enzyme panel     (ALT, AST, ALP, GGT, bilirubin, albumin, LDH, AFP, HA)
  ▸ Cholesterol profile    (TC, LDL, HDL, TG, VLDL, Non-HDL)
  ▸ Biochemical / metabolic (glucose, HbA1c, insulin, CRP, ferritin, iron, TIBC, uric acid)
  ▸ Haematology            (Hb, WBC, platelets)
  ▸ Renal                  (creatinine, INR)
  ▸ Fibrosis biomarkers    (AFP, hyaluronic acid — FIB-4 derivable from AST/ALT/platelets)

Anthropometrics per subject: weight, height, BMI, waist, hip → WHR derived at load.
"""

import sys
import os

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

from datetime import date, timedelta
from pipeline.db import init_engine, get_session, create_pipeline_run
from sqlalchemy import text


# ── Date helpers ──────────────────────────────────────────────

def d(days: int = 0) -> str:
    return (date.today() + timedelta(days=days)).strftime("%Y-%m-%d")

def visit(target_day: int, offset: int = 0) -> str:
    """Date for a protocol visit relative to baseline (90 days ago)."""
    return (date.today() - timedelta(days=90) + timedelta(days=target_day + offset)).strftime("%Y-%m-%d")

def baseline() -> str:
    return d(-90)


# ── Subject master ────────────────────────────────────────────
# (rave_id, site, dob, sex, arm, weight_kg, height_cm, waist_cm, hip_cm)

SUBJECTS = [
    # (rave_id, site, dob, sex, arm, weight_kg, height_cm, waist_cm, hip_cm)
    ("KRY-001", "S01", "1978-04-12", "M", "TREATMENT", 82.0, 175.0, 95.0, 102.0),  # F2
    ("KRY-002", "S01", "1965-09-30", "F", "PLACEBO",   71.5, 162.0, 88.0, 105.0),  # F2
    ("KRY-003", "S02", "1990-07-22", "M", "TREATMENT", 76.0, 178.0, 85.0,  95.0),  # F3
    ("KRY-004", "S02", "1983-11-05", "F", "TREATMENT", 68.0, 165.0, 82.0, 100.0),  # F3
    ("KRY-005", "S03", "1971-03-18", "M", "PLACEBO",   98.5, 171.0, 102.0, 108.0), # F3
    ("KRY-006", "S03", "1955-12-01", "F", "TREATMENT", 74.0, 164.0, 90.0, 106.0),  # F4 Cirrhosis
    ("KRY-007", "S04", "1999-08-14", "M", "TREATMENT", 70.0, 170.0, 80.0,  92.0),  # F2
    ("KRY-008", "S04", "INVALID-DOB","F", "PLACEBO",   65.0, 158.0, 86.0, 103.0),  # F2 (blocked)
    ("KRY-009", "S05", "2001-06-15", "F", "SCREEN_FAIL", 63.0, 162.0, 79.0, 95.0), # F1 — screen failure
]

# Subjects who did not proceed past screening (no randomization, no labs/treatment)
SCREEN_FAILURES = {"KRY-009"}


def _bmi(weight: float, height: float) -> float:
    return round(weight / ((height / 100) ** 2), 1)


# ── Lab panel definitions ─────────────────────────────────────
# Each entry: (test_name, normal_value, unit, low_ref, high_ref)
# Subject-specific overrides applied per visit below.

BASE_LIVER_ENZYMES = [
    ("ALT",               28.0,  "U/L",    7.0,   56.0),
    ("AST",               22.0,  "U/L",   10.0,   40.0),
    ("ALP",               88.0,  "U/L",   44.0,  147.0),
    ("GGT",               35.0,  "U/L",    9.0,   48.0),
    ("Total_Bilirubin",    0.8,  "mg/dL",  0.2,    1.2),
    ("Direct_Bilirubin",   0.15, "mg/dL",  0.0,    0.3),
    ("Indirect_Bilirubin", 0.65, "mg/dL",  0.2,    0.9),
    ("Albumin",            4.1,  "g/dL",   3.5,    5.0),
    ("Total_Protein",      7.2,  "g/dL",   6.4,    8.3),
    ("Globulin",           3.1,  "g/dL",   2.0,    3.5),
    ("LDH",              185.0,  "U/L",  140.0,  280.0),
    ("AFP",                4.2,  "ng/mL",  0.0,   10.0),
    ("Hyaluronic_Acid",   55.0,  "ng/mL",  0.0,   75.0),
    ("INR",                1.05, "ratio",  0.8,    1.2),
]

BASE_CHOLESTEROL = [
    ("Total_Cholesterol",  185.0, "mg/dL", 100.0, 200.0),
    ("LDL_Cholesterol",     98.0, "mg/dL",   0.0, 100.0),
    ("HDL_Cholesterol",     52.0, "mg/dL",  40.0, 100.0),
    ("Triglycerides",      130.0, "mg/dL",   0.0, 150.0),
    ("VLDL_Cholesterol",    26.0, "mg/dL",   5.0,  40.0),
    ("Non_HDL_Cholesterol", 133.0,"mg/dL",   0.0, 130.0),
]

BASE_BIOCHEMICAL = [
    ("Fasting_Glucose",    92.0,  "mg/dL",  70.0, 100.0),
    ("HbA1c",               5.4,  "%",       4.0,   5.6),
    ("Insulin",            10.5,  "μIU/mL",  2.0,  25.0),
    ("CRP",                 3.2,  "mg/L",    0.0,  10.0),
    ("Ferritin",           185.0, "ng/mL",  12.0, 300.0),
    ("Serum_Iron",          95.0, "μg/dL",  60.0, 170.0),
    ("TIBC",               310.0, "μg/dL", 250.0, 370.0),
    ("Uric_Acid",            5.2, "mg/dL",   3.5,   7.2),
]

BASE_HAEMATOLOGY = [
    ("Hemoglobin",  13.5, "g/dL",    11.5, 17.5),
    ("WBC",          6.8, "x10^9/L",  4.0, 11.0),
    ("Platelets",  210.0, "x10^9/L", 150.0, 400.0),
    ("Creatinine",   0.9, "mg/dL",    0.6,   1.2),
]

ALL_BASE_LABS = BASE_LIVER_ENZYMES + BASE_CHOLESTEROL + BASE_BIOCHEMICAL + BASE_HAEMATOLOGY


# Subject-specific overrides: {subject_id: {visit_name: {test: value}}}
# KRY-006 has critically elevated liver enzymes + dyslipidaemia + high CRP
# KRY-005 has metabolic syndrome pattern (high TG, low HDL, high glucose)

SUBJECT_OVERRIDES = {
    "KRY-006": {
        "Baseline": {
            "ALT":               320.0,   # 5.7× ULN — critical
            "AST":               180.0,   # 4.5× ULN — critical
            "GGT":               142.0,   # 2.9× ULN
            "Total_Bilirubin":     2.1,   # elevated
            "Direct_Bilirubin":    0.9,
            "AFP":                18.5,   # elevated — monitor
            "Hyaluronic_Acid":   210.0,   # elevated fibrosis marker
            "CRP":                22.4,   # elevated inflammation
            "Ferritin":          480.0,   # elevated (NAFLD pattern)
            "Total_Cholesterol": 245.0,   # hypercholesterolaemia
            "LDL_Cholesterol":   158.0,
            "HDL_Cholesterol":    36.0,   # low — cardiovascular risk
            "Triglycerides":     280.0,   # hypertriglyceridaemia
            "Fasting_Glucose":   126.0,   # diabetic range
            "HbA1c":               7.2,
        },
        "Week12": {
            "ALT":               185.0,   # improving but still elevated
            "AST":               105.0,
            "GGT":                88.0,
            "CRP":                14.0,
        },
    },
    "KRY-005": {
        "Baseline": {
            "Triglycerides":     245.0,   # metabolic syndrome
            "HDL_Cholesterol":    38.0,   # low
            "Fasting_Glucose":   112.0,   # pre-diabetic
            "HbA1c":               6.1,
            "Ferritin":          320.0,
            "Hyaluronic_Acid":   110.0,
        },
    },
    "KRY-001": {
        "Baseline": {
            "Hyaluronic_Acid":    88.0,   # mild fibrosis marker elevation
            "AFP":                 6.8,
            "Ferritin":          220.0,
        },
    },
}


def _get_lab_value(subj_id: str, visit_name: str, test: str, default: float) -> float:
    return SUBJECT_OVERRIDES.get(subj_id, {}).get(visit_name, {}).get(test, default)


def _flag(val: float, lo: float, hi: float) -> str:
    if val > hi * 2:  return "HH"
    if val > hi:      return "H"
    if val < lo / 2:  return "LL"
    if val < lo:      return "L"
    return ""


# ── Insert functions ─────────────────────────────────────────

def insert_demographics(sess, run_id: int):
    for s in SUBJECTS:
        rave_id, site, dob, sex, arm, weight, height, waist, hip = s
        bmi = _bmi(weight, height) if dob != "INVALID-DOB" else 0.0
        sess.execute(text("""
            INSERT INTO staging.demographics
            (run_id, rave_subject_id, site_id, subject_number, date_of_birth,
             sex, race, ethnicity, country, enrollment_date, randomization_date,
             treatment_arm, rave_status,
             weight_kg, height_cm, bmi, waist_cm, hip_cm)
            VALUES (:r, :id, :site, :num, :dob,
                    :sex, 'WHITE', 'NOT HISPANIC', 'US', :enrl, :rand,
                    :arm, 'RANDOMIZED',
                    :weight, :height, :bmi, :waist, :hip)
        """), {
            "r": run_id, "id": rave_id, "site": site, "num": rave_id,
            "dob": dob, "sex": sex,
            "enrl": d(-100), "rand": d(-90), "arm": arm,
            "weight": str(weight), "height": str(height),
            "bmi": str(bmi), "waist": str(waist), "hip": str(hip),
        })


def insert_consent(sess, run_id: int):
    consent_data = {
        "KRY-001": {"ver": "v3.0", "date": d(-105)},
        "KRY-002": {"ver": "v3.0", "date": d(-102)},
        "KRY-003": {"ver": "v3.0", "date": None},       # missing consent date
        "KRY-004": {"ver": "v2.1", "date": d(-110)},    # outdated ICF version
        "KRY-005": {"ver": "v3.0", "date": d(-100)},
        "KRY-006": {"ver": "v3.0", "date": d(-98)},
        "KRY-007": {"ver": "v3.0", "date": d(-95)},
        "KRY-008": {"ver": "v3.0", "date": d(-92), "wdraw": d(-10), "wreason": "Subject request"},
        "KRY-009": {"ver": "v3.0", "date": d(-120)},    # screen failure — consented but not enrolled
    }
    for subj, c in consent_data.items():
        site = next(s[1] for s in SUBJECTS if s[0] == subj)
        sess.execute(text("""
            INSERT INTO staging.consent
            (run_id, rave_subject_id, site_id, icf_version, consent_date,
             consent_obtained_by, withdrawal_date, withdrawal_reason)
            VALUES (:r, :id, :site, :ver, :cdate, 'Investigator', :wdraw, :wreason)
        """), {
            "r": run_id, "id": subj, "site": site,
            "ver": c["ver"], "cdate": c.get("date"),
            "wdraw": c.get("wdraw"), "wreason": c.get("wreason"),
        })


def insert_visits(sess, run_id: int):
    schedule = [
        ("Screening", -14, 0),
        ("Baseline",    1, 0),
        ("Week4",      28, 0),
        ("Week12",     84, 0),
        ("Week24",    168, 0),
    ]
    for s in SUBJECTS:
        rave_id, site = s[0], s[1]
        for vname, target, offset in schedule:
            # Screen failures only attend the Screening visit — not enrolled
            if rave_id in SCREEN_FAILURES and vname != "Screening":
                continue
            # KRY-005 Week12 is 15 days late — outside ±7d window
            voffset = 15 if (rave_id == "KRY-005" and vname == "Week12") else offset
            sess.execute(text("""
                INSERT INTO staging.visits
                (run_id, rave_subject_id, site_id, visit_name, visit_date, visit_status)
                VALUES (:r, :id, :site, :vname, :vdate, 'COMPLETED')
            """), {
                "r": run_id, "id": rave_id, "site": site,
                "vname": vname, "vdate": visit(target, voffset),
            })


def insert_labs(sess, run_id: int):
    """Insert full lab panel (liver enzymes + cholesterol + biochemical + haematology)."""
    lab_visits = [("Baseline", 1), ("Week12", 84)]

    for s in SUBJECTS:
        rave_id, site = s[0], s[1]
        if rave_id in SCREEN_FAILURES:
            continue   # screen failures have no post-screening labs
        for vname, vday in lab_visits:
            for test, default_val, unit, lo, hi in ALL_BASE_LABS:
                val = _get_lab_value(rave_id, vname, test, default_val)
                flag = _flag(val, lo, hi)
                sess.execute(text("""
                    INSERT INTO staging.labs
                    (run_id, rave_subject_id, site_id, visit_name, lab_date,
                     lab_test, lab_value, lab_unit, lab_normal_low, lab_normal_high, lab_flag)
                    VALUES (:r, :id, :site, :vname, :ldate,
                            :test, :val, :unit, :lo, :hi, :flag)
                """), {
                    "r": run_id, "id": rave_id, "site": site,
                    "vname": vname, "ldate": visit(vday),
                    "test": test, "val": str(val),
                    "unit": unit, "lo": str(lo), "hi": str(hi), "flag": flag,
                })


def insert_fibroscan(sess, run_id: int):
    """
    FibroScan data per visit.  Metavir stages derived from LSM in kPa.

    Stage  kPa range       Subjects
    ─────  ─────────────   ─────────────────────────────────────────────
    F0     < 5.5           (none — all enrolled subjects meet ≥ F2)
    F1     5.5 – 7.0       KRY-009 (6.5 kPa) — SCREEN FAILURE, not enrolled
    F2     7.1 – 9.4       KRY-001 (9.2), KRY-002 (8.5), KRY-007 (9.0), KRY-008 (8.8)
    F3     9.5 – 12.4      KRY-003 (11.1), KRY-004 (9.8), KRY-005 (12.1)
    F4     ≥ 12.5          KRY-006 (14.8) — F4 = Cirrhosis (Metavir)

    KRY-007 intentionally has no Baseline scan (missing assessment test).
    """
    # (rave_id, visit_name, lsm_kpa, iqr, success_rate, cap_score)
    scans = [
        ("KRY-001", "Screening",  9.5,  1.1, 72.0, 245.0),
        ("KRY-001", "Baseline",   9.2,  1.3, 70.0, 248.0),
        ("KRY-001", "Week24",     8.1,  1.0, 75.0, 230.0),
        ("KRY-002", "Screening",  8.8,  1.0, 74.0, 238.0),
        ("KRY-002", "Baseline",   8.5,  1.1, 71.0, 232.0),
        ("KRY-002", "Week24",     7.9,  0.9, 78.0, 220.0),
        ("KRY-003", "Screening", 11.4,  1.8, 65.0, 262.0),
        ("KRY-003", "Baseline",  11.1,  2.0, 62.0, 258.0),
        ("KRY-003", "Week24",    10.2,  1.5, 68.0, 250.0),
        ("KRY-004", "Screening", 10.0,  1.4, 69.0, 252.0),
        ("KRY-004", "Baseline",   9.8,  1.5, 67.0, 255.0),
        ("KRY-004", "Week24",     8.9,  1.2, 72.0, 240.0),
        ("KRY-005", "Screening", 12.4,  2.2, 63.0, 275.0),
        ("KRY-005", "Baseline",  12.1,  2.4, 60.0, 270.0),
        ("KRY-005", "Week24",    11.5,  2.0, 65.0, 265.0),
        ("KRY-006", "Screening", 15.2,  2.8, 58.0, 290.0),
        ("KRY-006", "Baseline",  14.8,  3.0, 55.0, 288.0),   # F4
        ("KRY-006", "Week24",    13.5,  2.5, 62.0, 278.0),
        # KRY-007 missing Baseline FibroScan (intentional gap for test)
        ("KRY-007", "Screening",  9.3,  1.2, 71.0, 244.0),
        ("KRY-007", "Week24",     8.5,  1.1, 74.0, 235.0),
        ("KRY-008", "Screening",  9.0,  1.3, 70.0, 241.0),
        ("KRY-008", "Baseline",   8.8,  1.4, 68.0, 238.0),
        ("KRY-008", "Week24",     8.2,  1.0, 76.0, 228.0),
        # KRY-009 screen failure — F1 (6.5 kPa) below 7.1 kPa inclusion threshold
        ("KRY-009", "Screening",  6.5,  0.8, 82.0, 198.0),
    ]

    site_map = {s[0]: s[1] for s in SUBJECTS}
    for rave_id, vname, lsm, iqr, sr, cap in scans:
        target_map = {"Screening": -14, "Baseline": 1, "Week24": 168}
        vday = target_map.get(vname, 0)
        sess.execute(text("""
            INSERT INTO staging.fibroscan
            (run_id, rave_subject_id, site_id, visit_name, scan_date,
             lsm_kpa, lsm_iqr, lsm_success_rate, cap_score, operator_id)
            VALUES (:r, :id, :site, :vname, :sdate,
                    :lsm, :iqr, :sr, :cap, 'OP-01')
        """), {
            "r": run_id, "id": rave_id, "site": site_map[rave_id],
            "vname": vname, "sdate": visit(vday),
            "lsm": str(lsm), "iqr": str(iqr),
            "sr": str(sr), "cap": str(cap),
        })


def insert_urinalysis(sess, run_id: int):
    for s in SUBJECTS:
        rave_id, site = s[0], s[1]
        if rave_id in SCREEN_FAILURES:
            continue   # screen failures: no urinalysis collected post-screening
        sess.execute(text("""
            INSERT INTO staging.urinalysis
            (run_id, rave_subject_id, site_id, visit_name, urine_date,
             specific_gravity, ph, protein, glucose, blood, ketones,
             leukocyte_esterase, nitrites)
            VALUES (:r, :id, :site, 'Baseline', :udate,
                    '1.015', '6.0', 'NEG', 'NEG', 'NEG', 'NEG', 'NEG', 'NEG')
        """), {
            "r": run_id, "id": rave_id, "site": site,
            "udate": visit(1),
        })


def insert_treatment(sess, run_id: int):
    for s in SUBJECTS:
        rave_id, site = s[0], s[1]
        if rave_id in SCREEN_FAILURES:
            continue   # screen failures received no study drug
        sess.execute(text("""
            INSERT INTO staging.treatment
            (run_id, rave_subject_id, site_id, drug_name, dose, dose_unit,
             frequency, route, start_date, ongoing, treatment_type)
            VALUES (:r, :id, :site, 'KRY-101', '50', 'mg',
                    'QD', 'ORAL', :sdate, 'Y', 'STUDY_DRUG')
        """), {
            "r": run_id, "id": rave_id, "site": site, "sdate": baseline(),
        })


# ── Entry point ───────────────────────────────────────────────

def generate(config_path: str = "config/config.yaml") -> int:
    """Insert mock data into staging and return the run_id."""
    init_engine(config_path)
    run_id = create_pipeline_run(triggered_by="mock_test", rave_env="TEST")
    print(f"Created pipeline run: run_id={run_id}")

    total_labs = len(SUBJECTS) * 2 * len(ALL_BASE_LABS)

    with get_session() as sess:
        insert_demographics(sess, run_id)
        print(f"  ✓ Demographics     ({len(SUBJECTS)} subjects — with weight, height, BMI, waist, hip)")
        insert_consent(sess, run_id)
        print(f"  ✓ Consent forms    ({len(SUBJECTS)} records)")
        insert_visits(sess, run_id)
        print(f"  ✓ Visits           ({len(SUBJECTS) * 5} records)")
        insert_labs(sess, run_id)
        print(f"  ✓ Labs             ({total_labs} records across {len(ALL_BASE_LABS)} tests)")
        print(f"      Panels: liver enzymes ({len(BASE_LIVER_ENZYMES)}), "
              f"cholesterol ({len(BASE_CHOLESTEROL)}), "
              f"biochemical ({len(BASE_BIOCHEMICAL)}), "
              f"haematology ({len(BASE_HAEMATOLOGY)})")
        insert_fibroscan(sess, run_id)
        print(f"  ✓ FibroScan        (24 records — F1 to F4/Cirrhosis all represented)")
        insert_urinalysis(sess, run_id)
        print(f"  ✓ Urinalysis       ({len(SUBJECTS)} records)")
        insert_treatment(sess, run_id)
        print(f"  ✓ Treatment        ({len(SUBJECTS)} records)")

    print(f"\nMock data ready. run_id={run_id}")
    return run_id


if __name__ == "__main__":
    run_id = generate()
    print(f"\nrun_id to use for pipeline stages: {run_id}")
