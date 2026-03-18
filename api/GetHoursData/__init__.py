import azure.functions as func
import pyodbc
import os
import json


def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    Returns per diem hours data from STAGING_VNDLY_SPEND.
    Used for the Hours Analysis sub-tab in the per diem view.
    Covers a rolling 13-week window.
    Returns individual rows with: contractor, vendor, health_system,
    work_site, labor_type, billing_week_start, billing_week_end,
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

        try:
            cursor.execute('''
                SELECT
                    CONCAT([Contractor First Name], ' ', [Contractor Last Name]) AS contractor_name,
                    [Vendor Company Name] AS vendor,
                    [Health System] AS health_system,
                    [Work Site Name] AS work_site,
                    [Labor Type] AS labor_type,
                    [Billing Cycle Start Date] AS billing_week_start,
                    [Billing Cycle End Date] AS billing_week_end,
                    [Item Date] AS item_date,
                    TRY_CAST([Hours] AS DECIMAL(10,2)) AS hours,
                    [Invoice Status] AS invoice_status,
                    CASE
                        WHEN [Vendor Company Name] LIKE \'%GHR%\'
                          OR [Vendor Company Name] LIKE \'%Planet Healthcare%\'
                        THEN 1 ELSE 0
                    END AS is_ghr
                FROM dbo.STAGING_VNDLY_SPEND
                WHERE [Labor Type] LIKE \'%Per Diem%\'
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
            print(f"Error loading hours data: {e}")
            import traceback
            traceback.print_exc()

        conn.close()
        print(f"Returning {len(rows)} hours rows")

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
