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
from shared_code.audit import record_change
from shared_code.auth import current_user_email


def _user_from_req(req):
    """The signed-in principal.

    Prefers the verified email from the principal blob so this endpoint
    attributes writes the same way SaveChange, Meetings and the config
    endpoints do, and falls back to the SWA name/id headers.
    """
    return (
        current_user_email(req)
        or req.headers.get('x-ms-client-principal-name')
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
        # Internal misconfig — user-facing message stays generic.
        print("GetClientAllowlist: app DB not configured (DB_HOST/APPDB/DB_USER/DB_PASSWORD missing)")
        return func.HttpResponse(
            json.dumps({'error': 'This feature is temporarily unavailable. Please try again later.'}),
            mimetype="application/json",
            status_code=503,
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
            except Exception:
                return func.HttpResponse(
                    json.dumps({'error': 'Invalid JSON body'}),
                    mimetype="application/json",
                    status_code=400,
                )

            # The handler replaces the whole table, so a body with no
            # 'allowlist' key used to default to [] and delete every manually
            # forced client for BOTH sources -- and these ids drive scope
            # resolution across every non-MSP endpoint, so the accounts simply
            # vanished from the dashboard. An absent key is a malformed request,
            # not an instruction to erase the configuration. (GH #68)
            if not isinstance(body, dict) or 'allowlist' not in body:
                return func.HttpResponse(
                    json.dumps({'error': 'missing_allowlist',
                                'detail': "body must contain an 'allowlist' array; "
                                          "send [] explicitly to clear it"}),
                    mimetype="application/json",
                    status_code=400,
                )
            entries = body.get('allowlist')
            if not isinstance(entries, list):
                return func.HttpResponse(
                    json.dumps({'error': 'allowlist_must_be_an_array'}),
                    mimetype="application/json",
                    status_code=400,
                )

            # Coerce + validate. A non-numeric client_id is REJECTED rather
            # than dropped: silently skipping it meant a payload whose ids were
            # all malformed cleared the table and reported success. Dedupe on
            # (source, client_id). (GH #68)
            cleaned = []
            seen = set()
            rejected = []
            for e in entries:
                if not isinstance(e, dict):
                    rejected.append({'entry': str(e)[:60], 'why': 'not_an_object'})
                    continue
                raw_id = e.get('client_id')
                try:
                    cid = int(raw_id)
                except (TypeError, ValueError):
                    rejected.append({'client_id': str(raw_id)[:60],
                                     'why': 'client_id_not_numeric'})
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

            if rejected:
                return func.HttpResponse(
                    json.dumps({'error': 'invalid_entries', 'rejected': rejected[:20],
                                'detail': 'nothing was changed'}),
                    mimetype="application/json",
                    status_code=400,
                )

            user = _user_from_req(req)
            # added_by already records the owner of each row; this puts the
            # replacement itself on the same timeline as every other config
            # change, before the delete so a part-way failure is still
            # attributed. Settings are open by design. (GH #52)
            record_change(cursor, 'config.client_allowlist', 'all',
                          {'action': 'replace_all', 'count': len(cleaned)}, user)
            cursor.execute('DELETE FROM impactmgr.bullhorn_client_allowlist')
            for e in cleaned:
                cursor.execute('''
                    INSERT INTO impactmgr.bullhorn_client_allowlist
                        (client_id, source, display_name, notes, added_by)
                    VALUES (?, ?, ?, ?, ?)
                ''', e['client_id'], e['source'], e['display_name'], e['notes'], user)

            conn.commit()
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
        # Full details go to server logs; surface a generic message to the UI.
        import traceback
        traceback.print_exc()
        print(f"GetClientAllowlist: unhandled error: {e}")
        # The POST deletes every row before re-inserting, so a failure between
        # the two must not be left to an implicit rollback at collection time.
        # pyodbc defaults to autocommit=False, so the transaction WAS rolled
        # back eventually -- but only whenever the leaked connection happened
        # to be collected. Rolled back here, explicitly. (GH #53)
        try:
            conn.rollback()
        except Exception:
            pass
        return func.HttpResponse(
            json.dumps({'error': 'Something went wrong saving the allowlist. Please try again.'}),
            mimetype="application/json",
            status_code=500,
        )
    finally:
        # Was closed only on the success paths, so every failing request leaked
        # a pooled connection. (GH #40, #53)
        try:
            conn.close()
        except Exception:
            pass
