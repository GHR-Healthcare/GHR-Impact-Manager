import azure.functions as func
import pyodbc
import os
import json
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


# Bullhorn placement statuses that mean "the placement was awarded and ran
# (or is currently running)." Includes historical Completed/Termination so
# past weeks' headcount reflects them. Excludes Cancellation/Archive (never
# happened / hidden).
BULLHORN_ACTIVE_STATUSES = (
    'Approved', 'Pending Start', 'Cleared', 'Onboarding', 'Started',
    'Completed', 'Termination',
)

# VNDLY work-order statuses meaning "this assignment was awarded and ran (or is
# running)". The Bullhorn list above has always included its historical statuses;
# VNDLY was filtered to 'Active' only, which applies a CURRENT status to PAST
# weeks — anyone who finished and got flipped to a terminal status vanished from
# the lookback. Older weeks lost proportionally more rows than recent ones, which
# manufactured a fake upward trend (and understated the prior-year line badly).
#
# Excluded as never-happened: Rejected, Withdrawn, Offer Declined, Cancelled.
# Excluded as pre-start funnel: Applied, Offer Released, Verification In Progress,
# Ready to Onboard (these belong to the pending queries below).
VNDLY_RAN_STATUSES = ('Active', 'Ended', 'Ended by Job Close')

# Terminal members of the set above. Their [End Date] is the ORIGINALLY SCHEDULED
# end, not the date the assignment actually stopped — 'Ended by Job Close' rows
# carry end dates over a year out. Counting those as still-running would inflate
# current and future headcount, so the effective end is capped at today: a
# terminal status cannot still be running.
VNDLY_TERMINAL_STATUSES = ('Ended', 'Ended by Job Close')

# VNDLY pre-start pipeline statuses. 'Ready to Onboard' was previously missing
# here even though the Pending tab's own statusBucket() classifies it.
VNDLY_PENDING_STATUSES = (
    'Applied', 'Offer Released', 'Verification In Progress', 'Ready to Onboard',
)


def _sql_list(values):
    """Render a tuple of statuses as a SQL IN-list literal."""
    return ', '.join("'" + v.replace("'", "''") + "'" for v in values)


# Effective end date for a VNDLY work order.
#
#   - Active: raw [End Date] (may be NULL for open-ended assignments).
#   - Ended / Ended by Job Close: [End Date] is the originally SCHEDULED end,
#     often over a year out — not the date the assignment actually stopped.
#     Use the last week we billed the contractor via STAGING_VNDLY_SPEND
#     as the actual stop marker. Terminal WOs with no spend row at all are
#     excluded upstream by VNDLY_HAS_SPEND_IF_TERMINAL_SQL — they never
#     actually ran, so they shouldn't contribute headcount.
#
# The v2.0.2 first pass capped terminal rows at today, which overstated
# current-week headcount (workers who really stopped weeks ago were still
# counted) and produced a hard cliff the moment you crossed the current
# week boundary. Using per-worker last-spend fixes both.
#
# Requires the outer STAGING_VNDLY_WORKORDERS to be aliased as `wo`.
VNDLY_EFFECTIVE_END_SQL = f'''CASE
        WHEN wo.[Current Status] IN ({_sql_list(VNDLY_TERMINAL_STATUSES)}) THEN (
            SELECT MAX(s.[Billing Cycle End Date])
            FROM dbo.STAGING_VNDLY_SPEND s
            WHERE s.[Contractor First Name] = wo.[Contractor First Name]
              AND s.[Contractor Last Name]  = wo.[Contractor Last Name]
        )
        ELSE wo.[End Date]
    END'''


# WHERE-clause filter to keep terminal-status WOs only if the contractor has
# at least one spend row. A terminal WO with no spend never actually ran
# (the WO was awarded then closed without any billed hours). Without this
# filter such rows would carry endDate = NULL, which the frontend treats as
# "still running today" and would inflate current headcount by ~380 workers.
#
# Requires the outer STAGING_VNDLY_WORKORDERS to be aliased as `wo`.
VNDLY_HAS_SPEND_IF_TERMINAL_SQL = f'''(
        wo.[Current Status] = 'Active'
        OR EXISTS (
            SELECT 1 FROM dbo.STAGING_VNDLY_SPEND s
            WHERE s.[Contractor First Name] = wo.[Contractor First Name]
              AND s.[Contractor Last Name]  = wo.[Contractor Last Name]
        )
    )'''


def _bullhorn_trend_data():
    """
    Returns {assignments, weekly_revenue} from the Bullhorn book. Raises on
    connection or query error so the caller can log + continue.
    """
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
    status_list = ', '.join("'" + s + "'" for s in BULLHORN_ACTIVE_STATUSES)

    assignments = []
    cursor.execute(f'''
        SELECT
            'Bullhorn' AS source_system,
            LTRIM(RTRIM(ISNULL(c.firstName,'') + ' ' + ISNULL(c.lastName,''))) AS worker_name,
            ({system_case}) AS system,
            cc.name AS facility,
            'GHR' AS agency,
            p.customText1 AS specialty,
            p.employmentType AS category,
            -- Division lives on the client (View_ClientCorporation.customTextBlock1),
            -- not the placement — customTextBlock1 on View_Placement is always NULL.
            -- Values arrive as a comma-separated list (e.g. "Allied,Nursing,RevCycle Workforce")
            -- because a single client can be serviced by multiple GHR internal teams.
            cc.customTextBlock1 AS division,
            NULL AS region,
            p.customText11 AS pm,
            p.status AS status,
            TRY_CAST(p.clientBillRate AS DECIMAL(10,2)) AS bill_rate,
            TRY_CAST(p.hoursPerDay * 5 AS DECIMAL(10,2)) AS weekly_hours,
            CAST(p.dateBegin AS DATE) AS startDate,
            CAST(p.dateEnd AS DATE) AS endDate
        FROM dbo.View_Placement p
        LEFT JOIN dbo.View_Candidate c ON p.candidateID = c.candidateID
        LEFT JOIN dbo.View_ClientCorporation cc ON p.clientCorporationID = cc.clientCorporationID
        WHERE p.isDeleted = 0
            AND p.status IN ({status_list})
            AND p.dateBegin IS NOT NULL
            AND (p.dateEnd IS NULL OR p.dateEnd >= DATEADD(WEEK, -4, GETDATE()))
            AND {scope_filter}
    ''')
    columns = [column[0] for column in cursor.description]
    for row in cursor.fetchall():
        row_dict = dict(zip(columns, row))
        if row_dict.get('startDate'):
            row_dict['startDate'] = row_dict['startDate'].isoformat() if hasattr(row_dict['startDate'], 'isoformat') else str(row_dict['startDate'])
        if row_dict.get('endDate'):
            row_dict['endDate'] = row_dict['endDate'].isoformat() if hasattr(row_dict['endDate'], 'isoformat') else str(row_dict['endDate'])
        assignments.append(row_dict)

    weekly_revenue = []
    cursor.execute(f'''
        ;WITH Weeks AS (
            SELECT TOP 8
                DATEADD(WEEK, 1 - ROW_NUMBER() OVER (ORDER BY (SELECT NULL)),
                        DATEADD(DAY, 1 - DATEPART(WEEKDAY, CAST(GETDATE() AS DATE)),
                                CAST(GETDATE() AS DATE))
                ) AS week_start
            FROM sys.all_objects
        )
        SELECT
            CONVERT(VARCHAR(10), w.week_start, 23) AS week_start,
            ({system_case}) AS system,
            'GHR' AS vendor_type,
            -- TRY_CAST for the same reason the assignments query above uses it:
            -- clientBillRate / hoursPerDay are free-text in the Bullhorn views
            -- and a single unparseable value fails the whole SUM without it.
            -- NOTE: this is a run-rate ESTIMATE (bill rate x scheduled hours),
            -- not billed dollars. Symplr's contribution to weekly_revenue is
            -- actual billed amount. The frontend unions the two.
            SUM(ISNULL(TRY_CAST(p.clientBillRate AS DECIMAL(18,2)), 0)
                * ISNULL(TRY_CAST(p.hoursPerDay AS DECIMAL(10,2)), 0) * 5) AS revenue
        FROM Weeks w
        INNER JOIN dbo.View_Placement p
            ON p.dateBegin <= DATEADD(DAY, 6, w.week_start)
            AND (p.dateEnd IS NULL OR p.dateEnd >= w.week_start)
        LEFT JOIN dbo.View_ClientCorporation cc ON p.clientCorporationID = cc.clientCorporationID
        WHERE p.isDeleted = 0
            AND p.status IN ({status_list})
            AND p.dateBegin IS NOT NULL
            AND {scope_filter}
        GROUP BY w.week_start, ({system_case})
    ''')
    for row in cursor.fetchall():
        weekly_revenue.append({
            'source_system': 'Bullhorn',
            'week_start': row[0],
            'system': row[1],
            'vendor_type': row[2],
            'revenue': float(row[3] or 0),
        })

    conn.close()
    return {'assignments': assignments, 'weekly_revenue': weekly_revenue}


def _symplr_trend_data():
    """
    Returns {assignments, weekly_revenue} from the Symplr book. Raises on
    connection / query error. Returns empty dicts if SYMPLR_* env vars
    aren't configured (get_symplr_conn returns None).
    """
    conn = get_symplr_conn()
    if conn is None:
        return {'assignments': [], 'weekly_revenue': [], 'errors': ['symplr_conn_none']}

    cursor = conn.cursor()
    app_conn = get_appdb_conn()
    try:
        symplr_master_ids = symplr_resolve_scope(app_conn)
    finally:
        if app_conn is not None:
            app_conn.close()
    system_case = symplr_system_case_expr('lt.clientid')
    scope_filter = symplr_scope_filter('lt.clientid', master_ids=symplr_master_ids)
    division_case = symplr_division_case_expr('lt.clientid')
    # Each of the three queries below runs in its own try/except so one bad
    # one doesn't zero out the whole Symplr contribution to trend.
    errors = []

    # ============================================================
    # Symplr — Long-term orders (filled placements) in rolling window
    # ============================================================
    assignments = []
    try:
        cursor.execute(f'''
            SELECT
                'Symplr' AS source_system,
                LTRIM(RTRIM(ISNULL(pt.firstname, '') + ' ' + ISNULL(pt.lastname, ''))) AS worker_name,
                ({system_case}) AS system,
                pc.clientname AS facility,
                'GHR' AS agency,
                lt.specialty AS specialty,
                lt.nursetype AS category,
                ({division_case}) AS division,
                pc.state AS region,
                NULL AS pm,
                lt.status AS status,
                NULL AS bill_rate,
                TRY_CAST(lt.HoursPerWeek AS DECIMAL(10,2)) AS weekly_hours,
                CAST(lt.date_start AS DATE) AS startDate,
                CAST(lt.date_end AS DATE) AS endDate
            FROM dbo.lt_order lt
            LEFT JOIN dbo.profile_client pc ON lt.clientid = pc.recordid
            LEFT JOIN dbo.profile_temp pt ON lt.tempid = pt.recordid
            WHERE lt.status = 'filled'
                AND lt.date_start IS NOT NULL
                AND (lt.date_end IS NULL OR lt.date_end >= DATEADD(WEEK, -4, GETDATE()))
                AND {scope_filter}
        ''')
        columns = [column[0] for column in cursor.description]
        for row in cursor.fetchall():
            row_dict = dict(zip(columns, row))
            if row_dict.get('startDate'):
                row_dict['startDate'] = row_dict['startDate'].isoformat() if hasattr(row_dict['startDate'], 'isoformat') else str(row_dict['startDate'])
            if row_dict.get('endDate'):
                row_dict['endDate'] = row_dict['endDate'].isoformat() if hasattr(row_dict['endDate'], 'isoformat') else str(row_dict['endDate'])
            assignments.append(row_dict)
    except Exception as e:
        errors.append(f"symplr_lt_order: {e}")
        print(f"Symplr trend lt_order query failed: {e}")
        import traceback; traceback.print_exc()

    # ============================================================
    # Symplr — Orderless filled orders (lt_orderid IN (0, NULL))
    # Per-shift bookings without an lt_order parent. Aggregate by
    # (worker, client) so a multi-shift assignment collapses into one row.
    # ============================================================
    system_case_orders = symplr_system_case_expr('o.customerid')
    scope_filter_orders = symplr_scope_filter('o.customerid', master_ids=symplr_master_ids)
    division_case_orders = symplr_division_case_expr('o.customerid')
    try:
        cursor.execute(f'''
            SELECT
                'Symplr' AS source_system,
                LTRIM(RTRIM(ISNULL(MAX(pt.firstname), '') + ' ' + ISNULL(MAX(pt.lastname), ''))) AS worker_name,
                ({system_case_orders}) AS system,
                MAX(pc.clientname) AS facility,
                'GHR' AS agency,
                MAX(o.specialty) AS specialty,
                MAX(o.nursetype) AS category,
                ({division_case_orders}) AS division,
                MAX(pc.state) AS region,
                NULL AS pm,
                'filled' AS status,
                NULL AS bill_rate,
                NULL AS weekly_hours,
                CAST(MIN(o.jobdatestart) AS DATE) AS startDate,
                CAST(MAX(o.jobdateend)   AS DATE) AS endDate
            FROM dbo.orders o
            LEFT JOIN dbo.profile_client pc ON o.customerid = pc.recordid
            LEFT JOIN dbo.profile_temp   pt ON o.filledby   = pt.recordid
            WHERE o.status = 'filled'
                AND (o.lt_orderid IS NULL OR o.lt_orderid = 0)
                AND o.filledby IS NOT NULL AND o.filledby > 0
                AND o.jobdatestart IS NOT NULL
                AND (o.jobdateend IS NULL OR o.jobdateend >= DATEADD(WEEK, -4, GETDATE()))
                AND {scope_filter_orders}
            GROUP BY o.customerid, o.filledby
        ''')
        columns = [column[0] for column in cursor.description]
        for row in cursor.fetchall():
            row_dict = dict(zip(columns, row))
            if row_dict.get('startDate'):
                row_dict['startDate'] = row_dict['startDate'].isoformat() if hasattr(row_dict['startDate'], 'isoformat') else str(row_dict['startDate'])
            if row_dict.get('endDate'):
                row_dict['endDate'] = row_dict['endDate'].isoformat() if hasattr(row_dict['endDate'], 'isoformat') else str(row_dict['endDate'])
            assignments.append(row_dict)
    except Exception as e:
        errors.append(f"symplr_orderless: {e}")
        print(f"Symplr trend orderless orders query failed: {e}")
        import traceback; traceback.print_exc()

    # ============================================================
    # Symplr — Weekly ACTUAL revenue from shift-level orders table
    # Symplr is the only source with real billed dollars per shift —
    # bucket by week of jobdatestart and sum totalbillamount.
    # ============================================================
    weekly_revenue = []
    try:
        # Precompute week_start and system in a CTE so the GROUP BY can
        # reference plain columns. SQL Server treats each interpolation of a
        # CASE-with-subquery as a distinct expression, so the previous
        # GROUP BY {system_case_expr} pattern failed to match the SELECT
        # instance (error 42000 code 144, "column not in aggregate/GROUP BY").
        cursor.execute(f'''
            WITH RevenueSource AS (
                SELECT
                    DATEADD(DAY, 1 - DATEPART(WEEKDAY, CAST(o.jobdatestart AS DATE)),
                            CAST(o.jobdatestart AS DATE)) AS week_start_date,
                    ({symplr_system_case_expr('o.customerid')}) AS system,
                    o.totalbillamount
                FROM dbo.orders o
                WHERE o.jobdatestart IS NOT NULL
                    AND CAST(o.jobdatestart AS DATE) >= DATEADD(WEEK, -8, GETDATE())
                    -- Only shifts that actually produced billable work. Without
                    -- this, open/cancelled orders carrying a quoted
                    -- totalbillamount inflate the revenue line. Gating on
                    -- billhours rather than status = 'filled' mirrors
                    -- GetHoursData and is safe if a worked shift later moves to
                    -- a terminal status other than 'filled'.
                    AND ISNULL(o.totalbillhours, 0) > 0
                    AND {symplr_scope_filter('o.customerid', master_ids=symplr_master_ids)}
            )
            SELECT
                CONVERT(VARCHAR(10), week_start_date, 23) AS week_start,
                system,
                'GHR' AS vendor_type,
                SUM(ISNULL(totalbillamount, 0)) AS revenue
            FROM RevenueSource
            GROUP BY week_start_date, system
        ''')
        for row in cursor.fetchall():
            weekly_revenue.append({
                'source_system': 'Symplr',
                'week_start': row[0],
                'system': row[1],
                'vendor_type': row[2],
                'revenue': float(row[3] or 0),
            })
    except Exception as e:
        errors.append(f"symplr_revenue: {e}")
        print(f"Symplr trend weekly_revenue query failed: {e}")
        import traceback; traceback.print_exc()

    conn.close()
    return {'assignments': assignments, 'weekly_revenue': weekly_revenue, 'errors': errors}


def _non_msp_trend(req: func.HttpRequest) -> func.HttpResponse:
    """
    Trend data for the non-MSP instance. Calls Bullhorn and Symplr independently
    and unions the results. Either side failing is logged but not fatal — the
    other source still contributes its rows.
    """
    assignments = []
    weekly_revenue = []
    errors = []

    try:
        bh = _bullhorn_trend_data()
        assignments.extend(bh['assignments'])
        weekly_revenue.extend(bh['weekly_revenue'])
    except Exception as e:
        print(f"Bullhorn trend error: {e}")
        import traceback; traceback.print_exc()
        errors.append(f"bullhorn: {e}")

    try:
        sp = _symplr_trend_data()
        assignments.extend(sp['assignments'])
        weekly_revenue.extend(sp['weekly_revenue'])
        # Surface per-query errors so the frontend / a diagnostic can see them
        # without needing Function App log access.
        for e in sp.get('errors') or []:
            errors.append(e)
    except Exception as e:
        print(f"Symplr trend error: {e}")
        import traceback; traceback.print_exc()
        errors.append(f"symplr: {e}")

    print(f"Returning {len(assignments)} non-MSP trend assignments, 0 pending, {len(weekly_revenue)} weekly revenue rows (errors: {errors or 'none'})")
    return func.HttpResponse(
        json.dumps({
            'assignments': assignments,
            'pending': [],
            'weekly_revenue': weekly_revenue,
            'errors': errors,
        }, default=str),
        mimetype="application/json",
        status_code=200,
    )


def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    Returns trend data: a rolling window of assignments from both B4Health and VNDLY.
    Covers 6 weeks back through present (and beyond via end dates) to support
    4-week lookback + current week + 4-week forward projection in the frontend.
    """
    auth_error = require_allowed_domain(req)
    if auth_error:
        return auth_error

    if is_non_msp():
        return _non_msp_trend(req)

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
        assignments = []
        # Each query below has its own try/except so one bad source doesn't zero
        # out the whole tab. Collect the failures so the response can say so —
        # otherwise a partial load is indistinguishable from a genuinely quiet
        # week. Mirrors the `errors` contract the non-MSP path already returns.
        errors = []

        # ============================================================
        # B4Health - Assignments in rolling window
        # ============================================================
        try:
            cursor.execute('''
                SELECT
                    'B4' AS source_system,
                    CONCAT(Last_Name, ', ', First_Name) AS worker_name,
                    Health_System AS system,
                    Facility AS facility,
                    Agency AS agency,
                    Care_Type AS specialty,
                    Program AS category,
                    Account_Manager AS pm,
                    Contract_Status AS status,
                    TRY_CAST(Awarded_Rate AS DECIMAL(10,2)) AS bill_rate,
                    TRY_CAST(Hours_per_Peek AS DECIMAL(10,2)) AS weekly_hours,
                    Start_Date AS startDate,
                    End_Date AS endDate
                FROM dhc.B4HealthOrder
                WHERE Contract_Status = 'Closed And Awarded'
                    AND Start_Date IS NOT NULL
                    AND (End_Date IS NULL OR End_Date >= DATEADD(WEEK, -4, GETDATE()))
                    AND Health_System <> 'Sunrise Senior Living Management (California)'
            ''')

            columns = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                if row_dict.get('startDate'):
                    row_dict['startDate'] = row_dict['startDate'].isoformat() if hasattr(row_dict['startDate'], 'isoformat') else str(row_dict['startDate'])
                if row_dict.get('endDate'):
                    row_dict['endDate'] = row_dict['endDate'].isoformat() if hasattr(row_dict['endDate'], 'isoformat') else str(row_dict['endDate'])
                assignments.append(row_dict)
        except Exception as e:
            print(f"Error loading B4 trend assignments: {e}")
            errors.append(f"b4_assignments: {e}")

        # ============================================================
        # VNDLY - Assignments in rolling window
        # ============================================================
        try:
            cursor.execute(f'''
                SELECT
                    'VNDLY' AS source_system,
                    CONCAT(wo.[Contractor First Name], ' ', wo.[Contractor Last Name]) AS worker_name,
                    wo.[Health System] AS system,
                    wo.[Default Work Site Name] AS facility,
                    wo.[Vendor Name] AS agency,
                    wo.[Job Title] AS specialty,
                    wo.[Labor Type] AS category,
                    wo.[Resource Manager] AS pm,
                    wo.[Current Status] AS status,
                    TRY_CAST(wo.[Bill Rate] AS DECIMAL(10,2)) AS bill_rate,
                    NULL AS weekly_hours,
                    wo.[Start Date] AS startDate,
                    {VNDLY_EFFECTIVE_END_SQL} AS endDate
                FROM dbo.STAGING_VNDLY_WORKORDERS wo
                WHERE wo.[Current Status] IN ({_sql_list(VNDLY_RAN_STATUSES)})
                    AND wo.[Start Date] IS NOT NULL
                    -- Terminal WOs with no spend history never actually ran — drop them.
                    AND {VNDLY_HAS_SPEND_IF_TERMINAL_SQL}
                    -- Prune anything whose effective end is older than the trend
                    -- window. For terminals the effective end is derived from the
                    -- spend table; for Active it's the raw [End Date] (may be NULL).
                    AND (
                        {VNDLY_EFFECTIVE_END_SQL} IS NULL
                        OR {VNDLY_EFFECTIVE_END_SQL} >= DATEADD(WEEK, -4, GETDATE())
                    )
            ''')

            columns = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                if row_dict.get('startDate'):
                    row_dict['startDate'] = row_dict['startDate'].isoformat() if hasattr(row_dict['startDate'], 'isoformat') else str(row_dict['startDate'])
                if row_dict.get('endDate'):
                    row_dict['endDate'] = row_dict['endDate'].isoformat() if hasattr(row_dict['endDate'], 'isoformat') else str(row_dict['endDate'])
                assignments.append(row_dict)
        except Exception as e:
            print(f"Error loading VNDLY trend assignments: {e}")
            errors.append(f"vndly_assignments: {e}")

        # ============================================================
        # B4Health - Pending submissions (With Requests = candidate submitted)
        # ============================================================
        pending = []
        try:
            cursor.execute('''
                SELECT
                    'B4' AS source_system,
                    CONCAT(Last_Name, ', ', First_Name) AS worker_name,
                    Health_System AS system,
                    Facility AS facility,
                    Agency AS agency,
                    Care_Type AS specialty,
                    Program AS category,
                    Account_Manager AS pm,
                    Contract_Status AS status,
                    TRY_CAST(Awarded_Rate AS DECIMAL(10,2)) AS bill_rate,
                    TRY_CAST(Hours_per_Peek AS DECIMAL(10,2)) AS weekly_hours,
                    Start_Date AS startDate,
                    End_Date AS endDate
                FROM dhc.B4HealthOrder
                WHERE Contract_Status = 'With Requests'
                    AND Start_Date IS NOT NULL
                    AND Start_Date >= DATEADD(WEEK, -1, GETDATE())
                    AND Health_System <> 'Sunrise Senior Living Management (California)'
            ''')
            columns = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                if row_dict.get('startDate'):
                    row_dict['startDate'] = row_dict['startDate'].isoformat() if hasattr(row_dict['startDate'], 'isoformat') else str(row_dict['startDate'])
                if row_dict.get('endDate'):
                    row_dict['endDate'] = row_dict['endDate'].isoformat() if hasattr(row_dict['endDate'], 'isoformat') else str(row_dict['endDate'])
                pending.append(row_dict)
        except Exception as e:
            print(f"Error loading B4 pending: {e}")
            errors.append(f"b4_pending: {e}")

        # ============================================================
        # VNDLY - Pending work orders (pre-start funnel; see VNDLY_PENDING_STATUSES)
        # ============================================================
        try:
            cursor.execute(f'''
                SELECT
                    'VNDLY' AS source_system,
                    CONCAT([Contractor First Name], ' ', [Contractor Last Name]) AS worker_name,
                    [Health System] AS system,
                    [Default Work Site Name] AS facility,
                    [Vendor Name] AS agency,
                    [Job Title] AS specialty,
                    [Labor Type] AS category,
                    [Resource Manager] AS pm,
                    [Current Status] AS status,
                    TRY_CAST([Bill Rate] AS DECIMAL(10,2)) AS bill_rate,
                    NULL AS weekly_hours,
                    [Start Date] AS startDate,
                    [End Date] AS endDate
                FROM dbo.STAGING_VNDLY_WORKORDERS
                WHERE [Current Status] IN ({_sql_list(VNDLY_PENDING_STATUSES)})
                    AND [Start Date] IS NOT NULL
                    AND [Start Date] >= DATEADD(WEEK, -1, GETDATE())
            ''')
            columns = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                if row_dict.get('startDate'):
                    row_dict['startDate'] = row_dict['startDate'].isoformat() if hasattr(row_dict['startDate'], 'isoformat') else str(row_dict['startDate'])
                if row_dict.get('endDate'):
                    row_dict['endDate'] = row_dict['endDate'].isoformat() if hasattr(row_dict['endDate'], 'isoformat') else str(row_dict['endDate'])
                pending.append(row_dict)
        except Exception as e:
            print(f"Error loading VNDLY pending: {e}")
            errors.append(f"vndly_pending: {e}")

        # ============================================================
        # Weekly actual revenue — B4 (from B4HealthESR Bill Total)
        # ============================================================
        weekly_revenue = []
        try:
            cursor.execute('''
                SELECT
                    CONVERT(VARCHAR(10),
                        DATEADD(DAY, 1 - DATEPART(WEEKDAY, CAST([Work Date] AS DATE)), CAST([Work Date] AS DATE)),
                        23) AS week_start,
                    [Health System] AS system,
                    CASE
                        WHEN [Agency Name] LIKE 'GHR%' OR [Agency Name] LIKE '%Planet Healthcare%'
                        THEN 'GHR' ELSE 'Affiliate'
                    END AS vendor_type,
                    SUM(ISNULL(TRY_CAST([Bill Total] AS DECIMAL(18,2)), 0)) AS revenue
                FROM dhc.B4HealthESR
                WHERE [Work Date] IS NOT NULL
                    AND CAST([Work Date] AS DATE) >= DATEADD(WEEK, -8, GETDATE())
                    AND [Health System] IS NOT NULL
                    AND [Health System] <> 'Sunrise Senior Living Management (California)'
                GROUP BY
                    DATEADD(DAY, 1 - DATEPART(WEEKDAY, CAST([Work Date] AS DATE)), CAST([Work Date] AS DATE)),
                    [Health System],
                    CASE
                        WHEN [Agency Name] LIKE 'GHR%' OR [Agency Name] LIKE '%Planet Healthcare%'
                        THEN 'GHR' ELSE 'Affiliate'
                    END
            ''')
            for row in cursor.fetchall():
                weekly_revenue.append({
                    'source_system': 'B4',
                    'week_start': row[0],
                    'system': row[1],
                    'vendor_type': row[2],
                    'revenue': float(row[3] or 0),
                })
        except Exception as e:
            print(f"Error loading B4 weekly revenue: {e}")
            errors.append(f"b4_revenue: {e}")

        # ============================================================
        # Weekly actual revenue — VNDLY (from STAGING_VNDLY_SPEND Client Amount)
        # ============================================================
        try:
            # Bucket VNDLY revenue by Billing Cycle Start Date (not Item Date,
            # which is the cycle END Saturday). Same fix we made on the
            # Financials side — keeps end-of-week work in the right week
            # rather than pushing it to the following one.
            cursor.execute('''
                SELECT
                    CONVERT(VARCHAR(10),
                        DATEADD(DAY, 1 - DATEPART(WEEKDAY, CAST([Billing Cycle Start Date] AS DATE)), CAST([Billing Cycle Start Date] AS DATE)),
                        23) AS week_start,
                    [Health System] AS system,
                    CASE
                        WHEN [Vendor Company Name] LIKE '%GHR%' OR [Vendor Company Name] LIKE '%Planet Healthcare%'
                        THEN 'GHR' ELSE 'Affiliate'
                    END AS vendor_type,
                    SUM(ISNULL(TRY_CAST([Client Amount] AS DECIMAL(18,2)), 0)) AS revenue
                FROM dbo.STAGING_VNDLY_SPEND
                WHERE [Billing Cycle Start Date] IS NOT NULL
                    AND CAST([Billing Cycle Start Date] AS DATE) >= DATEADD(WEEK, -8, GETDATE())
                    AND [Health System] IS NOT NULL
                GROUP BY
                    DATEADD(DAY, 1 - DATEPART(WEEKDAY, CAST([Billing Cycle Start Date] AS DATE)), CAST([Billing Cycle Start Date] AS DATE)),
                    [Health System],
                    CASE
                        WHEN [Vendor Company Name] LIKE '%GHR%' OR [Vendor Company Name] LIKE '%Planet Healthcare%'
                        THEN 'GHR' ELSE 'Affiliate'
                    END
            ''')
            for row in cursor.fetchall():
                weekly_revenue.append({
                    'source_system': 'VNDLY',
                    'week_start': row[0],
                    'system': row[1],
                    'vendor_type': row[2],
                    'revenue': float(row[3] or 0),
                })
        except Exception as e:
            print(f"Error loading VNDLY weekly revenue: {e}")
            errors.append(f"vndly_revenue: {e}")

        conn.close()

        print(f"Returning {len(assignments)} trend assignments, {len(pending)} pending, {len(weekly_revenue)} weekly revenue rows (errors: {errors or 'none'})")

        return func.HttpResponse(
            json.dumps({
                'assignments': assignments,
                'pending': pending,
                'weekly_revenue': weekly_revenue,
                'errors': errors,
            }, default=str),
            mimetype="application/json",
            status_code=200
        )

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return func.HttpResponse(
            json.dumps({'error': str(e), 'assignments': []}),
            mimetype="application/json",
            status_code=500
        )
