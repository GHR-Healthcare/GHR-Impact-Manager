import azure.functions as func
import pyodbc
import os
import json


def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    Returns per diem hours data from both B4HealthESR and STAGING_VNDLY_SPEND.
    Used for the Hours Analysis sub-tab in the per diem view.
    Covers a rolling 13-week window.
    Returns individual rows with: source_system, contractor_name, vendor,
    health_system, work_site, labor_type, billing_week_start, billing_week_end,
    item_date, hours, invoice_status, is_ghr
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
                        WHEN [Agency Name] LIKE '%GHR%'
                          OR [Agency Name] LIKE '%Planet Healthcare%'
                        THEN 1 ELSE 0
                    END AS is_ghr
                FROM dhc.B4HealthESR
                WHERE [Health System] IS NOT NULL
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
