import azure.functions as func
import pyodbc
import os
import json
import re
from datetime import datetime
from shared_code.auth import require_allowed_domain
from shared_code.data_source import is_non_msp, get_bullhorn_conn, get_symplr_conn
from shared_code.bullhorn_systems import (
    build_system_case_expr,
    build_scope_filter,
)
from shared_code.symplr_systems import (
    build_system_case_expr as symplr_system_case_expr,
    build_scope_filter as symplr_scope_filter,
    build_division_case_expr as symplr_division_case_expr,
)

# Mapping of B4 health system names to VNDLY health system names
# Used to detect overlap and prefer VNDLY data when available
B4_TO_VNDLY_SYSTEM_MAP = {
    'Richmond University Medical Center': 'RUMC',
    'Holy Redeemer Hospital': 'Redeemer Health',
}


# Same status filter as GetTrendData — includes Completed/Termination so that
# historical months reflect work that was actually performed under those
# placements, not just placements that happen to be in flight today.
BULLHORN_BILLABLE_STATUSES = (
    'Approved', 'Pending Start', 'Cleared', 'Onboarding', 'Started',
    'Completed', 'Termination',
)


# Service-line bucketing for Bullhorn customText1 (profession).
# Buckets match the MSP service_line vocabulary so the Financials tab table
# uses the same Nursing / Allied / Locums / Non-Clinical / Other rollup.
BULLHORN_SERVICE_LINE_CASE = """
    CASE
        WHEN p.customText1 = 'RN' THEN 'Nursing'
        WHEN p.customText1 IN ('CRNA', 'Anesthesiologist') THEN 'Locums'
        WHEN p.customText1 IN ('Coder', 'CDI Specialist', 'Coding Auditor', 'Medical Coder') THEN 'Non-Clinical'
        WHEN p.customText1 IN ('Customer Service Rep', 'Clerical') THEN 'Non-Clinical'
        WHEN p.customText1 = 'Registered Dietitian' THEN 'Allied'
        ELSE 'Other'
    END
"""


def _bullhorn_financial_data(date_from_sql: str, date_to_sql: str):
    """
    Returns the Bullhorn monthlyData rows. Raises on error.

    Bullhorn doesn't expose actual billed hours, so we compute SCHEDULED revenue
    by walking weekly overlaps:
        weekly_revenue = clientBillRate * hoursPerDay * 5
        weekly_hours   = hoursPerDay * 5
        weekly_margin  = weekly_revenue * (reportedMargin / 100)
    """
    conn = get_bullhorn_conn()
    cursor = conn.cursor()
    system_case = build_system_case_expr('p.clientCorporationID')
    scope_filter = build_scope_filter('p.clientCorporationID')
    status_list = ', '.join("'" + s + "'" for s in BULLHORN_BILLABLE_STATUSES)

    cursor.execute(f'''
        ;WITH Weeks AS (
            SELECT TOP 80
                DATEADD(WEEK, 1 - ROW_NUMBER() OVER (ORDER BY (SELECT NULL)),
                        DATEADD(DAY, 1 - DATEPART(WEEKDAY, CAST(GETDATE() AS DATE)),
                                CAST(GETDATE() AS DATE))
                ) AS week_start
            FROM sys.all_objects
        ),
        PlacementWeeks AS (
            SELECT
                p.placementID,
                w.week_start,
                FORMAT(w.week_start, 'yyyy-MM') AS month,
                ({system_case}) AS health_system,
                cc.name AS facility,
                ISNULL(p.employmentType, 'Unknown') AS category,
                ({BULLHORN_SERVICE_LINE_CASE.strip()}) AS service_line,
                ISNULL(p.customTextBlock1, 'Unknown') AS division,
                CAST(NULL AS NVARCHAR(50)) AS region,
                LTRIM(RTRIM(ISNULL(c.firstName, '') + ' ' + ISNULL(c.lastName, ''))) AS worker_name,
                ISNULL(p.clientBillRate, 0) * ISNULL(p.hoursPerDay, 0) * 5 AS weekly_revenue,
                ISNULL(p.hoursPerDay, 0) * 5 AS weekly_hours,
                ISNULL(p.clientBillRate, 0) * ISNULL(p.hoursPerDay, 0) * 5
                    * (ISNULL(p.reportedMargin, 0) / 100.0) AS weekly_margin
            FROM Weeks w
            INNER JOIN dbo.View_Placement p
                ON p.dateBegin <= DATEADD(DAY, 6, w.week_start)
                AND (p.dateEnd IS NULL OR p.dateEnd >= w.week_start)
            LEFT JOIN dbo.View_Candidate c ON p.candidateID = c.candidateID
            LEFT JOIN dbo.View_ClientCorporation cc ON p.clientCorporationID = cc.clientCorporationID
            WHERE p.isDeleted = 0
                AND p.status IN ({status_list})
                AND p.dateBegin IS NOT NULL
                AND {scope_filter}
                AND w.week_start >= {date_from_sql}
                AND w.week_start < {date_to_sql}
        )
        SELECT
            'Bullhorn' AS source_system,
            month, health_system, facility, category, service_line, division, region,
            'GHR' AS vendor_type,
            COUNT(DISTINCT worker_name) AS headcount,
            SUM(weekly_revenue) AS estimated_billing,
            SUM(weekly_hours) AS hours_worked,
            SUM(weekly_margin) AS gross_margin
        FROM PlacementWeeks
        GROUP BY month, health_system, facility, category, service_line, division, region
        ORDER BY month, health_system, facility
    ''')
    columns = [column[0] for column in cursor.description]
    rows = []
    for row in cursor.fetchall():
        row_dict = dict(zip(columns, row))
        row_dict['estimated_billing'] = float(row_dict['estimated_billing'] or 0)
        row_dict['headcount'] = int(row_dict['headcount'] or 0)
        row_dict['hours_worked'] = float(row_dict.get('hours_worked') or 0)
        row_dict['gross_margin'] = float(row_dict.get('gross_margin') or 0)
        rows.append(row_dict)
    conn.close()
    return rows


# Symplr service-line bucketing based on lt_order.nursetype.
# Education staffing is mostly clinical-adjacent — PCA, RN, etc.
SYMPLR_SERVICE_LINE_CASE = """
    CASE
        WHEN lt.nursetype IN ('RN', 'LPN') THEN 'Nursing'
        WHEN lt.nursetype IN ('PCA', 'CNA', 'Aide', 'Paraprofessional') THEN 'Non-Clinical'
        WHEN lt.nursetype IN ('OT', 'PT', 'SLP', 'Therapy', 'Behavior Therapist') THEN 'Allied'
        ELSE 'Other'
    END
"""


def _symplr_financial_data(date_from_sql: str, date_to_sql: str):
    """
    Returns the Symplr monthlyData rows. Real billed dollars from the shift-level
    orders table — more accurate than the Bullhorn scheduled-revenue proxy.

    Headcount is distinct workers per month (lt_order joined to orders for the
    same window). gross_margin not exposed in Symplr — 0 in this output.
    """
    conn = get_symplr_conn()
    if conn is None:
        return []
    cursor = conn.cursor()
    sym_scope_o = symplr_scope_filter('o.customerid')
    sym_case_o = symplr_system_case_expr('o.customerid')
    sym_division_o = symplr_division_case_expr('o.customerid')

    # Aggregate actual billed shifts by month + (system, facility from join, category).
    # Workers come from lt_order so headcount is meaningful.
    cursor.execute(f'''
        SELECT
            'Symplr' AS source_system,
            FORMAT(CAST(o.jobdatestart AS DATE), 'yyyy-MM') AS month,
            ({sym_case_o}) AS health_system,
            pc.clientname AS facility,
            ISNULL(lt.nursetype, 'Unknown') AS category,
            ({SYMPLR_SERVICE_LINE_CASE.strip()}) AS service_line,
            ISNULL(({sym_division_o}), 'Unknown') AS division,
            pc.state AS region,
            'GHR' AS vendor_type,
            COUNT(DISTINCT lt.tempid) AS headcount,
            SUM(ISNULL(o.totalbillamount, 0)) AS estimated_billing,
            SUM(ISNULL(o.totalbillhours, 0)) AS hours_worked,
            0 AS gross_margin
        FROM dbo.orders o
        LEFT JOIN dbo.lt_order lt ON o.lt_orderid = lt.lt_orderid
        LEFT JOIN dbo.profile_client pc ON o.customerid = pc.recordid
        WHERE o.jobdatestart IS NOT NULL
            AND CAST(o.jobdatestart AS DATE) >= {date_from_sql}
            AND CAST(o.jobdatestart AS DATE) < {date_to_sql}
            AND {sym_scope_o}
        GROUP BY
            FORMAT(CAST(o.jobdatestart AS DATE), 'yyyy-MM'),
            ({sym_case_o}),
            pc.clientname,
            ISNULL(lt.nursetype, 'Unknown'),
            ({SYMPLR_SERVICE_LINE_CASE.strip()}),
            ({sym_division_o}),
            pc.state
        ORDER BY month, health_system, facility
    ''')
    columns = [column[0] for column in cursor.description]
    rows = []
    for row in cursor.fetchall():
        row_dict = dict(zip(columns, row))
        row_dict['estimated_billing'] = float(row_dict['estimated_billing'] or 0)
        row_dict['headcount'] = int(row_dict['headcount'] or 0)
        row_dict['hours_worked'] = float(row_dict.get('hours_worked') or 0)
        row_dict['gross_margin'] = float(row_dict.get('gross_margin') or 0)
        rows.append(row_dict)
    conn.close()
    return rows


def _non_msp_financial(req: func.HttpRequest, date_from_sql: str, date_to_sql: str) -> func.HttpResponse:
    """Run Bullhorn + Symplr financial queries independently, union the results."""
    monthly_data = []
    errors = []
    try:
        monthly_data.extend(_bullhorn_financial_data(date_from_sql, date_to_sql))
    except Exception as e:
        print(f"Bullhorn financial error: {e}")
        import traceback; traceback.print_exc()
        errors.append(f"bullhorn: {e}")
    try:
        monthly_data.extend(_symplr_financial_data(date_from_sql, date_to_sql))
    except Exception as e:
        print(f"Symplr financial error: {e}")
        import traceback; traceback.print_exc()
        errors.append(f"symplr: {e}")
    print(f"Returning {len(monthly_data)} non-MSP financial rows (errors: {errors or 'none'})")
    return func.HttpResponse(
        json.dumps({'monthlyData': monthly_data}, default=str),
        mimetype="application/json",
        status_code=200,
    )


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
    auth_error = require_allowed_domain(req)
    if auth_error:
        return auth_error
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

        if is_non_msp():
            return _non_msp_financial(req, date_from, date_to)

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
