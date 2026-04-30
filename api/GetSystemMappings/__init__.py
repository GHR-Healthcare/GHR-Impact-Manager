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
            WHERE name = 'system_mappings' AND schema_id = SCHEMA_ID('impactmgr')
        )
        CREATE TABLE impactmgr.system_mappings (
            id                INT IDENTITY(1,1) PRIMARY KEY,
            keywords          NVARCHAR(MAX) NOT NULL,
            system_name       NVARCHAR(200) NOT NULL,
            sort_order        INT NOT NULL DEFAULT 0,
            perdiem_breakout  BIT NOT NULL DEFAULT 0,
            hidden            BIT NOT NULL DEFAULT 0
        )
    """)


def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    GET: Retrieve all system mappings
    POST: Save/update system mappings (replaces all)
    Storage: ghrappdb.impactmgr.system_mappings
    """
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
            cursor.execute('''
                SELECT id, keywords, system_name, sort_order,
                       CASE WHEN perdiem_breakout = 1 THEN 1 ELSE 0 END AS perdiem_breakout,
                       CASE WHEN hidden = 1 THEN 1 ELSE 0 END AS hidden
                FROM impactmgr.system_mappings
                ORDER BY sort_order, id
            ''')

            columns = [column[0] for column in cursor.description]
            mappings = []
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                row_dict['keywords'] = [k.strip() for k in row_dict['keywords'].split(',') if k.strip()]
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

            cursor.execute('DELETE FROM impactmgr.system_mappings')

            for idx, mapping in enumerate(mappings):
                keywords = mapping.get('keywords', [])
                system_name = mapping.get('system_name') or mapping.get('system', '')

                if isinstance(keywords, list):
                    keywords_str = ', '.join(keywords)
                else:
                    keywords_str = str(keywords)

                perdiem_breakout = 1 if mapping.get('perdiem_breakout') else 0
                hidden = 1 if mapping.get('hidden') else 0

                cursor.execute('''
                    INSERT INTO impactmgr.system_mappings (keywords, system_name, sort_order, perdiem_breakout, hidden)
                    VALUES (?, ?, ?, ?, ?)
                ''', keywords_str, system_name, idx, perdiem_breakout, hidden)

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
