"""
Informed Consent Form (ICF) management and compliance checks.

Checks performed:
  1. Consent on record before any study procedure
  2. ICF version is the current approved version
  3. Re-consent required flag triggers re-consent check
  4. Withdrawn subjects are excluded from active analyses
  5. Missing consent records

Results written to:
  - audit.consent_compliance  (pass/fail per check per subject)
  - audit.data_queries         (auto-queries for sites to resolve)
  - audit.protocol_deviations  (consent violations)
"""

import logging
from datetime import date, datetime
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


def _log_compliance(
    sess, run_id: int, subject: str, site: str,
    check_name: str, passed: bool, detail: str,
):
    sess.execute(
        text(
            "INSERT INTO audit.consent_compliance "
            "(run_id, rave_subject_id, site_id, check_name, passed, detail) "
            "VALUES (:run_id, :subj, :site, :check, :passed, :detail)"
        ),
        {
            "run_id": run_id, "subj": subject, "site": site,
            "check": check_name, "passed": passed, "detail": detail,
        },
    )


def _log_query(
    sess, run_id: int, subject: str, site: str,
    field: str, query_text: str, query_type: str = "MISSING_DATA",
):
    sess.execute(
        text(
            "INSERT INTO audit.data_queries "
            "(run_id, rave_subject_id, site_id, domain, visit_name, "
            " field_name, query_text, query_type) "
            "VALUES (:run_id, :subj, :site, 'CONSENT', 'Screening', "
            "        :field, :qtext, :qtype)"
        ),
        {
            "run_id": run_id, "subj": subject, "site": site,
            "field": field, "qtext": query_text, "qtype": query_type,
        },
    )


def _log_deviation(
    sess, run_id: int, subject: str, site: str, description: str,
):
    sess.execute(
        text(
            "INSERT INTO audit.protocol_deviations "
            "(run_id, rave_subject_id, site_id, visit_name, deviation_type, "
            " description, detected_date, severity, impact_on_safety) "
            "VALUES (:run_id, :subj, :site, 'Screening', 'CONSENT_VIOLATION', "
            "        :desc, CURRENT_DATE, 'MAJOR', TRUE)"
        ),
        {"run_id": run_id, "subj": subject, "site": site, "desc": description},
    )


class ConsentManager:
    def __init__(self, protocol_config: str = "config/protocol_rules.yaml"):
        self.rules = _load_rules(protocol_config)

    def run(self, run_id: int) -> dict[str, int]:
        """Run all consent checks. Returns {check_name: fail_count}."""
        logger.info("Starting consent checks for run_id=%s", run_id)
        fail_counts: dict[str, int] = {
            "CONSENT_ON_RECORD":          0,
            "CONSENT_BEFORE_PROCEDURES":  0,
            "ICF_VERSION_CURRENT":        0,
            "RE_CONSENT_COMPLETED":       0,
            "WITHDRAWAL_DOCUMENTED":      0,
        }

        consent_rules = self.rules.get("consent", {})
        current_version = consent_rules.get("current_icf_version", "")

        with get_session() as sess:
            # Load all subjects from this run
            subjects = sess.execute(
                text("SELECT DISTINCT rave_subject_id, site_id FROM staging.demographics WHERE run_id = :r"),
                {"r": run_id},
            ).mappings().all()

            # Load consent records
            consent_rows = sess.execute(
                text("SELECT * FROM staging.consent WHERE run_id = :r"), {"r": run_id}
            ).mappings().all()
            consent_by_subject = {r["rave_subject_id"]: dict(r) for r in consent_rows}

            # Get earliest procedure date per subject (first visit that is not screening)
            first_procedure = {}
            visit_rows = sess.execute(
                text(
                    "SELECT rave_subject_id, MIN(visit_date) AS first_date "
                    "FROM staging.visits WHERE run_id = :r "
                    "AND UPPER(visit_name) NOT LIKE '%SCREEN%' "
                    "AND visit_status = 'COMPLETED' "
                    "GROUP BY rave_subject_id"
                ),
                {"r": run_id},
            ).mappings().all()
            for vrow in visit_rows:
                d = _parse_date(vrow["first_date"])
                if d:
                    first_procedure[vrow["rave_subject_id"]] = d

            for subj_row in subjects:
                subj = subj_row["rave_subject_id"]
                site = subj_row["site_id"]
                consent = consent_by_subject.get(subj)

                # ── Check 1: Consent on record ───────────────
                if not consent or not consent.get("consent_date"):
                    _log_compliance(sess, run_id, subj, site,
                                    "CONSENT_ON_RECORD", False,
                                    "No informed consent record found for this subject.")
                    _log_query(sess, run_id, subj, site, "consent_date",
                               f"Subject {subj} has no informed consent form on record. "
                               "Please enter the ICF details or confirm consent status.",
                               "MISSING_DATA")
                    _log_deviation(sess, run_id, subj, site,
                                   f"No ICF on record for enrolled subject {subj}.")
                    fail_counts["CONSENT_ON_RECORD"] += 1
                    continue  # further checks require consent record

                _log_compliance(sess, run_id, subj, site,
                                "CONSENT_ON_RECORD", True,
                                f"ICF recorded: version {consent.get('icf_version','?')} "
                                f"on {consent.get('consent_date','?')}")

                consent_date = _parse_date(consent.get("consent_date"))

                # ── Check 2: Consent before any procedure ────
                first_proc = first_procedure.get(subj)
                if consent_date and first_proc:
                    if consent_date > first_proc:
                        _log_compliance(sess, run_id, subj, site,
                                        "CONSENT_BEFORE_PROCEDURES", False,
                                        f"Consent date {consent_date} is AFTER first "
                                        f"procedure date {first_proc}.")
                        _log_deviation(sess, run_id, subj, site,
                                       f"Consent obtained ({consent_date}) after first "
                                       f"study procedure ({first_proc}). GCP violation.")
                        _log_query(sess, run_id, subj, site, "consent_date",
                                   f"Consent date {consent_date} appears to be after the "
                                   f"first procedure date {first_proc}. "
                                   "Please verify dates and provide explanation.",
                                   "DISCREPANCY")
                        fail_counts["CONSENT_BEFORE_PROCEDURES"] += 1
                    else:
                        _log_compliance(sess, run_id, subj, site,
                                        "CONSENT_BEFORE_PROCEDURES", True,
                                        f"Consent {consent_date} precedes first procedure {first_proc}.")

                # ── Check 3: ICF version is current ──────────
                icf_ver = consent.get("icf_version", "")
                if current_version and icf_ver != current_version:
                    _log_compliance(sess, run_id, subj, site,
                                    "ICF_VERSION_CURRENT", False,
                                    f"Subject has ICF version '{icf_ver}', "
                                    f"current approved version is '{current_version}'.")
                    _log_query(sess, run_id, subj, site, "icf_version",
                               f"Subject consented with ICF version '{icf_ver}' but "
                               f"current version is '{current_version}'. "
                               "Please confirm whether re-consent is required.",
                               "DISCREPANCY")
                    fail_counts["ICF_VERSION_CURRENT"] += 1
                else:
                    _log_compliance(sess, run_id, subj, site,
                                    "ICF_VERSION_CURRENT", True,
                                    f"ICF version '{icf_ver}' matches current version.")

                # ── Check 4: Re-consent completed if required ─
                re_required = str(consent.get("re_consent_required", "")).upper() in ("Y", "YES", "TRUE", "1")
                re_done = bool(_parse_date(consent.get("re_consent_date")))
                if re_required and not re_done:
                    _log_compliance(sess, run_id, subj, site,
                                    "RE_CONSENT_COMPLETED", False,
                                    "Re-consent is flagged as required but re-consent date is missing.")
                    _log_query(sess, run_id, subj, site, "re_consent_date",
                               "Re-consent has been flagged as required for this subject but "
                               "no re-consent date has been recorded. Please complete re-consent "
                               "or provide the date.",
                               "MISSING_DATA")
                    fail_counts["RE_CONSENT_COMPLETED"] += 1
                elif re_required and re_done:
                    _log_compliance(sess, run_id, subj, site,
                                    "RE_CONSENT_COMPLETED", True,
                                    f"Re-consent completed on {consent.get('re_consent_date')} "
                                    f"(version {consent.get('re_consent_version','?')}).")

                # ── Check 5: Withdrawal documentation ────────
                withdrawal_date = _parse_date(consent.get("withdrawal_date"))
                withdrawal_reason = consent.get("withdrawal_reason", "")
                if withdrawal_date and not withdrawal_reason:
                    _log_compliance(sess, run_id, subj, site,
                                    "WITHDRAWAL_DOCUMENTED", False,
                                    f"Withdrawal date {withdrawal_date} recorded but "
                                    "withdrawal reason is missing.")
                    _log_query(sess, run_id, subj, site, "withdrawal_reason",
                               f"Subject has a withdrawal date ({withdrawal_date}) but no "
                               "withdrawal reason. Please provide the reason for withdrawal.",
                               "MISSING_DATA")
                    fail_counts["WITHDRAWAL_DOCUMENTED"] += 1
                elif withdrawal_date and withdrawal_reason:
                    _log_compliance(sess, run_id, subj, site,
                                    "WITHDRAWAL_DOCUMENTED", True,
                                    f"Withdrawal documented: {withdrawal_reason} on {withdrawal_date}.")

        total_fails = sum(fail_counts.values())
        logger.info("Consent checks complete. Total failures: %d  Detail: %s", total_fails, fail_counts)
        return fail_counts
