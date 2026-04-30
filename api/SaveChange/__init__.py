import azure.functions as func
import pyodbc
import os
import json


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
    try:
        change = req.get_json()

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
