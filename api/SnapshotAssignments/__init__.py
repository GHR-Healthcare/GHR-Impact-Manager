import azure.functions as func
import pyodbc
import os
import json
import datetime
from shared_code.auth import require_allowed_domain, current_user_email
from shared_code.data_source import get_appdb_conn


"""
Daily snapshot of assignment state.

Neither VMS records how far a start moved or how many times. B4's movement
history came from Bullhorn placement matching, which is off the MSP path, and
VNDLY has no Original Start Date at all — so "Moved 2+ Times" and "Avg Days
Delayed" have nothing to compute against.

This writes one row per assignment per day. Once a start date changes between
two snapshots, the movement becomes measurable: distinct start_date values per
assignment is the move count, and first-seen versus current is the slip.

It is forward-only by nature — the first useful numbers appear the day after a
start actually moves, and the metrics get better the longer it runs. That is
the same mechanism HIST_B4HealthOrder used, kept inside data we own.

Idempotent: MERGE on (entity_id, snapshot_date), so repeated calls in a day
update rather than accumulate, and the caller does not need to coordinate.
"""

SNAPSHOT_SOURCES = {
    'B4': """
        SELECT
            'B4'                                       AS source_system,
            LTRIM(RTRIM(Contract_ID))                  AS entity_id,
            CAST(Start_Date AS DATE)                   AS start_date,
            CAST(End_Date AS DATE)                     AS end_date,
            Contract_Status                            AS status,
            Health_System                              AS health_system,
            Agency                                     AS agency,
            TRY_CAST(Awarded_Rate AS DECIMAL(10,2))    AS bill_rate,
            Delayed_Starts                             AS delayed_flag
        FROM dhc.B4HealthOrder WITH (NOLOCK)
        WHERE Start_Date >= DATEADD(DAY, -90, CAST(GETDATE() AS DATE))
           OR End_Date   >= CAST(GETDATE() AS DATE)
    """,
    'VNDLY': """
        SELECT
            'VNDLY'                                    AS source_system,
            CAST(WOSystemKey AS NVARCHAR(120))         AS entity_id,
            TRY_CAST([Start Date] AS DATE)             AS start_date,
            TRY_CAST([End Date] AS DATE)               AS end_date,
            [Current Status]                           AS status,
            [Health System]                            AS health_system,
            [Vendor Name]                              AS agency,
            TRY_CAST([Bill Rate] AS DECIMAL(10,2))     AS bill_rate,
            NULL                                       AS delayed_flag
        FROM dbo.STAGING_VNDLY_WORKORDERS WITH (NOLOCK)
        WHERE TRY_CAST([Start Date] AS DATE) >= DATEADD(DAY, -90, CAST(GETDATE() AS DATE))
           OR TRY_CAST([End Date] AS DATE)   >= CAST(GETDATE() AS DATE)
    """,
}


def _ensure_schema(cursor):
    cursor.execute("""
        IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'impactmgr')
            EXEC('CREATE SCHEMA impactmgr')
    """)
    cursor.execute("""
        IF NOT EXISTS (
            SELECT 1 FROM sys.tables
            WHERE name = 'assignment_snapshots' AND schema_id = SCHEMA_ID('impactmgr')
        )
        CREATE TABLE impactmgr.assignment_snapshots (
            entity_id     NVARCHAR(120) NOT NULL,
            snapshot_date DATE          NOT NULL,
            source_system NVARCHAR(20)  NOT NULL,
            start_date    DATE          NULL,
            end_date      DATE          NULL,
            status        NVARCHAR(100) NULL,
            health_system NVARCHAR(200) NULL,
            agency        NVARCHAR(200) NULL,
            bill_rate     DECIMAL(10,2) NULL,
            delayed_flag  NVARCHAR(20)  NULL,
            captured_at   DATETIME2     NOT NULL,
            CONSTRAINT PK_assignment_snapshots PRIMARY KEY (entity_id, snapshot_date)
        )
    """)
    # Movement queries read by entity across dates, so index that way.
    cursor.execute("""
        IF NOT EXISTS (SELECT 1 FROM sys.indexes
                       WHERE name = 'IX_assignment_snapshots_entity'
                         AND object_id = OBJECT_ID('impactmgr.assignment_snapshots'))
        CREATE INDEX IX_assignment_snapshots_entity
            ON impactmgr.assignment_snapshots (entity_id, start_date)
    """)


def _positions_conn():
    return pyodbc.connect(
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={os.environ['DB_HOST']};"
        f"DATABASE={os.environ['POSITIONS_DB']};"
        f"UID={os.environ['DB_USER']};"
        f"PWD={os.environ['DB_PASSWORD']};"
        f"TrustServerCertificate=yes"
    )


def _capture(src_cursor, app_cursor, label, query, today, now):
    src_cursor.execute(query)
    cols = [c[0] for c in src_cursor.description]
    rows = [dict(zip(cols, r)) for r in src_cursor.fetchall()]
    written = 0
    for r in rows:
        entity = (r.get('entity_id') or '').strip()
        if not entity:
            continue
        app_cursor.execute("""
            MERGE impactmgr.assignment_snapshots AS t
            USING (SELECT ? AS entity_id, ? AS snapshot_date) AS src
              ON t.entity_id = src.entity_id AND t.snapshot_date = src.snapshot_date
            WHEN MATCHED THEN UPDATE SET
                source_system = ?, start_date = ?, end_date = ?, status = ?,
                health_system = ?, agency = ?, bill_rate = ?, delayed_flag = ?,
                captured_at = ?
            WHEN NOT MATCHED THEN
                INSERT (entity_id, snapshot_date, source_system, start_date, end_date,
                        status, health_system, agency, bill_rate, delayed_flag, captured_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
            entity, today,
            r.get('source_system'), r.get('start_date'), r.get('end_date'), r.get('status'),
            r.get('health_system'), r.get('agency'), r.get('bill_rate'), r.get('delayed_flag'), now,
            entity, today,
            r.get('source_system'), r.get('start_date'), r.get('end_date'), r.get('status'),
            r.get('health_system'), r.get('agency'), r.get('bill_rate'), r.get('delayed_flag'), now)
        written += 1
    return written


def main(req: func.HttpRequest) -> func.HttpResponse:
    auth_error = require_allowed_domain(req)
    if auth_error:
        return auth_error

    app_conn = get_appdb_conn()
    if app_conn is None:
        return func.HttpResponse(
            json.dumps({'error': 'appdb_not_configured'}),
            mimetype='application/json', status_code=503)

    src_conn = None
    try:
        app_cursor = app_conn.cursor()
        _ensure_schema(app_cursor)
        app_conn.commit()

        today = datetime.date.today()
        now = datetime.datetime.utcnow()

        # GET reports coverage without writing, so the client can decide whether
        # today's snapshot is already in place before doing any work.
        if req.method == 'GET':
            app_cursor.execute("""
                SELECT COUNT(*) AS rows_today FROM impactmgr.assignment_snapshots
                WHERE snapshot_date = ?
            """, today)
            rows_today = app_cursor.fetchone()[0]
            app_cursor.execute("""
                SELECT COUNT(DISTINCT snapshot_date) AS days,
                       MIN(snapshot_date) AS first_day,
                       COUNT(*) AS total_rows
                FROM impactmgr.assignment_snapshots
            """)
            days, first_day, total = app_cursor.fetchone()
            return func.HttpResponse(json.dumps({
                'capturedToday': rows_today > 0,
                'rowsToday': rows_today,
                'daysCaptured': days or 0,
                'firstDay': first_day.isoformat() if first_day else None,
                'totalRows': total or 0,
                # Movement is only measurable once there are two days to compare.
                'movementAvailable': (days or 0) >= 2,
            }, default=str), mimetype='application/json', status_code=200)

        src_conn = _positions_conn()
        src_cursor = src_conn.cursor()
        src_cursor.execute('SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED')

        captured, errors = {}, []
        for label, query in SNAPSHOT_SOURCES.items():
            try:
                captured[label] = _capture(src_cursor, app_cursor, label, query, today, now)
            except Exception as e:
                print(f'Snapshot: {label} failed: {e}')
                import traceback
                traceback.print_exc()
                errors.append(f'{label}: {e}')
        app_conn.commit()

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
        if src_conn is not None:
            src_conn.close()
        app_conn.close()
