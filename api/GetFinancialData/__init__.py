import azure.functions as func
import pyodbc
import os
import json
from datetime import datetime

# Mapping of B4 health system names to VNDLY health system names
# Used to detect overlap and prefer VNDLY data when available
B4_TO_VNDLY_SYSTEM_MAP = {
    'Richmond University Medical Center': 'RUMC',
    'Holy Redeemer Hospital': 'Redeemer Health',
}

def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    Returns financial data for the Financials tab:
    - Monthly billings by health system (GHR vs Affiliate)
    - Monthly headcounts by health system (GHR vs Total)
    - Fill rates by health system

    Combines data from B4HealthESR and STAGING_VNDLY_SPEND.
    Uses last 13 months of data.

    For systems that transitioned from B4 to VNDLY (e.g. RUMC, Redeemer),
    VNDLY data is preferred when available for a given month.
    B4 data is used as fallback for months before VNDLY coverage.
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
        vndly_data = []
        b4_data = []

        # ============================================================
        # VNDLY - Monthly billings and headcounts from Spend/Invoice data
        # Uses actual client amounts from STAGING_VNDLY_SPEND
        # ============================================================
        try:
            cursor.execute('''
                WITH DeduplicatedSpend AS (
                    SELECT
                        [Item ID],
                        [Contractor First Name],
                        [Contractor Last Name],
                        [Invoiced Date],
                        [Health System],
                        [Vendor Company Name],
                        [Client Amount],
                        ROW_NUMBER() OVER (PARTITION BY [Item ID] ORDER BY [Invoiced Date]) AS rn
                    FROM dbo.STAGING_VNDLY_SPEND
                    WHERE [Invoiced Date] >= DATEADD(MONTH, -13, DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1))
                        AND [Invoiced Date] < DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1)
                        AND [Health System] IS NOT NULL
                )
                SELECT
                    'VNDLY' AS source_system,
                    FORMAT(DATEFROMPARTS(YEAR([Invoiced Date]), MONTH([Invoiced Date]), 1), 'yyyy-MM') AS month,
                    [Health System] AS health_system,
                    CASE
                        WHEN [Vendor Company Name] LIKE '%GHR%' OR [Vendor Company Name] LIKE '%Planet Healthcare%'
                        THEN 'GHR' ELSE 'Affiliate'
                    END AS vendor_type,
                    COUNT(DISTINCT CONCAT([Contractor First Name], ' ', [Contractor Last Name])) AS headcount,
                    SUM(ISNULL(TRY_CAST([Client Amount] AS DECIMAL(18,2)), 0)) AS estimated_billing
                FROM DeduplicatedSpend
                WHERE rn = 1
                GROUP BY
                    FORMAT(DATEFROMPARTS(YEAR([Invoiced Date]), MONTH([Invoiced Date]), 1), 'yyyy-MM'),
                    [Health System],
                    CASE
                        WHEN [Vendor Company Name] LIKE '%GHR%' OR [Vendor Company Name] LIKE '%Planet Healthcare%'
                        THEN 'GHR' ELSE 'Affiliate'
                    END
                ORDER BY FORMAT(DATEFROMPARTS(YEAR([Invoiced Date]), MONTH([Invoiced Date]), 1), 'yyyy-MM'), [Health System]
            ''')

            columns = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                row_dict['estimated_billing'] = float(row_dict['estimated_billing'] or 0)
                row_dict['headcount'] = int(row_dict['headcount'] or 0)
                vndly_data.append(row_dict)
        except Exception as e:
            print(f"Error loading VNDLY financial data: {e}")

        # ============================================================
        # B4Health - Monthly billings and headcounts from ESR data
        # Uses actual bill totals and hours worked per day
        # Pull ALL systems - overlap filtering done in Python below
        # ============================================================
        try:
            cursor.execute('''
                WITH DeduplicatedESR AS (
                    SELECT DISTINCT
                        [Employee],
                        [Date Invoiced],
                        [Health System],
                        [Agency Name],
                        [Bill Total]
                    FROM dhc.B4HealthESR
                    WHERE [Date Invoiced] >= DATEADD(MONTH, -13, DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1))
                        AND [Date Invoiced] < DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1)
                        AND [Health System] IS NOT NULL
                )
                SELECT
                    'B4' AS source_system,
                    FORMAT(DATEFROMPARTS(YEAR([Date Invoiced]), MONTH([Date Invoiced]), 1), 'yyyy-MM') AS month,
                    [Health System] AS health_system,
                    CASE
                        WHEN [Agency Name] LIKE 'GHR%' OR [Agency Name] LIKE '%Planet Healthcare%'
                        THEN 'GHR' ELSE 'Affiliate'
                    END AS vendor_type,
                    COUNT(DISTINCT [Employee]) AS headcount,
                    SUM(ISNULL(TRY_CAST([Bill Total] AS DECIMAL(18,2)), 0)) AS estimated_billing
                FROM DeduplicatedESR
                GROUP BY
                    FORMAT(DATEFROMPARTS(YEAR([Date Invoiced]), MONTH([Date Invoiced]), 1), 'yyyy-MM'),
                    [Health System],
                    CASE
                        WHEN [Agency Name] LIKE 'GHR%' OR [Agency Name] LIKE '%Planet Healthcare%'
                        THEN 'GHR' ELSE 'Affiliate'
                    END
                ORDER BY FORMAT(DATEFROMPARTS(YEAR([Date Invoiced]), MONTH([Date Invoiced]), 1), 'yyyy-MM'), [Health System]
            ''')

            columns = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                row_dict['estimated_billing'] = float(row_dict['estimated_billing'] or 0)
                row_dict['headcount'] = int(row_dict['headcount'] or 0)
                b4_data.append(row_dict)
        except Exception as e:
            print(f"Error loading B4 financial data: {e}")

        conn.close()

        # ============================================================
        # Dedup: For systems that exist in both B4 and VNDLY,
        # prefer VNDLY data. Only include B4 data for months
        # where VNDLY has no data for that system.
        # ============================================================

        # Build a set of (month, vndly_system_name) pairs that VNDLY covers
        vndly_coverage = set()
        for r in vndly_data:
            vndly_coverage.add((r['month'], r['health_system']))

        # Filter B4 data: exclude rows where VNDLY already covers that system/month
        monthly_data = list(vndly_data)  # Start with all VNDLY data

        for r in b4_data:
            b4_system = r['health_system']
            vndly_name = B4_TO_VNDLY_SYSTEM_MAP.get(b4_system)

            if vndly_name:
                # This is a system that transitioned to VNDLY
                # Only include B4 data if VNDLY has NO data for this month
                if (r['month'], vndly_name) not in vndly_coverage:
                    monthly_data.append(r)
            else:
                # System is only in B4, always include
                monthly_data.append(r)

        vndly_count = len([r for r in monthly_data if r.get('source_system') == 'VNDLY'])
        b4_count = len([r for r in monthly_data if r.get('source_system') == 'B4'])
        print(f"Returning {len(monthly_data)} financial data rows (VNDLY: {vndly_count}, B4: {b4_count})")

        return func.HttpResponse(
            json.dumps({'monthlyData': monthly_data}, default=str),
            mimetype="application/json",
            status_code=200
        )

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return func.HttpResponse(
            json.dumps({'error': str(e), 'monthlyData': []}),
            mimetype="application/json",
            status_code=500
        )
