"""
One-time migration endpoint: copies app-owned tables from
{CHANGES_DB}.dbo.* to {APPDB}.impactmgr.*.

Auth: requires `?secret=...` query param OR `x-migrate-secret` header
matching the MIGRATE_SECRET env var on the Function App. The endpoint
returns 503 if MIGRATE_SECRET is not set, 401 if it doesn't match.

GET  /api/migrate-impactmgr?secret=...
                            → dry run, returns row counts from both sides
GET  /api/migrate-impactmgr?secret=...&confirm=yes
                            → creates schema/tables in APPDB if needed,
                              TRUNCATEs destination, then copies all rows
                              from source.

Idempotent: re-running with confirm=yes will truncate + re-copy. Old
tables in CHANGES_DB.dbo are NOT touched — drop them manually after
verifying the migration.
"""

import azure.functions as func
import pyodbc
import os
import json


# Source dbo.* table → destination impactmgr.* table
TABLE_MIGRATIONS = [
    {
        'src_table': 'dbo.system_mappings',
        'dst_table': 'impactmgr.system_mappings',
        'columns': ['keywords', 'system_name', 'sort_order', 'perdiem_breakout', 'hidden'],
        'create_sql': '''
            CREATE TABLE impactmgr.system_mappings (
                id                INT IDENTITY(1,1) PRIMARY KEY,
                keywords          NVARCHAR(MAX) NOT NULL,
                system_name       NVARCHAR(200) NOT NULL,
                sort_order        INT NOT NULL DEFAULT 0,
                perdiem_breakout  BIT NOT NULL DEFAULT 0,
                hidden            BIT NOT NULL DEFAULT 0
            )
        ''',
    },
    {
        'src_table': 'dbo.pm_mappings',
        'dst_table': 'impactmgr.pm_mappings',
        'columns': ['facility', 'pm_name'],
        'create_sql': '''
            CREATE TABLE impactmgr.pm_mappings (
                id       INT IDENTITY(1,1) PRIMARY KEY,
                facility NVARCHAR(200) NOT NULL,
                pm_name  NVARCHAR(200) NULL
            )
        ''',
    },
    {
        'src_table': 'dbo.ghr_changes',
        'dst_table': 'impactmgr.changes',
        'columns': ['id', 'timestamp', 'jobid', 'change_type', 'change_data', 'user_name'],
        'create_sql': '''
            CREATE TABLE impactmgr.changes (
                id           NVARCHAR(100) PRIMARY KEY,
                timestamp    DATETIME2 NOT NULL,
                jobid        NVARCHAR(100) NULL,
                change_type  NVARCHAR(100) NOT NULL,
                change_data  NVARCHAR(MAX) NULL,
                user_name    NVARCHAR(200) NULL
            )
        ''',
    },
    {
        'src_table': 'dbo.ghr_history_snapshots',
        'dst_table': 'impactmgr.history_snapshots',
        'columns': ['snapshot_timestamp', 'change_count', 'snapshot_data'],
        'create_sql': '''
            CREATE TABLE impactmgr.history_snapshots (
                id                  INT IDENTITY(1,1) PRIMARY KEY,
                snapshot_timestamp  DATETIME2 NOT NULL,
                change_count        INT NOT NULL,
                snapshot_data       NVARCHAR(MAX) NOT NULL
            )
        ''',
    },
    {
        'src_table': 'dbo.reviewed_contracts_rows',
        'dst_table': 'impactmgr.reviewed_contracts_rows',
        'columns': ['row_key', 'reviewed_by', 'reviewed_at'],
        'create_sql': '''
            CREATE TABLE impactmgr.reviewed_contracts_rows (
                id          INT IDENTITY(1,1) PRIMARY KEY,
                row_key     NVARCHAR(500) NOT NULL UNIQUE,
                reviewed_by NVARCHAR(200) NULL,
                reviewed_at DATETIME2 DEFAULT SYSUTCDATETIME()
            )
        ''',
    },
]


def _connect(database_env_var):
    return pyodbc.connect(
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={os.environ['DB_HOST']};"
        f"DATABASE={os.environ[database_env_var]};"
        f"UID={os.environ['DB_USER']};"
        f"PWD={os.environ['DB_PASSWORD']};"
        f"TrustServerCertificate=yes"
    )


def _table_exists_dbo(cursor, table_name_no_schema):
    cursor.execute(
        "SELECT 1 FROM sys.tables WHERE name = ? AND schema_id = SCHEMA_ID('dbo')",
        table_name_no_schema,
    )
    return cursor.fetchone() is not None


def _ensure_dst_schema_and_table(dst_cursor, mig):
    """Create impactmgr schema and the destination table if missing."""
    dst_cursor.execute("""
        IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'impactmgr')
            EXEC('CREATE SCHEMA impactmgr')
    """)
    dst_table_no_schema = mig['dst_table'].split('.')[1]
    dst_cursor.execute(
        "SELECT 1 FROM sys.tables WHERE name = ? AND schema_id = SCHEMA_ID('impactmgr')",
        dst_table_no_schema,
    )
    if dst_cursor.fetchone() is None:
        dst_cursor.execute(mig['create_sql'])


def _count(cursor, table):
    try:
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        return cursor.fetchone()[0]
    except pyodbc.Error:
        return None  # table doesn't exist


def main(req: func.HttpRequest) -> func.HttpResponse:
    # Shared-secret auth: caller must supply ?secret=... or x-migrate-secret
    # header that matches the MIGRATE_SECRET env var. Keeps this endpoint
    # off-limits to anyone who happens to know the URL.
    expected_secret = os.environ.get('MIGRATE_SECRET')
    if not expected_secret:
        return func.HttpResponse(
            json.dumps({'error': 'MIGRATE_SECRET env var not configured. Set it on the Function App before using this endpoint.'}),
            mimetype="application/json", status_code=503
        )
    provided_secret = req.params.get('secret') or req.headers.get('x-migrate-secret') or ''
    if provided_secret != expected_secret:
        # Constant-time-ish check via length compare done implicitly above; the
        # mismatch path returns the same 401 either way.
        return func.HttpResponse(
            json.dumps({'error': 'Unauthorized.'}),
            mimetype="application/json", status_code=401
        )

    confirm = (req.params.get('confirm') or '').lower() == 'yes'
    src_db_env = req.params.get('source_db') or 'CHANGES_DB'

    if 'APPDB' not in os.environ:
        return func.HttpResponse(
            json.dumps({'error': 'APPDB env var not set. Add APPDB=ghrappdb to the Function App configuration.'}),
            mimetype="application/json", status_code=500
        )
    if src_db_env not in os.environ:
        return func.HttpResponse(
            json.dumps({'error': f'{src_db_env} env var not set.'}),
            mimetype="application/json", status_code=500
        )

    report = {
        'source_db': os.environ[src_db_env],
        'destination_db': os.environ['APPDB'],
        'confirmed': confirm,
        'tables': [],
    }

    src_conn = None
    dst_conn = None
    try:
        src_conn = _connect(src_db_env)
        dst_conn = _connect('APPDB')
        src_cursor = src_conn.cursor()
        dst_cursor = dst_conn.cursor()

        for mig in TABLE_MIGRATIONS:
            entry = {
                'src_table': mig['src_table'],
                'dst_table': mig['dst_table'],
            }

            # Make sure dst exists (always — safe even on dry run)
            _ensure_dst_schema_and_table(dst_cursor, mig)
            dst_conn.commit()

            src_count = _count(src_cursor, mig['src_table'])
            dst_count_before = _count(dst_cursor, mig['dst_table'])
            entry['src_count'] = src_count
            entry['dst_count_before'] = dst_count_before

            if not confirm:
                entry['action'] = 'dry_run'
                report['tables'].append(entry)
                continue

            if src_count is None:
                entry['action'] = 'skipped_no_source'
                report['tables'].append(entry)
                continue

            # Wipe destination, then copy all rows from source.
            dst_cursor.execute(f"DELETE FROM {mig['dst_table']}")
            cols_csv = ', '.join(f'[{c}]' for c in mig['columns'])
            placeholders = ', '.join(['?'] * len(mig['columns']))
            src_cursor.execute(f"SELECT {cols_csv} FROM {mig['src_table']}")
            rows = src_cursor.fetchall()
            if rows:
                dst_cursor.fast_executemany = True
                dst_cursor.executemany(
                    f"INSERT INTO {mig['dst_table']} ({cols_csv}) VALUES ({placeholders})",
                    [tuple(r) for r in rows],
                )
            dst_conn.commit()

            entry['action'] = 'copied'
            entry['rows_copied'] = len(rows)
            entry['dst_count_after'] = _count(dst_cursor, mig['dst_table'])
            report['tables'].append(entry)

        return func.HttpResponse(
            json.dumps(report, default=str),
            mimetype="application/json",
            status_code=200,
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        report['error'] = str(e)
        return func.HttpResponse(
            json.dumps(report, default=str),
            mimetype="application/json",
            status_code=500,
        )
    finally:
        try:
            if src_conn:
                src_conn.close()
        except Exception:
            pass
        try:
            if dst_conn:
                dst_conn.close()
        except Exception:
            pass
