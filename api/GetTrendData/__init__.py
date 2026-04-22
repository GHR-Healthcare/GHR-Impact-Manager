import azure.functions as func
import pyodbc
import os
import json


def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    Returns trend data: a rolling window of assignments from both B4Health and VNDLY.
    Covers 6 weeks back through present (and beyond via end dates) to support
    4-week lookback + current week + 4-week forward projection in the frontend.
    """
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
            cursor.execute('''
                SELECT
                    CONVERT(VARCHAR(10),
                        DATEADD(DAY, 1 - DATEPART(WEEKDAY, CAST([Item Date] AS DATE)), CAST([Item Date] AS DATE)),
                        23) AS week_start,
                    [Health System] AS system,
                    CASE
                        WHEN [Vendor Company Name] LIKE '%GHR%' OR [Vendor Company Name] LIKE '%Planet Healthcare%'
                        THEN 'GHR' ELSE 'Affiliate'
                    END AS vendor_type,
                    SUM(ISNULL(TRY_CAST([Client Amount] AS DECIMAL(18,2)), 0)) AS revenue
                FROM dbo.STAGING_VNDLY_SPEND
                WHERE [Item Date] IS NOT NULL
                    AND CAST([Item Date] AS DATE) >= DATEADD(WEEK, -8, GETDATE())
                    AND [Health System] IS NOT NULL
                GROUP BY
                    DATEADD(DAY, 1 - DATEPART(WEEKDAY, CAST([Item Date] AS DATE)), CAST([Item Date] AS DATE)),
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
