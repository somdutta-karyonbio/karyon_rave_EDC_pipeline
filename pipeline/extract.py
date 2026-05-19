"""
Extraction layer — pulls data from RAVE and writes raw rows into staging tables.
Column mapping translates RAVE ItemOIDs to human-readable staging column names.
"""

import logging
from typing import Any

from sqlalchemy import text

from pipeline.db import get_session
from pipeline.rave_client import RaveClient

logger = logging.getLogger(__name__)

# ── RAVE ItemOID → staging column mappings ────────────────────
# These OIDs must match the EDC build for the Karyon Bio study.
# Update if the CRF is amended.

DEMOGRAPHICS_MAP = {
    "BRTHDAT":    "date_of_birth",
    "SEX":        "sex",
    "RACE":       "race",
    "ETHNIC":     "ethnicity",
    "COUNTRY":    "country",
    "ENRLDT":     "enrollment_date",
    "RANDDT":     "randomization_date",
    "ARM":        "treatment_arm",
    "SUBNUM":     "subject_number",
    "INITIALS":   "initials",
}

CONSENT_MAP = {
    "ICFVER":     "icf_version",
    "ICFDAT":     "consent_date",
    "ICFBY":      "consent_obtained_by",
    "REICF":      "re_consent_required",
    "REICFDAT":   "re_consent_date",
    "REICFVER":   "re_consent_version",
    "WDRAWDT":    "withdrawal_date",
    "WDRAWRSN":   "withdrawal_reason",
}

VISIT_MAP = {
    "VISITDAT":   "visit_date",
    "VISITSTAT":  "visit_status",
}

LABS_MAP = {
    "LBDAT":      "lab_date",
    "LBTEST":     "lab_test",
    "LBVAL":      "lab_value",
    "LBUNIT":     "lab_unit",
    "LBNRLO":     "lab_normal_low",
    "LBNRHI":     "lab_normal_high",
    "LBFLAG":     "lab_flag",
    "LBLABID":    "local_lab_id",
}

FIBROSCAN_MAP = {
    "FSBDAT":     "scan_date",
    "FSBLSM":     "lsm_kpa",
    "FSBIQR":     "lsm_iqr",
    "FSBSR":      "lsm_success_rate",
    "FSBCAP":     "cap_score",
    "FSBOP":      "operator_id",
    "FSBDEV":     "device_serial",
}

URINALYSIS_MAP = {
    "UALDAT":     "urine_date",
    "UALSG":      "specific_gravity",
    "UALPH":      "ph",
    "UALPRO":     "protein",
    "UALGLUC":    "glucose",
    "UALKET":     "ketones",
    "UALBLOOD":   "blood",
    "UALLE":      "leukocyte_esterase",
    "UALNITR":    "nitrites",
    "UALRBC":     "microscopy_rbc",
    "UALWBC":     "microscopy_wbc",
    "UALCAST":    "microscopy_casts",
}

TREATMENT_MAP = {
    "CMDRUG":     "drug_name",
    "CMDOSE":     "dose",
    "CMDOSU":     "dose_unit",
    "CMDOSFRQ":   "frequency",
    "CMROUTE":    "route",
    "CMSTDAT":    "start_date",
    "CMENDAT":    "end_date",
    "CMONGO":     "ongoing",
    "CMINDIC":    "indication",
    "CMTYPE":     "treatment_type",
}

AE_MAP = {
    "AETERM":     "ae_term",
    "AESTDAT":    "ae_start_date",
    "AEENDAT":    "ae_end_date",
    "AEONGO":     "ae_ongoing",
    "AESEV":      "severity",
    "AESER":      "serious",
    "AESCRIT":    "seriousness_criteria",
    "AEREL":      "relationship",
    "AEOUT":      "outcome",
    "AEACN":      "action_taken",
}


def _map_row(raw: dict, mapping: dict) -> dict:
    """Apply an OID→column mapping to a raw RAVE row."""
    mapped = {
        "rave_subject_id": raw.get("SubjectKey", ""),
        "site_id":         raw.get("SiteOID", ""),
    }
    visit_name = raw.get("StudyEventOID", "")
    if visit_name:
        mapped["visit_name"] = visit_name
    for oid, col in mapping.items():
        mapped[col] = raw.get(oid, None)
    return mapped


def _bulk_insert(session, table: str, rows: list[dict], run_id: int):
    if not rows:
        return
    for row in rows:
        row["run_id"] = run_id
    cols = list(rows[0].keys())
    placeholders = ", ".join(f":{c}" for c in cols)
    col_list = ", ".join(cols)
    sql = text(f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})")
    session.execute(sql, rows)
    logger.info("Inserted %d rows into %s", len(rows), table)


class Extractor:
    def __init__(self, config_path: str = "config/config.yaml"):
        self.client = RaveClient(config_path)

    def run(self, run_id: int) -> dict[str, int]:
        """Extract all domains and load into staging. Returns row counts per domain."""
        logger.info("Starting RAVE extraction for run_id=%s", run_id)

        if not self.client.ping():
            raise ConnectionError("Cannot reach RAVE Web Services. Aborting extraction.")

        raw = self.client.get_all_datasets()
        counts: dict[str, int] = {}

        domain_config: list[tuple[str, str, dict]] = [
            ("demographics", "staging.demographics", DEMOGRAPHICS_MAP),
            ("consent",      "staging.consent",      CONSENT_MAP),
            ("visits",       "staging.visits",        VISIT_MAP),
            ("labs",         "staging.labs",          LABS_MAP),
            ("fibroscan",    "staging.fibroscan",     FIBROSCAN_MAP),
            ("urinalysis",   "staging.urinalysis",    URINALYSIS_MAP),
            ("treatment",    "staging.treatment",     TREATMENT_MAP),
            ("adverse_events", "staging.adverse_events", AE_MAP),
        ]

        with get_session() as sess:
            for domain_key, table, mapping in domain_config:
                raw_rows = raw.get(domain_key, [])
                mapped = [_map_row(r, mapping) for r in raw_rows]
                _bulk_insert(sess, table, mapped, run_id)
                counts[domain_key] = len(mapped)

        logger.info("Extraction complete. Row counts: %s", counts)
        return counts
