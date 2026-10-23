import azure.functions as func
import pyodbc
import os
import json
from shared_code.auth import require_allowed_domain, current_user_email
from shared_code.audit import record_change


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
        snapshot = req.get_json()

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
            INSERT INTO impactmgr.history_snapshots (snapshot_timestamp, change_count, snapshot_data)
            VALUES (?, ?, ?)
        ''', (
            snapshot['timestamp'],
            snapshot['changeCount'],
            json.dumps(snapshot['data'])
        ))

        record_change(cursor, 'config.history_snapshot',
                      str(snapshot.get('timestamp') or ''),
                      {'changeCount': snapshot.get('changeCount')},
                      current_user_email(req))

        # Keep the last 100 snapshots, ordered by the IDENTITY id -- insertion
        # order, set by the server.
        #
        # This used to order by snapshot_timestamp, which comes straight from
        # the request body. A client with a skewed clock, or one sending a
        # far-future timestamp, pins itself at the top of that ordering and
        # pushes real history out of the retention window; a client sending a
        # past timestamp deletes its own snapshot on the next write. Neither
        # needs malice, and the delete is silent either way.
        #
        # id is monotonic and server-assigned, so it cannot be influenced by
        # the caller. snapshot_timestamp is still stored and still what the
        # history UI displays -- it is just no longer what decides survival.
        # (GH #80)
        cursor.execute('''
            DELETE FROM impactmgr.history_snapshots
            WHERE id NOT IN (
                SELECT TOP 100 id
                FROM impactmgr.history_snapshots
                ORDER BY id DESC
            )
        ''')

        conn.commit()
        conn.close()

        return func.HttpResponse(
            json.dumps({'success': True}),
            mimetype="application/json",
            status_code=200
        )
    except Exception as e:
        return func.HttpResponse(
            json.dumps({'error': str(e)}),
            mimetype="application/json",
            status_code=500
        )
