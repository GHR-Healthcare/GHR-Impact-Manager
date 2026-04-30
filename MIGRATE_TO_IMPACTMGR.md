# Migration: Move app-owned tables to `ghrappdb.impactmgr.*`

This is a one-time migration from `{CHANGES_DB}.dbo.*` to `ghrappdb.impactmgr.*`.

## What's moving

| Old | New |
|---|---|
| `dbo.system_mappings` | `impactmgr.system_mappings` |
| `dbo.pm_mappings` | `impactmgr.pm_mappings` |
| `dbo.ghr_changes` | `impactmgr.changes` |
| `dbo.ghr_history_snapshots` | `impactmgr.history_snapshots` |
| `dbo.reviewed_contracts_rows` | `impactmgr.reviewed_contracts_rows` |

Read-only external tables (`dhc.B4HealthOrder`, `dhc.B4HealthESR`, `dbo.STAGING_VNDLY_*`) **stay where they are** — they're owned by other systems.

## Steps

1. **Create the database** in Azure (SSMS, Azure Portal, or `CREATE DATABASE ghrappdb`). Same server as the existing `CHANGES_DB`. Same SQL user (`DB_USER`) needs `db_owner` on the new database.

2. **Add env vars to the Function App configuration**:
   - `APPDB=ghrappdb`
   - `MIGRATE_SECRET=<some long random string>` — required to call the migration endpoint. Use a password generator; no one needs to memorize it.

   Do NOT remove `CHANGES_DB` yet — the migration endpoint still needs it as the source.

3. **Deploy** the latest code (the 7 app-owned endpoints already point at `APPDB.impactmgr.*`, with self-bootstrap that creates schema + tables on first hit). `CHANGES_DB` is now unused by these endpoints; only the migration endpoint reads from it.

4. **Dry-run the migration** to confirm the source/destination row counts:
   ```
   GET https://<your-app>/api/migrate-impactmgr?secret=<MIGRATE_SECRET>
   ```
   (Or pass via header: `x-migrate-secret: <MIGRATE_SECRET>`.)

   Returns JSON listing each table with `src_count` (rows in `CHANGES_DB.dbo.*`) and `dst_count_before` (rows in `ghrappdb.impactmgr.*`, will be `0` on first run).

5. **Run the migration for real**:
   ```
   GET https://<your-app>/api/migrate-impactmgr?secret=<MIGRATE_SECRET>&confirm=yes
   ```
   This:
   - Creates the `impactmgr` schema in `ghrappdb` if missing
   - Creates each destination table if missing
   - **Truncates** each destination table
   - Copies rows from source

   Idempotent — you can re-run with `?confirm=yes` and it'll re-truncate + re-copy.

6. **Verify**: hit each tab in the app (Settings to confirm system_mappings, Contracts tab to confirm reviewed contracts, etc.). Or query `impactmgr.*` directly in SSMS.

7. **Later, when comfortable**: drop the old `dbo.*` tables in `CHANGES_DB`, remove the `CHANGES_DB` env var, and delete the `MigrateImpactMgr` function folder.

## Rollback

Old `dbo.*` tables are untouched. Roll back by:
- Setting `APPDB` env var back to whatever `CHANGES_DB` points to
- Reverting the schema rename in code (search/replace `impactmgr.` → `dbo.` and re-deploy)

## Notes

- Auto-bootstrap means the live endpoints don't error on first run if you set `APPDB` before tables exist — they create their own table on demand.
- The migration endpoint accepts `?source_db=` if you ever need to copy from a different env-var-named source.
