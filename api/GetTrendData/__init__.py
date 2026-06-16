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


# Bullhorn placement statuses that mean "the placement was awarded and ran
# (or is currently running)." Includes historical Completed/Termination so
# past weeks' headcount reflects them. Excludes Cancellation/Archive (never
# happened / hidden).
BULLHORN_ACTIVE_STATUSES = (
    'Approved', 'Pending Start', 'Cleared', 'Onboarding', 'Started',
    'Completed', 'Termination',
)


def _bullhorn_trend(req: func.HttpRequest) -> func.HttpResponse:
    """
    Trend data for the non-MSP (Bullhorn) instance.

    Shape mirrors the MSP response so the frontend doesn't branch:
      - assignments[]: weekly headcount source rows
      - pending[]: empty — non-MSP has no pre-active funnel (spec §5)
      - weekly_revenue[]: scheduled revenue per (week, system, vendor_type)
        computed as clientBillRate * hoursPerDay * 5 over overlap
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
        status_list = ', '.join("'" + s + "'" for s in BULLHORN_ACTIVE_STATUSES)

        # ============================================================
        # Bullhorn — Placements in rolling window
        # ============================================================
        assignments = []
        try:
            cursor.execute(f'''
                SELECT
                    'Bullhorn' AS source_system,
                    LTRIM(RTRIM(ISNULL(c.firstName,'') + ' ' + ISNULL(c.lastName,''))) AS worker_name,
                    ({system_case}) AS system,
                    cc.name AS facility,
                    'GHR' AS agency,
                    p.customText1 AS specialty,
                    p.employmentType AS category,
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
        except Exception as e:
            print(f"Error loading Bullhorn trend assignments: {e}")
            import traceback; traceback.print_exc()

        # ============================================================
        # Weekly scheduled revenue — Bullhorn
        # revenue per placement per week = clientBillRate * hoursPerDay * 5
        # ============================================================
        weekly_revenue = []
        try:
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
                    SUM(ISNULL(p.clientBillRate, 0) * ISNULL(p.hoursPerDay, 0) * 5) AS revenue
                FROM Weeks w
                INNER JOIN dbo.View_Placement p
                    ON p.dateBegin <= DATEADD(DAY, 6, w.week_start)
                    AND (p.dateEnd IS NULL OR p.dateEnd >= w.week_start)
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
        except Exception as e:
            print(f"Error loading Bullhorn weekly revenue: {e}")
            import traceback; traceback.print_exc()

        conn.close()
        print(f"Returning {len(assignments)} Bullhorn trend assignments, 0 pending, {len(weekly_revenue)} weekly revenue rows")

        return func.HttpResponse(
            json.dumps({
                'assignments': assignments,
                'pending': [],
                'weekly_revenue': weekly_revenue,
            }, default=str),
            mimetype="application/json",
            status_code=200,
        )
    except Exception as e:
        print(f"Bullhorn trend error: {e}")
        import traceback; traceback.print_exc()
        return func.HttpResponse(
            json.dumps({'error': str(e), 'assignments': [], 'pending': [], 'weekly_revenue': []}),
            mimetype="application/json",
            status_code=500,
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
        return _bullhorn_trend(req)

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

        # ============================================================
        # VNDLY - Assignments in rolling window
        # ============================================================
        try:
            cursor.execute('''
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
                WHERE [Current Status] = 'Active'
                    AND [Start Date] IS NOT NULL
                    AND ([End Date] IS NULL OR [End Date] >= DATEADD(WEEK, -4, GETDATE()))
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

        # ============================================================
        # VNDLY - Pending work orders (Applied, Offer Released, Verification)
        # ============================================================
        try:
            cursor.execute('''
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
                WHERE [Current Status] IN ('Applied', 'Offer Released', 'Verification In Progress')
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

        conn.close()

        print(f"Returning {len(assignments)} trend assignments, {len(pending)} pending, {len(weekly_revenue)} weekly revenue rows")

        return func.HttpResponse(
            json.dumps({
                'assignments': assignments,
                'pending': pending,
                'weekly_revenue': weekly_revenue,
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
