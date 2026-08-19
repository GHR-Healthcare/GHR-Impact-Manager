import azure.functions as func
import pyodbc
import os
import json
import datetime
from shared_code.auth import require_allowed_domain, current_user_email


"""
Daily snapshot of assignment state, written to the warehouse.

Two tables, one per VMS, so each side stays queryable on its own terms and
other apps can read them without going through this one:

    dhc.B4HealthOrder_Snapshot
    dbo.STAGING_VNDLY_WORKORDERS_Snapshot

Neither VMS records how far a start moved or how many times. B4's movement
history came from Bullhorn placement matching, which is off the MSP path, and
VNDLY has no Original Start Date at all — so "Moved 2+ Times" and "Avg Days
Delayed" have nothing to compute against.

One row per assignment per day. Once a start date changes between two
snapshots the movement is measurable: distinct Start_Date per assignment is
the move count, first-seen versus current is the slip.

Forward-only by nature — the first numbers appear the day after a start
actually moves, and improve the longer it runs. Same mechanism
HIST_B4HealthOrder used, on tables the warehouse owns.

Idempotent: MERGE on (key, Snapshot_Date), so repeat calls within a day update
rather than accumulate and the caller needs no coordination.

REQUIRES A GRANT. svc-impact-manager is db_datareader on ghrdhc and nothing
else, so until a DBA runs migrations/2026-08-19_assignment_snapshots.sql this
returns 403 naming what is missing. Deliberately no DDL in the app — the
tables belong to the warehouse, this only appends to them.
"""

SNAPSHOT_TARGETS = {
    'B4': {
        'table': 'dhc.B4HealthOrder_Snapshot',
        'key': 'Contract_ID',
        'select': """
            SELECT
                LTRIM(RTRIM(Contract_ID))               AS k,
                CAST(Start_Date AS DATE)                AS start_date,
                CAST(End_Date AS DATE)                  AS end_date,
                Contract_Status                         AS status,
                Health_System                           AS health_system,
                Agency                                  AS agency,
                TRY_CAST(Awarded_Rate AS DECIMAL(10,2)) AS rate,
                Delayed_Starts                          AS delayed_flag
            FROM dhc.B4HealthOrder WITH (NOLOCK)
            WHERE Start_Date >= DATEADD(DAY, -90, CAST(GETDATE() AS DATE))
               OR End_Date   >= CAST(GETDATE() AS DATE)
        """,
        'merge': """
            MERGE dhc.B4HealthOrder_Snapshot AS t
            USING (SELECT ? AS Contract_ID, ? AS Snapshot_Date) AS s
              ON t.Contract_ID = s.Contract_ID AND t.Snapshot_Date = s.Snapshot_Date
            WHEN MATCHED THEN UPDATE SET
                Start_Date = ?, End_Date = ?, Contract_Status = ?, Health_System = ?,
                Agency = ?, Awarded_Rate = ?, Delayed_Starts = ?, Captured_At = ?
            WHEN NOT MATCHED THEN
                INSERT (Contract_ID, Snapshot_Date, Start_Date, End_Date, Contract_Status,
                        Health_System, Agency, Awarded_Rate, Delayed_Starts, Captured_At)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        'has_delayed_flag': True,
    },
    'VNDLY': {
        'table': 'dbo.STAGING_VNDLY_WORKORDERS_Snapshot',
        'key': 'WOSystemKey',
        'select': """
            SELECT
                CAST(WOSystemKey AS NVARCHAR(120))       AS k,
                TRY_CAST([Start Date] AS DATE)           AS start_date,
                TRY_CAST([End Date] AS DATE)             AS end_date,
                [Current Status]                         AS status,
                [Health System]                          AS health_system,
                [Vendor Name]                            AS agency,
                TRY_CAST([Bill Rate] AS DECIMAL(10,2))   AS rate,
                NULL                                     AS delayed_flag
            FROM dbo.STAGING_VNDLY_WORKORDERS WITH (NOLOCK)
            WHERE TRY_CAST([Start Date] AS DATE) >= DATEADD(DAY, -90, CAST(GETDATE() AS DATE))
               OR TRY_CAST([End Date] AS DATE)   >= CAST(GETDATE() AS DATE)
        """,
        'merge': """
            MERGE dbo.STAGING_VNDLY_WORKORDERS_Snapshot AS t
            USING (SELECT ? AS WOSystemKey, ? AS Snapshot_Date) AS s
              ON t.WOSystemKey = s.WOSystemKey AND t.Snapshot_Date = s.Snapshot_Date
            WHEN MATCHED THEN UPDATE SET
                Start_Date = ?, End_Date = ?, Current_Status = ?, Health_System = ?,
                Vendor_Name = ?, Bill_Rate = ?, Captured_At = ?
            WHEN NOT MATCHED THEN
                INSERT (WOSystemKey, Snapshot_Date, Start_Date, End_Date, Current_Status,
                        Health_System, Vendor_Name, Bill_Rate, Captured_At)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        'has_delayed_flag': False,
    },
}

MIGRATION = 'migrations/2026-08-19_assignment_snapshots.sql'


def _conn():
    return pyodbc.connect(
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={os.environ['DB_HOST']};"
        f"DATABASE={os.environ['POSITIONS_DB']};"
        f"UID={os.environ['DB_USER']};"
        f"PWD={os.environ['DB_PASSWORD']};"
        f"TrustServerCertificate=yes"
    )


def _table_ready(cursor, table):
    """True when the table exists and this login can write to it."""
    cursor.execute("""
        SELECT CASE WHEN OBJECT_ID(?) IS NULL THEN 0 ELSE 1 END,
               ISNULL(HAS_PERMS_BY_NAME(?, 'OBJECT', 'INSERT'), 0)
    """, table, table)
    exists, can_insert = cursor.fetchone()
    return bool(exists), bool(can_insert)


def _capture(cursor, cfg, today, now):
    cursor.execute(cfg['select'])
    cols = [c[0] for c in cursor.description]
    rows = [dict(zip(cols, r)) for r in cursor.fetchall()]
    written = 0
    for r in rows:
        key = (r.get('k') or '').strip()
        if not key:
            continue
        vals = [r.get('start_date'), r.get('end_date'), r.get('status'),
                r.get('health_system'), r.get('agency'), r.get('rate')]
        if cfg['has_delayed_flag']:
            vals.append(r.get('delayed_flag'))
        vals.append(now)
        # MERGE needs the values twice — once for UPDATE, once for INSERT.
        cursor.execute(cfg['merge'], key, today, *vals, key, today, *vals)
        written += 1
    return written


def main(req: func.HttpRequest) -> func.HttpResponse:
    auth_error = require_allowed_domain(req)
    if auth_error:
        return auth_error

    conn = None
    try:
        conn = _conn()
        cursor = conn.cursor()
        cursor.execute('SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED')

        today = datetime.date.today()
        now = datetime.datetime.utcnow()

        readiness = {}
        for label, cfg in SNAPSHOT_TARGETS.items():
            exists, can_insert = _table_ready(cursor, cfg['table'])
            readiness[label] = {'table': cfg['table'], 'exists': exists, 'writable': can_insert}

        missing = [r for r in readiness.values() if not (r['exists'] and r['writable'])]
        if missing:
            # Say exactly what is missing and how to fix it, rather than
            # failing with a permissions error nobody can act on.
            return func.HttpResponse(json.dumps({
                'error': 'snapshot_tables_not_ready',
                'detail': 'The warehouse snapshot tables do not exist or are not writable '
                          'by this login.',
                'run': MIGRATION,
                'tables': readiness,
            }, default=str), mimetype='application/json', status_code=403)

        if req.method == 'GET':
            out = {}
            for label, cfg in SNAPSHOT_TARGETS.items():
                cursor.execute(f"""
                    SELECT COUNT(DISTINCT Snapshot_Date), MIN(Snapshot_Date),
                           SUM(CASE WHEN Snapshot_Date = ? THEN 1 ELSE 0 END)
                    FROM {cfg['table']}
                """, today)
                days, first, today_rows = cursor.fetchone()
                out[label] = {
                    'daysCaptured': days or 0,
                    'firstDay': first.isoformat() if first else None,
                    'rowsToday': today_rows or 0,
                }
            # Movement needs two days to compare.
            out['capturedToday'] = all(v['rowsToday'] > 0 for v in out.values()
                                       if isinstance(v, dict))
            out['movementAvailable'] = all(v['daysCaptured'] >= 2 for v in out.values()
                                           if isinstance(v, dict))
            return func.HttpResponse(json.dumps(out, default=str),
                                     mimetype='application/json', status_code=200)

        captured, errors = {}, []
        for label, cfg in SNAPSHOT_TARGETS.items():
            try:
                captured[label] = _capture(cursor, cfg, today, now)
            except Exception as e:
                print(f'Snapshot: {label} failed: {e}')
                import traceback
                traceback.print_exc()
                errors.append(f'{label}: {e}')
        conn.commit()

        print(f'Snapshot {today}: {captured} (errors: {errors or "none"}) '
              f'by {current_user_email(req)}')
        return func.HttpResponse(
            json.dumps({'snapshotDate': str(today), 'captured': captured, 'errors': errors}),
            mimetype='application/json', status_code=200)

    except Exception as e:
        print(f'Snapshot error: {e}')
        import traceback
        traceback.print_exc()
        return func.HttpResponse(
            json.dumps({'error': str(e)}),
            mimetype='application/json', status_code=500)
    finally:
        if conn is not None:
            conn.close()
