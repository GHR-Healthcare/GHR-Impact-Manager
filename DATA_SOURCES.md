# Where the data comes from

Reference for the MSP workspace (`index.html` + `api/`). Non-MSP is a separate
instance served by `legacy.html`; see the bottom section.

Verified against the live warehouse on 2026-08-19. Row counts are point-in-time
and only meant to show whether something is populated.

---

## Databases

| Database | Server | What it is | Used by MSP? |
|---|---|---|---|
| `ghrdhc` | `ghrdatadb` | **The MSP warehouse.** B4Health + VNDLY, plus the Bullhorn↔B4 match table | ✅ everything |
| `ghrappdb` (`impactmgr` schema) | `ghrdatadb` | App-owned state — mappings, meetings, workspace edits | ✅ writes |
| Bullhorn mirror (`DM_GeneralHealthCare_24187_EMS`) | VM, separate host | GHR's ATS | ❌ **not needed** — see note |
| `ghr_ctmsync` | `ghrdatadb` | Symplr mirror | ❌ non-MSP only |
| `ghr-netsuite` | `ghrdatadb` | NetSuite GL — `raw` is fully loaded (97,465 transactions, 1.1M lines, 19,919 customers). `stg` is a thin transform layer (511 / 63 / 8) and is **not** the whole picture | ❌ MSP is not invoiced through NetSuite |

### Bullhorn stays on the non-MSP side

MSP does not need a direct Bullhorn connection:

- **Rates/margin** — both MSP sources carry pay *and* bill natively
  (B4 `Awarded_Rate`/`Pay_Rate` at 99.9% coverage; VNDLY `Bill Rate`/`Pay Rate`
  at 98.7%). No cross-system join required.
- **The Bullhorn comparison on Extensions** — already pre-computed *inside*
  `ghrdhc` as `dbo.BH_PLACEMENT_RAW_TO_B4HealthOrder`, refreshed per warehouse
  run. We read the result, not Bullhorn itself.

The only thing a live Bullhorn connection would add is the Bullhorn side of the
reconciliation for **VNDLY-era** seats, which the match table does not cover
(it is B4-only). That link would have to be name-based — there is no shared ID —
and name matching is unsafe enough that it is not worth crossing the boundary
for. See *Known gaps*.

---

## The two VMSs

GHR moved from **B4Health** to **VNDLY**, starting ~Jun 2025. Both are live and
both are read. They are **unioned, never deduped**: during transition they cover
different workers, and once a system is fully cut over B4 simply stops producing
rows for it. This matches what `GetFinancialData` already does for the
transitioned systems (RUMC, Holy Redeemer, Cooper).

| | B4Health | VNDLY |
|---|---|---|
| Current state | `dhc.B4HealthOrder` (28,900 rows) | `dbo.STAGING_VNDLY_WORKORDERS` (1,377) |
| Open reqs | `dhc.B4HEALTHOPENORDER` (121) | `dbo.STAGING_VNDLY_JOBS` (35 active) |
| Submissions | `dhc.B4Health_Contract_Submissions` (76,427) | `dbo.STAGING_VNDLY_SUBMISSIONS` (1,378) |
| Hours/spend | `dhc.B4HealthESR` (729,177) | `dbo.STAGING_VNDLY_SPEND` (9,034) |
| History | `dbo.HIST_B4HealthOrder` (2.15M snapshots) | *none* |
| Change log | *none* | `dbo.STAGING_VNDLY_WORKODER_MODIFICATIONS` (1,189) |
| Health systems | 18 all-time | 3 |

Each returned row carries `source_system` so the split stays visible.

---

## Meeting stages

### Closed — `/api/closed-data`
`dhc.B4HealthOrder` ∪ `STAGING_VNDLY_WORKORDERS`, bucketed GHR WON /
AFFILIATE WON / MISSED / CANCELED.

Close date is anchored differently per system, and unevenly:

| | Anchor | Undated |
|---|---|---|
| B4 wins | `Awarded_Date` | 0 of 19,331 |
| B4 cancellations | none (HIST fallback, GHR-only) | 6,204 of 7,204 (86%) |
| B4 never-awarded | none | 2,015 of 2,043 (99%) |
| VNDLY | `Onboarded Date`, else `Last Modified` | 0 |

**B4 can date its wins and mostly cannot date its losses.** Undated rows are
excluded from the window and counted in the response's `coverage` block; the
view renders a banner naming the exclusion, because dropping them silently makes
capture rate read high.

### Open Jobs / Priority — `/api/get-positions`
`dhc.B4HEALTHOPENORDER` + `dbo.STAGING_VNDLY_JOBS` (`Job Status = 'Active'`),
with submissions attached from the per-system submission tables.

⚠️ `STAGING_VNDLY_JOBS` holds ~664 rows across only ~387 distinct `[Job Id]`.
Joining it on `Job Id` fans rows out — measured 112 where the truth was 60.
Collapse it first. `JobSystemKey` is a *unique* nvarchar business key
(`'CUH-3-294'`) and does **not** join to the int `[Job Id]`.

### Extensions — `/api/extensions-data`
Seats ending inside 45 days. B4 312 · VNDLY 58.

- VNDLY `[Original End Date]` makes an already-extended seat directly visible;
  B4 only has the `Parent_Contract_ID` chain.
- **Decision state** is derived from `STAGING_VNDLY_WORKODER_MODIFICATIONS`,
  not a status column. Filed inconsistently: 256 rows under `Date Extension`,
  another 17 under `Other` / `Ended in Error` / `Assignment Completed` with the
  detail only in `[Other Reason]`. Detection matches either. The free text is
  returned verbatim because it is where the real decision lives
  (*"Extension offered for Chemistry unit - new end date 12/26/26"*).
  `decision_state` is a heuristic and cannot separate *Pending Acceptance* from
  *Approved*.
- The Bullhorn-vs-VMS comparison reads
  `dbo.BH_PLACEMENT_RAW_TO_B4HealthOrder` — 7,748 matched rows, **1,093 end-date
  mismatches (14%)**, some off by a year. B4-era only.

### Onboarding — `/api/onboarding-data`
−30d/+45d around today. B4 420 · VNDLY 194.

The two systems measure movement differently and are **not** interchangeable:

| | B4 | VNDLY |
|---|---|---|
| Source | `HIST_B4HealthOrder` snapshots | modifications feed |
| Measure | days slipped (real elapsed) | flagged delay events |
| Coverage | 97% of GHR, **<5% of affiliate** | includes affiliate |

`HIST_B4HealthOrder` is built from Bullhorn placement matching, so it is
**GHR-only** — all 2.15M rows, all 6,356 contracts. Where history is absent,
`move_count` is `NULL`, not `0`, and rows carry `movement_tracked: false`.
VNDLY has `[Original End Date]` but **no Original Start Date anywhere**, so
days-delayed is not computable there; rows carry `delay_measure: 'flagged'`.

---

## Reporting tabs

Rendered by `report-views.js` (the previous UI's renderers, module intact, its
standalone boot disabled) and fed by that module's own loaders — those parse
dates into `Date` objects, derive `healthSystem`, and set the `*Loaded` flags
the renderers gate on.

| Tab | Endpoint(s) | Source |
|---|---|---|
| Revenue | `/api/financial-data` | `B4HealthESR` + `STAGING_VNDLY_SPEND` |
| Trends | `/api/trend-data`, `/api/hours-data` | same, plus placements |
| Per Diem | `/api/perdiem-data` | `STAGING_VNDLY_*` + B4 |
| Contracts | `/api/contracts-comparison`, `/api/reviewed-contracts` | match table + `impactmgr` |

Field names that bite: financial rows carry **`estimated_billing`** and
**`hours_worked`**, not `revenue`/`hours`.

---

## App-owned state — `ghrappdb`, `impactmgr` schema

Nothing here exists in a source system, and nothing here is written back to one.

| Table | Holds |
|---|---|
| `workspace_state` | Levers, extension decisions, onboarding edits, interview overrides. `(scope, entity_id)` → JSON |
| `meetings` | Scope, stages completed, captured actions, recap HTML as sent |
| `changes` | Append-only audit of every workspace edit |
| `system_mappings`, `pm_mappings`, `bullhorn_client_allowlist` | Settings |

⚠️ **There is no write-back to VNDLY or B4.** A decision recorded in a meeting
stays in `impactmgr`. The UI must not imply the VMS has been updated.

---

## Known gaps

| Gap | Effect | What would fix it |
|---|---|---|
| VNDLY `Original Start Date` missing | Days-delayed / moved-2+ are B4-only | A `HIST_VNDLY_WORKORDERS` snapshot mirroring the B4 pattern. Forward-only |
| B4 loss close-dates | Capture rate reads optimistic; 8,219 rows excluded | A close/cancel date on B4 |
| No market-rate feed | Rate tab cannot show peer/competitor rates | External rate source |
| No VNDLY↔Bullhorn key | VNDLY-era extension reconciliation not possible | A shared ID. Name matching is unsafe — 11 surnames matched 2,195 candidates |
| MSP revenue is estimated | Revenue uses `estimated_billing`, not invoiced dollars | MSP is not invoiced through NetSuite, so NetSuite cannot reconcile it. Would need whatever system does invoice MSP |
| "Impact %" undefined | Column renders `—` | A formula |
| "System Exceptions" undefined | Tile replaced with Critical (≤7d) | A definition |
| Blank `Agency`/`Vendor Name` | 193 rows in the extension window fall out of GHR-vs-affiliate maths | Source cleanup |
| VNDLY reason picklist duplicates | 7 spelling pairs split one reason in two | Fixed on read in `shared_code/vndly_reasons.py`; durable fix is upstream |

**Margin is *not* a gap** — both VMSs carry pay and bill natively (99.9% / 98.7%).
It renders `—` today only because it is not yet wired.

**On NetSuite:** it holds real volume in `raw`, but MSP business is not invoiced
through it, so it cannot be used to reconcile MSP revenue. It may still be the
right source for company-level or non-MSP financials — worth a look separately,
not for these views.

---

## Non-MSP

Served by `legacy.html`. The MSP build redirects there when
`/api/get-config` reports `dataSource: 'non_msp'`, before anything paints.
Reads the **Bullhorn mirror** and `ghr_ctmsync` (Symplr) and does not touch
`ghrdhc`. Unchanged by the MSP rebuild.
