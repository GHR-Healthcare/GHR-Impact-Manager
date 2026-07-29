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
)


# Bullhorn doesn't expose actual timesheet/shift data in the views we have.
# We project scheduled hours from each placement: hoursPerDay × 5 per overlap
# week. Status filter mirrors GetTrendData so historical placements still
# contribute hours for the weeks they actually ran.
BULLHORN_HOURS_STATUSES = (
    'Approved', 'Pending Start', 'Cleared', 'Onboarding', 'Started',
    'Completed', 'Termination',
)


def _bullhorn_hours_data():
    """Returns Bullhorn scheduled-hours rows. Raises on error."""
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
    status_list = ', '.join("'" + s + "'" for s in BULLHORN_HOURS_STATUSES)

    cursor.execute(f'''
        ;WITH Weeks AS (
            SELECT TOP 13
                DATEADD(WEEK, 1 - ROW_NUMBER() OVER (ORDER BY (SELECT NULL)),
                        DATEADD(DAY, 1 - DATEPART(WEEKDAY, CAST(GETDATE() AS DATE)),
                                CAST(GETDATE() AS DATE))
                ) AS week_start
            FROM sys.all_objects
        )
        SELECT
            'Bullhorn' AS source_system,
            LTRIM(RTRIM(ISNULL(c.firstName,'') + ' ' + ISNULL(c.lastName,''))) AS contractor_name,
            'GHR' AS vendor,
            ({system_case}) AS health_system,
            cc.name AS facility_name,
            NULL AS work_site,
            ISNULL(p.employmentType, 'Unknown') AS labor_type,
            -- Division lives on the client (see GetTrendData note).
            cc.customTextBlock1 AS division,
            NULL AS region,
            CAST(w.week_start AS DATE) AS billing_week_start,
            DATEADD(DAY, 6, CAST(w.week_start AS DATE)) AS billing_week_end,
            DATEADD(DAY, 6, CAST(w.week_start AS DATE)) AS item_date,
            TRY_CAST(p.hoursPerDay * 5 AS DECIMAL(10,2)) AS hours,
            p.status AS invoice_status,
            1 AS is_ghr
        FROM Weeks w
        INNER JOIN dbo.View_Placement p
            ON p.dateBegin <= DATEADD(DAY, 6, w.week_start)
            AND (p.dateEnd IS NULL OR p.dateEnd >= w.week_start)
        LEFT JOIN dbo.View_Candidate c ON p.candidateID = c.candidateID
        LEFT JOIN dbo.View_ClientCorporation cc ON p.clientCorporationID = cc.clientCorporationID
        WHERE p.isDeleted = 0
            AND p.status IN ({status_list})
            AND p.dateBegin IS NOT NULL
            AND ISNULL(p.hoursPerDay, 0) > 0
            AND {scope_filter}
        ORDER BY w.week_start, ({system_case})
    ''')
    columns = [column[0] for column in cursor.description]
    rows = []
    for row in cursor.fetchall():
        row_dict = dict(zip(columns, row))
        for date_col in ('billing_week_start', 'billing_week_end', 'item_date'):
            v = row_dict.get(date_col)
            if v:
                row_dict[date_col] = v.isoformat() if hasattr(v, 'isoformat') else str(v)
        if row_dict.get('hours') is not None:
            row_dict['hours'] = float(row_dict['hours'])
        rows.append(row_dict)
    conn.close()
    return rows


def _symplr_hours_data():
    """
    Returns Symplr ACTUAL hours rows from the orders (shift-level) table.
    One row per shift, aggregated weekly by worker + system.
    """
    conn = get_symplr_conn()
    if conn is None:
        return []
    cursor = conn.cursor()
    sys_case = symplr_system_case_expr('o.customerid')
    scope = symplr_scope_filter('o.customerid')
    division_case = symplr_division_case_expr('o.customerid')

    cursor.execute(f'''
        SELECT
            'Symplr' AS source_system,
            LTRIM(RTRIM(ISNULL(pt.firstname,'') + ' ' + ISNULL(pt.lastname,''))) AS contractor_name,
            'GHR' AS vendor,
            ({sys_case}) AS health_system,
            pc.clientname AS facility_name,
            NULL AS work_site,
            ISNULL(lt.nursetype, 'Unknown') AS labor_type,
            ({division_case}) AS division,
            pc.state AS region,
            CAST(DATEADD(DAY, 1 - DATEPART(WEEKDAY, CAST(o.jobdatestart AS DATE)),
                         CAST(o.jobdatestart AS DATE)) AS DATE) AS billing_week_start,
            CAST(DATEADD(DAY, 7 - DATEPART(WEEKDAY, CAST(o.jobdatestart AS DATE)),
                         CAST(o.jobdatestart AS DATE)) AS DATE) AS billing_week_end,
            CAST(o.jobdatestart AS DATE) AS item_date,
            TRY_CAST(o.totalbillhours AS DECIMAL(10,2)) AS hours,
            o.status AS invoice_status,
            1 AS is_ghr
        FROM dbo.orders o
        LEFT JOIN dbo.lt_order lt ON o.lt_orderid = lt.lt_orderid
        LEFT JOIN dbo.profile_temp pt ON lt.tempid = pt.recordid
        LEFT JOIN dbo.profile_client pc ON o.customerid = pc.recordid
        WHERE o.jobdatestart IS NOT NULL
            AND CAST(o.jobdatestart AS DATE) >= DATEADD(WEEK, -13, GETDATE())
            AND ISNULL(o.totalbillhours, 0) > 0
            AND {scope}
        ORDER BY o.jobdatestart
    ''')
    columns = [column[0] for column in cursor.description]
    rows = []
    for row in cursor.fetchall():
        row_dict = dict(zip(columns, row))
        for date_col in ('billing_week_start', 'billing_week_end', 'item_date'):
            v = row_dict.get(date_col)
            if v:
                row_dict[date_col] = v.isoformat() if hasattr(v, 'isoformat') else str(v)
        if row_dict.get('hours') is not None:
            row_dict['hours'] = float(row_dict['hours'])
        rows.append(row_dict)
    conn.close()
    return rows


def _non_msp_hours(req: func.HttpRequest) -> func.HttpResponse:
    rows = []
    errors = []
    try:
        rows.extend(_bullhorn_hours_data())
    except Exception as e:
        print(f"Bullhorn hours error: {e}")
        import traceback; traceback.print_exc()
        errors.append(f"bullhorn: {e}")
    try:
        rows.extend(_symplr_hours_data())
    except Exception as e:
        print(f"Symplr hours error: {e}")
        import traceback; traceback.print_exc()
        errors.append(f"symplr: {e}")
    print(f"non-MSP hours: {len(rows)} rows (errors: {errors or 'none'})")
    return func.HttpResponse(
        json.dumps({'rows': rows}, default=str),
        mimetype="application/json",
        status_code=200,
        )


def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    Returns per diem hours data from both B4HealthESR and STAGING_VNDLY_SPEND.
    Used for the Hours Analysis sub-tab in the per diem view.
    Covers a rolling 13-week window.
    Returns individual rows with: source_system, contractor_name, vendor,
    health_system, work_site, labor_type, billing_week_start, billing_week_end,
    item_date, hours, invoice_status, is_ghr
    """
    auth_error = require_allowed_domain(req)
    if auth_error:
        return auth_error

    if is_non_msp():
        return _non_msp_hours(req)

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
        rows = []

        # ============================================================
        # B4HealthESR — per diem hours, excluding non-shift WC types
        # WWE (Week Worked Ending) is the Saturday of the work week
        # ============================================================
        try:
            cursor.execute('''
                SELECT
                    'B4' AS source_system,
                    [Employee] AS contractor_name,
                    [Agency Name] AS vendor,
                    [Health System] AS health_system,
                    [Facility Name] AS facility_name,
                    [Unit] AS work_site,
                    [Care Type] AS labor_type,
                    DATEADD(DAY, -6, [WWE]) AS billing_week_start,
                    [WWE] AS billing_week_end,
                    [Work Date] AS item_date,
                    TRY_CAST([Hours] AS DECIMAL(10,2)) AS hours,
                    [Timesheet Status] AS invoice_status,
                    CASE
                        WHEN [Agency Name] LIKE 'GHR%'
                          OR [Agency Name] LIKE '%Planet Healthcare%'
                        THEN 1 ELSE 0
                    END AS is_ghr
                FROM dhc.B4HealthESR
                WHERE [Health System] IS NOT NULL
                    AND [Health System] <> 'Sunrise Senior Living Management (California)'
                    AND TRY_CAST([Hours] AS DECIMAL(10,2)) > 0
                    AND [WWE] >= DATEADD(WEEK, -13, GETDATE())
                    AND (
                        [WC] IS NULL OR (
                            [WC] NOT LIKE '%cancel%'

                            AND [WC] NOT LIKE 'Expense%'
                            AND [WC] NOT IN (
                                'Request Time Off', 'Unscheduled PTO', 'Sick Time',
                                'Call Out', 'Meeting', 'Education', 'Bonus'
                            )
                        )
                    )
                ORDER BY [WWE], [Health System]
            ''')

            columns = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                for date_col in ('billing_week_start', 'billing_week_end', 'item_date'):
                    v = row_dict.get(date_col)
                    if v:
                        row_dict[date_col] = v.isoformat() if hasattr(v, 'isoformat') else str(v)
                if row_dict.get('hours') is not None:
                    row_dict['hours'] = float(row_dict['hours'])
                rows.append(row_dict)

        except Exception as e:
            print(f"Error loading B4 hours data: {e}")
            import traceback
            traceback.print_exc()

        # ============================================================
        # STAGING_VNDLY_SPEND — per diem hours by billing cycle
        # ============================================================
        try:
            cursor.execute('''
                SELECT
                    'VNDLY' AS source_system,
                    CONCAT([Contractor First Name], ' ', [Contractor Last Name]) AS contractor_name,
                    [Vendor Company Name] AS vendor,
                    [Health System] AS health_system,
                    NULL AS facility_name,
                    [Work Site Name] AS work_site,
                    [Labor Type] AS labor_type,
                    [Billing Cycle Start Date] AS billing_week_start,
                    [Billing Cycle End Date] AS billing_week_end,
                    [Item Date] AS item_date,
                    TRY_CAST([Hours] AS DECIMAL(10,2)) AS hours,
                    [Invoice Status] AS invoice_status,
                    CASE
                        WHEN [Vendor Company Name] LIKE '%GHR%'
                          OR [Vendor Company Name] LIKE '%Planet Healthcare%'
                        THEN 1 ELSE 0
                    END AS is_ghr
                FROM dbo.STAGING_VNDLY_SPEND
                WHERE [Labor Type] LIKE '%Per Diem%'
                    AND [Health System] IS NOT NULL
                    AND TRY_CAST([Hours] AS DECIMAL(10,2)) > 0
                    AND [Billing Cycle End Date] >= DATEADD(WEEK, -13, GETDATE())
                ORDER BY [Billing Cycle Start Date], [Health System]
            ''')

            columns = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                for date_col in ('billing_week_start', 'billing_week_end', 'item_date'):
                    v = row_dict.get(date_col)
                    if v:
                        row_dict[date_col] = v.isoformat() if hasattr(v, 'isoformat') else str(v)
                if row_dict.get('hours') is not None:
                    row_dict['hours'] = float(row_dict['hours'])
                rows.append(row_dict)

        except Exception as e:
            print(f"Error loading VNDLY hours data: {e}")
            import traceback
            traceback.print_exc()

        conn.close()
        print(f"Returning {len(rows)} hours rows ({sum(1 for r in rows if r.get('source_system') == 'B4')} B4, {sum(1 for r in rows if r.get('source_system') == 'VNDLY')} VNDLY)")

        return func.HttpResponse(
            json.dumps({'rows': rows}, default=str),
            mimetype="application/json",
            status_code=200
        )

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return func.HttpResponse(
            json.dumps({'error': str(e), 'rows': []}),
            mimetype="application/json",
            status_code=500
        )
