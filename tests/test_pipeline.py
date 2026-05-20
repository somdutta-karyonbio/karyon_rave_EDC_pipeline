"""
End-to-end pipeline test using mock data.

Runs all pipeline stages against synthetic patient records and
asserts that known issues are correctly detected and reported.

Usage:
    python tests/test_pipeline.py
"""

import sys
import os

# Always resolve paths relative to the project root (one level up from tests/)
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)   # ensures config/config.yaml, reports/ etc. resolve correctly

from pipeline.db import init_engine, get_session, finish_pipeline_run
from pipeline.sanitize import Sanitizer
from pipeline.compliance import ComplianceChecker
from pipeline.consent import ConsentManager
from pipeline.load import Loader
from pipeline.report import Reporter
from sqlalchemy import text
from tests.generate_mock_data import generate

PASS = "\033[92m✓ PASS\033[0m"
FAIL = "\033[91m✗ FAIL\033[0m"

results = []

def check(name: str, condition: bool, detail: str = ""):
    status = PASS if condition else FAIL
    results.append((name, condition))
    print(f"  {status}  {name}" + (f" — {detail}" if detail else ""))
    return condition


def query_count(sess, sql: str, params: dict = {}) -> int:
    return sess.execute(text(sql), params).scalar() or 0


# ── Run pipeline stages ───────────────────────────────────────

def run_test():
    print("\n" + "="*60)
    print("  Karyon Bio — Pipeline End-to-End Test")
    print("="*60)

    init_engine()
    run_id = generate()

    print(f"\n[Stage 2] Sanitization checks  (run_id={run_id})")
    san_counts = Sanitizer().run(run_id)
    print(f"          Issues found: {san_counts}")

    print(f"\n[Stage 3] Consent checks  (run_id={run_id})")
    consent_counts = ConsentManager().run(run_id)
    print(f"          Failures: {consent_counts}")

    print(f"\n[Stage 4] Compliance checks  (run_id={run_id})")
    compliance_counts = ComplianceChecker().run(run_id)
    print(f"          Deviations: {compliance_counts}")

    print(f"\n[Stage 5] Load to clinical schema  (run_id={run_id})")
    load_counts = Loader().run(run_id)
    print(f"          Loaded: {load_counts}")

    print(f"\n[Stage 6] Export reports  (run_id={run_id})")
    report_counts = Reporter().run(run_id)
    print(f"          Report rows: {report_counts}")

    finish_pipeline_run(run_id, status="SUCCESS", notes="mock test run")

    # ── Assertions ────────────────────────────────────────────
    print("\n" + "="*60)
    print("  Assertions")
    print("="*60)

    with get_session() as sess:

        # ── Sanitization checks ───────────────────────────────
        print("\n  Sanitization:")

        invalid_dob = query_count(sess, """
            SELECT COUNT(*) FROM audit.sanitization_log
            WHERE run_id=:r AND rave_subject_id='KRY-008'
            AND issue_type='INVALID_DATE' AND field_name='date_of_birth'
        """, {"r": run_id})
        check("KRY-008 invalid DOB flagged", invalid_dob >= 1,
              f"{invalid_dob} issue(s) logged")

        high_alt = query_count(sess, """
            SELECT COUNT(*) FROM audit.sanitization_log
            WHERE run_id=:r AND rave_subject_id='KRY-006'
            AND domain='LABS' AND issue_type='OUT_OF_RANGE'
        """, {"r": run_id})
        check("KRY-006 high ALT flagged in sanitization", high_alt >= 1,
              f"{high_alt} issue(s) logged")

        # ── Consent checks ───────────────────────────────────
        print("\n  Consent:")

        missing_consent_date = query_count(sess, """
            SELECT COUNT(*) FROM audit.consent_compliance
            WHERE run_id=:r AND rave_subject_id='KRY-003'
            AND check_name='CONSENT_ON_RECORD' AND passed=FALSE
        """, {"r": run_id})
        check("KRY-003 missing consent date detected", missing_consent_date >= 1,
              f"{missing_consent_date} failure(s)")

        old_icf = query_count(sess, """
            SELECT COUNT(*) FROM audit.consent_compliance
            WHERE run_id=:r AND rave_subject_id='KRY-004'
            AND check_name='ICF_VERSION_CURRENT' AND passed=FALSE
        """, {"r": run_id})
        check("KRY-004 outdated ICF version detected", old_icf >= 1,
              f"{old_icf} failure(s)")

        good_consent = query_count(sess, """
            SELECT COUNT(*) FROM audit.consent_compliance
            WHERE run_id=:r AND rave_subject_id='KRY-001'
            AND check_name='CONSENT_ON_RECORD' AND passed=TRUE
        """, {"r": run_id})
        check("KRY-001 consent passed", good_consent >= 1,
              f"{good_consent} pass(es)")

        # ── Protocol compliance checks ────────────────────────
        print("\n  Protocol compliance:")

        visit_window = query_count(sess, """
            SELECT COUNT(*) FROM audit.protocol_deviations
            WHERE run_id=:r AND rave_subject_id='KRY-005'
            AND deviation_type='VISIT_WINDOW'
        """, {"r": run_id})
        check("KRY-005 out-of-window visit detected", visit_window >= 1,
              f"{visit_window} deviation(s)")

        critical_alt = query_count(sess, """
            SELECT COUNT(*) FROM audit.protocol_deviations
            WHERE run_id=:r AND rave_subject_id='KRY-006'
            AND deviation_type='CRITICAL_LAB_VALUE'
        """, {"r": run_id})
        check("KRY-006 critical ALT deviation logged", critical_alt >= 1,
              f"{critical_alt} deviation(s)")

        missing_fs = query_count(sess, """
            SELECT COUNT(*) FROM audit.protocol_deviations
            WHERE run_id=:r AND rave_subject_id='KRY-007'
            AND deviation_type='MISSING_ASSESSMENT'
        """, {"r": run_id})
        check("KRY-007 missing FibroScan deviation logged", missing_fs >= 1,
              f"{missing_fs} deviation(s)")

        # ── Load checks ──────────────────────────────────────
        print("\n  Clinical load:")

        subjects_loaded = query_count(sess, """
            SELECT COUNT(*) FROM clinical.subjects
        """)
        # KRY-008 has invalid DOB (ERROR) so may be blocked
        check("≥7 subjects loaded to clinical schema", subjects_loaded >= 7,
              f"{subjects_loaded} subjects")

        fibroscan_f3 = query_count(sess, """
            SELECT COUNT(*) FROM clinical.fibroscan
            WHERE fibrosis_stage IN ('F3','F4')
        """)
        check("Fibrosis stage derived from LSM", fibroscan_f3 >= 1,
              f"{fibroscan_f3} F3/F4 record(s)")

        x_uln_set = query_count(sess, """
            SELECT COUNT(*) FROM clinical.labs
            WHERE x_uln IS NOT NULL AND lab_test='ALT'
        """)
        check("x_ULN ratio calculated for ALT", x_uln_set >= 1,
              f"{x_uln_set} record(s)")

        # ── Data queries auto-generated ──────────────────────
        print("\n  Auto-generated data queries:")

        open_queries = query_count(sess, """
            SELECT COUNT(*) FROM audit.data_queries
            WHERE run_id=:r AND status='OPEN'
        """, {"r": run_id})
        check("Open queries generated for sites", open_queries >= 1,
              f"{open_queries} open query/queries")

        # ── Reports ───────────────────────────────────────────
        print("\n  Reports:")

        check("Subject listing report generated",
              report_counts.get("subject_listing", 0) >= 1,
              f"{report_counts.get('subject_listing', 0)} rows")

        check("Protocol deviation report generated",
              report_counts.get("protocol_deviations", 0) >= 1,
              f"{report_counts.get('protocol_deviations', 0)} rows")

        check("Consent status report generated",
              report_counts.get("consent_status", 0) >= 1,
              f"{report_counts.get('consent_status', 0)} rows")

    # ── Summary ───────────────────────────────────────────────
    print("\n" + "="*60)
    passed = sum(1 for _, r in results if r)
    total  = len(results)
    colour = "\033[92m" if passed == total else "\033[93m"
    print(f"  {colour}Result: {passed}/{total} checks passed\033[0m")
    print("="*60 + "\n")

    if passed < total:
        failed = [name for name, r in results if not r]
        print("  Failed checks:")
        for f in failed:
            print(f"    - {f}")
        sys.exit(1)


if __name__ == "__main__":
    run_test()
