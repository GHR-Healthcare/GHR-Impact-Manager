"""
Non-MSP client allowlist admin endpoint.

GET  /api/client-allowlist
     → { "allowlist": [ { client_id, display_name, notes, added_by, added_at }, ... ] }

POST /api/client-allowlist
     Body: { "allowlist": [ { client_id, display_name?, notes? }, ... ] }
     Replaces the full allowlist (matches the system_mappings save pattern).

MSP instance: 405 — this endpoint only applies to the non-MSP dashboard.

Storage: impactmgr.bullhorn_client_allowlist
    client_id     INT PRIMARY KEY       -- Bullhorn clientCorporationID
    display_name  NVARCHAR(200) NULL    -- optional override for the UI (falls back to cc.name)
    notes         NVARCHAR(500) NULL
    added_by      NVARCHAR(200) NULL    -- captured from x-ms-client-principal-name if present
    added_at      DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
"""
import azure.functions as func
import json

from shared_code.auth import require_allowed_domain
from shared_code.data_source import is_non_msp, get_appdb_conn


def _user_from_req(req):
    """Best-effort principal name from Static Web App auth headers."""
    return (
        req.headers.get('x-ms-client-principal-name')
        or req.headers.get('x-ms-client-principal-id')
        or 'unknown'
    )


def ensure_schema(cursor):
    cursor.execute("""
        IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'impactmgr')
            EXEC('CREATE SCHEMA impactmgr')
    """)
    cursor.execute("""
        IF NOT EXISTS (
            SELECT 1 FROM sys.tables
            WHERE name = 'bullhorn_client_allowlist' AND schema_id = SCHEMA_ID('impactmgr')
        )
        CREATE TABLE impactmgr.bullhorn_client_allowlist (
            client_id     INT NOT NULL PRIMARY KEY,
            display_name  NVARCHAR(200) NULL,
            notes         NVARCHAR(500) NULL,
            added_by      NVARCHAR(200) NULL,
            added_at      DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
        )
    """)


def main(req: func.HttpRequest) -> func.HttpResponse:
    auth_error = require_allowed_domain(req)
    if auth_error:
        return auth_error

    if not is_non_msp():
        return func.HttpResponse(
            json.dumps({'error': 'client-allowlist only applies to the non-MSP instance'}),
            mimetype="application/json",
            status_code=405,
        )

    conn = get_appdb_conn()
    if conn is None:
        return func.HttpResponse(
            json.dumps({'error': 'App DB not configured (DB_HOST/APPDB/DB_USER/DB_PASSWORD missing)'}),
            mimetype="application/json",
            status_code=500,
        )

    try:
        cursor = conn.cursor()
        ensure_schema(cursor)
        conn.commit()

        if req.method == 'GET':
            cursor.execute('''
                SELECT client_id, display_name, notes, added_by, added_at
                FROM impactmgr.bullhorn_client_allowlist
                ORDER BY client_id
            ''')
            allowlist = []
            for row in cursor.fetchall():
                allowlist.append({
                    'client_id': int(row[0]),
                    'display_name': row[1],
                    'notes': row[2],
                    'added_by': row[3],
                    'added_at': row[4].isoformat() if row[4] is not None else None,
                })
            conn.close()
            return func.HttpResponse(
                json.dumps({'allowlist': allowlist}),
                mimetype="application/json",
                status_code=200,
            )

        if req.method == 'POST':
            try:
                body = req.get_json()
                entries = body.get('allowlist', [])
            except Exception:
                return func.HttpResponse(
                    json.dumps({'error': 'Invalid JSON body'}),
                    mimetype="application/json",
                    status_code=400,
                )

            # Coerce + validate. Silently drop rows without a numeric client_id;
            # leaders shouldn't be blocked from saving over a stray blank row.
            cleaned = []
            seen = set()
            for e in entries:
                raw_id = e.get('client_id')
                try:
                    cid = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if cid in seen:
                    continue
                seen.add(cid)
                cleaned.append({
                    'client_id': cid,
                    'display_name': (e.get('display_name') or None) if isinstance(e.get('display_name'), str) else None,
                    'notes': (e.get('notes') or None) if isinstance(e.get('notes'), str) else None,
                })

            user = _user_from_req(req)
            cursor.execute('DELETE FROM impactmgr.bullhorn_client_allowlist')
            for e in cleaned:
                cursor.execute('''
                    INSERT INTO impactmgr.bullhorn_client_allowlist
                        (client_id, display_name, notes, added_by)
                    VALUES (?, ?, ?, ?)
                ''', e['client_id'], e['display_name'], e['notes'], user)

            conn.commit()
            conn.close()
            return func.HttpResponse(
                json.dumps({'success': True, 'count': len(cleaned)}),
                mimetype="application/json",
                status_code=200,
            )

        return func.HttpResponse(
            json.dumps({'error': 'Method not allowed'}),
            mimetype="application/json",
            status_code=405,
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        return func.HttpResponse(
            json.dumps({'error': str(e)}),
            mimetype="application/json",
            status_code=500,
        )
