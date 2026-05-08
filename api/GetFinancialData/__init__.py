import azure.functions as func
import pyodbc
import os
import json
import re
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
        # Optional date range params (format: YYYY-MM)
        from_month = req.params.get('from')  # e.g. '2025-02'
        to_month = req.params.get('to')      # e.g. '2026-02'

        # Validate date format to prevent injection (strict YYYY-MM)
        date_pattern = re.compile(r'^\d{4}-\d{2}$')
        if from_month and not date_pattern.match(from_month):
            from_month = None
        if to_month and not date_pattern.match(to_month):
            to_month = None

        # Default: 13 months ending before current month
        if from_month:
            date_from = f"'{from_month}-01'"
        else:
            date_from = "DATEADD(MONTH, -13, DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1))"

        if to_month:
            # to_month is inclusive, so we go to the first day of the next month
            date_to = f"DATEADD(MONTH, 1, '{to_month}-01')"
        else:
            # Include the current (in-progress) month so the latest billing
            # cycles show up immediately rather than waiting until next month.
            date_to = "DATEADD(MONTH, 1, DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1))"

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
            cursor.execute(f'''
                -- VNDLY [Item Date] is the Billing Cycle End Date (Saturday).
                -- Bucketing by end-date pushes the cycle ending May 2 (which
                -- covers Apr 26-May 2 work) entirely into May, leaving end-of-
                -- month gaps in every system's monthly total. Bucketing by
                -- Billing Cycle Start Date attributes the cycle to the month
                -- when most of the work actually happened.
                -- Each STAGING_VNDLY_SPEND row has a unique [System-InvKey]
                -- (the table's primary key). The previous DISTINCT keyed off a
                -- subset of business columns and silently collapsed legitimate
                -- distinct rows when [Item ID]+worker+amount happened to match
                -- across cycles — losing ~46% of Cooper April. Including
                -- [System-InvKey] in DISTINCT preserves every real row while
                -- still defending against true upload duplicates if they ever
                -- occur (same key inserted twice).
                WITH DeduplicatedSpend AS (
                    SELECT DISTINCT
                        [System-InvKey],
                        [Item ID],
                        [Contractor First Name],
                        [Contractor Last Name],
                        [Billing Cycle Start Date],
                        [Billing Cycle End Date],
                        [Health System],
                        [Work Site Name] AS facility,
                        [Labor Type] AS category,
                        [Vendor Company Name],
                        [Item Date],
                        [Hours],
                        [Client Amount]
                    FROM dbo.STAGING_VNDLY_SPEND
                    WHERE [Billing Cycle Start Date] >= {date_from}
                        AND [Billing Cycle Start Date] < {date_to}
                        AND [Health System] IS NOT NULL
                )
                SELECT
                    'VNDLY' AS source_system,
                    FORMAT(DATEFROMPARTS(YEAR([Billing Cycle Start Date]), MONTH([Billing Cycle Start Date]), 1), 'yyyy-MM') AS month,
                    [Health System] AS health_system,
                    facility,
                    category,
                    CASE
                        WHEN category IN ('Nursing', 'Per Diem Nursing') THEN 'Nursing'
                        WHEN category IN ('Allied', 'Per Diem Allied') THEN 'Allied'
                        WHEN category = 'Physicians' THEN 'Locums'
                        WHEN category = 'Non-Clinical' THEN 'Non-Clinical'
                        ELSE 'Other'
                    END AS service_line,
                    CASE
                        WHEN [Vendor Company Name] LIKE '%GHR%' OR [Vendor Company Name] LIKE '%Planet Healthcare%'
                        THEN 'GHR' ELSE 'Affiliate'
                    END AS vendor_type,
                    COUNT(DISTINCT CONCAT([Contractor First Name], ' ', [Contractor Last Name])) AS headcount,
                    SUM(ISNULL(TRY_CAST([Client Amount] AS DECIMAL(18,2)), 0)) AS estimated_billing,
                    SUM(ISNULL(TRY_CAST([Hours] AS DECIMAL(18,2)), 0)) AS hours_worked
                FROM DeduplicatedSpend
                GROUP BY
                    FORMAT(DATEFROMPARTS(YEAR([Billing Cycle Start Date]), MONTH([Billing Cycle Start Date]), 1), 'yyyy-MM'),
                    [Health System],
                    facility,
                    category,
                    CASE
                        WHEN category IN ('Nursing', 'Per Diem Nursing') THEN 'Nursing'
                        WHEN category IN ('Allied', 'Per Diem Allied') THEN 'Allied'
                        WHEN category = 'Physicians' THEN 'Locums'
                        WHEN category = 'Non-Clinical' THEN 'Non-Clinical'
                        ELSE 'Other'
                    END,
                    CASE
                        WHEN [Vendor Company Name] LIKE '%GHR%' OR [Vendor Company Name] LIKE '%Planet Healthcare%'
                        THEN 'GHR' ELSE 'Affiliate'
                    END
                ORDER BY FORMAT(DATEFROMPARTS(YEAR([Billing Cycle Start Date]), MONTH([Billing Cycle Start Date]), 1), 'yyyy-MM'), [Health System]
            ''')

            columns = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                row_dict['estimated_billing'] = float(row_dict['estimated_billing'] or 0)
                row_dict['headcount'] = int(row_dict['headcount'] or 0)
                row_dict['hours_worked'] = float(row_dict.get('hours_worked') or 0)
                vndly_data.append(row_dict)
        except Exception as e:
            print(f"Error loading VNDLY financial data: {e}")
            import traceback
            traceback.print_exc()

        # ============================================================
        # B4Health - Monthly billings and headcounts from ESR data
        # Uses actual bill totals and hours worked per day
        # Pull ALL systems - overlap filtering done in Python below
        # ============================================================
        try:
            cursor.execute(f'''
                -- Transitioned-system dedupe: for systems where the contract has
                -- migrated B4 → VNDLY (Cooper, RUMC, Holy Redeemer), the same
                -- worker can appear in BOTH B4 ESR and VNDLY SPEND during/after
                -- the cutover. Two complications:
                --   1. B4 stores names "Last, First" while VNDLY stores
                --      "First Last" — names are normalized lower-case "first
                --      last" before keying.
                --   2. B4 has individual Work Dates (one per shift) while
                --      VNDLY has weekly billing cycles (Item Date = cycle end).
                --      A B4 shift on Wed Apr 15 is dupe with a VNDLY cycle
                --      ending Apr 18 covering Apr 12-18 — equality on date
                --      won't catch that. We use BETWEEN cycle_start and
                --      cycle_end to check whether the B4 work day falls
                --      inside any matching VNDLY cycle for that worker+system.
                WITH VNDLYTransitionedKeys AS (
                    SELECT DISTINCT
                        CASE
                            WHEN [Health System] LIKE '%Cooper%'   THEN 'cooper'
                            WHEN [Health System] LIKE '%RUMC%'
                              OR [Health System] LIKE '%Richmond%' THEN 'rumc'
                            WHEN [Health System] LIKE '%Redeemer%' THEN 'redeemer'
                            ELSE NULL
                        END AS sys_canon,
                        LOWER(LTRIM(RTRIM(
                            CONCAT([Contractor First Name], ' ', [Contractor Last Name])
                        ))) AS norm_worker,
                        CAST([Billing Cycle Start Date] AS DATE) AS cycle_start,
                        CAST([Billing Cycle End Date] AS DATE) AS cycle_end
                    FROM dbo.STAGING_VNDLY_SPEND
                    WHERE [Billing Cycle Start Date] >= {date_from}
                        AND [Billing Cycle Start Date] < {date_to}
                        AND ([Health System] LIKE '%Cooper%'
                          OR [Health System] LIKE '%RUMC%'
                          OR [Health System] LIKE '%Richmond%'
                          OR [Health System] LIKE '%Redeemer%')
                ),
                B4WithKey AS (
                    SELECT
                        [Employee],
                        [Work Date],
                        [Health System],
                        [Facility Name] AS facility,
                        [Care Type] AS category,
                        [Program] AS program,
                        [Agency Name],
                        [Bill Total],
                        [Hours],
                        CASE
                            WHEN [Health System] LIKE '%Cooper%'   THEN 'cooper'
                            WHEN [Health System] LIKE '%RUMC%'
                              OR [Health System] LIKE '%Richmond%' THEN 'rumc'
                            WHEN [Health System] LIKE '%Redeemer%' THEN 'redeemer'
                            ELSE NULL
                        END AS sys_canon,
                        -- Normalize "Last, First" → "first last"; otherwise lower the existing string
                        LOWER(LTRIM(RTRIM(
                            CASE
                                WHEN CHARINDEX(',', [Employee]) > 0
                                THEN
                                    LTRIM(RTRIM(SUBSTRING([Employee], CHARINDEX(',', [Employee]) + 1, 200)))
                                    + ' '
                                    + LTRIM(RTRIM(SUBSTRING([Employee], 1, CHARINDEX(',', [Employee]) - 1)))
                                ELSE [Employee]
                            END
                        ))) AS norm_worker
                    FROM dhc.B4HealthESR
                    WHERE [Work Date] >= {date_from}
                        AND [Work Date] < {date_to}
                        AND [Health System] IS NOT NULL
                        -- B4 data-entry bug: Sunrise shifts appear under both "Main" and
                        -- "(California)" labels (same facilities in VA/NC, not CA), causing
                        -- a 2x inflation. Drop the mislabeled rows until B4 team fixes.
                        AND [Health System] <> 'Sunrise Senior Living Management (California)'
                ),
                B4Filtered AS (
                    SELECT b.* FROM B4WithKey b
                    WHERE b.sys_canon IS NULL  -- not a transitioned system, keep as-is
                       OR NOT EXISTS (
                           SELECT 1 FROM VNDLYTransitionedKeys v
                           WHERE v.sys_canon  = b.sys_canon
                             AND v.norm_worker = b.norm_worker
                             AND CAST(b.[Work Date] AS DATE)
                                 BETWEEN v.cycle_start AND v.cycle_end
                       )
                ),
                DeduplicatedESR AS (
                    SELECT DISTINCT
                        [Employee],
                        [Work Date],
                        [Health System],
                        facility,
                        category,
                        program,
                        [Agency Name],
                        [Bill Total],
                        [Hours]
                    FROM B4Filtered
                )
                SELECT
                    'B4' AS source_system,
                    FORMAT(DATEFROMPARTS(YEAR([Work Date]), MONTH([Work Date]), 1), 'yyyy-MM') AS month,
                    [Health System] AS health_system,
                    facility,
                    category,
                    CASE
                        WHEN program LIKE '%Nursing%' THEN 'Nursing'
                        WHEN program LIKE '%Allied%' OR program LIKE '%Pharmacy%' THEN 'Allied'
                        WHEN program LIKE '%Physician%' OR program LIKE '%Advanced Practices%' THEN 'Locums'
                        WHEN program = 'Non-Clinical' OR program LIKE '%Information Technology%' THEN 'Non-Clinical'
                        ELSE 'Other'
                    END AS service_line,
                    CASE
                        WHEN [Agency Name] LIKE 'GHR%' OR [Agency Name] LIKE '%Planet Healthcare%'
                        THEN 'GHR' ELSE 'Affiliate'
                    END AS vendor_type,
                    COUNT(DISTINCT [Employee]) AS headcount,
                    SUM(ISNULL(TRY_CAST([Bill Total] AS DECIMAL(18,2)), 0)) AS estimated_billing,
                    SUM(ISNULL(TRY_CAST([Hours] AS DECIMAL(18,2)), 0)) AS hours_worked
                FROM DeduplicatedESR
                GROUP BY
                    FORMAT(DATEFROMPARTS(YEAR([Work Date]), MONTH([Work Date]), 1), 'yyyy-MM'),
                    [Health System],
                    facility,
                    category,
                    CASE
                        WHEN program LIKE '%Nursing%' THEN 'Nursing'
                        WHEN program LIKE '%Allied%' OR program LIKE '%Pharmacy%' THEN 'Allied'
                        WHEN program LIKE '%Physician%' OR program LIKE '%Advanced Practices%' THEN 'Locums'
                        WHEN program = 'Non-Clinical' OR program LIKE '%Information Technology%' THEN 'Non-Clinical'
                        ELSE 'Other'
                    END,
                    CASE
                        WHEN [Agency Name] LIKE 'GHR%' OR [Agency Name] LIKE '%Planet Healthcare%'
                        THEN 'GHR' ELSE 'Affiliate'
                    END
                ORDER BY FORMAT(DATEFROMPARTS(YEAR([Work Date]), MONTH([Work Date]), 1), 'yyyy-MM'), [Health System]
            ''')

            columns = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                row_dict['estimated_billing'] = float(row_dict['estimated_billing'] or 0)
                row_dict['headcount'] = int(row_dict['headcount'] or 0)
                row_dict['hours_worked'] = float(row_dict.get('hours_worked') or 0)
                b4_data.append(row_dict)
        except Exception as e:
            print(f"Error loading B4 financial data: {e}")

        conn.close()

        # ============================================================
        # Combine B4 and VNDLY data.
        # For systems that transitioned (RUMC, Holy Redeemer), include BOTH
        # sources — B4 and VNDLY cover different workers/date ranges during
        # the transition period so there is no double-counting risk.
        # Once fully transitioned, B4 simply has no rows for those months.
        # ============================================================
        monthly_data = list(vndly_data)

        for r in b4_data:
            # Always include B4 data regardless of whether VNDLY has data
            # for the same system/month (they cover non-overlapping workers)
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
