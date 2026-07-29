import azure.functions as func
import pyodbc
import os
import json
from datetime import datetime, timedelta
from shared_code.auth import require_allowed_domain
from shared_code.data_source import is_non_msp, get_bullhorn_conn, get_symplr_conn, get_appdb_conn
from shared_code.bullhorn_systems import (
    build_system_case_expr,
    build_scope_filter,
    resolve_scope_client_ids,
)
from shared_code.symplr_systems import (
    build_system_case_expr as symplr_system_case_expr,
    build_scope_filter as symplr_scope_filter,
    build_division_case_expr as symplr_division_case_expr,
    resolve_scope_master_ids as symplr_resolve_scope,
)


# Statuses where the placement is awarded and actively in play. Per spec §5
# there is no pre-active funnel for non-MSP — future-dated 'Approved' /
# 'Pending Start' / etc. are the pipeline. Completed/Termination are excluded
# here because Stats is a snapshot of currently-in-play work, not history.
BULLHORN_ACTIVE_SNAPSHOT_STATUSES = (
    'Approved', 'Pending Start', 'Cleared', 'Onboarding', 'Started',
)


def _bullhorn_stats_data():
    """Returns (on_assignment[], upcoming[]) for the Bullhorn book. Raises on error."""
    conn = get_bullhorn_conn()
    cursor = conn.cursor()
    app_conn = get_appdb_conn()
    try:
        scope_ids = resolve_scope_client_ids(cursor, app_conn)
    finally:
        if app_conn is not None:
            app_conn.close()
    system_case = build_system_case_expr('p.clientCorporationID')
    scope_filter = build_scope_filter('p.clientCorporationID', client_ids=scope_ids)
    status_list = ', '.join("'" + s + "'" for s in BULLHORN_ACTIVE_SNAPSHOT_STATUSES)

    def _fetch_rows(date_clause):
        cursor.execute(f'''
            SELECT
                'Bullhorn' AS source_system,
                CAST(p.placementID AS NVARCHAR(50)) AS position_id,
                LTRIM(RTRIM(ISNULL(c.firstName, '') + ' ' + ISNULL(c.lastName, ''))) AS candidate_name,
                'GHR' AS agency,
                cc.name AS facility,
                ({system_case}) AS system,
                p.customText1 AS specialty,
                -- Division lives on the client (see GetTrendData note).
                cc.customTextBlock1 AS division,
                NULL AS region,
                CAST(p.dateBegin AS DATE) AS startDate,
                CAST(p.dateEnd AS DATE) AS endDate,
                p.status AS status
            FROM dbo.View_Placement p
            LEFT JOIN dbo.View_Candidate c ON p.candidateID = c.candidateID
            LEFT JOIN dbo.View_ClientCorporation cc ON p.clientCorporationID = cc.clientCorporationID
            WHERE p.isDeleted = 0
                AND p.status IN ({status_list})
                AND p.dateBegin IS NOT NULL
                AND {date_clause}
                AND {scope_filter}
        ''')
        columns = [column[0] for column in cursor.description]
        out = []
        for row in cursor.fetchall():
            row_dict = dict(zip(columns, row))
            if row_dict.get('startDate'):
                row_dict['startDate'] = row_dict['startDate'].isoformat() if hasattr(row_dict['startDate'], 'isoformat') else str(row_dict['startDate'])
            if row_dict.get('endDate'):
                row_dict['endDate'] = row_dict['endDate'].isoformat() if hasattr(row_dict['endDate'], 'isoformat') else str(row_dict['endDate'])
            out.append(row_dict)
        return out

    on_assignment = _fetch_rows("p.dateBegin <= GETDATE() AND (p.dateEnd IS NULL OR p.dateEnd >= GETDATE())")
    upcoming = _fetch_rows("p.dateBegin > GETDATE() AND p.dateBegin <= DATEADD(DAY, 30, GETDATE())")
    conn.close()
    return on_assignment, upcoming


def _symplr_stats_data():
    """Returns (on_assignment[], upcoming[]) for the Symplr book. Raises on error.

    Two sources are unioned: lt_order (multi-week placements) and orderless
    filled orders (per-shift bookings with lt_orderid IN (0, NULL)) — the
    latter aggregated by worker+client so each shift series collapses to one
    assignment row.
    """
    conn = get_symplr_conn()
    if conn is None:
        return [], []
    cursor = conn.cursor()
    app_conn = get_appdb_conn()
    try:
        symplr_master_ids = symplr_resolve_scope(app_conn)
    finally:
        if app_conn is not None:
            app_conn.close()
    sys_case = symplr_system_case_expr('lt.clientid')
    scope = symplr_scope_filter('lt.clientid', master_ids=symplr_master_ids)
    division_case = symplr_division_case_expr('lt.clientid')
    sys_case_orders = symplr_system_case_expr('o.customerid')
    scope_orders = symplr_scope_filter('o.customerid', master_ids=symplr_master_ids)
    division_case_orders = symplr_division_case_expr('o.customerid')

    def _serialize(row_dict):
        if row_dict.get('startDate'):
            row_dict['startDate'] = row_dict['startDate'].isoformat() if hasattr(row_dict['startDate'], 'isoformat') else str(row_dict['startDate'])
        if row_dict.get('endDate'):
            row_dict['endDate'] = row_dict['endDate'].isoformat() if hasattr(row_dict['endDate'], 'isoformat') else str(row_dict['endDate'])
        return row_dict

    def _fetch_lt_rows(date_clause):
        cursor.execute(f'''
            SELECT
                'Symplr' AS source_system,
                CAST(lt.lt_orderid AS NVARCHAR(50)) AS position_id,
                LTRIM(RTRIM(ISNULL(pt.firstname, '') + ' ' + ISNULL(pt.lastname, ''))) AS candidate_name,
                'GHR' AS agency,
                pc.clientname AS facility,
                ({sys_case}) AS system,
                lt.specialty AS specialty,
                ({division_case}) AS division,
                pc.state AS region,
                CAST(lt.date_start AS DATE) AS startDate,
                CAST(lt.date_end AS DATE) AS endDate,
                lt.status AS status
            FROM dbo.lt_order lt
            LEFT JOIN dbo.profile_client pc ON lt.clientid = pc.recordid
            LEFT JOIN dbo.profile_temp pt ON lt.tempid = pt.recordid
            WHERE lt.status = 'filled'
                AND lt.date_start IS NOT NULL
                AND {date_clause}
                AND {scope}
        ''')
        columns = [column[0] for column in cursor.description]
        return [_serialize(dict(zip(columns, row))) for row in cursor.fetchall()]

    def _fetch_order_rows(date_clause):
        """Orderless filled orders aggregated by worker+client.
        `date_clause` filters individual order shifts (e.g. happening now or
        starting in the next 30 days); aggregation produces one row per
        worker-client with min/max dates across qualifying shifts."""
        cursor.execute(f'''
            SELECT
                'Symplr' AS source_system,
                CAST(o.customerid AS NVARCHAR(50)) AS position_id,
                LTRIM(RTRIM(ISNULL(MAX(pt.firstname), '') + ' ' + ISNULL(MAX(pt.lastname), ''))) AS candidate_name,
                'GHR' AS agency,
                MAX(pc.clientname) AS facility,
                ({sys_case_orders}) AS system,
                MAX(o.specialty) AS specialty,
                ({division_case_orders}) AS division,
                MAX(pc.state) AS region,
                CAST(MIN(o.jobdatestart) AS DATE) AS startDate,
                CAST(MAX(o.jobdateend)   AS DATE) AS endDate,
                'filled' AS status
            FROM dbo.orders o
            LEFT JOIN dbo.profile_client pc ON o.customerid = pc.recordid
            LEFT JOIN dbo.profile_temp   pt ON o.filledby   = pt.recordid
            WHERE o.status = 'filled'
                AND (o.lt_orderid IS NULL OR o.lt_orderid = 0)
                AND o.filledby IS NOT NULL AND o.filledby > 0
                AND o.jobdatestart IS NOT NULL
                AND {date_clause}
                AND {scope_orders}
            GROUP BY o.customerid, o.filledby
        ''')
        columns = [column[0] for column in cursor.description]
        return [_serialize(dict(zip(columns, row))) for row in cursor.fetchall()]

    on_assignment = (
        _fetch_lt_rows("lt.date_start <= GETDATE() AND (lt.date_end IS NULL OR lt.date_end >= GETDATE())")
        + _fetch_order_rows("o.jobdatestart <= GETDATE() AND (o.jobdateend IS NULL OR o.jobdateend >= GETDATE())")
    )
    upcoming = (
        _fetch_lt_rows("lt.date_start > GETDATE() AND lt.date_start <= DATEADD(DAY, 30, GETDATE())")
        + _fetch_order_rows("o.jobdatestart > GETDATE() AND o.jobdatestart <= DATEADD(DAY, 30, GETDATE())")
    )
    conn.close()
    return on_assignment, upcoming


def _non_msp_stats(req: func.HttpRequest) -> func.HttpResponse:
    """Run Bullhorn + Symplr stats queries independently, union the results."""
    on_assignment = []
    upcoming = []
    errors = []
    try:
        a, b = _bullhorn_stats_data()
        on_assignment.extend(a); upcoming.extend(b)
    except Exception as e:
        print(f"Bullhorn stats error: {e}")
        import traceback; traceback.print_exc()
        errors.append(f"bullhorn: {e}")
    try:
        a, b = _symplr_stats_data()
        on_assignment.extend(a); upcoming.extend(b)
    except Exception as e:
        print(f"Symplr stats error: {e}")
        import traceback; traceback.print_exc()
        errors.append(f"symplr: {e}")
    print(f"non-MSP stats: {len(on_assignment)} active, {len(upcoming)} upcoming (errors: {errors or 'none'})")
    return func.HttpResponse(
        json.dumps({'onAssignment': on_assignment, 'upcoming': upcoming}, default=str),
        mimetype="application/json",
        status_code=200,
    )


def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    Returns stats data for:
    - onAssignment: Currently active work orders/assignments
    - upcoming: New starts in the near future
    
    Combines data from both B4Health and VNDLY systems.
    """
    auth_error = require_allowed_domain(req)
    if auth_error:
        return auth_error

    if is_non_msp():
        return _non_msp_stats(req)

    try:
        conn = pyodbc.connect(
            f"DRIVER={{ODBC Driver 17 for SQL Server}};"
            f"SERVER={os.environ['DB_HOST']};"
            f"DATABASE={os.environ['POSITIONS_DB']};"
            f"UID={os.environ['DB_USER']};"
            f"PWD={os.environ['DB_PASSWORD']};"
            f"TrustServerCertificate=yes"
        )
        
        cursor = conn.cursor()
        on_assignment = []
        upcoming = []
        
        # ============================================================
        # B4Health - Active Assignments
        # ============================================================
        try:
            cursor.execute('''
                SELECT
                    'B4' AS source_system,
                    Contract_ID AS position_id,
                    CONCAT(First_Name, ' ', Last_Name) AS candidate_name,
                    Agency AS agency,
                    Facility AS facility,
                    Health_System AS system,
                    Care_Type AS specialty,
                    Start_Date AS startDate,
                    End_Date AS endDate,
                    Contract_Status AS status
                FROM dhc.B4HealthOrder
                WHERE Contract_Status = 'Closed And Awarded'
                    AND Start_Date IS NOT NULL
                    AND Start_Date <= GETDATE()
                    AND (End_Date IS NULL OR End_Date >= GETDATE())
                    AND Health_System NOT LIKE '%Richmond University%'
                    AND Health_System NOT LIKE '%Redeemer%'
                    AND Health_System <> 'Sunrise Senior Living Management (California)'
            ''')

            columns = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                # Convert dates to ISO format
                if row_dict.get('startDate'):
                    row_dict['startDate'] = row_dict['startDate'].isoformat() if hasattr(row_dict['startDate'], 'isoformat') else str(row_dict['startDate'])
                if row_dict.get('endDate'):
                    row_dict['endDate'] = row_dict['endDate'].isoformat() if hasattr(row_dict['endDate'], 'isoformat') else str(row_dict['endDate'])
                on_assignment.append(row_dict)
        except Exception as e:
            print(f"Error loading B4 active assignments: {e}")
        
        # ============================================================
        # B4Health - Upcoming Starts
        # ============================================================
        try:
            cursor.execute('''
                SELECT
                    'B4' AS source_system,
                    Contract_ID AS position_id,
                    CONCAT(First_Name, ' ', Last_Name) AS candidate_name,
                    Agency AS agency,
                    Facility AS facility,
                    Health_System AS system,
                    Care_Type AS specialty,
                    Start_Date AS startDate,
                    End_Date AS endDate,
                    Contract_Status AS status
                FROM dhc.B4HealthOrder
                WHERE Contract_Status = 'Closed And Awarded'
                    AND Start_Date IS NOT NULL
                    AND Start_Date > GETDATE()
                    AND Start_Date <= DATEADD(day, 30, GETDATE())
                    AND Health_System NOT LIKE '%Richmond University%'
                    AND Health_System NOT LIKE '%Redeemer%'
                    AND Health_System <> 'Sunrise Senior Living Management (California)'
            ''')
            
            columns = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                if row_dict.get('startDate'):
                    row_dict['startDate'] = row_dict['startDate'].isoformat() if hasattr(row_dict['startDate'], 'isoformat') else str(row_dict['startDate'])
                if row_dict.get('endDate'):
                    row_dict['endDate'] = row_dict['endDate'].isoformat() if hasattr(row_dict['endDate'], 'isoformat') else str(row_dict['endDate'])
                upcoming.append(row_dict)
        except Exception as e:
            print(f"Error loading B4 upcoming: {e}")
        
        # ============================================================
        # VNDLY - Active Assignments (from Work Orders)
        # ============================================================
        try:
            cursor.execute('''
                SELECT
                    'VNDLY' AS source_system,
                    CAST([Work Order Id] AS NVARCHAR(50)) AS position_id,
                    CONCAT([Contractor First Name], ' ', [Contractor Last Name]) AS candidate_name,
                    [Vendor Name] AS agency,
                    [Default Work Site Name] AS facility,
                    [Health System] AS system,
                    [Job Title] AS specialty,
                    [Start Date] AS startDate,
                    [End Date] AS endDate,
                    [Current Status] AS status
                FROM dbo.STAGING_VNDLY_WORKORDERS
                WHERE [Current Status] = 'Active'
                    AND [Start Date] IS NOT NULL
                    AND [Start Date] <= GETDATE()
                    AND ([End Date] IS NULL OR [End Date] >= GETDATE())
            ''')
            
            columns = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                if row_dict.get('startDate'):
                    row_dict['startDate'] = row_dict['startDate'].isoformat() if hasattr(row_dict['startDate'], 'isoformat') else str(row_dict['startDate'])
                if row_dict.get('endDate'):
                    row_dict['endDate'] = row_dict['endDate'].isoformat() if hasattr(row_dict['endDate'], 'isoformat') else str(row_dict['endDate'])
                on_assignment.append(row_dict)
        except Exception as e:
            print(f"Error loading VNDLY active assignments: {e}")
        
        # ============================================================
        # VNDLY - Upcoming Starts (confirmed but not yet started)
        # ============================================================
        try:
            cursor.execute('''
                SELECT
                    'VNDLY' AS source_system,
                    CAST([Work Order Id] AS NVARCHAR(50)) AS position_id,
                    CONCAT([Contractor First Name], ' ', [Contractor Last Name]) AS candidate_name,
                    [Vendor Name] AS agency,
                    [Default Work Site Name] AS facility,
                    [Health System] AS system,
                    [Job Title] AS specialty,
                    [Start Date] AS startDate,
                    [End Date] AS endDate,
                    [Current Status] AS status
                FROM dbo.STAGING_VNDLY_WORKORDERS
                WHERE [Current Status] IN ('Verification In Progress', 'Ready to Onboard', 'Offer Released')
                    AND [Start Date] IS NOT NULL
                    AND [Start Date] > GETDATE()
                    AND [Start Date] <= DATEADD(day, 30, GETDATE())
            ''')
            
            columns = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                if row_dict.get('startDate'):
                    row_dict['startDate'] = row_dict['startDate'].isoformat() if hasattr(row_dict['startDate'], 'isoformat') else str(row_dict['startDate'])
                if row_dict.get('endDate'):
                    row_dict['endDate'] = row_dict['endDate'].isoformat() if hasattr(row_dict['endDate'], 'isoformat') else str(row_dict['endDate'])
                upcoming.append(row_dict)
        except Exception as e:
            print(f"Error loading VNDLY upcoming: {e}")
        
        conn.close()
        
        b4_active = len([r for r in on_assignment if r.get('source_system') == 'B4'])
        vndly_active = len([r for r in on_assignment if r.get('source_system') == 'VNDLY'])
        print(f"Returning {len(on_assignment)} active (B4: {b4_active}, VNDLY: {vndly_active}), {len(upcoming)} upcoming")
        
        return func.HttpResponse(
            json.dumps({
                'onAssignment': on_assignment,
                'upcoming': upcoming
            }, default=str),
            mimetype="application/json",
            status_code=200
        )
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return func.HttpResponse(
            json.dumps({'error': str(e), 'onAssignment': [], 'upcoming': []}),
            mimetype="application/json",
            status_code=500
        )