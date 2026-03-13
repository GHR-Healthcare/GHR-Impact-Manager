import azure.functions as func
import pyodbc
import os
import json

def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    GET: Retrieve all system mappings
    POST: Save/update system mappings (replaces all)
    """
    try:
        # Use CHANGES_DB (ghr_impact_mgr) for this table
        conn = pyodbc.connect(
            f"DRIVER={{ODBC Driver 17 for SQL Server}};"
            f"SERVER={os.environ['DB_HOST']};"
            f"DATABASE={os.environ['CHANGES_DB']};"
            f"UID={os.environ['DB_USER']};"
            f"PWD={os.environ['DB_PASSWORD']};"
            f"TrustServerCertificate=yes"
        )
        
        cursor = conn.cursor()
        
        if req.method == 'GET':
            # Get all mappings
            cursor.execute('''
                SELECT id, keywords, system_name, sort_order,
                       CASE WHEN perdiem_breakout = 1 THEN 1 ELSE 0 END AS perdiem_breakout
                FROM dbo.system_mappings
                ORDER BY sort_order, id
            ''')
            
            columns = [column[0] for column in cursor.description]
            mappings = []
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                # Parse keywords from comma-separated string to array
                row_dict['keywords'] = [k.strip() for k in row_dict['keywords'].split(',') if k.strip()]
                mappings.append(row_dict)
            
            conn.close()
            
            return func.HttpResponse(
                json.dumps({'mappings': mappings}),
                mimetype="application/json",
                status_code=200
            )
        
        elif req.method == 'POST':
            # Save mappings - replace all existing
            try:
                body = req.get_json()
                mappings = body.get('mappings', [])
            except:
                return func.HttpResponse(
                    json.dumps({'error': 'Invalid JSON body'}),
                    mimetype="application/json",
                    status_code=400
                )
            
            # Delete existing mappings
            cursor.execute('DELETE FROM dbo.system_mappings')
            
            # Insert new mappings
            for idx, mapping in enumerate(mappings):
                keywords = mapping.get('keywords', [])
                system_name = mapping.get('system_name') or mapping.get('system', '')
                
                # Convert keywords array to comma-separated string
                if isinstance(keywords, list):
                    keywords_str = ', '.join(keywords)
                else:
                    keywords_str = str(keywords)
                
                perdiem_breakout = 1 if mapping.get('perdiem_breakout') else 0

                cursor.execute('''
                    INSERT INTO dbo.system_mappings (keywords, system_name, sort_order, perdiem_breakout)
                    VALUES (?, ?, ?, ?)
                ''', keywords_str, system_name, idx, perdiem_breakout)
            
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