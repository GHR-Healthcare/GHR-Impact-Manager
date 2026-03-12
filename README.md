# GHR Impact Manager

## Version History

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