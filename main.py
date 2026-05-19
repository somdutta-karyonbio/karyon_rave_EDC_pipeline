"""
Karyon Bio — RAVE Clinical Data Pipeline
Entry point and orchestrator.

Usage:
    python main.py [--config config/config.yaml] [--setup-db] [--report-only] [--site SITE_ID]

Stages:
    1. Extract   — pull from RAVE REST API into staging schema
    2. Sanitize  — data quality checks → audit.sanitization_log
    3. Consent   — ICF compliance checks → audit.consent_compliance
    4. Compliance — protocol compliance checks → audit.protocol_deviations
    5. Load      — promote validated data to clinical schema
    6. Report    — export CSV reports
"""

import argparse
import logging
import os
import sys
import traceback
from pathlib import Path

import yaml

from pipeline.db import (
    create_pipeline_run,
    execute_sql_file,
    finish_pipeline_run,
    init_engine,
)
from pipeline.extract import Extractor
from pipeline.sanitize import Sanitizer
from pipeline.compliance import ComplianceChecker
from pipeline.consent import ConsentManager
from pipeline.load import Loader
from pipeline.report import Reporter


def _setup_logging(log_level: str = "INFO", log_file: str | None = None):
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        handlers=handlers,
    )


def _load_config(path: str) -> dict:
    import re
    with open(path) as f:
        raw = f.read()
    def _sub(m):
        key = m.group(1) or m.group(2)
        return os.environ.get(key, m.group(0))
    raw = re.sub(r'\$\{(\w+)\}|\$(\w+)', _sub, raw)
    return yaml.safe_load(raw)


def setup_database(sql_dir: str = "sql"):
    """
    Run all SQL setup scripts in order.
    Files 01-04 require superuser (CREATE SCHEMA / ROLE / TRIGGER).
    File 05 (views) runs as pipeline_user via SQLAlchemy.
    """
    logger = logging.getLogger("setup")
    sql_files = sorted(Path(sql_dir).glob("*.sql"))
    for sql_file in sql_files:
        logger.info("Applying: %s", sql_file)
        # All DDL (schemas, tables, triggers, views) runs as superuser via psql
        execute_sql_file(str(sql_file), superuser=True)
    logger.info("Database setup complete.")


def run_pipeline(config_path: str = "config/config.yaml") -> int:
    """
    Execute the full pipeline. Returns the run_id on success,
    or raises on unrecoverable failure.
    """
    config = _load_config(config_path)
    pipeline_cfg = config.get("pipeline", {})
    rave_env = config.get("rave", {}).get("environment", "")

    logger = logging.getLogger("pipeline")
    logger.info("=" * 60)
    logger.info("Karyon Bio RAVE Pipeline — starting")
    logger.info("=" * 60)

    run_id = create_pipeline_run(triggered_by="cli", rave_env=rave_env)

    try:
        # ── Stage 1: Extract ─────────────────────────────────
        logger.info("Stage 1/6 — Extract from RAVE")
        extract_counts = Extractor(config_path).run(run_id)
        logger.info("Extract counts: %s", extract_counts)

        # ── Stage 2: Sanitize ────────────────────────────────
        if pipeline_cfg.get("run_sanitization", True):
            logger.info("Stage 2/6 — Sanitization checks")
            san_counts = Sanitizer().run(run_id)
            logger.info("Sanitization issues: %s", san_counts)
        else:
            logger.info("Stage 2/6 — Sanitization SKIPPED (config)")

        # ── Stage 3: Consent ─────────────────────────────────
        if pipeline_cfg.get("run_consent_check", True):
            logger.info("Stage 3/6 — Consent compliance")
            consent_counts = ConsentManager().run(run_id)
            logger.info("Consent failures: %s", consent_counts)
        else:
            logger.info("Stage 3/6 — Consent check SKIPPED (config)")

        # ── Stage 4: Protocol Compliance ─────────────────────
        if pipeline_cfg.get("run_compliance", True):
            logger.info("Stage 4/6 — Protocol compliance")
            compliance_counts = ComplianceChecker().run(run_id)
            logger.info("Compliance deviations: %s", compliance_counts)
        else:
            logger.info("Stage 4/6 — Compliance SKIPPED (config)")

        # ── Stage 5: Load ────────────────────────────────────
        logger.info("Stage 5/6 — Load to clinical schema")
        load_counts = Loader().run(run_id)
        logger.info("Load counts: %s", load_counts)

        # ── Stage 6: Report ───────────────────────────────────
        if pipeline_cfg.get("export_reports", True):
            logger.info("Stage 6/6 — Export reports")
            output_dir = pipeline_cfg.get("report_output_dir", "reports/output")
            report_counts = Reporter(output_dir).run(run_id)
            logger.info("Report rows: %s", report_counts)
        else:
            logger.info("Stage 6/6 — Reporting SKIPPED (config)")

        finish_pipeline_run(run_id, status="SUCCESS")
        logger.info("Pipeline completed successfully. run_id=%s", run_id)
        return run_id

    except Exception as exc:
        logger.error("Pipeline FAILED: %s", exc)
        logger.debug(traceback.format_exc())
        finish_pipeline_run(run_id, status="FAILED", notes=str(exc))
        raise


def main():
    parser = argparse.ArgumentParser(description="Karyon Bio RAVE Data Pipeline")
    parser.add_argument("--config",      default="config/config.yaml",
                        help="Path to config.yaml")
    parser.add_argument("--setup-db",    action="store_true",
                        help="Run SQL schema setup scripts and exit")
    parser.add_argument("--report-only", action="store_true",
                        help="Skip extract/validate/load — regenerate reports only")
    parser.add_argument("--site",        default=None,
                        help="Export site-specific report for SITE_ID")
    args = parser.parse_args()

    config = _load_config(args.config)
    pipeline_cfg = config.get("pipeline", {})
    _setup_logging(
        log_level=pipeline_cfg.get("log_level", "INFO"),
        log_file=pipeline_cfg.get("log_file"),
    )

    init_engine(args.config)

    if args.setup_db:
        setup_database()
        return

    if args.site:
        output_dir = pipeline_cfg.get("report_output_dir", "reports/output")
        reporter = Reporter(output_dir)
        data = reporter.query_site(args.site)
        for section, rows in data.items():
            print(f"\n=== {section.upper()} ({len(rows)} records) ===")
            for r in rows[:5]:
                print(r)
        return

    if args.report_only:
        output_dir = pipeline_cfg.get("report_output_dir", "reports/output")
        Reporter(output_dir).run(run_id=None)
        return

    run_pipeline(args.config)


if __name__ == "__main__":
    main()
