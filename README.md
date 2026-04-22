# GHR Impact Manager

## Version History

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