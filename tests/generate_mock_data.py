"""
Mock data generator for pipeline end-to-end testing.

Inserts synthetic patient records directly into staging tables,
simulating a RAVE extraction without a live RAVE connection.

Patients intentionally include:
  - Normal cases (should pass all checks)
  - A subject with a missing consent date            → consent failure
  - A subject with an outdated ICF version           → consent failure
  - A subject with a out-of-window visit             → protocol deviation
  - A subject with a critically high ALT (≥5× ULN)  → sanitization + deviation
  - A subject with missing FibroScan at Baseline     → missing assessment deviation
  - A subject with invalid date of birth             → sanitization error
"""

import sys
import os

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

from datetime import date, timedelta
from pipeline.db import init_engine, get_session, create_pipeline_run
from sqlalchemy import text

# ── helpers ──────────────────────────────────────────────────

def d(days_from_today: int = 0) -> str:
    return (date.today() + timedelta(days=days_from_today)).strftime("%Y-%m-%d")

def baseline() -> str:
    """Baseline = 90 days ago."""
    return d(-90)

def visit(target_day: int, offset: int = 0) -> str:
    """Return date string for a visit relative to baseline."""
    return (date.today() - timedelta(days=90) + timedelta(days=target_day + offset)).strftime("%Y-%m-%d")


SUBJECTS = [
    # (rave_id,  site,   dob,          sex, arm,        note)
    ("KRY-001", "S01", "1978-04-12",  "M", "TREATMENT", "normal"),
    ("KRY-002", "S01", "1965-09-30",  "F", "PLACEBO",   "normal"),
    ("KRY-003", "S02", "1990-07-22",  "M", "TREATMENT", "missing consent date"),
    ("KRY-004", "S02", "1983-11-05",  "F", "TREATMENT", "outdated ICF version"),
    ("KRY-005", "S03", "1971-03-18",  "M", "PLACEBO",   "out-of-window visit"),
    ("KRY-006", "S03", "1955-12-01",  "F", "TREATMENT", "critically high ALT"),
    ("KRY-007", "S04", "1999-08-14",  "M", "TREATMENT", "missing fibroscan at baseline"),
    ("KRY-008", "S04", "INVALID-DOB", "F", "PLACEBO",   "invalid date of birth"),
]


def insert_demographics(sess, run_id: int):
    for s in SUBJECTS:
        sess.execute(text("""
            INSERT INTO staging.demographics
            (run_id, rave_subject_id, site_id, subject_number, date_of_birth,
             sex, race, ethnicity, country, enrollment_date, randomization_date,
             treatment_arm, rave_status)
            VALUES (:r, :id, :site, :num, :dob,
                    :sex, 'WHITE', 'NOT HISPANIC', 'US', :enrl, :rand,
                    :arm, 'RANDOMIZED')
        """), {
            "r":    run_id,
            "id":   s[0],
            "site": s[1],
            "num":  s[0],
            "dob":  s[2],
            "sex":  s[3],
            "enrl": d(-100),
            "rand": d(-90),
            "arm":  s[4],
        })


def insert_consent(sess, run_id: int):
    consent_data = {
        "KRY-001": {"ver": "v3.0", "date": d(-105), "wdraw": None,     "rewdraw": None},
        "KRY-002": {"ver": "v3.0", "date": d(-102), "wdraw": None,     "rewdraw": None},
        "KRY-003": {"ver": "v3.0", "date": None,     "wdraw": None,     "rewdraw": None},  # missing date
        "KRY-004": {"ver": "v2.1", "date": d(-110), "wdraw": None,     "rewdraw": None},  # old version
        "KRY-005": {"ver": "v3.0", "date": d(-100), "wdraw": None,     "rewdraw": None},
        "KRY-006": {"ver": "v3.0", "date": d(-98),  "wdraw": None,     "rewdraw": None},
        "KRY-007": {"ver": "v3.0", "date": d(-95),  "wdraw": None,     "rewdraw": None},
        "KRY-008": {"ver": "v3.0", "date": d(-92),  "wdraw": d(-10),   "rewdraw": "Subject request"},
    }
    for subj, c in consent_data.items():
        sess.execute(text("""
            INSERT INTO staging.consent
            (run_id, rave_subject_id, site_id, icf_version, consent_date,
             consent_obtained_by, withdrawal_date, withdrawal_reason)
            VALUES (:r, :id, :site, :ver, :cdate, 'Investigator', :wdraw, :wreason)
        """), {
            "r":       run_id,
            "id":      subj,
            "site":    next(s[1] for s in SUBJECTS if s[0] == subj),
            "ver":     c["ver"],
            "cdate":   c["date"],
            "wdraw":   c["wdraw"],
            "wreason": c["rewdraw"],
        })


def insert_visits(sess, run_id: int):
    # Protocol visits per subject — KRY-005 has an out-of-window Week 12
    visit_schedule = [
        ("Screening",  -14,  0),
        ("Baseline",     1,  0),
        ("Week4",       28,  0),
        ("Week12",      84,  0),
        ("Week24",     168,  0),
    ]
    for s in SUBJECTS:
        for vname, target, offset in visit_schedule:
            # KRY-005 Week12 is 15 days late (outside ±7d window)
            voffset = 15 if (s[0] == "KRY-005" and vname == "Week12") else offset
            sess.execute(text("""
                INSERT INTO staging.visits
                (run_id, rave_subject_id, site_id, visit_name, visit_date, visit_status)
                VALUES (:r, :id, :site, :vname, :vdate, 'COMPLETED')
            """), {
                "r":     run_id,
                "id":    s[0],
                "site":  s[1],
                "vname": vname,
                "vdate": visit(target, voffset),
            })


def insert_labs(sess, run_id: int):
    # Standard labs for all subjects at Baseline and Week12
    base_labs = [
        ("ALT",              28.0,  "U/L",   7.0,  56.0),
        ("AST",              22.0,  "U/L",  10.0,  40.0),
        ("ALP",              88.0,  "U/L",  44.0, 147.0),
        ("Total_Bilirubin",   0.8,  "mg/dL", 0.2,   1.2),
        ("Albumin",           4.1,  "g/dL",  3.5,   5.0),
        ("Platelets",       210.0,  "x10^9/L", 150.0, 400.0),
        ("Creatinine",        0.9,  "mg/dL", 0.6,   1.2),
        ("Hemoglobin",       13.5,  "g/dL", 11.5,  17.5),
    ]

    for s in SUBJECTS:
        for vname, vday in [("Baseline", 1), ("Week12", 84)]:
            for test, val, unit, lo, hi in base_labs:
                # KRY-006 has ALT = 320 U/L at Baseline (≥5× ULN = critically high)
                actual_val = 320.0 if (s[0] == "KRY-006" and test == "ALT" and vname == "Baseline") else val
                flag = "HH" if actual_val > hi * 2 else ("H" if actual_val > hi else "")

                sess.execute(text("""
                    INSERT INTO staging.labs
                    (run_id, rave_subject_id, site_id, visit_name, lab_date,
                     lab_test, lab_value, lab_unit, lab_normal_low, lab_normal_high, lab_flag)
                    VALUES (:r, :id, :site, :vname, :ldate,
                            :test, :val, :unit, :lo, :hi, :flag)
                """), {
                    "r":     run_id,
                    "id":    s[0],
                    "site":  s[1],
                    "vname": vname,
                    "ldate": visit(vday),
                    "test":  test,
                    "val":   str(actual_val),
                    "unit":  unit,
                    "lo":    str(lo),
                    "hi":    str(hi),
                    "flag":  flag,
                })


def insert_fibroscan(sess, run_id: int):
    # All subjects get FibroScan at Screening and Week24
    # KRY-007 is missing Baseline FibroScan (intentional gap)
    fibroscan_visits = [
        ("Screening", -14, 9.8,  1.2, 72.0, 245.0),
        ("Baseline",    1, 10.1, 1.5, 68.0, 250.0),
        ("Week24",    168,  8.3, 1.1, 75.0, 230.0),
    ]
    for s in SUBJECTS:
        for vname, vday, lsm, iqr, sr, cap in fibroscan_visits:
            # KRY-007 skips Baseline FibroScan
            if s[0] == "KRY-007" and vname == "Baseline":
                continue
            sess.execute(text("""
                INSERT INTO staging.fibroscan
                (run_id, rave_subject_id, site_id, visit_name, scan_date,
                 lsm_kpa, lsm_iqr, lsm_success_rate, cap_score, operator_id)
                VALUES (:r, :id, :site, :vname, :sdate,
                        :lsm, :iqr, :sr, :cap, 'OP-01')
            """), {
                "r":     run_id,
                "id":    s[0],
                "site":  s[1],
                "vname": vname,
                "sdate": visit(vday),
                "lsm":   str(lsm),
                "iqr":   str(iqr),
                "sr":    str(sr),
                "cap":   str(cap),
            })


def insert_urinalysis(sess, run_id: int):
    for s in SUBJECTS:
        sess.execute(text("""
            INSERT INTO staging.urinalysis
            (run_id, rave_subject_id, site_id, visit_name, urine_date,
             specific_gravity, ph, protein, glucose, blood, ketones,
             leukocyte_esterase, nitrites)
            VALUES (:r, :id, :site, 'Baseline', :udate,
                    '1.015', '6.0', 'NEG', 'NEG', 'NEG', 'NEG', 'NEG', 'NEG')
        """), {
            "r":     run_id,
            "id":    s[0],
            "site":  s[1],
            "udate": visit(1),
        })


def insert_treatment(sess, run_id: int):
    for s in SUBJECTS:
        # Study drug
        sess.execute(text("""
            INSERT INTO staging.treatment
            (run_id, rave_subject_id, site_id, drug_name, dose, dose_unit,
             frequency, route, start_date, ongoing, treatment_type)
            VALUES (:r, :id, :site, 'KRY-101', '50', 'mg',
                    'QD', 'ORAL', :sdate, 'Y', 'STUDY_DRUG')
        """), {
            "r":     run_id,
            "id":    s[0],
            "site":  s[1],
            "sdate": baseline(),
        })


# ── Entry point ───────────────────────────────────────────────

def generate(config_path: str = "config/config.yaml") -> int:
    """Insert mock data into staging and return the run_id."""
    init_engine(config_path)
    run_id = create_pipeline_run(triggered_by="mock_test", rave_env="TEST")
    print(f"Created pipeline run: run_id={run_id}")

    with get_session() as sess:
        insert_demographics(sess, run_id)
        print(f"  ✓ Demographics  ({len(SUBJECTS)} subjects)")
        insert_consent(sess, run_id)
        print(f"  ✓ Consent forms ({len(SUBJECTS)} records)")
        insert_visits(sess, run_id)
        print(f"  ✓ Visits        ({len(SUBJECTS) * 5} records)")
        insert_labs(sess, run_id)
        print(f"  ✓ Labs          ({len(SUBJECTS) * 2 * 8} records)")
        insert_fibroscan(sess, run_id)
        print(f"  ✓ FibroScan     ({len(SUBJECTS) * 3 - 1} records)")
        insert_urinalysis(sess, run_id)
        print(f"  ✓ Urinalysis    ({len(SUBJECTS)} records)")
        insert_treatment(sess, run_id)
        print(f"  ✓ Treatment     ({len(SUBJECTS)} records)")

    print(f"\nMock data ready. run_id={run_id}")
    return run_id


if __name__ == "__main__":
    run_id = generate()
    print(f"\nrun_id to use for pipeline stages: {run_id}")
