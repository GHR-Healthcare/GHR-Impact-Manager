-- Daily assignment snapshots, in the warehouse so other apps can read them.
--
-- Neither VMS records how far a start moved or how many times: B4's movement
-- history comes from Bullhorn placement matching (off the MSP path) and VNDLY
-- has no Original Start Date. One row per assignment per day makes movement
-- measurable — distinct start_date per assignment is the move count,
-- first-seen versus current is the slip.
--
-- Run against: ghrdhc
-- Volume: ~2,000 B4 + ~1,000 VNDLY rows per day.

CREATE TABLE dhc.B4HealthOrder_Snapshot (
    Contract_ID     NVARCHAR(120) NOT NULL,
    Snapshot_Date   DATE          NOT NULL,
    Start_Date      DATE          NULL,
    End_Date        DATE          NULL,
    Contract_Status NVARCHAR(100) NULL,
    Health_System   NVARCHAR(200) NULL,
    Agency          NVARCHAR(200) NULL,
    Awarded_Rate    DECIMAL(10,2) NULL,
    Delayed_Starts  NVARCHAR(20)  NULL,
    Captured_At     DATETIME2     NOT NULL,
    CONSTRAINT PK_B4HealthOrder_Snapshot PRIMARY KEY (Contract_ID, Snapshot_Date)
);
CREATE INDEX IX_B4HealthOrder_Snapshot_Start
    ON dhc.B4HealthOrder_Snapshot (Contract_ID, Start_Date);

CREATE TABLE dbo.STAGING_VNDLY_WORKORDERS_Snapshot (
    WOSystemKey    NVARCHAR(120) NOT NULL,
    Snapshot_Date  DATE          NOT NULL,
    Start_Date     DATE          NULL,
    End_Date       DATE          NULL,
    Current_Status NVARCHAR(100) NULL,
    Health_System  NVARCHAR(200) NULL,
    Vendor_Name    NVARCHAR(200) NULL,
    Bill_Rate      DECIMAL(10,2) NULL,
    Captured_At    DATETIME2     NOT NULL,
    CONSTRAINT PK_STAGING_VNDLY_WORKORDERS_Snapshot PRIMARY KEY (WOSystemKey, Snapshot_Date)
);
CREATE INDEX IX_VNDLY_WORKORDERS_Snapshot_Start
    ON dbo.STAGING_VNDLY_WORKORDERS_Snapshot (WOSystemKey, Start_Date);

-- Append only. No DDL: the app should not be creating warehouse objects.
GRANT SELECT, INSERT, UPDATE ON dhc.B4HealthOrder_Snapshot            TO [svc-impact-manager];
GRANT SELECT, INSERT, UPDATE ON dbo.STAGING_VNDLY_WORKORDERS_Snapshot TO [svc-impact-manager];

-- Once two days exist, movement reads like this:
--
--   SELECT Contract_ID,
--          COUNT(DISTINCT Start_Date) - 1              AS moves,
--          DATEDIFF(DAY, MIN(Start_Date), MAX(Start_Date)) AS days_moved
--   FROM dhc.B4HealthOrder_Snapshot
--   GROUP BY Contract_ID
--   HAVING COUNT(DISTINCT Start_Date) > 1;


-- ─────────────────────────────────────────────────────────────────────────
-- SCHEDULING
--
-- Preferred: append this to the existing daily warehouse load.
--
-- The load already runs every morning (see
-- BH_PLACEMENT_RAW_TO_B4HealthOrder_RUN_DIM, user GHRDataWarehouse) and
-- already writes to these schemas, so it needs no new credential, no new
-- auth path and no extra service. It also does not depend on anyone opening
-- the app: a day nobody signs in is a day of movement nobody can measure,
-- and a start that moves and moves back inside that gap is invisible
-- forever.
--
-- Idempotent — safe to run more than once a day, and safe to re-run after a
-- failed load. Run AFTER B4HealthOrder and STAGING_VNDLY_WORKORDERS refresh.
-- ─────────────────────────────────────────────────────────────────────────

INSERT INTO dhc.B4HealthOrder_Snapshot
    (Contract_ID, Snapshot_Date, Start_Date, End_Date, Contract_Status,
     Health_System, Agency, Awarded_Rate, Delayed_Starts, Captured_At)
SELECT LTRIM(RTRIM(o.Contract_ID)), CAST(GETDATE() AS DATE), CAST(o.Start_Date AS DATE),
       CAST(o.End_Date AS DATE), o.Contract_Status, o.Health_System, o.Agency,
       TRY_CAST(o.Awarded_Rate AS DECIMAL(10,2)), o.Delayed_Starts, SYSUTCDATETIME()
FROM dhc.B4HealthOrder o WITH (NOLOCK)
WHERE (o.Start_Date >= DATEADD(DAY,-90,CAST(GETDATE() AS DATE))
       OR o.End_Date >= CAST(GETDATE() AS DATE))
  AND LTRIM(RTRIM(o.Contract_ID)) <> ''
  AND NOT EXISTS (
      SELECT 1 FROM dhc.B4HealthOrder_Snapshot s
      WHERE s.Contract_ID = LTRIM(RTRIM(o.Contract_ID))
        AND s.Snapshot_Date = CAST(GETDATE() AS DATE));

INSERT INTO dbo.STAGING_VNDLY_WORKORDERS_Snapshot
    (WOSystemKey, Snapshot_Date, Start_Date, End_Date, Current_Status,
     Health_System, Vendor_Name, Bill_Rate, Captured_At)
SELECT CAST(w.WOSystemKey AS NVARCHAR(120)), CAST(GETDATE() AS DATE),
       TRY_CAST(w.[Start Date] AS DATE), TRY_CAST(w.[End Date] AS DATE),
       w.[Current Status], w.[Health System], w.[Vendor Name],
       TRY_CAST(w.[Bill Rate] AS DECIMAL(10,2)), SYSUTCDATETIME()
FROM dbo.STAGING_VNDLY_WORKORDERS w WITH (NOLOCK)
WHERE (TRY_CAST(w.[Start Date] AS DATE) >= DATEADD(DAY,-90,CAST(GETDATE() AS DATE))
       OR TRY_CAST(w.[End Date] AS DATE) >= CAST(GETDATE() AS DATE))
  AND w.WOSystemKey IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM dbo.STAGING_VNDLY_WORKORDERS_Snapshot s
      WHERE s.WOSystemKey = w.WOSystemKey
        AND s.Snapshot_Date = CAST(GETDATE() AS DATE));

-- Alternative if it cannot live in the ETL: a Logic App or Automation runbook
-- on a daily recurrence running the two statements above directly against
-- ghrdhc. Calling the app's /api/snapshot-assignments endpoint instead would
-- work but is the worse option — it needs x-ms-client-principal, so it means
-- a function key or an auth bypass for a job that only writes to SQL.
--
-- Day 1 was seeded manually on 2026-08-19: 2,018 B4 + 1,009 VNDLY rows.
