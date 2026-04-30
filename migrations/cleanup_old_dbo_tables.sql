-- ============================================================================
-- Cleanup: drop legacy app-owned tables from CHANGES_DB.dbo
-- ============================================================================
-- Run this AGAINST CHANGES_DB (e.g. ghr_impact_mgr) once you've verified the
-- new ghrappdb.impactmgr.* tables are working in production. This is the
-- final step of the move documented in MIGRATE_TO_IMPACTMGR.md.
--
-- Migration commit reference: 5875b0f (initial move) + 16c4ef0 (auth)
-- Migration verified rows copied: see your migrate-impactmgr response.
--
-- After running this script:
--   1. Remove the CHANGES_DB env var from the Function App configuration.
--   2. Remove the MIGRATE_SECRET env var (no longer needed).
--   3. Confirm staticwebapp.config.json no longer has the anonymous route
--      for /api/migrate-impactmgr (already removed in the cleanup commit).
-- ============================================================================

USE ghr_impact_mgr;  -- adjust if your CHANGES_DB has a different name
GO

-- Safety check: refuse to run if any of these tables still has rows that
-- haven't been mirrored to the new home. (You can delete this block if you
-- already verified counts in Postman.)
DECLARE @msg NVARCHAR(500) = '';
IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='system_mappings')
    SET @msg = @msg + 'dbo.system_mappings still exists. ';
IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='pm_mappings')
    SET @msg = @msg + 'dbo.pm_mappings still exists. ';
IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='ghr_changes')
    SET @msg = @msg + 'dbo.ghr_changes still exists. ';
IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='ghr_history_snapshots')
    SET @msg = @msg + 'dbo.ghr_history_snapshots still exists. ';
IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='reviewed_contracts_rows')
    SET @msg = @msg + 'dbo.reviewed_contracts_rows still exists. ';
IF @msg = ''
    PRINT 'Nothing to drop. Tables already removed.';
ELSE
    PRINT 'Tables present, proceeding to drop: ' + @msg;
GO

DROP TABLE IF EXISTS dbo.system_mappings;
DROP TABLE IF EXISTS dbo.pm_mappings;
DROP TABLE IF EXISTS dbo.ghr_changes;
DROP TABLE IF EXISTS dbo.ghr_history_snapshots;
DROP TABLE IF EXISTS dbo.reviewed_contracts_rows;
GO

PRINT 'Cleanup complete. The CHANGES_DB env var and ghr_impact_mgr database can now be retired at your convenience.';
GO
