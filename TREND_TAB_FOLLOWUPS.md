# Trend Tab — Open Follow-Ups

Last updated: 2026-07-31 · after v2.0.2 (PR #25)

What v2.0.2 fixed is in the README version history. This file is the remainder:
everything the audit surfaced that was **not** fixed, and why. Ordered by how much
it affects numbers people act on.

---

## 1. Verify after deploy (two v2.0.2 changes I could not confirm without DB access)

Both shipped in v2.0.2. Neither is speculative, but neither was verifiable from
the repo, so they should be eyeballed against live data.

### 1a. Symplr revenue is now gated on billable hours

`GetTrendData._symplr_trend_data` weekly-revenue query previously summed
`totalbillamount` across every `dbo.orders` row in scope with **no** work filter,
while the headcount query beside it requires `status = 'filled'`. It now filters
`ISNULL(o.totalbillhours, 0) > 0`.

- Chose billable-hours over `status = 'filled'` so a worked shift that later moves
  to a terminal status other than `filled` isn't silently dropped. Mirrors the
  existing pattern in `GetHoursData`.
- **If unfilled/cancelled orders carry `$0`, this is a no-op** and the
  overstatement flagged in the audit never existed. Compare the non-MSP revenue
  line before/after to find out.
- Note `GetFinancialData` sums the same column with no work filter either
  (`estimated_billing`). If this change proves correct, that query wants the same
  treatment — currently Trend and Financials will disagree on Symplr revenue.

### 1b. VNDLY headcount should have moved

Confirm the current-week number didn't jump when `Ended` /
`Ended by Job Close` were added to `VNDLY_RAN_STATUSES`. Expected: past weeks up,
the lookback flattening out, prior-year overlay up substantially, current week
roughly unchanged (the end-date cap exists to protect it).

---

## 2. Known residual: VNDLY terminal end dates are approximate

`STAGING_VNDLY_WORKORDERS.[End Date]` on a terminal work order is the
**originally scheduled** end, not the date the assignment actually stopped —
`Ended by Job Close` rows carry end dates over a year out. v2.0.2 caps terminal
rows at `GETDATE()` (`VNDLY_EFFECTIVE_END_SQL`) because a terminal status cannot
still be running.

**Residual imprecision:** a work order closed early still counts through today
rather than through its actual close date. So a placement closed in January is
still contributing to every week since.

**Proper fix:** an actual-end-date (or close-date) column in the staging extract.
There is nothing usable in the table today. Until then the lookback is directionally
right but individual past weeks are slightly overstated for early-closed rows —
the opposite of the pre-2.0.2 bug, and much smaller.

---

## 3. Needs a product decision

### 3a. Dead projection engine (~150 lines)

All of this is computed every render and thrown away:

| Symbol | Location | Status |
|---|---|---|
| `linearRegression` | `index.html:6433` | defined, never called |
| `holtSmooth` | called only by `buildProjection` | live but output unused |
| `buildBlendedProjection` | 3 call sites | all three results unused |
| `projectionData`, `ghrProjectionData`, `affProjectionData` | `index.html:6723-6737` | assigned, never read |
| `totalFillProjection`, `ghrFillProjection`, `affFillProjection` | `index.html:7969-7979` | assigned, never read |
| `PENDING_CONVERSION` | — | only feeds the above |

Two comments actively promise output that doesn't exist: `// Weekly summary rows:
… Trend Projection` and `// Overlay lines shared across account/PM views: Total,
Pipeline, Trend Projection`.

**Decision:** delete, or wire onto the chart? Wiring it up changes what people see
and would need the numbers sanity-checked. Deleting is safe and removes the
misleading comments. Note that if wired up, the projection inherits whatever bias
is left in the lookback (see §2).

### 3b. Non-MSP category taxonomy

`normalizeCategory` and `catGroupDefs` pattern-match B4 `Program` / VNDLY
`Labor Type` strings ("Travel Nursing", "Per Diem (Allied)"). Non-MSP supplies
Bullhorn `employmentType` and Symplr `nursetype`, almost none of which match, so
nearly everything falls into the `Other` catch-all and the Nursing/Allied grouping
collapses. Already listed as deferred in the 2.0.0 notes.

Inputs are known:

- `BULLHORN_PORT_SPEC.md` §6 gives the `employmentType` mapping — `Travel`,
  `PRN` → Per Diem, `Remote`, `Local` → Local Contract, `Permanent` → Perm Placement.
- `GetFinancialData` already has `SYMPLR_SERVICE_LINE_CASE` mapping `nursetype`
  → Nursing / Allied / Non-Clinical. Reuse it rather than re-deriving in the view.

**Decision:** keep Nursing/Allied as the grouping axis (needs the Symplr CASE
applied server-side and an employmentType mapping), or switch non-MSP to
Division-based grouping per spec §8 ("group by Division/Account/Category/
Profession")? The latter matches the spec but is a bigger change.

Recommendation: normalize server-side in `GetTrendData` so Trend, Financials and
Stats all bucket identically — doing it in the view guarantees future drift.

### 3c. PM view on non-MSP

Every non-MSP row resolves to `Unassigned`. PM comes from `pmMappings`
(facility → PM), an MSP admin table described in the Settings modal as feeding
this exact view. Meanwhile `GetTrendData` *does* select Bullhorn
`p.customText11 AS pm` and `normalizeTrendRow` then discards it (`r.pm = null`).

Spec §8 says non-MSP should group by Division, not PM.

**Decision:** hide the PM button on non-MSP (one-liner, consistent with how Vendor
and Fill Rate were handled in v2.0.2), or replace it with a Division axis (real
work, and arguably the more useful view)?

### 3d. Profession filter is silently ignored on Trend

`filterRow` in `View.trend()` checks systems, facilities, categories, specialties,
divisions and regions — but not `professions`. And `GetTrendData` returns no
profession column at all, though `GetPositions` does (Bullhorn `jo.customText1`,
Symplr `lt.specialty`).

So the non-MSP-only Profession dropdown has **zero effect** on this tab while
appearing to work. Either add the column to both trend queries and wire it into
`filterRow`, or hide the dropdown when the Trend tab is active. Silently ignoring
a visible filter is the worst of the three.

---

## 4. Data gaps upstream (not fixable in this repo)

### 4a. Bullhorn `region` is NULL

`GetTrendData` returns `NULL AS region` for Bullhorn (deliberate — spec says
"Bullhorn region is NULL for now"), while Symplr returns `pc.state`. The region
filter requires `r.region &&`, so **any** region selection drops the entire
Bullhorn book with no warning.

Left alone deliberately: the region dropdown is data-driven and only ever contains
Symplr states, so excluding unknown-region rows is a defensible reading of "show
me PA". Including them unconditionally would make the filter meaningless instead.
The real fix is populating Bullhorn region upstream.

### 4b. `DATEPART(WEEKDAY)` depends on connection language

Every week-bucketing expression across GetTrendData / GetYoYTrendData / GetHoursData
/ GetFinancialData uses `DATEPART(WEEKDAY, ...)`, which depends on the session's
`DATEFIRST` / language setting. Anything other than `us_english` shifts every
bucket off the Sunday the frontend assumes. Worth pinning with an explicit
`SET DATEFIRST 7` so it can't drift with a connection-string or server-default
change.

---

## 5. Lower-priority correctness / consistency

### 5a. Revenue ignores most active filters

The revenue series applies only the system filter (and the non-MSP division proxy).
Facility, specialty and region filters are dropped, while headcount honors them.
Consequences:

- The revenue line and headcount lines describe different populations whenever
  those filters are active.
- Worse, `avgPerWorkerPerWeek` is calibrated as full-system revenue ÷ filtered
  headcount, so the **Projected Revenue** KPI tile and the projected-revenue line
  inflate badly under a facility filter.

Revenue rows aren't tagged by category so that one genuinely can't be narrowed,
but facility and region could be.

### 5b. "Fill Rate" mode doesn't show a fill rate

- `Total Fill Rate` is `total/total` — a flat 100% line.
- `Total Fill Rate (Unconfirmed)` is `confirmed/(confirmed+pending)`, which *drops*
  as the pipeline grows, i.e. a healthier pipeline reads as a worse fill rate.
- `GHR Fill Rate` / `Affiliate Fill Rate` are share-of-placements — that's the
  GHR Capture % already in the table, not a fill rate.

Hidden entirely on non-MSP in v2.0.2. On MSP it still needs either a real
definition (filled requisitions ÷ total requisitions, which means bringing in
requisition data) or removal.

### 5c. Prior-year overlay applies a different filter set

`applyFiltersToYoY` checks only system / facility / category — no division, region
or specialty, and no `Utils.isHiddenHealthSystem`. So hidden systems appear in the
prior-year line but not the current-year line, and division/region filters silently
don't apply to it. Also `computeYoYDatasets` returns `[]` for the Vendor view, so
the toggle silently no-ops there.

### 5d. Systems are mapped twice, inconsistently

Non-MSP SQL already resolves the health system via `build_system_case_expr`, then
the frontend re-runs `Utils.getHealthSystem(r.facility, r.system)` on assignments —
which keyword-matches the **facility** name too and returns the first mapping that
hits, so it can override the server's answer. The revenue path uses
`getHealthSystem(null, r.system)` instead. Same client can therefore be attributed
to different systems in the Account view vs the Revenue line.

Fix: for non-MSP, trust the server's system and pass `null` for facility on both
paths.

### 5e. Bullhorn ↔ Symplr worker overlap

The B4/VNDLY dedup keys off `source_system === 'B4' | 'VNDLY'`, so it's a no-op on
non-MSP (which emits `Bullhorn` / `Symplr`). A worker present in both books now
collapses to one head in the Total (v2.0.2 keys on system + normalized name) but
still counts under both `employmentType` and `nursetype` categories, so category
rows can exceed the total. Low volume; worth checking whether any client is
serviced by both books before spending time on it.

### 5f. Business rules hardcoded in view code

`const transitionedSystems = new Set(['RUMC', 'Holy Redeemer', 'Cooper'])` is
duplicated at **three** separate places in `index.html` (lines 5363, 6058, 6347),
and `PM_EXCLUDED_SYSTEMS` (`['Jefferson', 'Sunrise Senior Living Management']`) is
inline in `View.trend()`. Both are business config living in render paths, and the
triplicated set will drift. Belongs alongside the system mappings in the DB.

### 5g. PM view can't tie to its own Total

PM buckets exclude `PM_EXCLUDED_SYSTEMS`, but the PM table reuses
`sysTableTotalRow`, which is the all-systems total. The PM rows therefore never sum
to the Total row shown above them. Either give the PM view its own total (sum of
included systems) or annotate the exclusion in the UI.

### 5h. Category filter can't narrow within a family

`matchesTrendCategory` falls back to a broad `/nurs/` ∨ `/allied/` match after the
exact check, so filtering to "Travel Nursing" returns **all** nursing categories.
Trend disagrees with every other tab under a category filter.

### 5i. Future-week deltas are structurally negative

`deltaSpan` compares future weeks (confirmed bookings only) against actuals, so
every future-week delta reads as a decline and the table looks like a cliff. Not a
code bug — the basis genuinely changes at the boundary — but there's no visual cue
that it did. A shaded divider or a footnote at the actuals→pipeline boundary would
stop the misread.

### 5j. Slug collisions in expand/collapse IDs

`catId` / `sysId` / `pmId` / `vendorId` are built with
`replace(/[^a-zA-Z0-9]/g, '_')`, so "GHR-1" and "GHR 1" produce the same class and
toggling one expands both. Needs a collision-safe slug (append an index).

---

## 6. Open question

**"A lot of 200 exact for trend"** — raised during the audit, never resolved. There
is no row cap anywhere in the trend path: neither assignments query uses `TOP`, and
`fetchall()` doesn't truncate. The only `TOP`s are week-generator CTEs (`TOP 8`
revenue weeks, `TOP 60` YoY weeks) and Azure SWA imposes no row limit.

Needs clarification on what the 200 refers to before it can be chased:

- **200 rows** in the `/api/trend-data` response → suspect the upstream staging
  load (a paged source API capped at 200/page that only fetched page 1).
- **A headcount of 200 in the UI** → was plausibly the name-collapsing bug fixed in
  v2.0.2; re-check now.
- **HTTP 200s in logs** → expected, not a problem.
