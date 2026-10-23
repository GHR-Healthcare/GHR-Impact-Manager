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


def main(req: func.HttpRequest) -> func.HttpResponse:
    auth_error = require_allowed_domain(req)
    if auth_error:
        return auth_error
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
        conn.commit()

        cursor.execute('''
            SELECT id, timestamp, jobid, change_type, change_data, user_name
            FROM impactmgr.changes
            ORDER BY timestamp ASC
        ''')

        changes = []
        for row in cursor.fetchall():
            change = {
                'id': row[0],
                'timestamp': row[1].isoformat() if row[1] else None,
                'jobId': row[2],
                'type': row[3],
                'data': json.loads(row[4]) if row[4] else {},
                'user': row[5]
            }
            changes.append(change)

        conn.close()
        return func.HttpResponse(
            json.dumps({'changes': changes}),
            mimetype="application/json",
            status_code=200
        )
    except Exception as e:
        # 500, not 200. An empty `changes` list with a 200 is indistinguishable
        # from "this record has no history", and the client renders it as such
        # -- so a dead query read as a clean audit trail. The detail stays in
        # the log; pyodbc text carries server and driver internals. (GH #28, #29)
        print(f"GetChanges failed: {e}")
        return func.HttpResponse(
            json.dumps({'error': 'Could not load change history.'}),
            mimetype="application/json",
            status_code=500
        )
