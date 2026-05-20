"""
Data sanitization checks.

Reads from staging tables, runs validation rules, and writes
issues to audit.sanitization_log. Raises no exceptions —
all problems are logged as records so the pipeline continues.
"""

import logging
import os
import re
from datetime import date, datetime
from typing import Any

import yaml
from sqlalchemy import text

from pipeline.db import get_session

logger = logging.getLogger(__name__)

DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%Y%m%d"]
TODAY = date.today()


def _load_rules(config_path: str = "config/protocol_rules.yaml") -> dict:
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _parse_date(val: str | None) -> date | None:
    if not val or str(val).strip() == "":
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(str(val).strip(), fmt).date()
        except ValueError:
            continue
    return None


def _log_issue(
    sess,
    run_id: int,
    subject_id: str,
    site_id: str,
    domain: str,
    field: str,
    value: Any,
    issue_type: str,
    severity: str,
    message: str,
):
    sess.execute(
        text(
            "INSERT INTO audit.sanitization_log "
            "(run_id, rave_subject_id, site_id, domain, field_name, field_value, "
            " issue_type, severity, message) "
            "VALUES (:run_id, :subj, :site, :domain, :field, :value, "
            "        :itype, :sev, :msg)"
        ),
        {
            "run_id": run_id,
            "subj":   subject_id,
            "site":   site_id,
            "domain": domain,
            "field":  field,
            "value":  str(value) if value is not None else "",
            "itype":  issue_type,
            "sev":    severity,
            "msg":    message,
        },
    )


# ── Domain-specific checks ────────────────────────────────────

def _check_demographics(sess, run_id: int, rules: dict):
    rows = sess.execute(
        text("SELECT * FROM staging.demographics WHERE run_id = :r"), {"r": run_id}
    ).mappings().all()

    seen_subjects: set[str] = set()

    for row in rows:
        subj = row["rave_subject_id"] or ""
        site = row["site_id"] or ""

        # Duplicate subject
        if subj in seen_subjects:
            _log_issue(sess, run_id, subj, site, "DEMOGRAPHICS", "rave_subject_id",
                       subj, "DUPLICATE", "ERROR",
                       f"Duplicate subject record: {subj}")
        seen_subjects.add(subj)

        # Required fields
        for field in ("rave_subject_id", "site_id", "date_of_birth", "sex",
                      "enrollment_date"):
            if not row[field]:
                _log_issue(sess, run_id, subj, site, "DEMOGRAPHICS", field,
                           None, "MISSING", "ERROR",
                           f"Required field '{field}' is missing for subject {subj}")

        # Date of birth validity
        dob = _parse_date(row["date_of_birth"])
        if row["date_of_birth"] and dob is None:
            _log_issue(sess, run_id, subj, site, "DEMOGRAPHICS", "date_of_birth",
                       row["date_of_birth"], "INVALID_DATE", "ERROR",
                       f"Unparseable date_of_birth: '{row['date_of_birth']}'")
        elif dob and dob > TODAY:
            _log_issue(sess, run_id, subj, site, "DEMOGRAPHICS", "date_of_birth",
                       str(dob), "INVALID_DATE", "ERROR",
                       "date_of_birth is in the future")

        # Age range check against protocol eligibility
        if dob:
            age = (TODAY - dob).days // 365
            age_min = rules["eligibility"]["inclusion"]["age_min"]
            age_max = rules["eligibility"]["inclusion"]["age_max"]
            if not (age_min <= age <= age_max):
                _log_issue(sess, run_id, subj, site, "DEMOGRAPHICS", "date_of_birth",
                           str(age), "OUT_OF_RANGE", "WARNING",
                           f"Calculated age {age} outside eligibility range "
                           f"[{age_min}, {age_max}]")

        # Sex controlled vocabulary
        if row["sex"] and row["sex"].upper() not in ("M", "F", "U", "MALE", "FEMALE", "UNKNOWN"):
            _log_issue(sess, run_id, subj, site, "DEMOGRAPHICS", "sex",
                       row["sex"], "FORMAT_ERROR", "WARNING",
                       f"Unexpected sex value: '{row['sex']}'")

        # Enrollment date must not be in the future
        enrl = _parse_date(row["enrollment_date"])
        if enrl and enrl > TODAY:
            _log_issue(sess, run_id, subj, site, "DEMOGRAPHICS", "enrollment_date",
                       str(enrl), "INVALID_DATE", "ERROR",
                       "enrollment_date is in the future")


def _check_labs(sess, run_id: int, rules: dict):
    rows = sess.execute(
        text("SELECT * FROM staging.labs WHERE run_id = :r"), {"r": run_id}
    ).mappings().all()

    lab_rules = rules.get("lab_ranges", {})

    for row in rows:
        subj = row["rave_subject_id"] or ""
        site = row["site_id"] or ""

        # Required fields
        for field in ("lab_date", "lab_test", "lab_value"):
            if not row[field]:
                _log_issue(sess, run_id, subj, site, "LABS", field,
                           None, "MISSING", "ERROR",
                           f"Missing '{field}' for subject {subj}, visit {row['visit_name']}")

        # Date validity
        lab_date = _parse_date(row["lab_date"])
        if row["lab_date"] and lab_date is None:
            _log_issue(sess, run_id, subj, site, "LABS", "lab_date",
                       row["lab_date"], "INVALID_DATE", "ERROR",
                       f"Unparseable lab_date: '{row['lab_date']}'")
        elif lab_date and lab_date > TODAY:
            _log_issue(sess, run_id, subj, site, "LABS", "lab_date",
                       str(lab_date), "INVALID_DATE", "ERROR",
                       "lab_date is in the future")

        # Numeric value check
        if row["lab_value"]:
            try:
                val = float(row["lab_value"])
            except ValueError:
                _log_issue(sess, run_id, subj, site, "LABS", "lab_value",
                           row["lab_value"], "FORMAT_ERROR", "ERROR",
                           f"lab_value is not numeric: '{row['lab_value']}'")
                continue

            # Negative values are never valid for clinical labs
            if val < 0:
                _log_issue(sess, run_id, subj, site, "LABS", "lab_value",
                           str(val), "OUT_OF_RANGE", "ERROR",
                           "lab_value is negative")

            # Protocol reference range check
            test = (row["lab_test"] or "").upper().replace(" ", "_")
            if test in lab_rules:
                ref = lab_rules[test]
                lo, hi = ref.get("low", 0), ref.get("high", float("inf"))
                if not (lo <= val <= hi * 5):   # 5× ULN = plausible extreme
                    _log_issue(sess, run_id, subj, site, "LABS", "lab_value",
                               str(val), "OUT_OF_RANGE", "WARNING",
                               f"{test} value {val} {ref.get('unit','')} is outside "
                               f"plausible range [{lo}, {hi * 5}]")


def _check_fibroscan(sess, run_id: int, rules: dict):
    rows = sess.execute(
        text("SELECT * FROM staging.fibroscan WHERE run_id = :r"), {"r": run_id}
    ).mappings().all()

    fs_rules = rules.get("fibroscan", {})

    for row in rows:
        subj = row["rave_subject_id"] or ""
        site = row["site_id"] or ""

        for field in ("scan_date", "lsm_kpa"):
            if not row[field]:
                _log_issue(sess, run_id, subj, site, "FIBROSCAN", field,
                           None, "MISSING", "ERROR",
                           f"Missing '{field}' for subject {subj}")

        if row["lsm_kpa"]:
            try:
                lsm = float(row["lsm_kpa"])
            except ValueError:
                _log_issue(sess, run_id, subj, site, "FIBROSCAN", "lsm_kpa",
                           row["lsm_kpa"], "FORMAT_ERROR", "ERROR",
                           "lsm_kpa is not numeric")
                continue

            if lsm < 1.5 or lsm > 75.0:
                _log_issue(sess, run_id, subj, site, "FIBROSCAN", "lsm_kpa",
                           str(lsm), "OUT_OF_RANGE", "WARNING",
                           f"LSM {lsm} kPa is outside plausible range [1.5, 75.0]")

        # IQR quality check: IQR/LSM ≤ 30% for reliable result
        if row["lsm_kpa"] and row["lsm_iqr"]:
            try:
                lsm = float(row["lsm_kpa"])
                iqr = float(row["lsm_iqr"])
                if lsm > 0 and (iqr / lsm) > 0.30:
                    _log_issue(sess, run_id, subj, site, "FIBROSCAN", "lsm_iqr",
                               str(iqr), "OUT_OF_RANGE", "WARNING",
                               f"FibroScan IQR/LSM ratio {iqr/lsm:.2f} exceeds 0.30 "
                               f"— result quality may be unreliable")
            except (ValueError, ZeroDivisionError):
                pass

        # Success rate ≥ 60%
        if row["lsm_success_rate"]:
            try:
                sr = float(row["lsm_success_rate"])
                if sr < 60.0:
                    _log_issue(sess, run_id, subj, site, "FIBROSCAN", "lsm_success_rate",
                               str(sr), "OUT_OF_RANGE", "WARNING",
                               f"FibroScan success rate {sr}% < 60% — consider repeat")
            except ValueError:
                pass


def _check_urinalysis(sess, run_id: int, rules: dict):
    rows = sess.execute(
        text("SELECT * FROM staging.urinalysis WHERE run_id = :r"), {"r": run_id}
    ).mappings().all()

    ua_rules = rules.get("urinalysis", {})
    semiquant = ("NEG", "TRACE", "1+", "2+", "3+", "4+")

    for row in rows:
        subj = row["rave_subject_id"] or ""
        site = row["site_id"] or ""

        if not row["urine_date"]:
            _log_issue(sess, run_id, subj, site, "URINALYSIS", "urine_date",
                       None, "MISSING", "ERROR",
                       f"Missing urine_date for subject {subj}")

        # Validate semi-quantitative fields
        for field in ("protein", "glucose", "blood", "ketones", "leukocyte_esterase"):
            val = (row[field] or "").strip().upper()
            if val and val not in [v.upper() for v in semiquant] + ["POSITIVE", "NEGATIVE"]:
                _log_issue(sess, run_id, subj, site, "URINALYSIS", field,
                           row[field], "FORMAT_ERROR", "WARNING",
                           f"Unexpected value '{row[field]}' for {field}")

        # Alert thresholds from protocol rules
        protein_alert = ua_rules.get("protein_alert", "2+")
        if row["protein"] and row["protein"].upper() in ("2+", "3+", "4+"):
            level = row["protein"]
            if semiquant.index(level) >= semiquant.index(protein_alert):
                _log_issue(sess, run_id, subj, site, "URINALYSIS", "protein",
                           level, "OUT_OF_RANGE", "WARNING",
                           f"Proteinuria ≥ {protein_alert} — clinical review required")


def _check_treatment(sess, run_id: int, rules: dict):
    rows = sess.execute(
        text("SELECT * FROM staging.treatment WHERE run_id = :r"), {"r": run_id}
    ).mappings().all()

    for row in rows:
        subj = row["rave_subject_id"] or ""
        site = row["site_id"] or ""

        if not row["drug_name"]:
            _log_issue(sess, run_id, subj, site, "TREATMENT", "drug_name",
                       None, "MISSING", "ERROR",
                       f"Missing drug_name for subject {subj}")

        start = _parse_date(row["start_date"])
        end = _parse_date(row["end_date"])

        if row["start_date"] and start is None:
            _log_issue(sess, run_id, subj, site, "TREATMENT", "start_date",
                       row["start_date"], "INVALID_DATE", "ERROR",
                       f"Unparseable start_date: '{row['start_date']}'")

        if start and end and end < start:
            _log_issue(sess, run_id, subj, site, "TREATMENT", "end_date",
                       str(end), "INVALID_DATE", "ERROR",
                       f"end_date {end} is before start_date {start}")


# ── Public entry point ────────────────────────────────────────

class Sanitizer:
    def __init__(self, protocol_config: str = "config/protocol_rules.yaml"):
        self.rules = _load_rules(protocol_config)

    def run(self, run_id: int) -> dict[str, int]:
        """Run all sanitization checks for run_id. Returns issue counts per domain."""
        logger.info("Starting sanitization checks for run_id=%s", run_id)
        counts: dict[str, int] = {}

        checks = [
            ("DEMOGRAPHICS", _check_demographics),
            ("LABS",         _check_labs),
            ("FIBROSCAN",    _check_fibroscan),
            ("URINALYSIS",   _check_urinalysis),
            ("TREATMENT",    _check_treatment),
        ]

        with get_session() as sess:
            for domain, check_fn in checks:
                before = sess.execute(
                    text("SELECT COUNT(*) FROM audit.sanitization_log WHERE run_id = :r"),
                    {"r": run_id}
                ).scalar()
                check_fn(sess, run_id, self.rules)
                after = sess.execute(
                    text("SELECT COUNT(*) FROM audit.sanitization_log WHERE run_id = :r"),
                    {"r": run_id}
                ).scalar()
                counts[domain] = after - before
                logger.info("Sanitization [%s]: %d issues found", domain, counts[domain])

        total = sum(counts.values())
        logger.info("Sanitization complete. Total issues: %d", total)
        return counts
