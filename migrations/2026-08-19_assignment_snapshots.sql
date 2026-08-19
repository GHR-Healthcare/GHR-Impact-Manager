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
