# Non-MSP Instance Port Spec (Bullhorn + Symplr)

Status: **in flight** · Last updated: 2026-06-29

The original June 1 spec is preserved below (§1–§11). Most of the port has
shipped — see "Current status" for what's live, what's in flight, and what's
left. The spec sections in §1–§11 are historical context; some of their
choices have evolved (e.g. dispatch flag is `non_msp` not `bullhorn`, Symplr
was pulled forward into the same instance, etc.).

---

## Current status (read this first)

### What's live

- **Dispatch architecture**: single `main` branch, env-flag `DATA_SOURCE` =
  `msp` (default) or `non_msp`. Non-MSP unifies Bullhorn (Rev Cycle + Locums)
  and Symplr (Education) inside the same instance. Two SWAs in production:
  - MSP — `ghr-impact-manager`, custom domain `impactmgr.ghrhealthcare.com`
  - Non-MSP — `ghr-nonmsp-impactmgr`, custom domain `impactmgr-nonmsp.ghrhealthcare.com`
- **All non-MSP endpoints ported and unioned across Bullhorn + Symplr**:
  GetTrendData, GetStatsData, GetYoYTrendData, GetFinancialData, GetHoursData,
  GetPositions, GetSystemMappings. GetPendingData and GetPerDiemData
  short-circuit to empty on non-MSP (the concepts don't apply).
- **Symplr rollup expanded by `MasterClientID`** so sub-orgs of DCIU/Reading/
  Allentown auto-include (`shared_code/symplr_systems.py`). Three Education
  districts in scope today; the master-id list is the source of truth.
- **Symplr orderless orders folded into headcount** — `orders` rows with
  `lt_orderid IN (0, NULL)` are unioned into Trend/Stats/YoY/Positions,
  aggregated by worker+client.
- **GetPositions surfaces uncovered shifts under filled lt_orders** as a
  third Symplr positions source.
- **Auth + tenant allowlist**: `api/shared_code/auth.py` enforces a domain
  allowlist (`ghrhealthcare.com`, `unitedanesthesia.com`, `ghreducation.com`)
  on every endpoint via `require_allowed_domain(req)`. SWA `auth` block was
  dropped from `staticwebapp.config.json` because the validator started
  rejecting it on Free tier; allowlist is the replacement.
- **Backend `division` + `region` fields on every non-MSP endpoint** (v1.8.0):
  - Bullhorn `division` = `View_Placement.customTextBlock1` /
    `View_JobOrder.customTextBlock1`. Values include `Allied`, `GHR Internal`,
    `Locum Tenens`, `Nursing`, `RevCycle Workforce`, `Search`, `Technology`,
    `United`, `Workforce Solutions`.
  - Symplr `division` = configured per system in `SYMPLR_SYSTEM_ROLLUP`
    (all three districts → `Education`). New
    `build_division_case_expr(column)` helper parallels the existing
    `build_system_case_expr`.
  - Symplr `region` = `profile_client.state`. Bullhorn `region` is `NULL` for
    now.
  - GetSystemMappings exposes `division` per system in its JSON.

### What's in flight (PR 2)

- **Frontend filter UI** on the non-MSP site:
  - Add `filterSelection.divisions` / `professions` / `regions` Sets to
    `window.store.state`
  - Render Division / Profession / Region dropdowns in the filter sidebar on
    non-MSP only
  - Hide the hardcoded "All Nursing / All Allied" block on non-MSP
  - Data-driven dropdown values (read from actual records, not hardcoded
    keyword lists)
  - Wire the new dimensions into `getFilteredJobs()`, `kpis()`, the trend /
    stats / financial views
  - Continue from `index.html:3470` (`setupFilters()` and `renderDropdown()`).
    Filter HTML containers live at lines 201-216.

### What's left

- **Cleanup of MSP-keyword utilities** so they work source-agnostically:
  `Utils.checkCategoryMatch` (`index.html:2201`) and
  `Utils.matchesStatsCategoryFilter` (`index.html:2214`). Currently rely on
  `CONSTANTS.NURSING_KEYWORDS` / `ALLIED_KEYWORDS` at line 1032.
- **Per Diem "Loaded 0" log message** — suppress on non-MSP. Harmless but
  alarming in the console.
- **`POSITIONS_DB` env var** still set on the non-MSP SWA even though the
  non-MSP code path never queries it. Safe to delete via
  `az staticwebapp appsettings delete`.

### Future (eventually-merged dashboard)

- **Add `division` to the MSP side too** so the same filter dimension works
  on both. Sets up a true unified view.
- **`DATA_SOURCE=combined`** mode — every endpoint queries both books, UI
  shows a Source filter alongside Division. The data-driven filter machinery
  from PR 2 makes this much easier.
- **Region for Bullhorn** — currently NULL. Could come from
  `View_ClientCorporation.state` or `customText19` (city/state on placement).

### Recent version history

- **v1.7.5** drop SWA `auth` block + tenant domain allowlist
- **v1.7.6** remove Pending sub-rows from Trend table
- **v1.7.7** Symplr master expansion + Pending/Per Diem dispatch guards
- **v1.7.8** Symplr orderless orders folded into headcount
- **v1.7.9** GetPositions: open shifts under lt_orders
- **v1.7.10** fix GetSystemMappings 500 + Symplr positions vanishing
- **v1.8.0** backend emits `division` + `region` (PR 1 of 2)
- **v1.8.1** MSP financial date-range timeout fix (B4 dedup) — *broke B4*
- **v1.8.2** MSP financial fix that doesn't break B4 (single-statement
  split CTE anti-join)

### Key files for non-MSP work

- `api/shared_code/data_source.py` — dispatch helper + connection helpers
- `api/shared_code/bullhorn_systems.py` — Bullhorn rollup config + helpers
- `api/shared_code/symplr_systems.py` — Symplr rollup config + helpers
  (includes `build_division_case_expr`)
- `api/shared_code/auth.py` — domain allowlist
- `api/Get*/__init__.py` — endpoints, each with `is_non_msp()` branch
- `staticwebapp.config.json` — SWA routes (no `auth` block since v1.7.5)
- `index.html` — frontend; non-MSP-specific UI gated on
  `window.store.state.dataSource === 'non_msp'`

### Two Azure CLI things worth knowing

- Both SWAs are listed under `az staticwebapp list` — names
  `ghr-impact-manager` (MSP) and `ghr-nonmsp-impactmgr` (non-MSP). Resource
  group is `GHR_Azure_Resources`.
- Env vars: `az staticwebapp appsettings list --name <swa> --resource-group GHR_Azure_Resources`
  (returns a noisy header line we have to strip with `tail -n +2` before
  parsing as JSON).

---

## 1. Why this exists

GHR's MSP book (current app) and non-MSP book (Rev Cycle, Locums, Education)
live in different source systems. The non-MSP business has comparable
operational reporting needs — headcount trend, financials, account-level KPIs —
but no dashboard. This spec defines how to extend the existing app to serve
that book without forking the codebase.

---

## 2. Strategic decision: one codebase, dispatch by data source

**Decision**: ship the non-MSP instance from the same `main` branch as the MSP
instance. Each API endpoint dispatches to a data-source-specific implementation
based on an environment variable (`DATA_SOURCE=msp` or `DATA_SOURCE=bullhorn`).

**Why not a long-lived feature branch:**

- Every `api/Get*/__init__.py` has SQL coupled to B4/VNDLY. A branch would
  diverge in almost every file in `api/` plus parts of `index.html`.
- Every UI/feature change on `main` (Trend tab, Capture %, Vendor view, etc.)
  has to be merged into the Bullhorn branch. Conflicts compound. Drift
  accumulates. Fixes get forgotten.
- Two SWAs is fine; two codebases is not.

**Why dispatch in the same codebase works:**

- The `/api/*` JSON contract is already the seam — `index.html` doesn't know
  what's behind it.
- Adding a second backend behind the same JSON shape is the natural next move.
- One SWA per instance, same source branch, different env config.

**Out of scope**: a third Symplr-backed instance for Education clients. Same
dispatch pattern applies once we get there, but Bullhorn comes first.

---

## 3. Data sources

### MSP (existing — for reference)

- B4 → `dhc.B4HealthOrder`, `dhc.B4HealthESR`
- VNDLY → `dbo.STAGING_VNDLY_WORKORDERS`, `dbo.STAGING_VNDLY_SPEND`
- Same positions DB (`POSITIONS_DB` env var)

### Bullhorn (new)

- Server: `BULLHORN_HOST` / `BULLHORN_DB` / `BULLHORN_USER` / `BULLHORN_PASSWORD`
- All env vars already exist (see `api/GetContractsComparison/__init__.py`)
- Primary tables:
  - `dbo.View_Placement` — placements (= assignments/workorders)
  - `dbo.View_Candidate` — workers
  - `dbo.View_ClientCorporation` — clients
  - `dbo.View_JobOrder` — requisitions
- Analytical layer (`MSP*` views) is **not useful** — those aggregations cover
  the MSP slice of Bullhorn, not Rev Cycle / Locums.

### Symplr (future — Education)

Not in this spec. Same dispatch pattern (`DATA_SOURCE=symplr`) when we get
there.

---

## 4. Account scope

12 named non-MSP accounts, two divisions in Bullhorn (Education is in Symplr):

| Division | Accounts |
|---|---|
| Rev Cycle | Orlando Health, Solventum, Memorial Hermann, Montefiore, Lakeland Regional |
| Locums | Duke Health, Cone Health, CarolinaEast Health, University of Miami |
| Education *(Symplr — not in this port)* | Reading SD, Allentown SD, DCIU |

**Open question**: discovery turned up other Locums-flagged clients in
Bullhorn (Beth Israel Deaconess, Bozeman Health Deaconess, Conemaugh,
MultiCare Deaconess, Renfrew). Decide whether the dashboard caps at the named
12 or shows the whole non-MSP book.

### Facility rollup needed

Each top-level account has multiple Bullhorn `View_ClientCorporation` rows for
sub-facilities. Need a `BULLHORN_SYSTEM_MAPPINGS` config (sibling to the
existing `HEALTH_SYSTEM_MAPPINGS` in `index.html`):

| Roll up to | Example facility names in Bullhorn |
|---|---|
| Cone Health | `Cone Health Alamance Regional Medical Center`, `Cone Health Annie Penn Hospital`, `Cone Health Imaging at MedCenter Mebane`, `Cone Health Moses Cone Hospital` |
| Duke | `Duke University Hospital`, `Duke Raleigh Hospital`, `Duke Raleigh Hospital Surgery Center` |
| Orlando Health | `Orlando Health`, `Orlando Health Orlando Regional Medical Center` |
| (etc.) | |

Production code should match on `clientCorporationID` lists, not `LIKE` on
name strings. A keyword match for "Orlando" pulls in `Renfrew - Orlando`
(an unrelated eating-disorder clinic).

---

## 5. Status vocabulary

Bullhorn placement statuses observed in the non-MSP book, with treatment:

| Bullhorn status | Treat as | Volume (lifetime) |
|---|---|---|
| `Approved` | Active / confirmed | 360 |
| `Pending Start` | Active / confirmed | 2 |
| `Cleared` | Active / confirmed (Locums credentialing done) | 21 |
| `Onboarding` | Active / confirmed | 3 |
| `Completed` | Historical (ran to term) | 1300 |
| `Termination` | Historical (ended early) | 267 |
| `Cancellation` | Exclude | 95 |
| `Archive` | Exclude | 2 |

**Critical structural finding**: there are **no submission-stage statuses**
(no `Submitted` / `Offered` / `In Process`). Non-MSP placements skip the
pending funnel — they appear in Bullhorn already in `Approved` state,
optionally with a future `dateBegin`.

**Implication**: the new instance does not need a Pending tab, Pending
sub-rows, "Unconfirmed" chart lines, or "Expected Starts" KPI tile.
Future-dated `Approved` rows ARE the pipeline.

---

## 6. Category / dimension mapping

### Primary: `employmentType` (5 values, 100% populated)

| Bullhorn | Display |
|---|---|
| `Travel` | Travel |
| `PRN` | Per Diem |
| `Remote` | Remote |
| `Local` | Local Contract |
| `Permanent` | Perm Placement |

### Secondary: `customText1` — profession (13 values, 100% populated)

| Seen values | Mostly in |
|---|---|
| `CRNA`, `Anesthesiologist`, `RN`, `Registered Dietitian` | Locums |
| `Coder`, `CDI Specialist`, `Coding Auditor`, `Medical Coder`, `Customer Service Rep`, `Clerical` | Rev Cycle |
| `Director` | One-off |

**Note on usage**: for **Rev Cycle**, `employmentType` is ~always `Remote`,
so `customText1` (profession) is the useful category dimension. For
**Locums**, both axes matter — Travel-CRNA vs PRN-CRNA vs Travel-RN.

### Other custom fields worth knowing

| Field | Content |
|---|---|
| `customText2` | Sub-specialty (Inpatient, Outpatient, General, ProFee, ER, Coding, Call Center, etc.) |
| `customText7` | Client account code (`ORL001W`, `MTF001W`, etc.) — stable client-key |
| `customText8` | Cost-center numeric ID |
| `customText11` | Account manager |
| `customText12` | Recruiter |
| `customText19` | City, State (geo dimension) |
| `customText20` | Vendor/program / contact type |

Skip: `costCenter` (always NULL — use `customText8`), `positionCode` (always
NULL).

---

## 7. Rate / revenue model

Every active placement has populated:

- `clientBillRate` (float) — bill rate to client
- `payRate` (money) — pay to candidate
- `markUpPercentage` (float) — bill/pay markup
- `reportedMargin` (float) — gross margin **← exposed directly, this is a real upgrade from MSP**
- `hoursPerDay` (float) — observed as `8` universally
- `durationWeeks` (float)
- `workWeekStart` (int) — `7` (Sunday) universally
- `isMultirate` (bit) — flag for OT/shift-differential rate variants (use
  `customBillRate1..10` / `customPayRate1..10` when set)

`salary` is always 0 — everything is hourly.

**Weekly revenue formula:**

```
weekly_revenue_per_placement = clientBillRate × hoursPerDay × 5
total_weekly_revenue = sum over placements active in that week
```

Cleaner than B4 (`Hours_per_Peek` is weekly) and VNDLY (defaults to 36 hr/wk).

**Gross margin** is reported directly per placement, so the new instance
can surface margin in the Financials tab and trend chart — feature that's
been missing on the MSP side.

---

## 8. Endpoint port matrix

One row per `/api/Get*` endpoint. Status legend: ✅ port · ⚠️ port with
modification · ❌ N/A (concept doesn't exist for non-MSP) · ♻️ reuse as-is.

| Endpoint | Status | Bullhorn source | Notes |
|---|---|---|---|
| `GetTrendData` | ✅ | `View_Placement` + `View_ClientCorporation` + `View_Candidate` | Active = statuses listed in §5. Pipeline = future `dateBegin`. No pending bucket. Group by Division/Account/Category/Profession instead of HealthSystem/Category. |
| `GetPendingData` | ❌ | — | Concept doesn't exist for non-MSP. Endpoint should either 404 or return an empty list when `DATA_SOURCE=bullhorn`. Frontend hides the Pending tab. |
| `GetPerDiemData` | ❌ | — | No shift/timesheet data exposed in the views we have. PRN placements exist as contracts but actuals-worked isn't there. Frontend hides the Per Diem tab. |
| `GetFinancialData` | ✅ | `View_Placement` | Revenue = `clientBillRate × hoursPerDay × 5` summed over weeks of overlap. Margin = `reportedMargin` per placement (new column in the JSON output, frontend can opt into rendering). |
| `GetStatsData` | ✅ | `View_Placement` + `View_JobOrder` | Active headcount + open positions |
| `GetPositions` | ⚠️ | `View_JobOrder` | Need to inspect status vocabulary and `numOpenings`/`numAssigned` columns before finalizing |
| `GetHoursData` | ⚠️ | `View_Placement` | No actual hours worked. Use `hoursPerDay × 5 × durationWeeks` as scheduled-hours proxy. |
| `GetContractsComparison` | ♻️ | — | Already works. Compares Bullhorn placements against B4/VNDLY assignments — orthogonal to the data-source dispatch. |
| `GetSystemMappings` | ⚠️ | (config) | Returns `BULLHORN_SYSTEM_MAPPINGS` when `DATA_SOURCE=bullhorn`, `HEALTH_SYSTEM_MAPPINGS` otherwise. |
| `GetPMMappings` | ♻️ | (config) | App-side, source-agnostic |
| `GetYoYTrendData` | ✅ | `View_Placement` | Same query shape with `-364 day` offset for prior-year overlay |
| `GetConfig` | ♻️ | — | App-side. Returns `appVersion` + `dataSource` so frontend can hide/show tabs. |
| `GetChanges` / `SaveChange` / `GetHistory` / `SaveHistory` | ♻️ | — | App-side, source-agnostic |
| `SearchUsers` / `ValidatePassword` | ♻️ | — | Auth-side, source-agnostic |
| `/api/health` | ♻️ | — | Already public, source-agnostic |

### Net shape of the non-MSP instance

The Bullhorn instance ends up being a **slimmer dashboard** than MSP:

- ✅ Trend tab (with Division filter as new first-class dimension)
- ✅ Financials tab (with gross-margin column — improvement over MSP)
- ✅ Stats tab
- ✅ Contracts Comparison tab (already works)
- ✅ Settings (mappings, PMs)
- ❌ Pending tab (hidden)
- ❌ Per Diem tab (hidden)
- ⚠️ Positions tab (depends on `View_JobOrder` inspection)

---

## 9. Implementation plan

### Phase 1 — code structure

1. Create `api/shared_code/data_sources/__init__.py` with a `get_source()`
   helper that reads `DATA_SOURCE` env var.
2. For each endpoint, split the SQL into source-specific modules:
   - `api/shared_code/data_sources/msp.py` (existing logic moves here, no
     behavior change)
   - `api/shared_code/data_sources/bullhorn.py` (new)
3. Each endpoint's `main()` becomes a thin dispatcher:
   ```python
   from shared_code.data_sources import get_source

   def main(req):
       auth_error = require_allowed_domain(req)
       if auth_error: return auth_error
       source = get_source()  # 'msp' | 'bullhorn'
       return source.get_trend_data(req)
   ```
4. Frontend reads `DATA_SOURCE` from `GetConfig` and uses it to hide tabs
   that don't apply.

### Phase 2 — Bullhorn endpoints

Port endpoints in this order (each is a separate PR):

1. `GetSystemMappings` (config-only, smallest blast radius, unblocks UI)
2. `GetTrendData` (the core endpoint — biggest win)
3. `GetFinancialData` (with margin column)
4. `GetStatsData`
5. `GetYoYTrendData`
6. `GetPositions` (after `View_JobOrder` inspection)
7. `GetHoursData` (scheduled-hours proxy)

### Phase 3 — deploy

- New SWA: `ghr-impact-manager-bullhorn` (or similar)
- Same `main` branch source
- Different env config:
  - `DATA_SOURCE=bullhorn`
  - `BULLHORN_*` env vars (already set)
  - Domain allowlist same (`ghrhealthcare.com`, `unitedanesthesia.com`,
    `ghreducation.com`)
- Custom domain TBD

### Phase 4 — Symplr (Education) — separate effort

Out of scope for this spec. When ready, add `DATA_SOURCE=symplr` and a
third source module. Same dispatch shape.

---

## 10. Open questions to resolve before/during implementation

1. **Account scope**: cap at the 12 named accounts, or include all non-MSP
   Bullhorn clients?
2. **Division filter UX**: single dashboard with a Division toggle (Rev
   Cycle / Locums / All), or separate URLs per division?
3. **Margin display**: where should `reportedMargin` surface in the UI?
   (Tile on Financials? Column in Trend table?)
4. **`View_JobOrder` shape**: needs an inspection pass before `GetPositions`
   can be finalized.
5. **Worker dedup**: MSP code dedups B4↔VNDLY for transitioned systems
   (Cooper, RUMC, Holy Redeemer). Bullhorn has no equivalent — but if a
   candidate appears in both MSP and Bullhorn books (unlikely but possible),
   the comparison endpoint already handles it. Verify no other endpoint
   assumes single-source.
6. **Authentication**: the v1.7.5 domain allowlist enforces 3 domains. No
   change needed for the new instance — same allowlist applies.

---

## 11. Discovery artifacts

Queries used during discovery (2026-06-01) — kept here for reference if
re-running:

```sql
-- View_Placement schema
SELECT COLUMN_NAME, DATA_TYPE
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_NAME = 'View_Placement'
ORDER BY ORDINAL_POSITION;

-- Status vocabulary for non-MSP accounts (lifetime)
SELECT p.status, COUNT(*) AS n,
       MIN(p.dateBegin) AS earliest, MAX(p.dateBegin) AS latest
FROM dbo.View_Placement p
JOIN dbo.View_ClientCorporation cc ON p.clientCorporationID = cc.clientCorporationID
WHERE p.isDeleted = 0
  AND (cc.name LIKE '%Orlando%' OR cc.name LIKE '%Solventum%' OR cc.name LIKE '%Memorial Hermann%'
    OR cc.name LIKE '%Montefiore%' OR cc.name LIKE '%Lakeland%' OR cc.name LIKE '%Duke%'
    OR cc.name LIKE '%Cone Health%' OR cc.name LIKE '%CarolinaEast%' OR cc.name LIKE '%University of Miami%')
GROUP BY p.status ORDER BY n DESC;

-- Active placement rate / category sample
SELECT TOP 100
    p.placementID, cc.name AS client, p.status,
    p.dateBegin, p.dateEnd, p.durationWeeks,
    p.employmentType, p.customText1 AS profession, p.customText2 AS subspecialty,
    p.clientBillRate, p.payRate, p.markUpPercentage, p.reportedMargin,
    p.hoursPerDay, p.workWeekStart, p.isMultirate,
    p.customText11 AS account_manager, p.customText12 AS recruiter,
    p.customText19 AS location
FROM dbo.View_Placement p
JOIN dbo.View_ClientCorporation cc ON p.clientCorporationID = cc.clientCorporationID
WHERE p.isDeleted = 0
  AND p.status IN ('Approved','Pending Start','Cleared','Onboarding')
  AND (cc.name LIKE '%Orlando%' OR cc.name LIKE '%Solventum%' OR cc.name LIKE '%Memorial Hermann%'
    OR cc.name LIKE '%Montefiore%' OR cc.name LIKE '%Lakeland%' OR cc.name LIKE '%Duke%'
    OR cc.name LIKE '%Cone Health%' OR cc.name LIKE '%CarolinaEast%' OR cc.name LIKE '%University of Miami%')
ORDER BY p.dateBegin DESC;
```
