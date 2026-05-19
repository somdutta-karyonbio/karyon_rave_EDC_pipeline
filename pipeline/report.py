"""
Reporting module — queries clinical/audit views and exports to CSV.

Reports generated:
  1. Subject Listing
  2. Lab Out-of-Range Listing
  3. FibroScan Staging Summary
  4. Visit Compliance Listing
  5. Open Data Queries
  6. Protocol Deviations
  7. Consent Status
  8. Site Dashboard
  9. Sanitization Summary
"""

import csv
import logging
import os
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

from pipeline.db import get_session

logger = logging.getLogger(__name__)

TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")


def _export_query(
    sess,
    sql: str,
    params: dict,
    output_path: Path,
    report_name: str,
) -> int:
    rows = sess.execute(text(sql), params).mappings().all()
    if not rows:
        logger.info("Report '%s': no data, skipping export.", report_name)
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    logger.info("Report '%s': %d rows → %s", report_name, len(rows), output_path)
    return len(rows)


REPORTS: list[dict] = [
    {
        "name": "subject_listing",
        "sql": "SELECT * FROM clinical.v_subject_listing ORDER BY site_id, rave_subject_id",
        "params": {},
    },
    {
        "name": "labs_out_of_range",
        "sql": "SELECT * FROM clinical.v_labs_oor",
        "params": {},
    },
    {
        "name": "fibroscan_summary",
        "sql": "SELECT * FROM clinical.v_fibroscan_summary",
        "params": {},
    },
    {
        "name": "visit_compliance",
        "sql": "SELECT * FROM clinical.v_visit_compliance ORDER BY site_id, rave_subject_id, visit_date",
        "params": {},
    },
    {
        "name": "open_data_queries",
        "sql": "SELECT * FROM audit.v_open_queries",
        "params": {},
    },
    {
        "name": "protocol_deviations",
        "sql": "SELECT * FROM audit.v_protocol_deviations",
        "params": {},
    },
    {
        "name": "consent_status",
        "sql": "SELECT * FROM audit.v_consent_status ORDER BY site_id, rave_subject_id",
        "params": {},
    },
    {
        "name": "site_dashboard",
        "sql": "SELECT * FROM audit.v_site_dashboard",
        "params": {},
    },
    {
        "name": "sanitization_summary",
        "sql": "SELECT * FROM audit.v_sanitization_summary",
        "params": {},
    },
]


def _run_report_query(
    sess, sql: str, params: dict
) -> list[dict]:
    rows = sess.execute(text(sql), params).mappings().all()
    return [dict(r) for r in rows]


class Reporter:
    def __init__(self, output_dir: str = "reports/output"):
        self.output_dir = Path(output_dir)

    def run(self, run_id: int | None = None) -> dict[str, int]:
        """Generate all reports. Returns {report_name: row_count}."""
        logger.info("Generating reports (run_id=%s)", run_id)
        counts: dict[str, int] = {}
        run_dir = self.output_dir / TIMESTAMP
        run_dir.mkdir(parents=True, exist_ok=True)

        with get_session() as sess:
            for report in REPORTS:
                params = {**report["params"]}
                if run_id and ":run_id" in report["sql"]:
                    params["run_id"] = run_id

                out_path = run_dir / f"{report['name']}.csv"
                counts[report["name"]] = _export_query(
                    sess, report["sql"], params, out_path, report["name"]
                )

        # Write a run manifest
        manifest_path = run_dir / "manifest.txt"
        with open(manifest_path, "w") as f:
            f.write(f"Pipeline Run ID : {run_id}\n")
            f.write(f"Generated At    : {datetime.now().isoformat()}\n")
            f.write(f"Output Dir      : {run_dir}\n\n")
            f.write("Report                   Rows\n")
            f.write("-" * 40 + "\n")
            for name, count in counts.items():
                f.write(f"{name:<30} {count}\n")

        logger.info("Reports written to: %s", run_dir)
        return counts

    def query_site(self, site_id: str) -> dict[str, list[dict]]:
        """Return all data for a specific site (for site-level review)."""
        with get_session() as sess:
            return {
                "subjects": _run_report_query(
                    sess,
                    "SELECT * FROM clinical.v_subject_listing WHERE site_id = :s",
                    {"s": site_id},
                ),
                "open_queries": _run_report_query(
                    sess,
                    "SELECT * FROM audit.v_open_queries WHERE site_id = :s",
                    {"s": site_id},
                ),
                "deviations": _run_report_query(
                    sess,
                    "SELECT * FROM audit.v_protocol_deviations WHERE site_id = :s",
                    {"s": site_id},
                ),
                "consent": _run_report_query(
                    sess,
                    "SELECT * FROM audit.v_consent_status WHERE site_id = :s",
                    {"s": site_id},
                ),
            }
