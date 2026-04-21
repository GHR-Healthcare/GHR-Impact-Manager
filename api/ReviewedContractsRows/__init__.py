import azure.functions as func
import pyodbc
import os
import json


def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    GET  → returns { keys: [...] } of all reviewed contract row keys
    POST { action: 'add' | 'remove', key: '...', user?: '...' }
    """
    try:
        conn = pyodbc.connect(
            f"DRIVER={{ODBC Driver 17 for SQL Server}};"
            f"SERVER={os.environ['DB_HOST']};"
            f"DATABASE={os.environ['CHANGES_DB']};"
            f"UID={os.environ['DB_USER']};"
            f"PWD={os.environ['DB_PASSWORD']};"
            f"TrustServerCertificate=yes"
        )
        cursor = conn.cursor()

        # Auto-create table
        cursor.execute("""
            IF NOT EXISTS (
                SELECT 1 FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_NAME = 'reviewed_contracts_rows'
            )
            CREATE TABLE dbo.reviewed_contracts_rows (
                id          INT IDENTITY(1,1) PRIMARY KEY,
                row_key     NVARCHAR(500) NOT NULL UNIQUE,
                reviewed_by NVARCHAR(200) NULL,
                reviewed_at DATETIME2 DEFAULT SYSUTCDATETIME()
            )
        """)
        conn.commit()

        if req.method == 'GET':
            cursor.execute('SELECT row_key FROM dbo.reviewed_contracts_rows')
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
            user = (body.get('user') or '').strip() or None

            if not key:
                return func.HttpResponse(
                    json.dumps({'error': 'Missing key'}),
                    mimetype="application/json", status_code=400
                )

            if action == 'add':
                # Insert if not exists
                cursor.execute("""
                    IF NOT EXISTS (SELECT 1 FROM dbo.reviewed_contracts_rows WHERE row_key = ?)
                        INSERT INTO dbo.reviewed_contracts_rows (row_key, reviewed_by) VALUES (?, ?)
                """, key, key, user)
                conn.commit()
            elif action == 'remove':
                cursor.execute('DELETE FROM dbo.reviewed_contracts_rows WHERE row_key = ?', key)
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
