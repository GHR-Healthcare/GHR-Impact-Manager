import azure.functions as func
import os
import json
import datetime
from shared_code.auth import require_allowed_domain, current_user_email
from shared_code.data_source import get_appdb_conn


# Per-record state the meeting workspace captures but no source system owns:
#   lever      — Impact levers toggled on an open job   (entity = job id)
#   extension  — client decision, notes, workflow steps (entity = contract / WOSystemKey)
#   onboarding — revised start, delay category, context (entity = contract / WOSystemKey)
#   interview  — interview outcome overrides            (entity = submission key)
#   margin     — manual margin override                 (entity = job / contract id)
VALID_SCOPES = ('lever', 'extension', 'onboarding', 'interview', 'margin')

MAX_STATE_BYTES = 64 * 1024


def _ensure_schema(cursor):
    cursor.execute("""
        IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'impactmgr')
            EXEC('CREATE SCHEMA impactmgr')
    """)
    cursor.execute("""
        IF NOT EXISTS (
            SELECT 1 FROM sys.tables
            WHERE name = 'workspace_state' AND schema_id = SCHEMA_ID('impactmgr')
        )
        CREATE TABLE impactmgr.workspace_state (
            scope      NVARCHAR(40)  NOT NULL,
            entity_id  NVARCHAR(120) NOT NULL,
            state_json NVARCHAR(MAX) NULL,
            updated_at DATETIME2     NOT NULL,
            updated_by NVARCHAR(200) NULL,
            CONSTRAINT PK_workspace_state PRIMARY KEY (scope, entity_id)
        )
    """)


def _get(cursor, scope):
    if scope:
        cursor.execute("""
            SELECT scope, entity_id, state_json, updated_at, updated_by
            FROM impactmgr.workspace_state WHERE scope = ?
        """, scope)
    else:
        cursor.execute("""
            SELECT scope, entity_id, state_json, updated_at, updated_by
            FROM impactmgr.workspace_state
        """)
    out = {}
    for row in cursor.fetchall():
        s, entity, payload, updated_at, updated_by = row
        try:
            value = json.loads(payload) if payload else None
        except (ValueError, TypeError):
            value = None
        out.setdefault(s, {})[entity] = {
            'state': value,
            'updatedAt': updated_at.isoformat() if updated_at else None,
            'updatedBy': updated_by,
        }
    return out


def _put(cursor, scope, entity_id, state, user):
    payload = json.dumps(state, default=str)
    if len(payload.encode('utf-8')) > MAX_STATE_BYTES:
        raise ValueError(f'state too large for {scope}/{entity_id}')
    # MERGE so a second edit to the same record updates in place rather than
    # accumulating rows — this table holds current state, and impactmgr.changes
    # already carries the append-only audit trail.
    cursor.execute("""
        MERGE impactmgr.workspace_state AS t
        USING (SELECT ? AS scope, ? AS entity_id) AS src
          ON t.scope = src.scope AND t.entity_id = src.entity_id
        WHEN MATCHED THEN
            UPDATE SET state_json = ?, updated_at = ?, updated_by = ?
        WHEN NOT MATCHED THEN
            INSERT (scope, entity_id, state_json, updated_at, updated_by)
            VALUES (?, ?, ?, ?, ?);
    """, scope, entity_id,
         payload, datetime.datetime.utcnow(), user,
         scope, entity_id, payload, datetime.datetime.utcnow(), user)


def _audit(cursor, scope, entity_id, state, user):
    """Append to the existing change log so edits stay traceable over time."""
    try:
        cursor.execute("""
            IF EXISTS (SELECT 1 FROM sys.tables
                       WHERE name = 'changes' AND schema_id = SCHEMA_ID('impactmgr'))
            INSERT INTO impactmgr.changes (id, timestamp, jobid, change_type, change_data, user_name)
            VALUES (?, ?, ?, ?, ?, ?)
        """, f'{scope}:{entity_id}:{datetime.datetime.utcnow().isoformat()}',
             datetime.datetime.utcnow(), entity_id, f'workspace.{scope}',
             json.dumps(state, default=str)[:4000], user)
    except Exception as e:
        # Audit is best-effort; never fail the save because logging failed.
        print(f'WorkspaceState: audit write skipped: {e}')


def main(req: func.HttpRequest) -> func.HttpResponse:
    auth_error = require_allowed_domain(req)
    if auth_error:
        return auth_error

    user = current_user_email(req)
    conn = get_appdb_conn()
    if conn is None:
        return func.HttpResponse(
            json.dumps({'error': 'appdb_not_configured'}),
            mimetype='application/json', status_code=503)

    try:
        cursor = conn.cursor()
        _ensure_schema(cursor)
        conn.commit()

        if req.method == 'GET':
            scope = req.params.get('scope')
            if scope and scope not in VALID_SCOPES:
                return func.HttpResponse(
                    json.dumps({'error': f'unknown scope: {scope}'}),
                    mimetype='application/json', status_code=400)
            return func.HttpResponse(
                json.dumps(_get(cursor, scope), default=str),
                mimetype='application/json', status_code=200)

        body = req.get_json()
        # Accept one edit or a batch, so a meeting can flush several at once.
        items = body if isinstance(body, list) else [body]
        saved = 0
        for item in items:
            scope = (item or {}).get('scope')
            entity_id = str((item or {}).get('entityId') or '').strip()
            if scope not in VALID_SCOPES:
                return func.HttpResponse(
                    json.dumps({'error': f'unknown scope: {scope}'}),
                    mimetype='application/json', status_code=400)
            if not entity_id:
                return func.HttpResponse(
                    json.dumps({'error': 'entityId required'}),
                    mimetype='application/json', status_code=400)
            state = item.get('state')
            _put(cursor, scope, entity_id, state, user)
            _audit(cursor, scope, entity_id, state, user)
            saved += 1

        conn.commit()
        print(f'WorkspaceState: saved {saved} item(s) for {user}')
        return func.HttpResponse(
            json.dumps({'saved': saved}),
            mimetype='application/json', status_code=200)

    except ValueError as e:
        return func.HttpResponse(
            json.dumps({'error': str(e)}),
            mimetype='application/json', status_code=400)
    except Exception as e:
        print(f'WorkspaceState error: {e}')
        import traceback
        traceback.print_exc()
        return func.HttpResponse(
            json.dumps({'error': str(e)}),
            mimetype='application/json', status_code=500)
    finally:
        conn.close()
