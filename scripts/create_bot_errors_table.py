#!/usr/bin/env python3
"""Create bot_errors table on Supabase (Error Intelligence Hub — wf00).

Usage:
    # Option 1: DB password via env var
    export SUPABASE_DB_PASSWORD='your-db-password'
    python3 scripts/create_bot_errors_table.py

    # Option 2: Full connection string
    export DATABASE_URL='postgresql://postgres.oimlamjilivrcnhztwvj:PASSWORD@aws-0-eu-central-1.pooler.supabase.com:6543/postgres'
    python3 scripts/create_bot_errors_table.py

    # Option 3: Copy the DDL from cockpit_ddl.sql (v8 section) into Supabase SQL Editor
"""
import os
import sys
import ssl
import certifi

# ── Load env ──
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

DDL = """
CREATE TABLE IF NOT EXISTS bot_errors (
    id                 BIGSERIAL PRIMARY KEY,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    workflow_id        TEXT NOT NULL,
    workflow_name      TEXT,
    node_name          TEXT,
    execution_id       TEXT,
    severity           TEXT NOT NULL CHECK (severity IN ('P0','P1','P2','P3')),
    error_type         TEXT NOT NULL,
    error_message      TEXT,
    error_fingerprint  TEXT NOT NULL,
    context            JSONB DEFAULT '{}',
    resolved           BOOLEAN DEFAULT FALSE,
    resolved_at        TIMESTAMPTZ,
    resolved_by        TEXT,
    recovery_attempted BOOLEAN DEFAULT FALSE,
    recovery_result    TEXT,
    notification_sent  BOOLEAN DEFAULT FALSE,
    duplicate_of       BIGINT REFERENCES bot_errors(id)
);

CREATE INDEX IF NOT EXISTS idx_bot_errors_fingerprint
    ON bot_errors (error_fingerprint, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_bot_errors_severity
    ON bot_errors (severity, created_at DESC)
    WHERE resolved = FALSE;

CREATE INDEX IF NOT EXISTS idx_bot_errors_created
    ON bot_errors (created_at DESC);
"""


def main():
    import psycopg2

    # Build connection string
    db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        supa_url = os.environ.get('SUPABASE_URL', '')
        project_ref = supa_url.replace('https://', '').split('.')[0]
        db_pass = os.environ.get('SUPABASE_DB_PASSWORD', '')
        if not db_pass:
            print("❌ No DATABASE_URL or SUPABASE_DB_PASSWORD found.")
            print("   Set one of them in .env or as env var, then re-run.")
            print(f"\n   Or paste the DDL below into Supabase SQL Editor:\n{DDL}")
            sys.exit(1)
        db_url = (
            f"postgresql://postgres.{project_ref}:{db_pass}"
            f"@aws-0-eu-central-1.pooler.supabase.com:6543/postgres"
        )

    print(f"Connecting to Supabase DB...")
    conn = psycopg2.connect(db_url, sslmode='require')
    conn.autocommit = True
    cur = conn.cursor()

    print("Executing DDL...")
    cur.execute(DDL)
    print("✅ Table bot_errors created (or already exists).")

    # Verify
    cur.execute("SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = 'bot_errors' ORDER BY ordinal_position")
    cols = cur.fetchall()
    print(f"\n📋 bot_errors columns ({len(cols)}):")
    for name, dtype in cols:
        print(f"   {name}: {dtype}")

    cur.close()
    conn.close()
    print("\n🚀 Done!")


if __name__ == '__main__':
    main()
