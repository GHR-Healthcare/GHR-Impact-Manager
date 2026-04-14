import azure.functions as func
import pyodbc
import os
import json


def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    GET: Retrieve all facility → PM mappings
    POST: Save/update mappings (replaces all)

    Table: dbo.pm_mappings
      id        INT IDENTITY PRIMARY KEY
      facility  NVARCHAR(200) NOT NULL
      pm_name   NVARCHAR(200) NULL
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

        # Migrate old schema (pm_name + facilities columns) to new (facility + pm_name)
        cursor.execute("""
            IF EXISTS (
                SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = 'pm_mappings' AND COLUMN_NAME = 'facilities'
            )
            BEGIN
                DROP TABLE dbo.pm_mappings
            END
        """)
        conn.commit()

        # Auto-create table if it doesn't exist
        cursor.execute("""
            IF NOT EXISTS (
                SELECT 1 FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_NAME = 'pm_mappings'
            )
            CREATE TABLE dbo.pm_mappings (
                id       INT IDENTITY(1,1) PRIMARY KEY,
                facility NVARCHAR(200) NOT NULL,
                pm_name  NVARCHAR(200) NULL
            )
        """)
        conn.commit()

        if req.method == 'GET':
            cursor.execute('SELECT facility, pm_name FROM dbo.pm_mappings ORDER BY facility')
            mappings = [
                {'facility': row[0], 'pm_name': row[1] or ''}
                for row in cursor.fetchall()
            ]
            conn.close()

            return func.HttpResponse(
                json.dumps({'mappings': mappings}),
                mimetype="application/json",
                status_code=200
            )

        elif req.method == 'POST':
            try:
                body = req.get_json()
                mappings = body.get('mappings', [])
            except Exception:
                return func.HttpResponse(
                    json.dumps({'error': 'Invalid JSON body'}),
                    mimetype="application/json",
                    status_code=400
                )

            cursor.execute('DELETE FROM dbo.pm_mappings')

            saved = 0
            for m in mappings:
                facility = (m.get('facility') or '').strip()
                pm_name = (m.get('pm_name') or '').strip()
                if facility and pm_name:
                    cursor.execute(
                        'INSERT INTO dbo.pm_mappings (facility, pm_name) VALUES (?, ?)',
                        facility, pm_name
                    )
                    saved += 1

            conn.commit()
            conn.close()

            return func.HttpResponse(
                json.dumps({'success': True, 'count': saved}),
                mimetype="application/json",
                status_code=200
            )

        else:
            return func.HttpResponse(
                json.dumps({'error': 'Method not allowed'}),
                mimetype="application/json",
                status_code=405
            )

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return func.HttpResponse(
            json.dumps({'error': str(e)}),
            mimetype="application/json",
            status_code=500
        )
