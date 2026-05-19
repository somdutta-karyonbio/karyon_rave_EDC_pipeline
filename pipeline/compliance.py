"""
Protocol compliance checks.

Reads from staging tables and writes findings to:
  - audit.protocol_deviations  (visit windows, eligibility, dose deviations)
  - audit.data_queries          (auto-generated queries for site review)

All checks are non-blocking — violations are logged, not raised.
"""

import logging
from datetime import date, datetime, timedelta
from typing import Any

import yaml
from sqlalchemy import text

from pipeline.db import get_session

logger = logging.getLogger(__name__)

DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%Y%m%d"]


def _load_rules(config_path: str = "config/protocol_rules.yaml") -> dict:
    with open(config_path) as f:
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


def _log_deviation(
    sess, run_id: int, subject: str, site: str,
    visit: str, dev_type: str, description: str,
    deviation_date: date | None = None,
    severity: str = "MAJOR",
    safety_impact: bool = False,
    efficacy_impact: bool = False,
):
    sess.execute(
        text(
            "INSERT INTO audit.protocol_deviations "
            "(run_id, rave_subject_id, site_id, visit_name, deviation_type, "
            " description, detected_date, deviation_date, severity, "
            " impact_on_safety, impact_on_efficacy) "
            "VALUES (:run_id, :subj, :site, :visit, :dtype, :desc, "
            "        CURRENT_DATE, :devdt, :sev, :safe, :eff)"
        ),
        {
            "run_id": run_id, "subj": subject, "site": site, "visit": visit,
            "dtype": dev_type, "desc": description,
            "devdt": deviation_date, "sev": severity,
            "safe": safety_impact, "eff": efficacy_impact,
        },
    )


def _log_query(
    sess, run_id: int, subject: str, site: str,
    domain: str, visit: str, field: str,
    query_text: str, query_type: str = "CLARIFICATION",
):
    sess.execute(
        text(
            "INSERT INTO audit.data_queries "
            "(run_id, rave_subject_id, site_id, domain, visit_name, "
            " field_name, query_text, query_type) "
            "VALUES (:run_id, :subj, :site, :domain, :visit, "
            "        :field, :qtext, :qtype)"
        ),
        {
            "run_id": run_id, "subj": subject, "site": site,
            "domain": domain, "visit": visit, "field": field,
            "qtext": query_text, "qtype": query_type,
        },
    )


# ── Check 1: Visit Window Compliance ─────────────────────────

def _check_visit_windows(sess, run_id: int, rules: dict):
    """Compare each subject's actual visit dates to protocol-defined windows."""
    visit_rules: list[dict] = rules.get("visits", [])

    # Get baseline date per subject
    baselines = {}
    baseline_rows = sess.execute(
        text(
            "SELECT rave_subject_id, visit_date FROM staging.visits "
            "WHERE run_id = :r AND UPPER(visit_name) LIKE '%BASELINE%'"
        ),
        {"r": run_id},
    ).mappings().all()

    for row in baseline_rows:
        d = _parse_date(row["visit_date"])
        if d:
            baselines[row["rave_subject_id"]] = d

    visit_rows = sess.execute(
        text("SELECT * FROM staging.visits WHERE run_id = :r"), {"r": run_id}
    ).mappings().all()

    for row in visit_rows:
        subj = row["rave_subject_id"] or ""
        site = row["site_id"] or ""
        visit_name = (row["visit_name"] or "").upper()
        visit_date = _parse_date(row["visit_date"])

        if not visit_date:
            continue

        baseline = baselines.get(subj)
        if not baseline:
            continue

        # Find matching protocol visit definition
        proto_visit = next(
            (v for v in visit_rules if v["name"].upper() in visit_name or visit_name in v["name"].upper()),
            None,
        )
        if not proto_visit:
            continue

        target_day = proto_visit["target_day"]
        window_early = proto_visit["window_early"]
        window_late  = proto_visit["window_late"]

        actual_day = (visit_date - baseline).days
        earliest_allowed = target_day + window_early
        latest_allowed   = target_day + window_late

        if not (earliest_allowed <= actual_day <= latest_allowed):
            deviation_days = actual_day - target_day
            _log_deviation(
                sess, run_id, subj, site, row["visit_name"],
                "VISIT_WINDOW",
                f"Visit '{row['visit_name']}' occurred on Day {actual_day} "
                f"(target Day {target_day}, window [{earliest_allowed}, {latest_allowed}]). "
                f"Deviation: {deviation_days:+d} days.",
                deviation_date=visit_date,
                severity="MINOR" if abs(deviation_days) <= 7 else "MAJOR",
            )
            _log_query(
                sess, run_id, subj, site, "VISITS", row["visit_name"], "visit_date",
                f"Visit date {visit_date} is outside the protocol window for "
                f"'{row['visit_name']}' (Day {target_day} ± {abs(window_early)}/{window_late}d). "
                f"Please confirm the date or document as protocol deviation.",
                "DISCREPANCY",
            )


# ── Check 2: Eligibility Criteria ────────────────────────────

def _check_eligibility(sess, run_id: int, rules: dict):
    """Flag subjects who may not meet inclusion/exclusion criteria."""
    incl = rules["eligibility"]["inclusion"]
    excl = rules["eligibility"]["exclusion"]

    demo_rows = sess.execute(
        text("SELECT * FROM staging.demographics WHERE run_id = :r"), {"r": run_id}
    ).mappings().all()

    for row in demo_rows:
        subj = row["rave_subject_id"] or ""
        site = row["site_id"] or ""

        # Age check
        dob = _parse_date(row["date_of_birth"])
        if dob:
            age = (date.today() - dob).days // 365
            if age < incl["age_min"] or age > incl["age_max"]:
                _log_deviation(
                    sess, run_id, subj, site, "Screening",
                    "ELIGIBILITY_VIOLATION",
                    f"Subject age {age} years does not meet inclusion criteria "
                    f"(min {incl['age_min']}, max {incl['age_max']})",
                    severity="MAJOR", safety_impact=False, efficacy_impact=True,
                )

    # BMI (if available in labs as derived or separate field)
    bmi_rows = sess.execute(
        text(
            "SELECT rave_subject_id, site_id, lab_value FROM staging.labs "
            "WHERE run_id = :r AND UPPER(lab_test) = 'BMI'"
        ),
        {"r": run_id},
    ).mappings().all()

    for row in bmi_rows:
        subj = row["rave_subject_id"] or ""
        site = row["site_id"] or ""
        try:
            bmi = float(row["lab_value"])
            if bmi < incl["bmi_min"] or bmi > incl["bmi_max"]:
                _log_deviation(
                    sess, run_id, subj, site, "Screening",
                    "ELIGIBILITY_VIOLATION",
                    f"BMI {bmi} kg/m² outside inclusion range "
                    f"[{incl['bmi_min']}, {incl['bmi_max']}]",
                    severity="MAJOR",
                )
        except (ValueError, TypeError):
            pass

    # FibroScan eligibility — must be ≥ F2 at screening
    fs_rows = sess.execute(
        text(
            "SELECT rave_subject_id, site_id, lsm_kpa FROM staging.fibroscan "
            "WHERE run_id = :r AND UPPER(visit_name) LIKE '%SCREEN%'"
        ),
        {"r": run_id},
    ).mappings().all()

    fs_min = incl["fibroscan_min_kpa"]
    for row in fs_rows:
        subj = row["rave_subject_id"] or ""
        site = row["site_id"] or ""
        try:
            lsm = float(row["lsm_kpa"])
            if lsm < fs_min:
                _log_deviation(
                    sess, run_id, subj, site, "Screening",
                    "ELIGIBILITY_VIOLATION",
                    f"Screening FibroScan LSM {lsm} kPa < {fs_min} kPa (F2 threshold). "
                    f"Subject may not meet inclusion criterion for liver fibrosis stage.",
                    severity="MAJOR", efficacy_impact=True,
                )
        except (ValueError, TypeError):
            pass


# ── Check 3: Missing Protocol-Mandated Assessments ───────────

def _check_missing_assessments(sess, run_id: int, rules: dict):
    """Flag visits that are missing key mandated assessments."""
    required_per_visit = {
        "BASELINE": ["labs", "fibroscan", "urinalysis"],
        "WEEK12":   ["labs", "fibroscan"],
        "WEEK24":   ["labs", "fibroscan", "urinalysis"],
        "EOT":      ["labs", "fibroscan", "urinalysis"],
    }

    visit_rows = sess.execute(
        text(
            "SELECT DISTINCT rave_subject_id, site_id, visit_name "
            "FROM staging.visits WHERE run_id = :r AND visit_status = 'COMPLETED'"
        ),
        {"r": run_id},
    ).mappings().all()

    for vrow in visit_rows:
        subj = vrow["rave_subject_id"]
        site = vrow["site_id"]
        visit_upper = (vrow["visit_name"] or "").upper().replace(" ", "")

        for key, domains in required_per_visit.items():
            if key not in visit_upper:
                continue
            for domain in domains:
                table = f"staging.{domain}"
                count = sess.execute(
                    text(
                        f"SELECT COUNT(*) FROM {table} "
                        "WHERE run_id = :r AND rave_subject_id = :s "
                        "AND UPPER(REPLACE(visit_name,' ','')) = :v"
                    ),
                    {"r": run_id, "s": subj, "v": visit_upper},
                ).scalar()
                if count == 0:
                    _log_deviation(
                        sess, run_id, subj, site, vrow["visit_name"],
                        "MISSING_ASSESSMENT",
                        f"No {domain.upper()} data found for completed visit "
                        f"'{vrow['visit_name']}' — required by protocol.",
                        severity="MAJOR", efficacy_impact=True,
                    )
                    _log_query(
                        sess, run_id, subj, site, domain.upper(),
                        vrow["visit_name"], domain,
                        f"Protocol requires {domain.upper()} at visit "
                        f"'{vrow['visit_name']}' but no data has been entered. "
                        "Please enter the data or confirm assessment was not performed.",
                        "MISSING_DATA",
                    )


# ── Check 4: Critical Lab Threshold Alerts ───────────────────

def _check_critical_labs(sess, run_id: int, rules: dict):
    """Generate queries for critically abnormal lab values (NCI CTCAE Grade ≥ 3)."""
    lab_rules = rules.get("lab_ranges", {})

    # Grade 3+ thresholds relative to ULN (simplified — update to full CTCAE table)
    grade3_multipliers = {
        "ALT":   5.0,
        "AST":   5.0,
        "ALP":   5.0,
        "GGT":   5.0,
        "TOTAL_BILIRUBIN": 3.0,
    }

    lab_rows = sess.execute(
        text("SELECT * FROM staging.labs WHERE run_id = :r"), {"r": run_id}
    ).mappings().all()

    for row in lab_rows:
        subj = row["rave_subject_id"] or ""
        site = row["site_id"] or ""
        test = (row["lab_test"] or "").upper().replace(" ", "_")

        if test not in grade3_multipliers or not row["lab_value"]:
            continue
        try:
            val = float(row["lab_value"])
        except ValueError:
            continue

        uln = lab_rules.get(test.lower(), {}).get("uln", 0)
        threshold = grade3_multipliers[test]

        if uln and val >= uln * threshold:
            _log_deviation(
                sess, run_id, subj, site, row["visit_name"],
                "CRITICAL_LAB_VALUE",
                f"{test} = {val} {lab_rules.get(test.lower(),{}).get('unit','')} "
                f"(≥ {threshold}× ULN of {uln}). "
                f"NCI CTCAE Grade ≥ 3 — immediate clinical review required.",
                deviation_date=_parse_date(row["lab_date"]),
                severity="MAJOR",
                safety_impact=True,
            )


# ── Public entry point ────────────────────────────────────────

class ComplianceChecker:
    def __init__(self, protocol_config: str = "config/protocol_rules.yaml"):
        self.rules = _load_rules(protocol_config)

    def run(self, run_id: int) -> dict[str, int]:
        """Run all protocol compliance checks. Returns deviation counts per check."""
        logger.info("Starting compliance checks for run_id=%s", run_id)
        counts: dict[str, int] = {}

        checks = [
            ("VISIT_WINDOWS",        _check_visit_windows),
            ("ELIGIBILITY",          _check_eligibility),
            ("MISSING_ASSESSMENTS",  _check_missing_assessments),
            ("CRITICAL_LABS",        _check_critical_labs),
        ]

        with get_session() as sess:
            for check_name, check_fn in checks:
                before = sess.execute(
                    text("SELECT COUNT(*) FROM audit.protocol_deviations WHERE run_id = :r"),
                    {"r": run_id}
                ).scalar()
                check_fn(sess, run_id, self.rules)
                after = sess.execute(
                    text("SELECT COUNT(*) FROM audit.protocol_deviations WHERE run_id = :r"),
                    {"r": run_id}
                ).scalar()
                counts[check_name] = after - before
                logger.info("Compliance [%s]: %d deviations found", check_name, counts[check_name])

        total = sum(counts.values())
        logger.info("Compliance checks complete. Total deviations: %d", total)
        return counts
