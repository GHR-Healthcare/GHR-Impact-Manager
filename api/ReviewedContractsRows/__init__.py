import azure.functions as func
import pyodbc
import os
import json
from shared_code.auth import require_allowed_domain, current_user_email


def ensure_schema(cursor):
    cursor.execute("""
        IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'impactmgr')
            EXEC('CREATE SCHEMA impactmgr')
    """)
    cursor.execute("""
        IF NOT EXISTS (
            SELECT 1 FROM sys.tables
            WHERE name = 'reviewed_contracts_rows' AND schema_id = SCHEMA_ID('impactmgr')
        )
        CREATE TABLE impactmgr.reviewed_contracts_rows (
            id          INT IDENTITY(1,1) PRIMARY KEY,
            row_key     NVARCHAR(500) NOT NULL UNIQUE,
            reviewed_by NVARCHAR(200) NULL,
            reviewed_at DATETIME2 DEFAULT SYSUTCDATETIME()
        )
    """)


# Matches the row_key column width in the table above.
MAX_ROW_KEY_LEN = 500


def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    GET  → returns { keys: [...] } of all reviewed contract row keys
    POST { action: 'add' | 'remove', key: '...', user?: '...' }
    Storage: ghrappdb.impactmgr.reviewed_contracts_rows
    """
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

        if req.method == 'GET':
            cursor.execute('SELECT row_key FROM impactmgr.reviewed_contracts_rows')
            keys = [row[0] for row in cursor.fetchall()]
            conn.close()
            return func.HttpResponse(
                json.dumps({'keys': keys}),
                mimetype="application/json",
                status_code=200
            )

        elif req.method == 'POST':
            try:
                body = req.get_json()
            except Exception:
                return func.HttpResponse(
                    json.dumps({'error': 'Invalid JSON body'}),
                    mimetype="application/json", status_code=400
                )

            action = (body.get('action') or '').strip().lower()
            key = (body.get('key') or '').strip()
            # Reviewed-by is an audit field, so it comes from the signed-in
            # principal rather than a body value the caller chooses. (GH #87)
            user = current_user_email(req) or (body.get('user') or '').strip() or None

            if not key:
                return func.HttpResponse(
                    json.dumps({'error': 'Missing key'}),
                    mimetype="application/json", status_code=400
                )

            # row_key is NVARCHAR(500) NOT NULL UNIQUE. A longer key was
            # inserted anyway and SQL Server answered "String or binary data
            # would be truncated", which surfaced as a 500 carrying the raw
            # driver message. The key is built by concatenating worker,
            # facility and date fields, so an unusually long facility name is
            # enough to trip it -- a validation error, not a server fault.
            # (GH #43)
            if len(key) > MAX_ROW_KEY_LEN:
                return func.HttpResponse(
                    json.dumps({'error': 'key_too_long', 'max': MAX_ROW_KEY_LEN,
                                'got': len(key)}),
                    mimetype="application/json", status_code=400
                )

            if action == 'add':
                cursor.execute("""
                    IF NOT EXISTS (SELECT 1 FROM impactmgr.reviewed_contracts_rows WHERE row_key = ?)
                        INSERT INTO impactmgr.reviewed_contracts_rows (row_key, reviewed_by) VALUES (?, ?)
                """, key, key, user)
                conn.commit()
            elif action == 'remove':
                cursor.execute('DELETE FROM impactmgr.reviewed_contracts_rows WHERE row_key = ?', key)
                conn.commit()
            else:
                conn.close()
                return func.HttpResponse(
                    json.dumps({'error': f'Unknown action: {action}'}),
                    mimetype="application/json", status_code=400
                )

            conn.close()
            return func.HttpResponse(
                json.dumps({'success': True}),
                mimetype="application/json", status_code=200
            )

        else:
            conn.close()
            return func.HttpResponse(
                json.dumps({'error': 'Method not allowed'}),
                mimetype="application/json", status_code=405
            )

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return func.HttpResponse(
            json.dumps({'error': str(e)}),
            mimetype="application/json", status_code=500
        )
