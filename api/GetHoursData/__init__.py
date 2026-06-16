import azure.functions as func
import pyodbc
import os
import json
from shared_code.auth import require_allowed_domain
from shared_code.data_source import is_non_msp
from shared_code.bullhorn_systems import (
    build_system_case_expr,
    build_scope_filter,
)


# Bullhorn doesn't expose actual timesheet/shift data in the views we have.
# We project scheduled hours from each placement: hoursPerDay × 5 per overlap
# week. Status filter mirrors GetTrendData so historical placements still
# contribute hours for the weeks they actually ran.
BULLHORN_HOURS_STATUSES = (
    'Approved', 'Pending Start', 'Cleared', 'Onboarding', 'Started',
    'Completed', 'Termination',
)


def _bullhorn_hours(req: func.HttpRequest) -> func.HttpResponse:
    """
    Hours data for the non-MSP (Bullhorn) instance.

    No actual timesheet data exists — we project scheduled hours by walking
    weekly overlaps for placements active in the last 13 weeks:

        hours_per_week = hoursPerDay * 5

    One row per (placement, week) in the window. Row shape mirrors MSP so
    reports that consume this endpoint render without branching.
    """
    try:
        conn = pyodbc.connect(
            f"DRIVER={{ODBC Driver 17 for SQL Server}};"
            f"SERVER={os.environ['BULLHORN_HOST']};"
            f"DATABASE={os.environ['BULLHORN_DB']};"
            f"UID={os.environ['BULLHORN_USER']};"
            f"PWD={os.environ['BULLHORN_PASSWORD']};"
            f"TrustServerCertificate=yes;"
            f"Encrypt=yes"
        )
        cursor = conn.cursor()

        system_case = build_system_case_expr('p.clientCorporationID')
        scope_filter = build_scope_filter('p.clientCorporationID')
        status_list = ', '.join("'" + s + "'" for s in BULLHORN_HOURS_STATUSES)

        rows = []
        try:
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
            print(f"Error loading Bullhorn hours data: {e}")
            import traceback; traceback.print_exc()

        conn.close()
        print(f"Returning {len(rows)} Bullhorn scheduled-hours rows")

        return func.HttpResponse(
            json.dumps({'rows': rows}, default=str),
            mimetype="application/json",
            status_code=200,
        )
    except Exception as e:
        print(f"Bullhorn hours error: {e}")
        import traceback; traceback.print_exc()
        return func.HttpResponse(
            json.dumps({'error': str(e), 'rows': []}),
            mimetype="application/json",
            status_code=500,
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
        return _bullhorn_hours(req)

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
