"""PostgreSQL connection and session management."""

import logging
import os
from contextlib import contextmanager

import yaml
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger(__name__)

_engine = None
_Session = None


def _load_config(config_path: str = "config/config.yaml") -> dict:
    with open(config_path, encoding="utf-8") as f:
        raw = f.read()
    # Expand $VAR and ${VAR} patterns from environment
    import re
    def _sub(m):
        key = m.group(1) or m.group(2)
        return os.environ.get(key, m.group(0))
    raw = re.sub(r'\$\{(\w+)\}|\$(\w+)', _sub, raw)
    return yaml.safe_load(raw)


def init_engine(config_path: str = "config/config.yaml"):
    global _engine, _Session
    cfg = _load_config(config_path)["database"]
    url = (
        f"postgresql+psycopg://{cfg['user']}:{cfg['password']}"
        f"@{cfg['host']}:{cfg['port']}/{cfg['name']}"
    )
    _engine = create_engine(
        url,
        pool_size=cfg.get("pool_size", 5),
        max_overflow=cfg.get("max_overflow", 10),
        connect_args={"connect_timeout": cfg.get("connect_timeout", 10)},
        echo=False,
    )
    _Session = sessionmaker(bind=_engine)
    logger.info("Database engine initialised: %s:%s/%s", cfg["host"], cfg["port"], cfg["name"])
    return _engine


def get_engine():
    if _engine is None:
        raise RuntimeError("Call init_engine() before get_engine()")
    return _engine


@contextmanager
def get_session():
    if _Session is None:
        raise RuntimeError("Call init_engine() before get_session()")
    session = _Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _split_sql(sql: str) -> list[str]:
    """
    Split a SQL file into individual statements.
    Correctly handles:
      - Dollar-quoted blocks:  DO $$ ... $$  /  $body$ ... $body$
      - Single-quoted strings: 'it''s fine'
      - Single-line comments:  -- comment
      - Block comments:        /* comment */
    Only splits on ';' that appear outside of all of the above.
    """
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(sql)

    while i < n:
        # ── single-line comment ──────────────────────────────
        if sql[i] == '-' and i + 1 < n and sql[i + 1] == '-':
            end = sql.find('\n', i)
            end = end if end != -1 else n
            buf.append(sql[i:end])
            i = end
            continue

        # ── block comment ────────────────────────────────────
        if sql[i] == '/' and i + 1 < n and sql[i + 1] == '*':
            end = sql.find('*/', i + 2)
            end = (end + 2) if end != -1 else n
            buf.append(sql[i:end])
            i = end
            continue

        # ── dollar-quoted string  $tag$...$tag$ ──────────────
        if sql[i] == '$':
            j = sql.find('$', i + 1)
            if j != -1:
                tag = sql[i: j + 1]          # e.g. "$$" or "$body$"
                close = sql.find(tag, j + 1)
                if close != -1:
                    end = close + len(tag)
                    buf.append(sql[i:end])
                    i = end
                    continue

        # ── single-quoted string ─────────────────────────────
        if sql[i] == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'" :
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2          # escaped quote ''
                        continue
                    break
                j += 1
            buf.append(sql[i: j + 1])
            i = j + 1
            continue

        # ── statement terminator ─────────────────────────────
        if sql[i] == ';':
            stmt = ''.join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue

        buf.append(sql[i])
        i += 1

    # trailing statement without trailing semicolon
    stmt = ''.join(buf).strip()
    if stmt:
        statements.append(stmt)

    return statements


def execute_sql_file(path: str, superuser: bool = False):
    """
    Run a SQL file against the database.

    superuser=True  → uses psql CLI with the postgres superuser
                      (needed for CREATE SCHEMA, CREATE ROLE, triggers).
    superuser=False → uses the SQLAlchemy engine (pipeline_user).
    """
    if superuser:
        import subprocess
        cfg = _load_config()["database"]
        pg_pass = os.environ.get("PGPASSWORD", os.environ.get("DB_PASSWORD", ""))
        pg_user = os.environ.get("SETUP_DB_USER", "postgres")
        env = os.environ.copy()
        env["PGPASSWORD"] = pg_pass
        cmd = [
            "psql",
            "-h", cfg["host"],
            "-p", str(cfg["port"]),
            "-U", pg_user,
            "-d", cfg["name"],
            "-f", path,
            "-v", "ON_ERROR_STOP=1",
        ]
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"psql failed for {path}:\n{result.stderr}"
            )
        logger.info("Executed SQL file (psql): %s", path)
        return

    # pipeline_user path — Python-side dollar-quote-aware splitter
    engine = get_engine()
    with open(path, encoding="utf-8") as f:
        sql = f.read()
    statements = _split_sql(sql)
    with engine.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        for stmt in statements:
            conn.execute(text(stmt))
    logger.info("Executed SQL file: %s  (%d statements)", path, len(statements))


def create_pipeline_run(triggered_by: str = "manual", rave_env: str = "") -> int:
    """Insert a new pipeline_run record and return its run_id."""
    with get_session() as sess:
        result = sess.execute(
            text(
                "INSERT INTO staging.pipeline_run (triggered_by, rave_env) "
                "VALUES (:by, :env) RETURNING run_id"
            ),
            {"by": triggered_by, "env": rave_env},
        )
        run_id = result.scalar()
    logger.info("Pipeline run created: run_id=%s", run_id)
    return run_id


def finish_pipeline_run(run_id: int, status: str = "SUCCESS", notes: str = ""):
    with get_session() as sess:
        sess.execute(
            text(
                "UPDATE staging.pipeline_run "
                "SET run_finished_at = NOW(), status = :status, notes = :notes "
                "WHERE run_id = :run_id"
            ),
            {"status": status, "notes": notes, "run_id": run_id},
        )
    logger.info("Pipeline run %s finished with status: %s", run_id, status)
