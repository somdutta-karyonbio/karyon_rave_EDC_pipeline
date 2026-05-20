"""
Load layer — promotes validated staging data into the clinical schema.

Strategy:
  - UPSERT (INSERT ... ON CONFLICT DO UPDATE) to be idempotent.
  - Derives typed fields (dates, numerics, fibrosis stage, steatosis grade).
  - Skips records with blocking ERROR-level sanitization issues.
  - Links to clinical.subjects via rave_subject_id FK.
"""

import logging
from datetime import date, datetime
from typing import Any

import yaml
from sqlalchemy import text

from pipeline.db import get_session

logger = logging.getLogger(__name__)

DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%Y%m%d"]


def _parse_date(val: Any) -> date | None:
    if not val or str(val).strip() == "":
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(str(val).strip(), fmt).date()
        except ValueError:
            continue
    return None


def _to_num(val: Any) -> float | None:
    try:
        return float(val) if val not in (None, "", "null") else None
    except (ValueError, TypeError):
        return None


def _to_bool(val: Any) -> bool | None:
    if val is None:
        return None
    return str(val).upper() in ("Y", "YES", "TRUE", "1")


def _fibrosis_stage(lsm: float | None) -> str | None:
    """
    Metavir fibrosis staging from FibroScan LSM (kPa) — NASH/NAFLD thresholds.

    F0  < 5.5 kPa   No fibrosis
    F1  5.5–7.0 kPa Portal fibrosis without septa
    F2  7.1–9.4 kPa Portal fibrosis with few septa (significant fibrosis)
    F3  9.5–12.4 kPa Bridging fibrosis / numerous septa
    F4  ≥ 12.5 kPa  Cirrhosis (F4 = cirrhosis in Metavir staging)
    """
    if lsm is None:
        return None
    if lsm < 5.5:
        return "F0"          # No fibrosis
    if lsm < 7.1:
        return "F1"          # Portal fibrosis, no septa
    if lsm < 9.5:
        return "F2"          # Significant fibrosis
    if lsm < 12.5:
        return "F3"          # Bridging fibrosis
    return "F4 (Cirrhosis)"  # F4 = cirrhosis in Metavir; ≥ 12.5 kPa


def _steatosis_grade(cap: float | None) -> str | None:
    if cap is None:
        return None
    if cap < 238:
        return "S0"
    if cap < 260:
        return "S1"
    if cap < 290:
        return "S2"
    return "S3"


def _blocked_subjects(sess, run_id: int) -> set[str]:
    """Return subject IDs with unresolved ERROR-level sanitization issues."""
    rows = sess.execute(
        text(
            "SELECT DISTINCT rave_subject_id FROM audit.sanitization_log "
            "WHERE run_id = :r AND severity = 'ERROR' AND resolved = FALSE"
        ),
        {"r": run_id},
    ).scalars().all()
    blocked = set(rows)
    if blocked:
        logger.warning("Blocking %d subjects from clinical load due to ERROR issues: %s",
                       len(blocked), blocked)
    return blocked


def _load_subjects(sess, run_id: int, blocked: set[str]) -> dict[str, int]:
    """Upsert subjects, return rave_subject_id → subject_id map."""
    rows = sess.execute(
        text("SELECT * FROM staging.demographics WHERE run_id = :r"), {"r": run_id}
    ).mappings().all()

    id_map: dict[str, int] = {}
    for row in rows:
        subj_id = row["rave_subject_id"]
        if subj_id in blocked:
            continue
        dob = _parse_date(row["date_of_birth"])
        sex_raw = (row["sex"] or "").upper()
        sex = "M" if sex_raw in ("M", "MALE") else "F" if sex_raw in ("F", "FEMALE") else "U"

        # Derive waist-hip ratio if both measurements present
        waist = _to_num(row.get("waist_cm"))
        hip   = _to_num(row.get("hip_cm"))
        whr   = round(waist / hip, 3) if waist and hip and hip > 0 else None

        result = sess.execute(
            text(
                "INSERT INTO clinical.subjects "
                "(rave_subject_id, site_id, subject_number, date_of_birth, sex, "
                " race, ethnicity, country, enrollment_date, randomization_date, "
                " treatment_arm, rave_status, "
                " weight_kg, height_cm, bmi, waist_cm, hip_cm, waist_hip_ratio) "
                "VALUES (:rave_id, :site, :subnum, :dob, :sex, "
                "        :race, :ethnic, :country, :enrl, :rand, :arm, :status, "
                "        :weight, :height, :bmi, :waist, :hip, :whr) "
                "ON CONFLICT (rave_subject_id) DO UPDATE SET "
                "  site_id = EXCLUDED.site_id, "
                "  treatment_arm = EXCLUDED.treatment_arm, "
                "  rave_status = EXCLUDED.rave_status, "
                "  weight_kg = EXCLUDED.weight_kg, "
                "  height_cm = EXCLUDED.height_cm, "
                "  bmi = EXCLUDED.bmi, "
                "  waist_cm = EXCLUDED.waist_cm, "
                "  hip_cm = EXCLUDED.hip_cm, "
                "  waist_hip_ratio = EXCLUDED.waist_hip_ratio, "
                "  updated_at = NOW() "
                "RETURNING subject_id"
            ),
            {
                "rave_id":  subj_id,
                "site":     row["site_id"],
                "subnum":   row["subject_number"],
                "dob":      dob,
                "sex":      sex,
                "race":     row["race"],
                "ethnic":   row["ethnicity"],
                "country":  row["country"],
                "enrl":     _parse_date(row["enrollment_date"]),
                "rand":     _parse_date(row["randomization_date"]),
                "arm":      row["treatment_arm"],
                "status":   row["rave_status"],
                "weight":   _to_num(row.get("weight_kg")),
                "height":   _to_num(row.get("height_cm")),
                "bmi":      _to_num(row.get("bmi")),
                "waist":    waist,
                "hip":      hip,
                "whr":      whr,
            },
        )
        sid = result.scalar()
        id_map[subj_id] = sid

    logger.info("Loaded %d subjects", len(id_map))
    return id_map


def _load_consent(sess, run_id: int, id_map: dict[str, int]):
    rows = sess.execute(
        text("SELECT * FROM staging.consent WHERE run_id = :r"), {"r": run_id}
    ).mappings().all()

    count = 0
    for row in rows:
        subj = row["rave_subject_id"]
        if subj not in id_map:
            continue
        consent_date = _parse_date(row["consent_date"])
        if not consent_date:
            continue

        re_req = _to_bool(row["re_consent_required"])
        withdrawal = _parse_date(row["withdrawal_date"])
        status = "WITHDRAWN" if withdrawal else ("PENDING_RE_CONSENT" if re_req and not _parse_date(row["re_consent_date"]) else "ACTIVE")

        sess.execute(
            text(
                "INSERT INTO clinical.consent_forms "
                "(subject_id, rave_subject_id, icf_version, consent_date, "
                " consent_obtained_by, re_consent_required, re_consent_date, "
                " re_consent_version, withdrawal_date, withdrawal_reason, consent_status) "
                "VALUES (:sid, :rave, :ver, :cdate, :by, :req, :redate, :rever, "
                "        :wdraw, :wreason, :status) "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "sid":     id_map[subj],
                "rave":    subj,
                "ver":     row["icf_version"],
                "cdate":   consent_date,
                "by":      row["consent_obtained_by"],
                "req":     re_req,
                "redate":  _parse_date(row["re_consent_date"]),
                "rever":   row["re_consent_version"],
                "wdraw":   withdrawal,
                "wreason": row["withdrawal_reason"],
                "status":  status,
            },
        )
        count += 1
    logger.info("Loaded %d consent records", count)


def _load_visits(sess, run_id: int, id_map: dict[str, int]) -> dict[tuple, int]:
    """Returns (subject_id, visit_name) → visit_id map."""
    rows = sess.execute(
        text("SELECT * FROM staging.visits WHERE run_id = :r"), {"r": run_id}
    ).mappings().all()

    # Get baseline dates for day calculation
    baselines: dict[str, date] = {}
    for row in rows:
        if "BASELINE" in (row["visit_name"] or "").upper():
            d = _parse_date(row["visit_date"])
            if d:
                baselines[row["rave_subject_id"]] = d

    visit_map: dict[tuple, int] = {}
    for row in rows:
        subj = row["rave_subject_id"]
        if subj not in id_map:
            continue
        visit_date = _parse_date(row["visit_date"])
        if not visit_date:
            continue

        days = None
        if subj in baselines:
            days = (visit_date - baselines[subj]).days

        result = sess.execute(
            text(
                "INSERT INTO clinical.visits "
                "(subject_id, rave_subject_id, visit_name, visit_date, "
                " visit_status, days_from_baseline) "
                "VALUES (:sid, :rave, :vname, :vdate, :vstatus, :days) "
                "ON CONFLICT (subject_id, visit_name, visit_date) DO UPDATE SET "
                "  visit_status = EXCLUDED.visit_status, "
                "  days_from_baseline = EXCLUDED.days_from_baseline "
                "RETURNING visit_id"
            ),
            {
                "sid":     id_map[subj],
                "rave":    subj,
                "vname":   row["visit_name"],
                "vdate":   visit_date,
                "vstatus": (row["visit_status"] or "COMPLETED").upper(),
                "days":    days,
            },
        )
        vid = result.scalar()
        visit_map[(id_map[subj], row["visit_name"])] = vid

    logger.info("Loaded %d visits", len(visit_map))
    return visit_map


def _load_labs(sess, run_id: int, id_map: dict[str, int], visit_map: dict[tuple, int]):
    rows = sess.execute(
        text("SELECT * FROM staging.labs WHERE run_id = :r"), {"r": run_id}
    ).mappings().all()

    count = 0
    for row in rows:
        subj = row["rave_subject_id"]
        if subj not in id_map:
            continue
        sid = id_map[subj]
        lab_date = _parse_date(row["lab_date"])
        if not lab_date:
            continue

        val = _to_num(row["lab_value"])
        uln = _to_num(row["lab_normal_high"])
        x_uln = round(val / uln, 3) if val and uln and uln > 0 else None
        vid = visit_map.get((sid, row["visit_name"]))

        sess.execute(
            text(
                "INSERT INTO clinical.labs "
                "(subject_id, rave_subject_id, visit_id, visit_name, lab_date, "
                " lab_test, lab_value, lab_value_raw, lab_unit, "
                " lab_normal_low, lab_normal_high, lab_flag, x_uln) "
                "VALUES (:sid, :rave, :vid, :vname, :ldate, "
                "        :test, :val, :rawval, :unit, :nlo, :nhi, :flag, :xuln) "
                "ON CONFLICT (subject_id, visit_name, lab_date, lab_test) DO UPDATE SET "
                "  lab_value = EXCLUDED.lab_value, lab_flag = EXCLUDED.lab_flag, "
                "  x_uln = EXCLUDED.x_uln"
            ),
            {
                "sid":    sid,       "rave":   subj,
                "vid":    vid,       "vname":  row["visit_name"],
                "ldate":  lab_date,  "test":   row["lab_test"],
                "val":    val,       "rawval": row["lab_value"],
                "unit":   row["lab_unit"],
                "nlo":    _to_num(row["lab_normal_low"]),
                "nhi":    uln,
                "flag":   row["lab_flag"],
                "xuln":   x_uln,
            },
        )
        count += 1
    logger.info("Loaded %d lab records", count)


def _load_fibroscan(sess, run_id: int, id_map: dict[str, int], visit_map: dict[tuple, int]):
    rows = sess.execute(
        text("SELECT * FROM staging.fibroscan WHERE run_id = :r"), {"r": run_id}
    ).mappings().all()

    count = 0
    for row in rows:
        subj = row["rave_subject_id"]
        if subj not in id_map:
            continue
        sid = id_map[subj]
        scan_date = _parse_date(row["scan_date"])
        if not scan_date:
            continue

        lsm = _to_num(row["lsm_kpa"])
        iqr = _to_num(row["lsm_iqr"])
        sr  = _to_num(row["lsm_success_rate"])
        cap = _to_num(row["cap_score"])
        quality = bool(sr and sr >= 60.0 and lsm and iqr and (iqr / lsm) <= 0.30) if lsm else None

        sess.execute(
            text(
                "INSERT INTO clinical.fibroscan "
                "(subject_id, rave_subject_id, visit_id, visit_name, scan_date, "
                " lsm_kpa, lsm_iqr, lsm_success_rate, cap_score, "
                " fibrosis_stage, steatosis_grade, operator_id, device_serial, quality_adequate) "
                "VALUES (:sid, :rave, :vid, :vname, :sdate, "
                "        :lsm, :iqr, :sr, :cap, :fstage, :sgrade, :op, :dev, :qual) "
                "ON CONFLICT (subject_id, visit_name, scan_date) DO UPDATE SET "
                "  lsm_kpa = EXCLUDED.lsm_kpa, fibrosis_stage = EXCLUDED.fibrosis_stage, "
                "  quality_adequate = EXCLUDED.quality_adequate"
            ),
            {
                "sid":    sid,        "rave":   subj,
                "vid":    visit_map.get((sid, row["visit_name"])),
                "vname":  row["visit_name"],
                "sdate":  scan_date,
                "lsm":    lsm,        "iqr":    iqr,
                "sr":     sr,         "cap":    cap,
                "fstage": _fibrosis_stage(lsm),
                "sgrade": _steatosis_grade(cap),
                "op":     row["operator_id"],
                "dev":    row["device_serial"],
                "qual":   quality,
            },
        )
        count += 1
    logger.info("Loaded %d FibroScan records", count)


def _load_urinalysis(sess, run_id: int, id_map: dict[str, int], visit_map: dict[tuple, int]):
    rows = sess.execute(
        text("SELECT * FROM staging.urinalysis WHERE run_id = :r"), {"r": run_id}
    ).mappings().all()

    count = 0
    for row in rows:
        subj = row["rave_subject_id"]
        if subj not in id_map:
            continue
        sid = id_map[subj]
        urine_date = _parse_date(row["urine_date"])
        if not urine_date:
            continue

        sess.execute(
            text(
                "INSERT INTO clinical.urinalysis "
                "(subject_id, rave_subject_id, visit_id, visit_name, urine_date, "
                " specific_gravity, ph, protein, glucose, ketones, blood, "
                " leukocyte_esterase, nitrites, microscopy_rbc, microscopy_wbc, microscopy_casts) "
                "VALUES (:sid, :rave, :vid, :vname, :udate, "
                "        :sg, :ph, :prot, :gluc, :ket, :blood, "
                "        :le, :nitr, :rbc, :wbc, :casts) "
                "ON CONFLICT (subject_id, visit_name, urine_date) DO NOTHING"
            ),
            {
                "sid":   sid,   "rave":  subj,
                "vid":   visit_map.get((sid, row["visit_name"])),
                "vname": row["visit_name"],
                "udate": urine_date,
                "sg":    _to_num(row["specific_gravity"]),
                "ph":    _to_num(row["ph"]),
                "prot":  row["protein"],
                "gluc":  row["glucose"],
                "ket":   row["ketones"],
                "blood": row["blood"],
                "le":    row["leukocyte_esterase"],
                "nitr":  row["nitrites"],
                "rbc":   _to_num(row["microscopy_rbc"]),
                "wbc":   _to_num(row["microscopy_wbc"]),
                "casts": row["microscopy_casts"],
            },
        )
        count += 1
    logger.info("Loaded %d urinalysis records", count)


def _load_treatment(sess, run_id: int, id_map: dict[str, int]):
    rows = sess.execute(
        text("SELECT * FROM staging.treatment WHERE run_id = :r"), {"r": run_id}
    ).mappings().all()

    count = 0
    for row in rows:
        subj = row["rave_subject_id"]
        if subj not in id_map or not row["drug_name"]:
            continue

        sess.execute(
            text(
                "INSERT INTO clinical.treatment "
                "(subject_id, rave_subject_id, drug_name, dose, dose_unit, "
                " frequency, route, start_date, end_date, ongoing, indication, treatment_type) "
                "VALUES (:sid, :rave, :drug, :dose, :dosu, "
                "        :freq, :route, :sdate, :edate, :ongo, :indic, :ttype)"
            ),
            {
                "sid":   id_map[subj],
                "rave":  subj,
                "drug":  row["drug_name"],
                "dose":  _to_num(row["dose"]),
                "dosu":  row["dose_unit"],
                "freq":  row["frequency"],
                "route": row["route"],
                "sdate": _parse_date(row["start_date"]),
                "edate": _parse_date(row["end_date"]),
                "ongo":  _to_bool(row["ongoing"]),
                "indic": row["indication"],
                "ttype": (row["treatment_type"] or "CONMED").upper(),
            },
        )
        count += 1
    logger.info("Loaded %d treatment records", count)


class Loader:
    def run(self, run_id: int) -> dict[str, int]:
        """Load all validated staging data into clinical schema."""
        logger.info("Starting clinical load for run_id=%s", run_id)
        counts: dict[str, int] = {}

        with get_session() as sess:
            blocked = _blocked_subjects(sess, run_id)
            id_map = _load_subjects(sess, run_id, blocked)
            counts["subjects"] = len(id_map)

            visit_map = _load_visits(sess, run_id, id_map)
            counts["visits"] = len(visit_map)

            _load_consent(sess, run_id, id_map)
            _load_labs(sess, run_id, id_map, visit_map)
            _load_fibroscan(sess, run_id, id_map, visit_map)
            _load_urinalysis(sess, run_id, id_map, visit_map)
            _load_treatment(sess, run_id, id_map)

        logger.info("Clinical load complete: %s", counts)
        return counts
