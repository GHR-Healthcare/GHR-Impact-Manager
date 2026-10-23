"""
One place to record who changed configuration.

Settings in this app are deliberately open -- anyone who can sign in can edit
the client allowlist, the system mappings and the PM mappings. That is the
intended model, so the control is not authorisation but attribution: every
config write lands in impactmgr.changes with the signed-in principal against
it, giving one queryable timeline of who changed what and when.

impactmgr.changes already exists for the job-level change log, and
WorkspaceState has mirrored into it since 2.x. This module is that code,
factored out so the config endpoints record changes the same way rather than
each inventing its own.
"""
import datetime
import json

# impactmgr.changes.id is NVARCHAR(100). A composite id built from a long
# entity id used to exceed it, and because the audit write is best-effort the
# row was silently dropped -- the change happened with nothing recorded against
# it. Truncating keeps the row; the timestamp tail is what makes it unique, so
# it is preserved by trimming the entity id rather than the whole string.
_MAX_ID = 100


def _change_id(change_type, entity_id, ts_iso):
    tail = f':{ts_iso}'
    head = f'{change_type}:{entity_id}'
    room = _MAX_ID - len(tail)
    return (head[:room] if room > 0 else head[:_MAX_ID]) + tail[:_MAX_ID]


def record_change(cursor, change_type, entity_id, detail, user):
    """Append one row to impactmgr.changes. Never raises.

    change_type  a dotted name, e.g. 'config.system_mappings'
    entity_id    what was changed; '' when the write replaces a whole set
    detail       JSON-serialisable summary, truncated to fit the column
    user         the signed-in principal, from auth.current_user_email(req)
    """
    try:
        ts = datetime.datetime.utcnow()
        cursor.execute("""
            IF EXISTS (SELECT 1 FROM sys.tables
                       WHERE name = 'changes' AND schema_id = SCHEMA_ID('impactmgr'))
            INSERT INTO impactmgr.changes
                (id, timestamp, jobid, change_type, change_data, user_name)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            _change_id(change_type, entity_id, ts.isoformat()),
            ts,
            str(entity_id)[:100],
            change_type[:100],
            json.dumps(detail, default=str)[:4000],
            (user or 'unknown')[:200],
        )
    except Exception as e:
        # Best-effort: a failure to log must never fail the write it describes.
        print(f'audit: {change_type} write skipped: {e}')
