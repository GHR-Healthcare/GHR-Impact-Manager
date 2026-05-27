# GHR Impact Manager

## Version History

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