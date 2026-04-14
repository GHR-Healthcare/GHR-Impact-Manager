import azure.functions as func
import pyodbc
import os
import json


def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    GET: Retrieve all PM-to-facility mappings
    POST: Save/update PM mappings (replaces all)

    Table: dbo.pm_mappings
      id            INT IDENTITY PRIMARY KEY
      pm_name       NVARCHAR(200) NOT NULL
      facilities    NVARCHAR(MAX)   -- comma-separated facility names
      sort_order    INT DEFAULT 0
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

        # Auto-create table if it doesn't exist
        cursor.execute("""
            IF NOT EXISTS (
                SELECT 1 FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_NAME = 'pm_mappings'
            )
            CREATE TABLE dbo.pm_mappings (
                id         INT IDENTITY(1,1) PRIMARY KEY,
                pm_name    NVARCHAR(200) NOT NULL,
                facilities NVARCHAR(MAX) NULL,
                sort_order INT DEFAULT 0
            )
        """)
        conn.commit()

        if req.method == 'GET':
            cursor.execute('''
                SELECT id, pm_name, facilities, sort_order
                FROM dbo.pm_mappings
                ORDER BY sort_order, pm_name
            ''')

            columns = [column[0] for column in cursor.description]
            mappings = []
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                # Parse facilities from comma-separated string to array
                fac_str = row_dict.get('facilities') or ''
                row_dict['facilities'] = [f.strip() for f in fac_str.split(',') if f.strip()]
                mappings.append(row_dict)

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

            for idx, mapping in enumerate(mappings):
                pm_name = (mapping.get('pm_name') or '').strip()
                if not pm_name:
                    continue
                facilities = mapping.get('facilities', [])
                if isinstance(facilities, list):
                    facilities_str = ', '.join(f.strip() for f in facilities if f.strip())
                else:
                    facilities_str = str(facilities)

                cursor.execute('''
                    INSERT INTO dbo.pm_mappings (pm_name, facilities, sort_order)
                    VALUES (?, ?, ?)
                ''', pm_name, facilities_str, idx)

            conn.commit()
            conn.close()

            return func.HttpResponse(
                json.dumps({'success': True, 'count': len(mappings)}),
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
