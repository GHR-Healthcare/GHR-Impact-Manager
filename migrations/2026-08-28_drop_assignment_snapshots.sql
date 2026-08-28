-- Drop the assignment snapshot tables.
--
-- These captured one row per assignment per day so start-date movement could
-- be reconstructed, on the understanding that neither VMS recorded it. That
-- turned out to be wrong on both sides:
--
--   B4     dbo.HIST_B4HealthOrder already holds the movement history, with
--          years of depth. It was mistakenly read as Bullhorn-derived and
--          dropped; it is a history OF dhc.B4HealthOrder (all 6,440 of its
--          contracts exist there, and it includes affiliate rows). Restored
--          in 9148d2a.
--   VNDLY  STAGING_VNDLY_JOBS.[Start Date] is the job's planned start, against
--          which the work order's actual start gives the slip directly.
--
-- So the snapshot only ever offered forward-only history that already existed
-- with more depth. It captured 2 days (2026-08-19, 2026-08-20) before the
-- client-side trigger stopped firing — which is the other reason to remove it:
-- a table with a hole in it reads as history and is not.
--
-- Run against: ghrdhc

DROP TABLE IF EXISTS dhc.B4HealthOrder_Snapshot;
DROP TABLE IF EXISTS dbo.STAGING_VNDLY_WORKORDERS_Snapshot;

-- The grants go with the tables. Nothing else was granted to
-- svc-impact-manager on ghrdhc, so it returns to db_datareader only.
