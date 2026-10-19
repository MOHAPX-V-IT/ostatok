"""PostgreSQL connections and transactional, checksum-verified SQL migrations."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "migrations"


def connect(url: str | None = None):
    url = url or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is required; SQLite fallback does not exist")
    return psycopg.connect(
        url, row_factory=dict_row, connect_timeout=5,
        application_name="ostatok", options="-c timezone=UTC -c lock_timeout=8000 -c statement_timeout=15000",
    )


def migrate(url: str | None = None) -> list[str]:
    applied = []
    with connect(url) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(784512334)")
        conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version text PRIMARY KEY, checksum text NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT clock_timestamp())""")
        known = {r["version"]: r["checksum"] for r in conn.execute("SELECT version, checksum FROM schema_migrations")}
        files = sorted(MIGRATIONS.glob("[0-9]*.sql"))
        if not files:
            raise RuntimeError("SQL migration files not found")
        if set(known) - {p.name for p in files}:
            raise RuntimeError("Database has unknown migration versions; refusing downgrade")
        for file in files:
            sql = file.read_text(encoding="utf-8")
            digest = hashlib.sha256(sql.encode()).hexdigest()
            if file.name in known:
                if known[file.name] != digest:
                    raise RuntimeError(f"Migration checksum mismatch: {file.name}")
                continue
            conn.execute(sql)
            conn.execute("INSERT INTO schema_migrations(version,checksum) VALUES (%s,%s)", (file.name, digest))
            applied.append(file.name)
    return applied


def ready(url: str | None = None) -> bool:
    with connect(url) as conn:
        rows = conn.execute("SELECT version,checksum FROM schema_migrations").fetchall()
        expected = {p.name: hashlib.sha256(p.read_text(encoding="utf-8").encode()).hexdigest() for p in MIGRATIONS.glob("[0-9]*.sql")}
        return bool(expected) and {r["version"]: r["checksum"] for r in rows} == expected
