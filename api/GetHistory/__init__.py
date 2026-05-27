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
            WHERE name = 'history_snapshots' AND schema_id = SCHEMA_ID('impactmgr')
        )
        CREATE TABLE impactmgr.history_snapshots (
            id                  INT IDENTITY(1,1) PRIMARY KEY,
            snapshot_timestamp  DATETIME2 NOT NULL,
            change_count        INT NOT NULL,
            snapshot_data       NVARCHAR(MAX) NOT NULL
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
            SELECT TOP 100 id, snapshot_timestamp, change_count, snapshot_data
            FROM impactmgr.history_snapshots
            ORDER BY snapshot_timestamp DESC
        ''')

        snapshots = []
        for row in cursor.fetchall():
            snapshot = {
                'id': row[0],
                'timestamp': row[1].isoformat() if row[1] else None,
                'changeCount': row[2],
                'data': json.loads(row[3]) if row[3] else {}
            }
            snapshots.append(snapshot)

        conn.close()
        return func.HttpResponse(
            json.dumps(snapshots),
            mimetype="application/json",
            status_code=200
        )
    except Exception as e:
        return func.HttpResponse(
            json.dumps({'error': str(e)}),
            mimetype="application/json",
            status_code=500
        )
