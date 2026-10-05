import azure.functions as func
import pyodbc
import os
import json
from shared_code.auth import require_allowed_domain


def ensure_schema(cursor):
    cursor.execute("""
        IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'impactmgr')
            EXEC('CREATE SCHEMA impactmgr')
    """)
    cursor.execute("""
        IF NOT EXISTS (
            SELECT 1 FROM sys.tables
            WHERE name = 'changes' AND schema_id = SCHEMA_ID('impactmgr')
        )
        CREATE TABLE impactmgr.changes (
            id           NVARCHAR(100) PRIMARY KEY,
            timestamp    DATETIME2 NOT NULL,
            jobid        NVARCHAR(100) NULL,
            change_type  NVARCHAR(100) NOT NULL,
            change_data  NVARCHAR(MAX) NULL,
            user_name    NVARCHAR(200) NULL
        )
    """)


# The audit row's own id. NVARCHAR(100) in the table above, so a longer one is
# a truncation error from the driver rather than a saved row. (GH #69)
MAX_ID_LEN = 100
REQUIRED_FIELDS = ('id', 'timestamp', 'jobId', 'type')


def main(req: func.HttpRequest) -> func.HttpResponse:
    auth_error = require_allowed_domain(req)
    if auth_error:
        return auth_error

    # Validate before touching the database. These were indexed directly, so a
    # body missing any of them -- or one that is not an object, or not JSON at
    # all -- raised KeyError/ValueError and surfaced as a 500 carrying the raw
    # exception text. A malformed request is the caller's error, and 400 says
    # which field is wrong instead of leaking a driver message. (GH #30, #29)
    try:
        change = req.get_json()
    except Exception:
        return func.HttpResponse(
            json.dumps({'error': 'invalid_json'}),
            mimetype='application/json', status_code=400)

    if not isinstance(change, dict):
        return func.HttpResponse(
            json.dumps({'error': 'body_must_be_an_object'}),
            mimetype='application/json', status_code=400)

    missing = [f for f in REQUIRED_FIELDS if change.get(f) in (None, '')]
    if missing:
        return func.HttpResponse(
            json.dumps({'error': 'missing_fields', 'fields': missing}),
            mimetype='application/json', status_code=400)

    if len(str(change['id'])) > MAX_ID_LEN:
        return func.HttpResponse(
            json.dumps({'error': 'id_too_long', 'max': MAX_ID_LEN}),
            mimetype='application/json', status_code=400)

    conn = None
    try:
        conn = pyodbc.connect(
            f"DRIVER={{ODBC Driver 17 for SQL Server}};"
            f"SERVER={os.environ['DB_HOST']};"
            f"DATABASE={os.environ['APPDB']};"
            f"UID={os.environ['DB_USER']};"
            f"PWD={os.environ['DB_PASSWORD']};"
            f"TrustServerCertificate=yes"
        )

        cursor = conn.cursor()
        ensure_schema(cursor)

        cursor.execute('''
            INSERT INTO impactmgr.changes (id, timestamp, jobid, change_type, change_data, user_name)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            change['id'],
            change['timestamp'],
            change['jobId'],
            change['type'],
            json.dumps(change['data']),
            change.get('user', 'Unknown')
        ))

        conn.commit()

        return func.HttpResponse(
            json.dumps({'success': True}),
            mimetype="application/json",
            status_code=200
        )
    except Exception as e:
        # Logged, not returned: pyodbc messages carry the server name, database
        # and driver version. (GH #29)
        print(f'SaveChange error: {e}')
        import traceback
        traceback.print_exc()
        return func.HttpResponse(
            json.dumps({'error': 'save_failed'}),
            mimetype="application/json",
            status_code=500
        )
    finally:
        # Closed on every path. It used to close only on success, so each
        # failing request leaked a pooled connection. (GH #40)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
