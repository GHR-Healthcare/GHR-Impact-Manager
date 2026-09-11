import azure.functions as func
import pyodbc
import os
import json
from datetime import datetime, timedelta
from shared_code.auth import require_allowed_domain
from shared_code.credentials import (
    normalize as normalize_credential,
    service_line as credential_service_line,
)
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
# How far back the Placements pane looks by default. The pane only ever showed
# seats live right now, so a facility that turned over last month looked
# untouched. Historical rows ride alongside the active ones under their own key
# -- onAssignment keeps its exact meaning, because the Contracts tab and the
# headline counts read it and must not move.
RECENT_END_DAYS = 90

# What a seat's status reads once it has ended, per book. Measured 2026-09-11
# over the trailing 90 days; the active-status lists match none of these, so a
# date-only widening would have returned almost nothing.
#
#   Bullhorn  Completed 1147, Cancellation 313, Termination 146
#   VNDLY     Ended by Job Close 151, Ended 147
#   B4        Closed And Awarded 896 -- same status it carries while running
#
# Excluded deliberately: VNDLY's Rejected / Withdrawn / Offer Declined /
# Applied and B4's Closed And Cancelled / Closed Not Awarded. Those are
# pipeline outcomes on seats nobody ever worked, not placement history.
BULLHORN_ENDED_SNAPSHOT_STATUSES = (
    'Completed', 'Termination', 'Cancellation',
)
VNDLY_ENDED_SNAPSHOT_STATUSES = (
    'Ended', 'Ended by Job Close',
)

BULLHORN_ACTIVE_SNAPSHOT_STATUSES = (
    'Approved', 'Pending Start', 'Cleared', 'Onboarding', 'Started',
)



def _apply_service_line(row_dict):
    """Attach the normalised credential and its service line.

    Stats rows carried no category at all, which is why the client fell back to
    keyword-matching the specialty text. With a real service line on the row
    that guesswork can stop.
    """
    raw = row_dict.pop('credential_raw', None)
    if raw:
        row_dict['profession'] = normalize_credential(raw)
        row_dict['service_line'] = credential_service_line(raw)
    # The response is dumped with default=str, which would send a Decimal rate
    # as the string "85.00". That survives Number() but not arithmetic done
    # before it, so the rate is made a real number here -- every assignment
    # query routes through this function.
    if row_dict.get('bill_rate') is not None:
        try:
            row_dict['bill_rate'] = float(row_dict['bill_rate'])
        except (TypeError, ValueError):
            row_dict['bill_rate'] = None
    return row_dict


def _bullhorn_stats_data():
    """Returns (on_assignment[], upcoming[], recently_ended[]) for Bullhorn. Raises on error."""
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
    def _status_list(statuses):
        return ', '.join("'" + s + "'" for s in statuses)

    status_list = _status_list(BULLHORN_ACTIVE_SNAPSHOT_STATUSES)

    def _fetch_rows(date_clause, statuses=None):
        status_list = _status_list(statuses) if statuses else _status_list(BULLHORN_ACTIVE_SNAPSHOT_STATUSES)
        cursor.execute(f'''
            SELECT
                'Bullhorn' AS source_system,
                CAST(p.placementID AS NVARCHAR(50)) AS position_id,
                LTRIM(RTRIM(ISNULL(c.firstName, '') + ' ' + ISNULL(c.lastName, ''))) AS candidate_name,
                'GHR' AS agency,
                cc.name AS facility,
                ({system_case}) AS system,
                -- customText1 on a placement is the credential (RN, Coder,
                -- CRNA), not a specialty. It is emitted as such so the two
                -- books can be compared; Symplr sends its real specialty in
                -- the same field position.
                p.customText1 AS specialty,
                p.customText1 AS credential_raw,
                -- The assignment pane exists "for rate and vendor comparison"
                -- and had no rate to compare: every one of these queries left
                -- its source's rate column unselected, so the pane's own
                -- footnote promised something it could not show.
                TRY_CAST(p.clientBillRate AS DECIMAL(10,2)) AS bill_rate,
                -- Division lives on the client (see GetTrendData note).
                cc.customTextBlock1 AS division,
                NULL AS region,
                CAST(p.dateBegin AS DATE) AS startDate,
                CAST(p.dateEnd AS DATE) AS endDate,
                p.status AS status
            FROM dbo.View_Placement p
            LEFT JOIN dbo.View_Candidate c ON p.candidateID = c.candidateID
            LEFT JOIN dbo.View_ClientCorporation cc ON p.clientCorporationID = cc.clientCorporationID
            LEFT JOIN dbo.View_ClientCorporation pcc ON cc.parentClientCorporationID = pcc.clientCorporationID
            WHERE p.isDeleted = 0
                AND p.status IN ({status_list})
                AND p.dateBegin IS NOT NULL
                AND {date_clause}
                AND {scope_filter}
        ''')
        columns = [column[0] for column in cursor.description]
        out = []
        for row in cursor.fetchall():
            row_dict = _apply_service_line(dict(zip(columns, row)))
            if row_dict.get('startDate'):
                row_dict['startDate'] = row_dict['startDate'].isoformat() if hasattr(row_dict['startDate'], 'isoformat') else str(row_dict['startDate'])
            if row_dict.get('endDate'):
                row_dict['endDate'] = row_dict['endDate'].isoformat() if hasattr(row_dict['endDate'], 'isoformat') else str(row_dict['endDate'])
            out.append(row_dict)
        return out

    on_assignment = _fetch_rows("p.dateBegin <= GETDATE() AND (p.dateEnd IS NULL OR p.dateEnd >= GETDATE())")
    upcoming = _fetch_rows("p.dateBegin > GETDATE() AND p.dateBegin <= DATEADD(DAY, 30, GETDATE())")
    recently_ended = _fetch_rows(
        f"p.dateEnd < GETDATE() AND p.dateEnd >= DATEADD(DAY, -{RECENT_END_DAYS}, GETDATE())",
        statuses=BULLHORN_ENDED_SNAPSHOT_STATUSES,
    )
    conn.close()
    return on_assignment, upcoming, recently_ended


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
        symplr_master_ids = symplr_resolve_scope(app_conn, symplr_cursor=cursor)
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
                lt.nursetype AS credential_raw,
                -- lt_order has no rate column; reported as absent, not zero.
                NULL AS bill_rate,
                ({division_case}) AS division,
                pc.state AS region,
                CAST(lt.date_start AS DATE) AS startDate,
                CAST(lt.date_end AS DATE) AS endDate,
                lt.status AS status
            FROM dbo.lt_order lt
            LEFT JOIN dbo.profile_client pc ON lt.clientid = pc.recordid
            LEFT JOIN dbo.profile_client m  ON pc.MasterClientID = m.recordid
            LEFT JOIN dbo.regions r ON r.regionid = TRY_CAST(pc.region AS INT)
            LEFT JOIN dbo.profile_temp pt ON lt.tempid = pt.recordid
            WHERE lt.status = 'filled'
                AND lt.date_start IS NOT NULL
                AND {date_clause}
                AND {scope}
        ''')
        columns = [column[0] for column in cursor.description]
        return [_serialize(_apply_service_line(dict(zip(columns, row)))) for row in cursor.fetchall()]

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
                MAX({sys_case_orders}) AS system,
                MAX(o.specialty) AS specialty,
                MAX(o.nursetype) AS credential_raw,
                MAX({division_case_orders}) AS division,
                MAX(pc.state) AS region,
                CAST(MIN(o.jobdatestart) AS DATE) AS startDate,
                CAST(MAX(o.jobdateend)   AS DATE) AS endDate,
                'filled' AS status
            FROM dbo.orders o
            LEFT JOIN dbo.profile_client pc ON o.customerid = pc.recordid
            LEFT JOIN dbo.profile_client m  ON pc.MasterClientID = m.recordid
            LEFT JOIN dbo.regions r ON r.regionid = TRY_CAST(pc.region AS INT)
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
        return [_serialize(_apply_service_line(dict(zip(columns, row)))) for row in cursor.fetchall()]

    on_assignment = (
        _fetch_lt_rows("lt.date_start <= GETDATE() AND (lt.date_end IS NULL OR lt.date_end >= GETDATE())")
        + _fetch_order_rows("o.jobdatestart <= GETDATE() AND (o.jobdateend IS NULL OR o.jobdateend >= GETDATE())")
    )
    upcoming = (
        _fetch_lt_rows("lt.date_start > GETDATE() AND lt.date_start <= DATEADD(DAY, 30, GETDATE())")
        + _fetch_order_rows("o.jobdatestart > GETDATE() AND o.jobdatestart <= DATEADD(DAY, 30, GETDATE())")
    )
    # No status change needed here: a Symplr shift stays 'filled' after it ends,
    # unlike Bullhorn and VNDLY, which restate the seat once it closes.
    recently_ended = (
        _fetch_lt_rows(f"lt.date_end < GETDATE() AND lt.date_end >= DATEADD(DAY, -{RECENT_END_DAYS}, GETDATE())")
        + _fetch_order_rows(f"o.jobdateend < GETDATE() AND o.jobdateend >= DATEADD(DAY, -{RECENT_END_DAYS}, GETDATE())")
    )
    conn.close()
    return on_assignment, upcoming, recently_ended


def _non_msp_stats(req: func.HttpRequest) -> func.HttpResponse:
    """Run Bullhorn + Symplr stats queries independently, union the results."""
    on_assignment = []
    upcoming = []
    recently_ended = []
    errors = []
    try:
        a, b, c = _bullhorn_stats_data()
        on_assignment.extend(a); upcoming.extend(b); recently_ended.extend(c)
    except Exception as e:
        print(f"Bullhorn stats error: {e}")
        import traceback; traceback.print_exc()
        errors.append(f"bullhorn: {e}")
    try:
        a, b, c = _symplr_stats_data()
        on_assignment.extend(a); upcoming.extend(b); recently_ended.extend(c)
    except Exception as e:
        print(f"Symplr stats error: {e}")
        import traceback; traceback.print_exc()
        errors.append(f"symplr: {e}")
    print(f"non-MSP stats: {len(on_assignment)} active, {len(upcoming)} upcoming, "
          f"{len(recently_ended)} recently ended (errors: {errors or 'none'})")
    return func.HttpResponse(
        json.dumps({'onAssignment': on_assignment, 'upcoming': upcoming,
                    'recentlyEnded': recently_ended}, default=str),
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
        recently_ended = []
        
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
                    TRY_CAST(Awarded_Rate AS DECIMAL(10,2)) AS bill_rate,
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
                row_dict = _apply_service_line(dict(zip(columns, row)))
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
                    TRY_CAST(Awarded_Rate AS DECIMAL(10,2)) AS bill_rate,
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
                row_dict = _apply_service_line(dict(zip(columns, row)))
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
                    TRY_CAST([Bill Rate] AS DECIMAL(10,2)) AS bill_rate,
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
                row_dict = _apply_service_line(dict(zip(columns, row)))
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
                    TRY_CAST([Bill Rate] AS DECIMAL(10,2)) AS bill_rate,
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
                row_dict = _apply_service_line(dict(zip(columns, row)))
                if row_dict.get('startDate'):
                    row_dict['startDate'] = row_dict['startDate'].isoformat() if hasattr(row_dict['startDate'], 'isoformat') else str(row_dict['startDate'])
                if row_dict.get('endDate'):
                    row_dict['endDate'] = row_dict['endDate'].isoformat() if hasattr(row_dict['endDate'], 'isoformat') else str(row_dict['endDate'])
                upcoming.append(row_dict)
        except Exception as e:
            print(f"Error loading VNDLY upcoming: {e}")
        
        conn.close()
        
        # ---- Recently ended, the Placements pane's 90-day history ----
        # B4 keeps 'Closed And Awarded' after a contract ends, so this is the
        # active query with the date window turned around. VNDLY restates the
        # work order, so it matches its own ended statuses instead.
        for label, sql in (
            ('B4', f'''
                SELECT
                    'B4' AS source_system,
                    Contract_ID AS position_id,
                    CONCAT(First_Name, ' ', Last_Name) AS candidate_name,
                    Agency AS agency,
                    Facility AS facility,
                    Health_System AS system,
                    Care_Type AS specialty,
                    TRY_CAST(Awarded_Rate AS DECIMAL(10,2)) AS bill_rate,
                    Start_Date AS startDate,
                    End_Date AS endDate,
                    Contract_Status AS status
                FROM dhc.B4HealthOrder
                WHERE Contract_Status = 'Closed And Awarded'
                    AND Start_Date IS NOT NULL
                    AND End_Date < GETDATE()
                    AND End_Date >= DATEADD(day, -{RECENT_END_DAYS}, GETDATE())
                    AND Health_System NOT LIKE '%Richmond University%'
                    AND Health_System NOT LIKE '%Redeemer%'
                    AND Health_System <> 'Sunrise Senior Living Management (California)'
            '''),
            ('VNDLY', f'''
                SELECT
                    'VNDLY' AS source_system,
                    CAST(WOSystemKey AS NVARCHAR(50)) AS position_id,
                    LTRIM(RTRIM(ISNULL([Contractor First Name], '') + ' '
                              + ISNULL([Contractor Last Name], ''))) AS candidate_name,
                    [Vendor Name] AS agency,
                    [Default Work Site Name] AS facility,
                    [Health System] AS system,
                    [Job Title] AS specialty,
                    TRY_CAST([Bill Rate] AS DECIMAL(10,2)) AS bill_rate,
                    [Start Date] AS startDate,
                    [End Date] AS endDate,
                    [Current Status] AS status
                FROM dbo.STAGING_VNDLY_WORKORDERS
                WHERE [Current Status] IN ({', '.join("'" + x + "'" for x in VNDLY_ENDED_SNAPSHOT_STATUSES)})
                    AND [Start Date] IS NOT NULL
                    AND TRY_CAST([End Date] AS DATE) < CAST(GETDATE() AS DATE)
                    AND TRY_CAST([End Date] AS DATE) >= DATEADD(day, -{RECENT_END_DAYS}, CAST(GETDATE() AS DATE))
            '''),
        ):
            try:
                cursor.execute(sql)
                columns = [column[0] for column in cursor.description]
                for row in cursor.fetchall():
                    row_dict = _apply_service_line(dict(zip(columns, row)))
                    for k in ('startDate', 'endDate'):
                        if row_dict.get(k):
                            row_dict[k] = (row_dict[k].isoformat()
                                           if hasattr(row_dict[k], 'isoformat') else str(row_dict[k]))
                    recently_ended.append(row_dict)
            except Exception as e:
                print(f"Error loading {label} recently ended: {e}")

        b4_active = len([r for r in on_assignment if r.get('source_system') == 'B4'])
        vndly_active = len([r for r in on_assignment if r.get('source_system') == 'VNDLY'])
        print(f"Returning {len(on_assignment)} active (B4: {b4_active}, VNDLY: {vndly_active}), "
              f"{len(upcoming)} upcoming, {len(recently_ended)} recently ended")
        
        return func.HttpResponse(
            json.dumps({
                'onAssignment': on_assignment,
                'upcoming': upcoming,
                'recentlyEnded': recently_ended
            }, default=str),
            mimetype="application/json",
            status_code=200
        )
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return func.HttpResponse(
            json.dumps({'error': str(e), 'onAssignment': [], 'upcoming': [], 'recentlyEnded': []}),
            mimetype="application/json",
            status_code=500
        )