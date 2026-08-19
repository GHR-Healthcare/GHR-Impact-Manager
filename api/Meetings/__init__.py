import azure.functions as func
import json
import datetime
from shared_code.auth import require_allowed_domain, current_user_email
from shared_code.data_source import get_appdb_conn


MAX_ACTIONS_BYTES = 256 * 1024


def _ensure_schema(cursor):
    cursor.execute("""
        IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'impactmgr')
            EXEC('CREATE SCHEMA impactmgr')
    """)
    cursor.execute("""
        IF NOT EXISTS (
            SELECT 1 FROM sys.tables
            WHERE name = 'meetings' AND schema_id = SCHEMA_ID('impactmgr')
        )
        CREATE TABLE impactmgr.meetings (
            meeting_id  NVARCHAR(60)  NOT NULL PRIMARY KEY,
            health_system NVARCHAR(200) NULL,
            facilities  NVARCHAR(MAX) NULL,   -- JSON array
            recipients  NVARCHAR(MAX) NULL,
            stage       INT           NULL,
            completed   NVARCHAR(400) NULL,   -- JSON array of stage indexes
            actions     NVARCHAR(MAX) NULL,   -- JSON array of captured actions
            recap_html  NVARCHAR(MAX) NULL,   -- recap exactly as it was sent
            started_at  DATETIME2     NULL,
            ended_at    DATETIME2     NULL,
            updated_at  DATETIME2     NOT NULL,
            created_by  NVARCHAR(200) NULL
        )
    """)
    # Additive migration: tables created before recap_html existed.
    cursor.execute("""
        IF NOT EXISTS (
            SELECT 1 FROM sys.columns
            WHERE object_id = OBJECT_ID('impactmgr.meetings') AND name = 'recap_html'
        )
        ALTER TABLE impactmgr.meetings ADD recap_html NVARCHAR(MAX) NULL
    """)


def _row_to_meeting(row, cols):
    r = dict(zip(cols, row))
    for k in ('facilities', 'completed', 'actions'):
        if r.get(k):
            try:
                r[k] = json.loads(r[k])
            except (ValueError, TypeError):
                r[k] = []
        else:
            r[k] = []
    for k in ('started_at', 'ended_at', 'updated_at'):
        if r.get(k) is not None and hasattr(r[k], 'isoformat'):
            r[k] = r[k].isoformat()
    return r


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
            meeting_id = req.params.get('id')
            if meeting_id:
                cursor.execute("""
                    SELECT meeting_id, health_system, facilities, recipients, stage,
                           completed, actions, started_at, ended_at, updated_at, created_by,
                           recap_html
                    FROM impactmgr.meetings WHERE meeting_id = ?
                """, meeting_id)
                row = cursor.fetchone()
                cols = [c[0] for c in cursor.description]
                if not row:
                    return func.HttpResponse(
                        json.dumps({'error': 'not_found'}),
                        mimetype='application/json', status_code=404)
                return func.HttpResponse(
                    json.dumps(_row_to_meeting(row, cols), default=str),
                    mimetype='application/json', status_code=200)

            # Recent meetings, newest first — the workspace offers to resume an
            # unfinished one rather than silently starting a second.
            try:
                limit = min(int(req.params.get('limit') or 20), 100)
            except (TypeError, ValueError):
                limit = 20
            cursor.execute(f"""
                SELECT TOP {limit} meeting_id, health_system, facilities, recipients, stage,
                       completed, actions, started_at, ended_at, updated_at, created_by
                FROM impactmgr.meetings ORDER BY started_at DESC, updated_at DESC
            """)
            cols = [c[0] for c in cursor.description]
            rows = [_row_to_meeting(r, cols) for r in cursor.fetchall()]
            return func.HttpResponse(
                json.dumps(rows, default=str),
                mimetype='application/json', status_code=200)

        body = req.get_json() or {}
        meeting_id = str(body.get('meetingId') or '').strip()
        if not meeting_id:
            return func.HttpResponse(
                json.dumps({'error': 'meetingId required'}),
                mimetype='application/json', status_code=400)

        actions = json.dumps(body.get('actions') or [], default=str)
        recap_html = body.get('recapHtml')
        if len(actions.encode('utf-8')) > MAX_ACTIONS_BYTES:
            return func.HttpResponse(
                json.dumps({'error': 'actions payload too large'}),
                mimetype='application/json', status_code=400)

        now = datetime.datetime.utcnow()

        def _dt(v):
            if not v:
                return None
            try:
                return datetime.datetime.fromisoformat(str(v).replace('Z', '+00:00')).replace(tzinfo=None)
            except ValueError:
                return None

        # MERGE so the client can save repeatedly through a meeting — each
        # stage completion overwrites rather than inserting a new row.
        cursor.execute("""
            MERGE impactmgr.meetings AS t
            USING (SELECT ? AS meeting_id) AS src ON t.meeting_id = src.meeting_id
            WHEN MATCHED THEN UPDATE SET
                health_system = ?, facilities = ?, recipients = ?, stage = ?,
                completed = ?, actions = ?, started_at = ?, ended_at = ?, updated_at = ?,
                recap_html = COALESCE(?, recap_html)
            WHEN NOT MATCHED THEN
                INSERT (meeting_id, health_system, facilities, recipients, stage,
                        completed, actions, started_at, ended_at, updated_at, created_by,
                        recap_html)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
            meeting_id,
            body.get('healthSystem'), json.dumps(body.get('facilities') or []),
            body.get('recipients'), body.get('stage'),
            json.dumps(body.get('completed') or []), actions,
            _dt(body.get('startedAt')), _dt(body.get('endedAt')), now, recap_html,
            meeting_id,
            body.get('healthSystem'), json.dumps(body.get('facilities') or []),
            body.get('recipients'), body.get('stage'),
            json.dumps(body.get('completed') or []), actions,
            _dt(body.get('startedAt')), _dt(body.get('endedAt')), now, user, recap_html)
        conn.commit()
        print(f'Meetings: saved {meeting_id} ({len(body.get("actions") or [])} actions) for {user}')
        return func.HttpResponse(
            json.dumps({'saved': meeting_id}),
            mimetype='application/json', status_code=200)

    except Exception as e:
        print(f'Meetings error: {e}')
        import traceback
        traceback.print_exc()
        return func.HttpResponse(
            json.dumps({'error': str(e)}),
            mimetype='application/json', status_code=500)
    finally:
        conn.close()
