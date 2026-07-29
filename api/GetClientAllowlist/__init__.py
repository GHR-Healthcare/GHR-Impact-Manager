"""
Non-MSP client allowlist admin endpoint.

GET  /api/client-allowlist
     → { "allowlist": [ { client_id, source, display_name, notes, added_by, added_at }, ... ] }

POST /api/client-allowlist
     Body: { "allowlist": [ { client_id, source?, display_name?, notes? }, ... ] }
     Replaces the full allowlist (matches the system_mappings save pattern).
     `source` is one of 'bullhorn' (default) or 'symplr' — controls which
     data source the ID is force-included into.

MSP instance: 405 — this endpoint only applies to the non-MSP dashboard.

Storage: impactmgr.bullhorn_client_allowlist
    client_id     INT NOT NULL          -- Bullhorn clientCorporationID or Symplr profile_client.recordid (master)
    source        NVARCHAR(20) NOT NULL DEFAULT 'bullhorn'   -- 'bullhorn' or 'symplr'
    display_name  NVARCHAR(200) NULL    -- optional override for the UI (falls back to cc.name / clientname)
    notes         NVARCHAR(500) NULL
    added_by      NVARCHAR(200) NULL    -- captured from x-ms-client-principal-name if present
    added_at      DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
    PRIMARY KEY (source, client_id)
Table name kept as `bullhorn_client_allowlist` for backward compat with
existing deploys that already created it.
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


VALID_SOURCES = ('bullhorn', 'symplr')


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
            client_id     INT NOT NULL,
            source        NVARCHAR(20) NOT NULL CONSTRAINT DF_bh_allowlist_source DEFAULT 'bullhorn',
            display_name  NVARCHAR(200) NULL,
            notes         NVARCHAR(500) NULL,
            added_by      NVARCHAR(200) NULL,
            added_at      DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
            CONSTRAINT PK_bh_allowlist PRIMARY KEY (source, client_id)
        )
    """)
    # If an earlier deploy created the table without the `source` column, add it
    # in place (idempotent) so bumping to the new build doesn't require a
    # manual migration. Older rows default to 'bullhorn' which matches their
    # original semantics.
    cursor.execute("""
        IF EXISTS (
            SELECT 1 FROM sys.tables t
            JOIN sys.schemas s ON s.schema_id = t.schema_id
            WHERE t.name = 'bullhorn_client_allowlist' AND s.name = 'impactmgr'
        )
        AND NOT EXISTS (
            SELECT 1 FROM sys.columns
            WHERE object_id = OBJECT_ID('impactmgr.bullhorn_client_allowlist')
              AND name = 'source'
        )
        BEGIN
            ALTER TABLE impactmgr.bullhorn_client_allowlist
                ADD source NVARCHAR(20) NOT NULL
                    CONSTRAINT DF_bh_allowlist_source DEFAULT 'bullhorn' WITH VALUES;
        END
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
                SELECT client_id, source, display_name, notes, added_by, added_at
                FROM impactmgr.bullhorn_client_allowlist
                ORDER BY source, client_id
            ''')
            allowlist = []
            for row in cursor.fetchall():
                allowlist.append({
                    'client_id': int(row[0]),
                    'source': row[1] or 'bullhorn',
                    'display_name': row[2],
                    'notes': row[3],
                    'added_by': row[4],
                    'added_at': row[5].isoformat() if row[5] is not None else None,
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

            # Coerce + validate. Silently drop rows without a numeric client_id
            # or an unrecognized source. Dedupe on (source, client_id).
            cleaned = []
            seen = set()
            for e in entries:
                raw_id = e.get('client_id')
                try:
                    cid = int(raw_id)
                except (TypeError, ValueError):
                    continue
                source = (e.get('source') or 'bullhorn').lower()
                if source not in VALID_SOURCES:
                    source = 'bullhorn'
                key = (source, cid)
                if key in seen:
                    continue
                seen.add(key)
                cleaned.append({
                    'client_id': cid,
                    'source': source,
                    'display_name': (e.get('display_name') or None) if isinstance(e.get('display_name'), str) else None,
                    'notes': (e.get('notes') or None) if isinstance(e.get('notes'), str) else None,
                })

            user = _user_from_req(req)
            cursor.execute('DELETE FROM impactmgr.bullhorn_client_allowlist')
            for e in cleaned:
                cursor.execute('''
                    INSERT INTO impactmgr.bullhorn_client_allowlist
                        (client_id, source, display_name, notes, added_by)
                    VALUES (?, ?, ?, ?, ?)
                ''', e['client_id'], e['source'], e['display_name'], e['notes'], user)

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
