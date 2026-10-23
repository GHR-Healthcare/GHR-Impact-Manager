/* ============================================================================
   Financials (Revenue) tab — one-time report, 2026-01-01 through 2026-06-30.

   Reproduces GetFinancialData's MSP path exactly: the same two source queries,
   the same service-line and vendor bucketing, and the same transitioned-system
   dedupe. Run against the POSITIONS_DB the app uses (the one holding both
   dhc.B4HealthESR and dbo.STAGING_VNDLY_SPEND).

   Range is half-open, matching the app: >= 2026-01-01 and < 2026-07-01, so
   June is included in full and nothing from July leaks in.

   Two things worth knowing before anyone reconciles this against a finance
   figure:

     - VNDLY bills on WEEKLY CYCLES and is bucketed by [Billing Cycle Start
       Date]; B4 bills per SHIFT and is bucketed by [Work Date]. A VNDLY cycle
       spanning a month boundary lands wholly in the month its cycle STARTED.
       The app does the same, so this matches the tab — but it is not a strict
       calendar-month cut of VNDLY revenue.

     - Headcount is DISTINCT WORKERS PER GROUP, so it is not additive. Summing
       the headcount column across months or service lines double-counts anyone
       appearing in more than one. Only billings and hours sum safely.
   ============================================================================ */

DECLARE @from DATE = '2026-01-01';
DECLARE @to   DATE = '2026-07-01';   -- exclusive

/* --------------------------------------------------------------------------
   VNDLY — actual client billings from the spend feed.
   DISTINCT guards against a true upload duplicate (same key inserted twice).
   -------------------------------------------------------------------------- */
WITH DeduplicatedSpend AS (
    SELECT DISTINCT
        [System-InvKey], [Item ID],
        [Contractor First Name], [Contractor Last Name],
        [Billing Cycle Start Date], [Billing Cycle End Date],
        [Health System],
        [Work Site Name] AS facility,
        [Labor Type]     AS category,
        [Vendor Company Name],
        [Item Date], [Hours], [Client Amount]
    FROM dbo.STAGING_VNDLY_SPEND
    WHERE [Billing Cycle Start Date] >= @from
      AND [Billing Cycle Start Date] <  @to
      AND [Health System] IS NOT NULL
),
VNDLY AS (
    SELECT
        'VNDLY' AS source_system,
        FORMAT(DATEFROMPARTS(YEAR([Billing Cycle Start Date]), MONTH([Billing Cycle Start Date]), 1), 'yyyy-MM') AS month,
        [Health System] AS health_system,
        facility,
        category,
        CASE
            WHEN category IN ('Nursing', 'Per Diem Nursing') THEN 'Nursing'
            WHEN category IN ('Allied', 'Per Diem Allied')   THEN 'Allied'
            WHEN category = 'Physicians'                     THEN 'Locums'
            WHEN category = 'Non-Clinical'                   THEN 'Non-Clinical'
            ELSE 'Other'
        END AS service_line,
        CASE
            WHEN [Vendor Company Name] LIKE '%GHR%'
              OR [Vendor Company Name] LIKE '%Planet Healthcare%' THEN 'GHR'
            ELSE 'Affiliate'
        END AS vendor_type,
        COUNT(DISTINCT CONCAT([Contractor First Name], ' ', [Contractor Last Name])) AS headcount,
        SUM(ISNULL(TRY_CAST([Client Amount] AS DECIMAL(18,2)), 0)) AS billings,
        SUM(ISNULL(TRY_CAST([Hours]         AS DECIMAL(18,2)), 0)) AS hours_worked
    FROM DeduplicatedSpend
    GROUP BY
        FORMAT(DATEFROMPARTS(YEAR([Billing Cycle Start Date]), MONTH([Billing Cycle Start Date]), 1), 'yyyy-MM'),
        [Health System], facility, category,
        CASE
            WHEN category IN ('Nursing', 'Per Diem Nursing') THEN 'Nursing'
            WHEN category IN ('Allied', 'Per Diem Allied')   THEN 'Allied'
            WHEN category = 'Physicians'                     THEN 'Locums'
            WHEN category = 'Non-Clinical'                   THEN 'Non-Clinical'
            ELSE 'Other'
        END,
        CASE
            WHEN [Vendor Company Name] LIKE '%GHR%'
              OR [Vendor Company Name] LIKE '%Planet Healthcare%' THEN 'GHR'
            ELSE 'Affiliate'
        END
),

/* --------------------------------------------------------------------------
   B4 — actual billings from ESR, with the transitioned-system dedupe.

   Cooper / RUMC / Holy Redeemer moved from B4 to VNDLY. During the overlap a
   worker can appear in both feeds for the same period, so a B4 shift is
   dropped when a VNDLY billing cycle for the same worker+system covers that
   work date. Matching is on a normalised name because B4 stores "Last, First"
   and VNDLY "First Last", and on BETWEEN cycle_start AND cycle_end because a
   B4 shift is one day while a VNDLY cycle is a week.
   -------------------------------------------------------------------------- */
VNDLYTransitionedKeys AS (
    SELECT DISTINCT
        CASE
            WHEN [Health System] LIKE '%Cooper%'   THEN 'cooper'
            WHEN [Health System] LIKE '%RUMC%'
              OR [Health System] LIKE '%Richmond%' THEN 'rumc'
            WHEN [Health System] LIKE '%Redeemer%' THEN 'redeemer'
        END AS sys_canon,
        LOWER(LTRIM(RTRIM(CONCAT([Contractor First Name], ' ', [Contractor Last Name])))) AS norm_worker,
        CAST([Billing Cycle Start Date] AS DATE) AS cycle_start,
        CAST([Billing Cycle End Date]   AS DATE) AS cycle_end
    FROM dbo.STAGING_VNDLY_SPEND
    WHERE [Billing Cycle Start Date] >= @from
      AND [Billing Cycle Start Date] <  @to
      AND ([Health System] LIKE '%Cooper%'
        OR [Health System] LIKE '%RUMC%'
        OR [Health System] LIKE '%Richmond%'
        OR [Health System] LIKE '%Redeemer%')
),
B4WithKey AS (
    SELECT
        [Employee], [Work Date], [Health System],
        [Facility Name] AS facility,
        [Care Type]     AS category,
        [Program]       AS program,
        [Agency Name], [Bill Total], [Hours],
        CASE
            WHEN [Health System] LIKE '%Cooper%'   THEN 'cooper'
            WHEN [Health System] LIKE '%RUMC%'
              OR [Health System] LIKE '%Richmond%' THEN 'rumc'
            WHEN [Health System] LIKE '%Redeemer%' THEN 'redeemer'
        END AS sys_canon,
        LOWER(LTRIM(RTRIM(
            CASE WHEN CHARINDEX(',', [Employee]) > 0
                 THEN LTRIM(RTRIM(SUBSTRING([Employee], CHARINDEX(',', [Employee]) + 1, 200)))
                      + ' ' + LTRIM(RTRIM(SUBSTRING([Employee], 1, CHARINDEX(',', [Employee]) - 1)))
                 ELSE [Employee] END
        ))) AS norm_worker
    FROM dhc.B4HealthESR
    WHERE [Work Date] >= @from
      AND [Work Date] <  @to
      AND [Health System] IS NOT NULL
      -- B4 data-entry bug: Sunrise shifts appear under both "Main" and
      -- "(California)" labels for the same VA/NC facilities, doubling them.
      AND [Health System] <> 'Sunrise Senior Living Management (California)'
),
DeduplicatedESR AS (
    SELECT DISTINCT * FROM (
        SELECT [Employee], [Work Date], [Health System], facility, category, program,
               [Agency Name], [Bill Total], [Hours]
        FROM B4WithKey WHERE sys_canon IS NULL
        UNION ALL
        SELECT b.[Employee], b.[Work Date], b.[Health System], b.facility, b.category, b.program,
               b.[Agency Name], b.[Bill Total], b.[Hours]
        FROM B4WithKey b
        LEFT JOIN VNDLYTransitionedKeys v
               ON v.sys_canon   = b.sys_canon
              AND v.norm_worker = b.norm_worker
              AND CAST(b.[Work Date] AS DATE) BETWEEN v.cycle_start AND v.cycle_end
        WHERE b.sys_canon IS NOT NULL
          AND v.sys_canon IS NULL
    ) all_b4
),
B4 AS (
    SELECT
        'B4' AS source_system,
        FORMAT(DATEFROMPARTS(YEAR([Work Date]), MONTH([Work Date]), 1), 'yyyy-MM') AS month,
        [Health System] AS health_system,
        facility,
        category,
        CASE
            WHEN program LIKE '%Nursing%'                                        THEN 'Nursing'
            WHEN program LIKE '%Allied%'    OR program LIKE '%Pharmacy%'          THEN 'Allied'
            WHEN program LIKE '%Physician%' OR program LIKE '%Advanced Practices%' THEN 'Locums'
            WHEN program = 'Non-Clinical'   OR program LIKE '%Information Technology%' THEN 'Non-Clinical'
            ELSE 'Other'
        END AS service_line,
        CASE
            WHEN [Agency Name] LIKE 'GHR%'
              OR [Agency Name] LIKE '%Planet Healthcare%' THEN 'GHR'
            ELSE 'Affiliate'
        END AS vendor_type,
        COUNT(DISTINCT [Employee]) AS headcount,
        SUM(ISNULL(TRY_CAST([Bill Total] AS DECIMAL(18,2)), 0)) AS billings,
        SUM(ISNULL(TRY_CAST([Hours]      AS DECIMAL(18,2)), 0)) AS hours_worked
    FROM DeduplicatedESR
    GROUP BY
        FORMAT(DATEFROMPARTS(YEAR([Work Date]), MONTH([Work Date]), 1), 'yyyy-MM'),
        [Health System], facility, category,
        CASE
            WHEN program LIKE '%Nursing%'                                        THEN 'Nursing'
            WHEN program LIKE '%Allied%'    OR program LIKE '%Pharmacy%'          THEN 'Allied'
            WHEN program LIKE '%Physician%' OR program LIKE '%Advanced Practices%' THEN 'Locums'
            WHEN program = 'Non-Clinical'   OR program LIKE '%Information Technology%' THEN 'Non-Clinical'
            ELSE 'Other'
        END,
        CASE
            WHEN [Agency Name] LIKE 'GHR%'
              OR [Agency Name] LIKE '%Planet Healthcare%' THEN 'GHR'
            ELSE 'Affiliate'
        END
),
Combined AS (
    SELECT * FROM VNDLY
    UNION ALL
    SELECT * FROM B4
)

/* ---- DETAIL: the grain the tab holds in memory ---------------------------- */
SELECT month, health_system, facility, service_line, category,
       vendor_type, source_system, headcount, billings, hours_worked
FROM Combined
ORDER BY month, health_system, facility, service_line, vendor_type;


/* ============================================================================
   ROLLUPS — swap the final SELECT above for whichever of these is wanted.
   Each re-uses the same Combined CTE, so re-run the whole script with the
   detail SELECT commented out and one of these in its place.
   ============================================================================ */

/* ---- 1. Monthly totals, GHR vs Affiliate (the tab's headline) -------------
SELECT month,
       SUM(CASE WHEN vendor_type = 'GHR'       THEN billings ELSE 0 END) AS ghr_billings,
       SUM(CASE WHEN vendor_type = 'Affiliate' THEN billings ELSE 0 END) AS affiliate_billings,
       SUM(billings)     AS total_billings,
       SUM(hours_worked) AS total_hours,
       CAST(100.0 * SUM(CASE WHEN vendor_type = 'GHR' THEN billings ELSE 0 END)
            / NULLIF(SUM(billings), 0) AS DECIMAL(5,1)) AS ghr_capture_pct
FROM Combined GROUP BY month ORDER BY month;
*/

/* ---- 2. Six-month total by health system ---------------------------------
SELECT health_system,
       SUM(CASE WHEN vendor_type = 'GHR'       THEN billings ELSE 0 END) AS ghr_billings,
       SUM(CASE WHEN vendor_type = 'Affiliate' THEN billings ELSE 0 END) AS affiliate_billings,
       SUM(billings) AS total_billings,
       CAST(100.0 * SUM(CASE WHEN vendor_type = 'GHR' THEN billings ELSE 0 END)
            / NULLIF(SUM(billings), 0) AS DECIMAL(5,1)) AS ghr_capture_pct
FROM Combined GROUP BY health_system ORDER BY total_billings DESC;
*/

/* ---- 3. By service line ---------------------------------------------------
SELECT service_line,
       SUM(CASE WHEN vendor_type = 'GHR'       THEN billings ELSE 0 END) AS ghr_billings,
       SUM(CASE WHEN vendor_type = 'Affiliate' THEN billings ELSE 0 END) AS affiliate_billings,
       SUM(billings) AS total_billings, SUM(hours_worked) AS total_hours
FROM Combined GROUP BY service_line ORDER BY total_billings DESC;
*/

/* ---- 4. Single six-month headline figure ----------------------------------
   Headcount here is a TRUE distinct count over the period, which is why it is
   recomputed from source rather than summed off Combined — summing the grouped
   headcount column would count a worker once per month, system and service
   line they appear in.

WITH Workers AS (
    SELECT DISTINCT LOWER(LTRIM(RTRIM(CONCAT([Contractor First Name], ' ', [Contractor Last Name])))) AS w
    FROM dbo.STAGING_VNDLY_SPEND
    WHERE [Billing Cycle Start Date] >= '2026-01-01' AND [Billing Cycle Start Date] < '2026-07-01'
    UNION
    SELECT DISTINCT LOWER(LTRIM(RTRIM([Employee])))
    FROM dhc.B4HealthESR
    WHERE [Work Date] >= '2026-01-01' AND [Work Date] < '2026-07-01'
      AND [Health System] <> 'Sunrise Senior Living Management (California)'
)
SELECT (SELECT SUM(billings) FROM Combined)                                        AS total_billings,
       (SELECT SUM(hours_worked) FROM Combined)                                    AS total_hours,
       (SELECT COUNT(*) FROM Workers)                                              AS distinct_workers;
*/
