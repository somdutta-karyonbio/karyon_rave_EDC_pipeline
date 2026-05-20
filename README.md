# Karyon Bio — RAVE Clinical Data Pipeline

A PostgreSQL-based ETL pipeline for the Karyon Bio liver fibrosis clinical trial.
Integrates with **Medidata RAVE** via REST API to extract, validate, and report on
clinical trial data across all study domains.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Project Structure](#project-structure)
3. [Database Design](#database-design)
4. [Pipeline Stages](#pipeline-stages)
5. [Python Modules](#python-modules)
6. [Configuration](#configuration)
7. [Protocol Rules](#protocol-rules)
8. [Setup & Installation](#setup--installation)
9. [Running the Pipeline](#running-the-pipeline)
10. [Testing](#testing)
11. [Reports](#reports)
12. [Adding New Checks](#adding-new-checks)

---

## Architecture Overview

```
Medidata RAVE (REST API)
        │
        ▼
  rave_client.py          ODM-XML → Python dicts
        │
        ▼
   extract.py             OID mapping → staging tables
        │
        ├──► sanitize.py  → audit.sanitization_log
        ├──► consent.py   → audit.consent_compliance
        ├──► compliance.py→ audit.protocol_deviations
        │                   audit.data_queries
        ▼
    load.py               staging → clinical schema
        │
        ▼
   report.py              SQL views → CSV exports
```

All data flows through three PostgreSQL schemas:

| Schema | Purpose |
|---|---|
| `staging` | Raw data from RAVE — untouched, append-only |
| `clinical` | Validated, typed, query-resolved data |
| `audit` | Queries, deviations, consent log, full audit trail |

---

## Project Structure

```
pipeline_build/
├── config/
│   ├── config.yaml              # DB + RAVE connection settings
│   └── protocol_rules.yaml      # Visit windows, lab ranges, eligibility, ICF version
├── sql/
│   ├── 01_schemas.sql           # Create schemas + grant permissions
│   ├── 02_staging.sql           # Raw staging tables (all TEXT, append-only)
│   ├── 03_clinical.sql          # Typed clinical tables with constraints
│   ├── 04_audit.sql             # Audit trail, queries, deviations, consent log
│   └── 05_views.sql             # 8 reporting views
├── pipeline/
│   ├── __init__.py
│   ├── db.py                    # PostgreSQL connection manager
│   ├── rave_client.py           # RAVE Web Services REST client
│   ├── extract.py               # Extraction + OID mapping orchestrator
│   ├── sanitize.py              # Data quality / sanitization checks
│   ├── compliance.py            # Protocol compliance checks
│   ├── consent.py               # Informed consent (ICF) management
│   ├── load.py                  # Staging → clinical UPSERT loader
│   └── report.py                # CSV report exports
├── tests/
│   ├── generate_mock_data.py    # Synthetic patient data generator
│   └── test_pipeline.py        # End-to-end test suite (15 assertions)
├── reports/
│   └── output/                  # Generated CSV reports (gitignored)
├── main.py                      # CLI pipeline orchestrator
├── requirements.txt
└── README.md
```

---

## Database Design

### Schemas

#### `staging` — Raw RAVE data
One table per clinical domain. All columns are `TEXT` — no type coercion
at this layer. Every row carries a `run_id` linking it to the pipeline run
that extracted it.

| Table | Contents |
|---|---|
| `pipeline_run` | Run manifest — start time, status, who triggered it |
| `demographics` | Subject demographics from RAVE |
| `consent` | ICF records |
| `visits` | Visit schedule and completion status |
| `labs` | Laboratory results (CBC, chemistry, LFTs) |
| `fibroscan` | Liver stiffness (LSM kPa) and CAP scores |
| `urinalysis` | Urine analysis results |
| `treatment` | Study drug and concomitant medications |
| `adverse_events` | AE/SAE records |

#### `clinical` — Validated data
Typed columns (`DATE`, `NUMERIC`, `BOOLEAN`). Populated by the load stage.
Subjects with unresolved ERROR-level sanitization issues are excluded.

| Table | Key derived fields |
|---|---|
| `subjects` | Demographics |
| `consent_forms` | Consent status (ACTIVE / WITHDRAWN / PENDING_RE_CONSENT) |
| `visits` | `days_from_baseline`, `window_compliant` |
| `labs` | `x_uln` (value/ULN ratio), `graded_toxicity` (NCI CTCAE grade) |
| `fibroscan` | `fibrosis_stage` (F0–F4), `steatosis_grade` (S0–S3), `quality_adequate` |
| `urinalysis` | Semi-quantitative dipstick values |
| `treatment` | Study drug, conmeds, prior medications |
| `adverse_events` | AE/SAE with severity, causality, outcome |

#### `audit` — Audit and compliance

| Table | Contents |
|---|---|
| `sanitization_log` | Data quality issues found per run |
| `consent_compliance` | Pass/fail per consent check per subject |
| `protocol_deviations` | Protocol violations with severity and impact |
| `data_queries` | Auto-generated DCF-style queries for sites |
| `audit_trail` | Every INSERT/UPDATE/DELETE on clinical tables (trigger-based) |

### Reporting Views

| View | Description |
|---|---|
| `clinical.v_subject_listing` | All subjects with demographics + consent status |
| `clinical.v_labs_oor` | Lab values flagged as out of range |
| `clinical.v_fibroscan_summary` | FibroScan results with derived fibrosis stage |
| `clinical.v_visit_compliance` | Visits with window compliance flag |
| `audit.v_open_queries` | Outstanding site queries with days open |
| `audit.v_protocol_deviations` | All protocol deviations |
| `audit.v_consent_status` | Consent flag per subject (OK / MISSING / WITHDRAWN / RE_CONSENT_PENDING) |
| `audit.v_site_dashboard` | Site-level summary: enrolled, queries, deviations, missing consent |
| `audit.v_sanitization_summary` | Issue counts grouped by domain, type, severity |

---

## Pipeline Stages

| # | Stage | Module | Output |
|---|---|---|---|
| 1 | **Extract** | `rave_client` + `extract` | `staging.*` tables |
| 2 | **Sanitize** | `sanitize` | `audit.sanitization_log` |
| 3 | **Consent** | `consent` | `audit.consent_compliance` |
| 4 | **Compliance** | `compliance` | `audit.protocol_deviations`, `audit.data_queries` |
| 5 | **Load** | `load` | `clinical.*` tables |
| 6 | **Report** | `report` | CSV files in `reports/output/<timestamp>/` |

---

## Python Modules

### `db.py`
Database connection manager. Reads `config/config.yaml`, creates the
SQLAlchemy engine and session factory. Provides `get_session()` context
manager with automatic commit/rollback. Also manages `pipeline_run` records
and runs SQL setup files.

### `rave_client.py`
Medidata RAVE Web Services REST client. Authenticates with HTTP Basic Auth,
downloads clinical datasets as ODM-XML, and parses them into flat Python
dictionaries. Handles retries, backoff, and per-domain fetching.

### `extract.py`
Maps RAVE ItemOIDs (e.g. `BRTHDAT`, `LBVAL`) to staging column names and
bulk-inserts rows into staging tables. **Update the OID mappings here when
the EDC build changes.**

### `sanitize.py`
Data quality checks across all staging domains. Detects missing fields,
invalid/future dates, non-numeric values, duplicates, and out-of-range lab
values. Writes findings to `audit.sanitization_log`. ERROR-level issues
block a subject from the clinical load.

### `compliance.py`
Protocol compliance engine. Checks visit windows against protocol-defined
targets, eligibility criteria (age, BMI, FibroScan), mandatory assessments
per visit, and critical lab thresholds (NCI CTCAE Grade ≥3). All rules are
driven by `config/protocol_rules.yaml`.

### `consent.py`
GCP-focused informed consent checks. Verifies consent precedes procedures,
ICF version matches current approved version, re-consent is completed where
flagged, and withdrawals are fully documented.

### `load.py`
UPSERT loader from staging to clinical schema. Skips blocked subjects,
converts text to typed values, calculates derived fields (fibrosis stage,
steatosis grade, x_ULN ratio, FibroScan quality flag), and links domain
records to their parent visit.

### `report.py`
Queries the 8 reporting views and writes timestamped CSV exports to
`reports/output/<timestamp>/`. Also generates a `manifest.txt` per run.
Supports site-specific reporting via `query_site(site_id)`.

---

## Configuration

### `config/config.yaml`
All sensitive values are read from **environment variables** — never
hardcoded.

```yaml
database:
  host: localhost
  port: 5432
  name: karyon_rave
  user: pipeline_user
  password: "${DB_PASSWORD}"      # set $env:DB_PASSWORD

rave:
  base_url: "https://${RAVE_INSTANCE}.mdsol.com/RaveWebServices"
  username: "${RAVE_USERNAME}"
  password: "${RAVE_PASSWORD}"
  study_oid: "${RAVE_STUDY_OID}"
  environment: "${RAVE_ENV}"      # DEV | UAT | PROD
```

Set before running:
```powershell
$env:DB_PASSWORD   = "your_db_password"
$env:RAVE_INSTANCE = "your-rave-instance"
$env:RAVE_USERNAME = "api_user"
$env:RAVE_PASSWORD = "api_password"
$env:RAVE_STUDY_OID = "Karyon_LF_001(Prod)"
$env:RAVE_ENV      = "PROD"
```

---

## Protocol Rules

### `config/protocol_rules.yaml`
All protocol-specific parameters in one place. Update this file when the
protocol is amended — no code changes needed.

Key sections:

```yaml
eligibility:
  inclusion:
    age_min: 18
    age_max: 75
    fibroscan_min_kpa: 7.1     # ≥ F2 stage required
    bmi_min: 18.0
    bmi_max: 40.0

visits:
  - name: "Week12"
    target_day: 84
    window_early: -7
    window_late: 7

lab_ranges:
  ALT:
    low: 7
    high: 56
    uln: 56
    unit: "U/L"

consent:
  current_icf_version: "v3.0"
```

---

## Setup & Installation

### Prerequisites
- Python 3.12+
- PostgreSQL 16+ (running on localhost:5432)
- Database `karyon_rave` created
- User `pipeline_user` created with password

### Install dependencies
```powershell
pip install -r requirements.txt
```

### Create database (one-time)
```powershell
psql -U postgres -c "CREATE DATABASE karyon_rave;"
psql -U postgres -c "CREATE USER pipeline_user WITH PASSWORD 'your_password';"
psql -U postgres -c "GRANT ALL PRIVILEGES ON DATABASE karyon_rave TO pipeline_user;"
```

### Apply schema (one-time)
```powershell
$env:DB_PASSWORD   = "your_db_password"
$env:PGPASSWORD    = "your_postgres_password"
$env:SETUP_DB_USER = "postgres"
python main.py --setup-db
```

---

## Running the Pipeline

### Full pipeline run
```powershell
$env:DB_PASSWORD   = "your_db_password"
$env:RAVE_INSTANCE = "your-rave-instance"
$env:RAVE_USERNAME = "api_user"
$env:RAVE_PASSWORD = "api_password"
$env:RAVE_STUDY_OID = "Karyon_LF_001(Prod)"
$env:RAVE_ENV      = "PROD"
python main.py
```

### Regenerate reports only (no RAVE extraction)
```powershell
python main.py --report-only
```

### Export data for a specific site
```powershell
python main.py --site S01
```

---

## Testing

Run the end-to-end test suite against 8 synthetic patients:
```powershell
$env:DB_PASSWORD = "your_db_password"
python tests/test_pipeline.py
```

### What the test covers

| Subject | Injected issue | Checked |
|---|---|---|
| KRY-001/002 | Clean — no issues | All checks pass |
| KRY-003 | Missing consent date | `consent_compliance` failure |
| KRY-004 | Outdated ICF version | `consent_compliance` failure |
| KRY-005 | Week 12 visit 15 days late | `protocol_deviations` VISIT_WINDOW |
| KRY-006 | ALT = 320 U/L (≥5× ULN) | `sanitization_log` + `protocol_deviations` |
| KRY-007 | No FibroScan at Baseline | `protocol_deviations` MISSING_ASSESSMENT |
| KRY-008 | Invalid date of birth | `sanitization_log` ERROR → blocked from clinical load |

Expected result: **15/15 assertions passing**.

---

## Reports

Reports are written to `reports/output/<timestamp>/` after each run:

| File | Description |
|---|---|
| `subject_listing.csv` | All subjects with demographics and consent status |
| `labs_out_of_range.csv` | Flagged lab values with x_ULN ratio |
| `fibroscan_summary.csv` | LSM, CAP, fibrosis stage per visit |
| `visit_compliance.csv` | Visit dates with window compliance flag |
| `open_data_queries.csv` | Outstanding queries with days open |
| `protocol_deviations.csv` | All PDs with severity and impact flags |
| `consent_status.csv` | ICF status per subject |
| `site_dashboard.csv` | Site-level summary metrics |
| `sanitization_summary.csv` | Issue counts by domain and severity |

---

## Adding New Checks

### New sanitization check
Add a function `_check_<domain>(sess, run_id, rules)` in `sanitize.py`
and register it in `Sanitizer.run()`.

### New compliance check
Add a function `_check_<rule>(sess, run_id, rules)` in `compliance.py`
and register it in `ComplianceChecker.run()`.

### New protocol rule
Add the parameter to `config/protocol_rules.yaml` and reference it via
`rules["section"]["key"]` in the relevant check function.

### New RAVE domain
1. Add a dataset name to `config/config.yaml` under `rave.datasets`
2. Add a staging table to `sql/02_staging.sql`
3. Add a clinical table to `sql/03_clinical.sql`
4. Add an OID mapping dict and entry in `extract.py`
5. Add a load function in `load.py`
6. Add sanitization checks in `sanitize.py`
