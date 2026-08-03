# GHR Impact Manager

## Version History

**2.0.4** - Trend tab: revenue filter parity + YoY overlay filter parity + PM view total tie-out

Tier 2 of the audit follow-ups — numbers that were wrong under specific filter combinations.

- **§5a Revenue overlay honors facility and region filters** (both instances). Previously the revenue series applied only the system filter and the non-MSP division proxy — facility, specialty, and region were dropped, while headcount honored them. So revenue and headcount described different populations whenever those filters were on, and `avgRevPerWorker` (revenue ÷ filtered headcount) inflated the Projected Revenue KPI under a facility filter. Bullhorn / Symplr / B4 / VNDLY revenue queries now emit `facility` and `region` columns and GROUP BY them; frontend applies both filters to the revenue overlay with NULL-passes-any semantics (Bullhorn `region` is NULL upstream; some MSP spend rows may not carry a facility either). Category/specialty remain structurally unfilterable on revenue — the rows aren't tagged and would need per-worker rollups to close.
- **§5c Prior-year overlay uses the same filter set as current-year**. `applyFiltersToYoY` was checking only systems + facilities + categories (with the same over-broadening `/nurs/` fallback we removed from the main filter in v2.0.3), and skipping division / region / specialty / profession / `isHiddenHealthSystem` entirely. So hidden systems appeared in the prior-year line but not the current line, and non-MSP filters silently didn't apply to the overlay. Now mirrors `filterRow`.
- **§5g PM view has its own total**. The PM view excludes `PM_EXCLUDED_SYSTEMS` (Jefferson, Sunrise Senior Living Management) but its Total row reused the all-systems `sysTableTotalRow`, so PM rows never summed to the total shown above them. New `pmTotalGhr(i)` / `pmTotalAff(i)` helpers sum across PM buckets (each worker maps to one PM, so sum-of-counts = deduplicated total). Tooltip on the Total cell now names the excluded systems.

**2.0.3** - Trend tab: fix VNDLY current-week cliff + Tier 1 silent-filter bugs

- **VNDLY terminal WOs use last spend week as effective end** (closes §2 of TREND_TAB_FOLLOWUPS.md). v2.0.2 capped `Ended` / `Ended by Job Close` at `GETDATE()` because raw `[End Date]` is the *originally scheduled* end; that overstated the current week by ~250 workers and created a hard cliff at next week. `STAGING_VNDLY_SPEND` has the ground truth — `VNDLY_EFFECTIVE_END_SQL` now uses `MAX(s.[Billing Cycle End Date])` per contractor. Terminal WOs with zero spend rows are excluded entirely via `VNDLY_HAS_SPEND_IF_TERMINAL_SQL` (they never actually ran — ~380 such rows were being counted). Live verification: current week 496 → 234, no cliff, smooth history 244→248→243→238→234→234→231→225. Fix applied to both `GetTrendData` and `GetYoYTrendData`.
- **Profession filter on Trend actually filters** (§3d). Was silently ignored — the dropdown appeared to work but did nothing. `GetTrendData` now selects `jo.customText1 AS profession` on the Bullhorn placement side (JOIN to View_JobOrder) and `lt.specialty` / `MAX(o.specialty)` on the two Symplr paths; `View.trend().filterRow` now checks it.
- **Region filter treats NULL region as "passes any region filter"** (§4a). Was silently dropping the entire Bullhorn book on non-MSP whenever any region was selected, because Bullhorn `region` is NULL upstream. Applied across 6 filter sites: positions match, KPIs, Stats, Trend `filterRow`, Trend outlook, `passesPendingFilters`.
- **Category filter no longer over-broadens** (§5h). `matchesTrendCategory` fell back to a broad `/nurs/` ∨ `/allied/` regex match after the exact/substring check, so filtering to "Travel Nursing" returned every Nursing category and Trend disagreed with every other tab. Fallback removed; substring check kept.
- **Non-MSP: trust the server's health system, don't re-key on facility** (§5d). `Utils.getHealthSystem` was keyword-matching against facility name too and could override the server's `build_system_case_expr` answer, so the same client could show under two different systems in the Account view vs the Revenue line. On non-MSP the helper now returns `dbHealthSystem` verbatim when the server supplied one.

**2.0.2** - Trend tab: finish the non-MSP Aff/AV strip + Symplr revenue overstatement

Audit of the Trend tab found the 2.0.0 "GHR / Affiliate strip" only reached 2 of the 8 places that render a `GHR / Aff` cell pair, so most of the tab still showed `N / 0` on non-MSP.

- **`splitCell()` helper single-sources the split.** The `GHR / Affiliate` markup was hand-rolled in 8 render paths and only `buildCatRow` + `buildTotalRow` checked the flag. Category subtotal rows, the whole Account view (rows, category sub-rows, total row) and the whole PM view (PM, PM→system, PM→system→category rows) all rendered a purple zero on non-MSP. All 22 cell sites now route through one helper, so the suppression can't be forgotten again.
- **Vendor group-by hidden on non-MSP.** Vendor is an affiliate-only axis; `sortedVendors` is always empty there, so the table was a lone "Total Affiliate — / 0" row. Button removed and the table build short-circuits. `__trendView` is also reset off `vendor` defensively.
- **Fill Rate toggle hidden on non-MSP.** Without an affiliate split or a pending funnel every fill-rate series is `total/total` — a flat 100% line across all 9 weeks.
- **Pending/unconfirmed UI removed on non-MSP**, per BULLHORN_PORT_SPEC.md §5 ("no Pending sub-rows, 'Unconfirmed' chart lines, or 'Expected Starts' KPI tile" — future-dated `Approved` rows *are* the pipeline). The `Total Unconfirmed` chart line drew exactly on top of `Total Pipeline`; the Weekly Summary showed 5 rows that were either permanently 0 or verbatim copies of another row (now 2); the `Expected Starts` tile was permanently ±0 and `Booked Starts` was numerically identical to `Total Headcount` (KPI row is now 2 tiles).
- **Symplr weekly revenue no longer counts unworked shifts.** The `weekly_revenue` query summed `totalbillamount` over every `dbo.orders` row in scope with no work filter, while the headcount query beside it requires `status = 'filled'` — so open/cancelled orders carrying a quoted amount inflated the revenue line. Now gated on `ISNULL(o.totalbillhours,0) > 0`, matching GetHoursData; chosen over `status = 'filled'` so a worked shift that later moves to another terminal status isn't dropped.
- **Bullhorn weekly revenue casts its inputs.** `SUM(clientBillRate * hoursPerDay * 5)` had no `TRY_CAST` even though the assignments query two blocks up does — one unparseable free-text rate failed the entire SUM.
- **KPI tiles compared against the wrong week.** Every tile read "vs `<next week>`" while the delta is next-vs-current, i.e. it named the week being measured rather than the baseline. Now names the current week. (Affects MSP too.)

Headcount correctness (both instances):

- **Breakdown rows now reconcile with the Total row.** Three separate causes, all fixed:
  1. *Verification In Progress ran too late.* The `catGhr` / `sysGhr` / `pmGhr` / `vendorAff` sets were converted to counts **before** the loop that promotes Verification-In-Progress rows into them, while the Total read `ghrNames.size` live at return time — so those workers landed in the totals and the chart but were absent from every Category / Account / PM / Vendor row. The accumulation logic existed as two hand-copied blocks (main loop + promotion), which is how they drifted; it's now a single `addRow()` closure and the count conversion happens after all accumulation.
  2. *Headcount was keyed on the bare worker name.* Two different people sharing a name at two systems merged into one head in the Total but stayed separate in the sub-rows (Total came out lower than the rows feeding it), while one person reported by two feeds double-counted because B4's "Last, First" never string-matched VNDLY's "First Last". Now keyed on `healthSystem + normalizeWorkerName(...)`, computed once per row instead of per week.
  3. *Nameless rows collapsed into one phantom worker.* Every row with a blank name (Symplr rows whose `profile_temp` join misses, B4 rows rendering as ", ") shared the key `''` and counted as a single head. Each now gets a unique key and counts as one.
- **Group subtotals take a distinct union.** Nursing/Allied/etc. subtotals summed per-category counts, so anyone holding two categories in the same week (e.g. a nurse on both travel and per diem) counted twice and the subtotal could exceed the Total. `weekData` now retains the raw key sets and subtotals union them.
- **Legend no longer goes stale.** Switching Group By or toggling Prior Year called `updateTrendChart` without rebuilding the legend, so the legend kept describing the previous view's series. Legend rendering moved inside `updateTrendChart` so no caller can skip it.
- **Series hide state follows the series, not the slot.** Chart.js tracks visibility by dataset index, so hiding "Affiliate" in Category view left an unrelated health system hidden after switching to Account view. Now tracked by label in `__trendHiddenLabels`.
- **Partial API failures are visible.** Every source query is individually try/except'd server-side and still returns 200, so a failed B4/VNDLY/Symplr query rendered as a clean chart quietly missing a whole book. The MSP path now returns the same `errors[]` contract the non-MSP path already did, and the Trend tab shows an amber banner listing which sources failed.

VNDLY history (MSP — the fake-upward-trend fix):

- **Historical weeks no longer shrink.** `GetTrendData` and `GetYoYTrendData` filtered VNDLY work orders to `[Current Status] = 'Active'` — a *current* status applied to *past* weeks. Anyone who finished and was flipped to a terminal status disappeared from the lookback entirely, and older weeks lost proportionally more rows than recent ones, manufacturing an upward trend that wasn't real. Over a full year of drift this is also why the prior-year overlay read far below the current year. Status vocabulary inventoried against live VNDLY and split into `VNDLY_RAN_STATUSES` = `Active` (250) + `Ended` (183) + `Ended by Job Close` (409); excluded as never-happened: `Rejected` (237), `Withdrawn` (57), `Offer Declined` (37), `Cancelled` (9).
- **Terminal work orders get their end date capped at today.** `[End Date]` on an ended work order is the *originally scheduled* end, not the actual stop date — `Ended by Job Close` rows carry end dates over a year out. Counting those as still-running would have traded the old understatement for an inflated current week and pipeline, so `VNDLY_EFFECTIVE_END_SQL` caps terminal rows at `GETDATE()`. Residual imprecision: a work order closed early still counts through today rather than through its actual close date. Eliminating that needs an actual-end-date column in the staging extract.
- **`Ready to Onboard` added to the VNDLY pending set.** It's a genuine pre-start stage (5 rows) that the trend pending query omitted even though the Pending tab's own `statusBucket()` classifies it. Pending statuses are now a named constant shared across the queries.
- **Prior-year window bounded on end date, not start date.** All five YoY source queries (MSP B4 + VNDLY, non-MSP Bullhorn + Symplr lt_order + Symplr orderless) filtered on `start >= -62 weeks`, which drops a long-running assignment that began before the window but was still running inside it — understating the oldest prior-year weeks. Now bounded on `end >= -61 weeks`, with the existing join supplying the upper bound.

**2.0.1** - Non-MSP: Bullhorn submissions wired + full Aff/AV strip + per-row Bullhorn/Symplr allowlist source

Follow-up polish on top of 2.0.0:

- **Submissions wired.** `_bullhorn_positions_data` now joins `dbo.JobSubmission` (raw table — every other core entity is `View_*` but not this one) and populates `ghrSubs` / `ghrDeclines` per open job. Declined statuses = `Client Rejected`, `GHR Rejected`, `Offer Rejected`, `Submission Withdrawn`. Terminal statuses (`Placed`, `Placement`) are excluded from both counts. Verified against live Bullhorn: 1,043 open jobs × 479 non-terminal submissions (~373 active / ~111 declined) — the non-MSP Active/Declines KPIs will stop showing 0/0.
- **Full AV/Aff strip on non-MSP** across everything the 2.0.0 first pass left in place: List tab KPI cards (`Active Sub Mix`, `GHR / Overall Fill / Active` → `Total Submissions`, `Fill Rate / Active`), List row `GHR / Aff` split cells on Active + Declines columns, Financials tab (Aff Billings / Aff Headcount cards dropped, Affiliate Vendor Billings + Affiliate Avg Bill Rate tables hidden, GHR Fill Rates section hidden entirely — always 100% by definition — labels retitled from "MSP Billings / MSP Headcounts" to "Billings / Headcounts"), Financials XLSX export (single "Billings" + "Headcounts" sheets instead of the 6-sheet GHR/Aff/Fill breakdown), Contracts Comparison Agency column, YoY Fill Rate chart (GHR + Affiliate line series dropped).
- **Per-row Bullhorn/Symplr source on the allowlist.** New Source dropdown per row in Settings → Non-MSP Clients — leaders pick Bullhorn (uses `clientCorporationID`) or Symplr (uses profile_client master `recordid`, expansion via MasterClientID applies). Table schema extended with `source NVARCHAR(20) NOT NULL DEFAULT 'bullhorn'`; primary key is `(source, client_id)` so the same numeric ID can exist under both sources. Backfill for pre-2.0.1 rows: `ALTER TABLE ADD ... DEFAULT 'bullhorn' WITH VALUES` runs idempotently in `ensure_schema()` so existing entries keep their original semantics.
- **Symplr scope resolution goes dynamic.** New `resolve_scope_master_ids(app_conn) = SYMPLR_SYSTEM_ROLLUP ∪ get_manual_symplr_allowlist_ids()`; `build_scope_filter(column, master_ids=...)` accepts the resolved set. All 6 non-MSP endpoints (GetTrendData, GetStatsData, GetPositions, GetHoursData, GetFinancialData, GetYoYTrendData) now call `symplr_resolve_scope(app_conn)` alongside the existing Bullhorn resolver and pass `master_ids=` through every `symplr_scope_filter` call. Same fail-open behavior as Bullhorn: if AppDB is unreachable the manual list silently falls back to empty, hardcoded rollup still populates the dashboard.
- **Header pill.** `[MSP]` / `[Non-MSP]` badge next to the title + non-MSP subtitle change to "Non-MSP · Bullhorn + Symplr Education book" so nobody has to check the browser tab to tell which instance they're on.

**2.0.0** - Non-MSP overhaul: dynamic scope, admin allowlist, cross-instance toggle, GHR/Affiliate strip
- **Dynamic Bullhorn scope.** Replaces the hardcoded 8-account rollup with a runtime UNION of three sources: (1) hardcoded rollup in `BULLHORN_SYSTEM_ROLLUP` — kept as-is, still defines display groupings like Cone Health = 4 IDs; (2) `impactmgr.bullhorn_client_allowlist` — new table, leader-editable via Settings → "Non-MSP Clients"; (3) auto-active — any Bullhorn client with an on-assignment placement right now AND whose `cc.customTextBlock1` contains at least one non-MSP division token (`NON_MSP_DIVISIONS`: Allied, Nursing, RevCycle Workforce, United, Locum Tenens, Technology, Search, Workforce Solutions, Planet Healthcare, Acute, Human Services). Without the division whitelist the auto-scope pulled in ~400 clients including Education-only and untagged historical records.
- **build_system_case_expr** now falls back to `cc.name` for IDs not in the hardcoded rollup, so auto-added / allowlisted clients render under their raw Bullhorn client name in Trend / Stats / Financials. Added missing `LEFT JOIN cc` on `GetTrendData.weekly_revenue` query so the fallback works there too.
- **New Function `GetClientAllowlist`** (`GET`/`POST /api/client-allowlist`) — non-MSP only, backed by `impactmgr.bullhorn_client_allowlist`. Follows the same "POST replaces all" pattern as `system-mappings`. Table auto-creates via `ensure_schema()` on first call.
- **Every non-MSP endpoint** (GetTrendData, GetPositions, GetStatsData, GetHoursData, GetFinancialData, GetYoYTrendData) now resolves the effective scope at the start of the request via `resolve_scope_client_ids(bullhorn_cursor, app_conn)` and passes the ID set into `build_scope_filter`. If the app DB is unreachable, the manual allowlist silently falls back to empty (auto-active + hardcoded rollup still populate the dashboard).
- **Settings modal** gains a third tab, "Non-MSP Clients", visible only on the non-MSP instance. Add/remove client IDs with optional display-name override and notes; POST saves the full list.
- **GHR ↔ Affiliate strip on non-MSP UI.** All non-MSP records are GHR direct staffing, so the GHR vs Affiliate distinction is meaningless there. On the non-MSP instance the Trend chart now hides the GHR + Affiliate series (keeps Total headcount + Revenue), the Category / Account / PM / Vendor breakdown tables drop the "GHR / Affiliate" split cells and the "GHR Capture %" row, and the "Category" heading no longer shows the split legend.
- **MSP ↔ non-MSP instance toggle** in the header. Reads `otherInstanceUrl` and `otherInstanceLabel` from `/api/get-config`; each instance's Azure config sets `OTHER_INSTANCE_URL` pointing at its sibling. Falls back to the ghrhealthcare.com custom hostnames so a fresh deploy still works before the env var is set. Hidden if config has no URL.
- **Azure env vars bumped** on both instances: `APP_VERSION=2.0.0`, `OTHER_INSTANCE_URL` cross-linked between `impactmgr.ghrhealthcare.com` and `impactmgr-nonmsp.ghrhealthcare.com`.
- **New helper** `get_appdb_conn()` in `data_source.py` — reads `DB_HOST` / `APPDB` / `DB_USER` / `DB_PASSWORD`; returns None if unset (dev/local safe).

Diagnostic snapshot at time of ship (2026-07-29): non-MSP scope resolves to 388 auto-active + 15 hardcoded = 388 unique client IDs. Bullhorn categories flowing through: Travel (1,115), Remote (623), Local (550), PRN (339), Permanent (218). Total trend rows: ~2,845.

Deferred to a follow-up: (1) non-MSP-specific `catGroupDefs` so Bullhorn's Travel/PRN/Remote/Local/Permanent categories get their own breakdown rows instead of falling into "Other"; (2) rework of non-MSP KPI card labels ("GHR / Overall Fill / Active", "Active Sub Mix (GHR% / AV%)") which still show GHR/Affiliate framing.

**1.8.7** - Wire Division / Region filters into Trend tab
- Trend tab's `filterRow` only checked systems/facilities/categories/specialties, so picking Division = Rev Cycle did nothing and Symplr (Education) rows stayed in the chart, category breakdown, and outlook. Now filters by `division` (via `Utils.matchesSelectedDivisions`, which splits Bullhorn's comma-separated `customTextBlock1`) and `region`. Applied in three places: the assignment/pending `filterRow`, the Outlook forecast iteration over `trendData.assignments`, and `passesPendingFilters`.
- Weekly revenue overlay (orange line) also now respects Division. Revenue rows aren't division-tagged, so when a Division filter is active we infer from `source_system`: Symplr rows count only when Education is selected; Bullhorn rows count only when a non-Education division is selected.

**1.8.6** - Non-MSP browser tab title
- Sets `document.title` to "GHR Impact Manager — Non-MSP" on the non-MSP instance (was "GHR Impact Manager" on both, so they were indistinguishable when open in adjacent tabs). MSP unchanged.

**1.8.5** - Fix Symplr revenue GROUP BY + cap non-MSP open positions to 45 days
- Symplr trend `weekly_revenue` query was erroring 42000/144 (column not in aggregate/GROUP BY). Root cause: SQL Server treats each interpolation of a CASE-with-subquery as a distinct expression, so the SELECT and GROUP BY instances didn't match. Precomputed week_start and system in a CTE so GROUP BY references plain columns.
- Non-MSP open positions now capped to items opened in the last 45 days across all four sources: Bullhorn `View_JobOrder.dateAdded`, Symplr `lt_order.date_entered` (status='open'), Symplr orderless `orders.datetimecreated` (status='open'), and Symplr uncovered shifts under filled lt_orders (`orders.datetimecreated`). Stale postings drop off automatically.

**1.8.4** - Non-MSP filter fixes: Bullhorn division source + Symplr trend visibility
- Bullhorn `division` now sourced from `View_ClientCorporation.customTextBlock1` (client-level, comma-separated list) instead of `View_Placement.customTextBlock1` which is always NULL. Applied across GetTrendData, GetStatsData, GetYoYTrendData, GetFinancialData, GetHoursData, GetPositions.
- Frontend `Utils.matchesSelectedDivisions` splits the comma-separated string when building the dropdown values and when matching records against selected divisions. A client tagged "Allied,Nursing,RevCycle Workforce" matches any of those three filter selections.
- Division filter dropdown moved to first slot on non-MSP (was after Specialty)
- `_symplr_trend_data` now wraps each of its three queries (lt_order / orderless orders / weekly revenue) in its own try/except so one bad query doesn't zero out the Symplr contribution. Errors are surfaced in the JSON response for diagnosis.

**1.8.3** - Non-MSP: Division / Profession / Region filter UI (PR 2 of 2)
- New filter dropdowns visible only on the non-MSP instance: Division, Profession, Region
- Data-driven: dropdown values come from actual records (Bullhorn `customTextBlock1`, Symplr rollup, Symplr `profile_client.state`)
- "All Nursing / All Allied" header suppressed on non-MSP (the MSP keyword buckets don't map to Bullhorn/Symplr categories)
- Filter logic wired into `getFilteredJobs` (list view), `kpis` (Stats KPI tiles), and cascading dropdown narrowing
- MSP UI unchanged — the new selections stay empty on MSP so their match conditions are no-ops
- Job records on the frontend now carry `division`, `profession`, `region` fields (populated from API on non-MSP, empty strings on MSP)

**1.8.2** - Fix v1.8.1 regression: B4 disappeared from MSP financial
- v1.8.1 moved the B4 dedup to a multi-statement batch (SELECT INTO #temp + CREATE INDEX + SELECT) so pyodbc tripped on the result-set handling and returned 0 B4 rows
- Reverted to a single-statement CTE but kept the optimizer-friendly anti-join. Split B4 into two CTE branches: B4NonTransitioned (no join, fast path for the bulk of rows) and B4TransitionedKept (LEFT JOIN against the dedup keys, restricted to Cooper / RUMC / Holy Redeemer)
- Same logical result as the original NOT EXISTS, no temp table, stable plan across date ranges

**1.8.1** - Fix MSP financial: optimize B4 dedup so date-range changes don't gateway-timeout
- The transitioned-system dedup CTE (NOT EXISTS against `VNDLYTransitionedKeys`) was plan-sensitive — small changes in the from/to date range could flip SQL Server's plan choice and push the query past the 45s SWA gateway timeout, returning 500 to the frontend (which kept the stale default data)
- Materialized the VNDLY keys into a `#vndly_keys` temp table with an index on `(sys_canon, norm_worker, cycle_start, cycle_end)`, and switched the dedup from `NOT EXISTS` to `LEFT JOIN ... WHERE v.sys_canon IS NULL` so the optimizer uses a stable seek every time
- Equivalent results; faster and deterministic regardless of date range

**1.8.0** - Non-MSP: backend emits `division` + `region` fields (PR 1 of 2)
- All non-MSP endpoints now return `division` (from Bullhorn `customTextBlock1` per placement/JobOrder, from Symplr rollup config per system) and `region` (Symplr `profile_client.state`)
- New `build_division_case_expr` helper in `symplr_systems.py` parallels `build_system_case_expr`
- `GetSystemMappings` exposes `division` per system in its JSON
- Backend only — frontend filter UI changes coming in PR 2

**1.7.10** - Fix: GetSystemMappings 500 + Symplr positions silently dropped
- `GetSystemMappings` was throwing on non-MSP because it read `entry['client_ids']` from `SYMPLR_SYSTEM_ROLLUP` — v1.7.7 renamed that field to `master_ids`. Fixed to surface `master_ids` as `client_ids` in the JSON response shape
- `_symplr_positions_data()` now runs each of its three queries (lt_order open / orderless open / uncovered shifts) in its own try/except. Previously, a single SQL error killed the whole Symplr positions read — the non-MSP list view was showing only the ~30 Bullhorn positions and zero Symplr
- Fixed the uncovered-shifts query's invalid `MAX(CASE-with-subquery)` by including `lt.clientid` in `GROUP BY` so the system_case expression can run non-aggregated. Same de-MAX-ing applied to the orderless-open query

**1.7.9** - GetPositions: open shifts under lt_orders
- `GetPositions` now picks up open future shifts where the parent `lt_order` is itself NOT open (avoids double-counting requisitions we already surface). 141 such uncovered-shift slots in scope today; each lt_orderid becomes one position row with `num_positions = COUNT(open shifts)` and `time_type = 'Uncovered Shifts'`

**1.7.8** - Symplr orderless orders folded into headcount
- 18% of Symplr `orders` (over 6mo) have `lt_orderid IN (0, NULL)` — per-shift bookings with no `lt_order` parent. These workers (116 distinct, 120 worker-client pairs) were invisible to every headcount-side endpoint
- `GetTrendData`, `GetStatsData`, `GetYoYTrendData`, `GetPositions` now UNION their lt_order-derived data with an orders-derived path, aggregated by worker+client so per-shift work collapses to one synthetic assignment row
- For `GetPositions`, orderless `open` orders aggregate by (customer, specialty, nursetype) with `num_positions = COUNT(shifts)`

**1.7.7** - Non-MSP fixes: Symplr master expansion + Pending/Per Diem guards
- `symplr_systems.py` now expands by `MasterClientID` instead of a flat list of recordids — sub-orgs (e.g. "DCIU ECE - <school>") auto-include without code changes
- Added missing DCIU masters (`122454 DCIU School Age`, `122455 DCIU ECE`) — covers the ~370 placements that were being dropped from the rollup
- `GetPendingData` and `GetPerDiemData` short-circuit with empty payloads on non-MSP instead of attempting an MSP DB connection that would 500
- Trend table's future-week drill-through to Pending is now disabled on non-MSP — was rendering as a clickable link that the view-toggle bounced back to List view

**1.7.6** - Remove Pending sub-rows from Trend table
- Reverted yesterday's `b2839d8` — per-category/system/PM/vendor "Pending" sub-rows added too much visual noise (mostly empty cells)
- Pending statuses still flow into the chart "Unconfirmed" lines, the "Expected Starts (unconfirmed)" KPI tile, and the summary rows (unchanged from before yesterday)
- VIP fold-in (from v1.7.4) preserved

**1.7.5** - SWA Free build fix + tenant domain allowlist
- Removed the `auth` block from `staticwebapp.config.json` (Microsoft tightened the SWA validator on 2026-05-27 and the block was inactive anyway because the openIdIssuer was still the `YOUR_TENANT_ID` placeholder)
- Added `api/shared_code/auth.py` enforcing a domain allowlist: `ghrhealthcare.com`, `unitedanesthesia.com`, `ghreducation.com`. Every API endpoint now returns 401/403 if the SWA principal's email isn't in one of these domains
- Closes the gap where SWA Free's built-in Microsoft provider was accepting any Microsoft account

**1.7.4** - VIP rolled into confirmed + vendor chart top-5 cap
- "Verification In Progress" VNDLY workorders now count in the main GHR/Affiliate row (and the chart's confirmed line) instead of the Pending sub-row, since they're far enough along to treat as confirmed
- Vendor-view line chart now plots only the top 5 affiliate vendors by total headcount across the visible window; the table still lists every vendor

**1.7.3** - GHR Capture % row on Trend table
- New "GHR Capture %" row above Total on the Category / Account / PM views (GHR / (GHR + Affiliate))
- WoW delta in percentage points highlights whether capture is improving across future projection weeks

**1.7.2** - Trend chart extended one more week forward
- Headcount trend now shows 4 back + current + 4 forward (was 3 forward)

**1.7.1** - Pending $ KPIs + richer revenue tooltip
- Pending tab has a new 4-card KPI row: pipeline weekly run rate with Δ vs 4-wk actual avg, next-3-wk expected $, last complete week actual, baseline avg
- Pipeline $ computed as `bill_rate × weekly_hours` on pending + assignment workorders (B4 uses Awarded_Rate/Hours_per_Peek, VNDLY uses Bill Rate with 36hr/wk default)
- Trend chart revenue tooltip now shows week-over-week Δ% and a GHR/Affiliate split line
- Fixed GetTrendData reading stale `B4HealthESR2` — switched to the live `B4HealthESR` table

**1.7.0** - Revenue line on Trend chart
- Trend chart now overlays actual weekly revenue on a secondary y-axis (gold line, $ formatting)
- Revenue sourced from B4HealthESR2 Bill Total + STAGING_VNDLY_SPEND Client Amount, grouped by Sun-Sat week
- Honors the current system filter; transitioned systems dedupe (VNDLY wins when both report same week)
- Pending tab stale-record filter: records with no milestone activity in 60 days are hidden

**1.6.3** - Pending outlook rewired to match Trend tab deltas
- Outlook counts now come from workorder start dates (Trend tab data source) instead of submission RTO dates, so numbers line up with the +/- deltas shown on the Trend breakdown
- Workers deduped per system+status+week to match Trend's distinct-headcount logic
- Tab order: Trend and Pending are now adjacent; Financials moved one right
- API change: `/api/GetTrendData` now returns the `status` column on assignments and pending rows

**1.6.2** - Pending 3-week outlook split by status
- Each outlook week now has 3 sub-columns (Submitted / Offer Pending / RTO), color-coded to match the pipeline groups
- Expected-start date falls back to VNDLY's Ready-to-Onboard date when RTO is empty

**1.6.1** - Pending tab summary redesign + detail table width fix
- Pending summary table now groups Submitted / Offer Pending / RTO columns visually with color-coded headers and a legend
- Added 3-week outlook columns to the summary, bucketing GHR/Affiliate pending candidates by expected RTO date
- Totals row across all systems
- Detail table now fills the full container width (switched wrapper to `overflow-x-auto` + `min-w-full`)

**1.6.0** - Holt's forecast + clearer projection styling
- Swapped projection formula from linear regression to Holt's exponential smoothing (weights recent weeks, reacts to trend changes)
- Projection line now anchors at the last actual value so it connects seamlessly to the actuals line
- Pipeline / Pending / Projection dashed patterns are now visually distinct (fine dots / dash-dot / long dashes)

**1.5.0** - Trend Tab with Headcount Projection
- New Trend tab with 4-week lookback and 4-week forward projection
- Line chart showing Actual HC, Pipeline (confirmed), and linear trend projection
- Weekly summary table with actuals, pipeline, and trend projection columns
- Follows all active filters (health system, facility, category, specialty)
- New API endpoint: `/api/GetTrendData` (route: `trend-data`)

**1.4.0** - Per Diem Analytics Tab
- New Per Diem tab with weekly metrics per health system (headcount, actives worked, % worked, shifts, shifts/nurse avg)
- Data sourced from B4Health and VNDLY systems
- Line charts for % of Actives Worked and Shifts/Nurse Average trends
- Date range picker for custom reporting periods

**1.3.3** - Connection validation & cascading filters
- Require database connection before allowing interaction
- Cascading dropdown filters (selecting one filter updates others to show relevant options)
- App version moved to environment variable

**1.3.2** - Next step history & stats filtering
- Next step history modal to view all previous next steps for a position
- Stats page now respects category and specialty filters

**1.3.1** - Stats data improvements
- Updated SQL queries for better stats accuracy
- Filter active assignments by 'Closed And Awarded' status

**1.3.0** - Connection status & health system matching
- Connection status indicator in header
- Connection lost modal blocks interaction until refresh
- Robust health system matching logic

**1.2.2** - VNDLY Integration
- Added VNDLY data source for positions and submissions
- Bill rate and facility fallback handling

**1.2.1** - Stats & KPI filtering
- Stats and KPI cards now obey filter selections
- Privacy/redaction mode on by default

**1.2.0** - Mobile & filter improvements
- Mobile view improvements
- Filter dropdown UI updates
- Multiple filter bug fixes

**1.1.0** - Fill rate & submissions
- Fill rate tracking and display
- Submission interview scheduling and removal
- History modal improvements

**1.0.0** - Initial release
- Database-driven position management
- Change tracking system
- Lever/action tracking
- Export functionality

---