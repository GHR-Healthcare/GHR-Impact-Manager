# GHR Impact Manager

## Version History

**3.0.1** - Category filter: "All Allied" was dropping 88% of allied

Reported from the field: filtering the tracker to Allied gave figures far below the unfiltered totals.

`matchesTrendCategory` compared the selected value against the raw category by substring, but **"All Nursing" and "All Allied" are sentinels, not category names**, so the test could never match them properly. Against `All Allied` the only raw category that passed was the one literally called `Allied` — 105 of 852 allied rows. `Local Contract Allied Health` (322), `Allied Health` (245), `Travel Allied Health`, `Contract (Allied)` and `Per Diem (Allied)` were all dropped. `All Nursing` kept 44%.

This was introduced by an earlier fix. Trend used to fall back to a broad `/nurs/ ∨ /allied/` regex, which made a specific category like "Travel Nursing" return *all* nursing. Removing that fallback fixed the specific case and silently broke the sentinels, which had been relying on it. Trend now delegates sentinels to `Utils.checkCategoryMatch`, the way every other tab does.

Two neighbouring bugs fell out of checking it against the real vocabulary — all 28 distinct programs across B4 and VNDLY:

- **`Information Technology` counted as Allied.** The sentinels tested `ALLIED_KEYWORDS`, which contains `tech`. Those keyword lists are for free specialty text; a program is a short controlled vocabulary where `nurs` and `allied` classify all 28 correctly. This affected every tab, not just Trend.
- **A specific category pulled in a broader one.** The substring test ran both directions, so selecting `Travel Nursing` also matched plain `Nursing`, and `Allied Health` swallowed `Allied` — a separate category with its own 105 rows. Only the raw-contains-selected direction is needed, since the dropdown offers normalized names while Trend holds the raw value.

**Default category is now Nursing + Allied** (requested by Daniel). The two sentinels used to be mutually exclusive — picking one cleared the other — so that combination was literally unreachable. They now toggle like any other option and OR together. The default applies once per session, only when the Category filter is untouched, and only on MSP, where Category is a service line; on non-MSP it is engagement mode and the sentinels mean nothing.

**3.1.0** - Closed stage on non-MSP

The Closed stage now runs on the non-MSP side, re-anchored on fill outcome instead of vendor capture. MSP asks who won a seat against affiliate agencies; non-MSP is GHR's direct book with no panel to compete against, so the stage answers what the meeting actually needs there — fill rate and velocity over historical orders.

Orders resolve into FILLED / UNFILLED / CANCELED across Bullhorn and Symplr. Fill rate is `FILLED / (FILLED + UNFILLED)`; cancellations are excluded from the denominator because those orders stopped existing rather than going unfilled. Velocity is measured per order and reported as mean and median.

### Four measurement traps found on the way

Each of these produces a plausible wrong number rather than an error, which is how the earlier filter defects behaved too.

- **Bullhorn's `Archive` status is not an outcome.** It is the largest status on the book — 38,062 orders in 180 days — and not one has ever carried a placement. They spread evenly across major systems, the signature of an inbound feed that is ingested and auto-archived rather than worked. Counted as unfilled they report a 5% fill rate against a real 52%. Excluded.
- **Bullhorn's `Filled` status lies.** Of 269 orders marked Filled, 12 have a placement; `Placed` carries 2,204 of 2,279. Fill is derived from placement existence, never from the status label.
- **`dateClosed` is populated on zero resolved orders** despite existing in the schema. `dateLastModified` covers 100% and anchors unfilled orders; filled orders anchor on their placement date.
- **Symplr velocity cannot use `date_start`.** Orders are routinely logged after the shift begins, so `date_entered → date_start` averages *negative four days*. `BookedByDT` is populated on 100% of fills and never precedes entry.

### Fill rate has an honest denominator

Symplr's `voidreason` separates orders GHR lost (Filled by Competition, Unable to Fill, Filled by Internal Staff) from orders that stopped existing (Scheduling Error, Census Dropped). Leaving the latter in understates the rate by about seven points — 63.9% raw against 70.9% on true opportunities. Voids with no recorded reason, 40% of them, fall through to unfilled and count against the rate; that count is reported in `coverage.unfilledWithoutReason` so the figure is quoted as a floor rather than a point estimate.

Symplr `lt_order` carries no bill or pay rate, only `ratecode` and `rateSheetID`. Those fields stay null and 13-week value is Bullhorn-only, rather than being filled with a guess.

### Where we lose shifts

`coverage.competitive` rolls competitive losses up by health system, facility, credential and service line, each reporting how many entries were omitted so a top-15 is never mistaken for the whole list. The full void vocabulary — 13 values over a year — is mapped exhaustively rather than by prefix, so a reason Symplr starts writing tomorrow surfaces under its own name instead of being folded into a neighbouring bucket.

Two things the breakdown makes obvious. **Competitors are not the main threat: clients filling shifts with their own staff outnumber competitive losses roughly ten to one** (166 against 17 in a 30-day window). And `No Show` / `Call-out` / `DNR` are separated as *Clinician fell through* — those shifts were booked and then failed, which is a service problem, not a sourcing one, and lumping them under "unable to fill" would point the meeting at the wrong fix.

This is Symplr-only. Bullhorn job orders carry no cancellation-reason field, so a competitive loss there is indistinguishable from any other unfilled order; the payload names its sources rather than implying book-wide coverage.

### Bullhorn's own close reasons

`View_JobOrder.reasonClosed` exists and carries a real vocabulary, including an explicit `Lost to Competition` and a named competitor (`Filled by HCTec Partners`). It is wired in, with a hard limit on what it may be used for.

Book-wide the field has run at 1.3%-3.8% for six years — not abandoned, just rarely filled in. Within the thirteen accounts this app actually scopes to it is better, 35.7% all-time, but carries only **8** `Lost to Competition` rows ever, against 2,585 outside that scope. So Bullhorn close reasons label individual orders and must never drive a competitive *rate*; `coverage.competitive.reasonCoverage` reports the populated share per source so the counts read as a floor.

Three of its values — `Duplicate`, `Data Cleanup - Admin`, `System Error` — are data artifacts rather than lost work, and now leave the fill-rate denominator alongside Symplr's `Scheduling Error`.

The more useful discovery is adjacent: `View_Placement.terminationReason` is populated on 21.9% of placements with an operational vocabulary (Candidate Cancellation 2,852, Company Cancellation 1,513, GHR Cancellation 1,074). That is fallout data, and it belongs to the Onboarding and Extensions stages rather than to Closed.

### Velocity is reported as median, not just mean

Mean days-to-fill is 6.6 against a **median of 0** — most shifts book same-day and a long tail drags the average. Both are returned; the mean alone would describe a book that doesn't exist.

## Extensions on non-MSP

133 seats ending inside the 45-day horizon (34 Bullhorn, 99 Symplr), banded by urgency on the same legend MSP uses.

Bullhorn does something neither MSP source can: it shows the seat's real extension history. `EditHistoryPlacement` records every change to `dateEnd` with old and new values, so an extension is an edit where the end date moved *later* — not an inference from a parent-contract chain (B4) or a reason-text match (VNDLY). Count, date and the user who made it come straight from the audit trail, and the note writes itself: *"End date moved 2026-08-22 to 2026-08-29"*, by Joe Andrews.

Symplr has no such trail. `original_lt_orderid` looks like B4's parent chain but is populated on 2 rows out of 4,735 in a year, so it is read and flagged where present and relied on nowhere. Its 99 seats are genuine long-term assignments rather than per-diem shifts — the average span in the window is 139 days.

Neither book has a VMS decision feed, so `decision_state` is `Not tracked in Bullhorn` / `Not tracked in Symplr` on every row and the decision is whatever the meeting records. Unlike MSP, that means the stage starts empty and earns its value after a few meetings.

### Fields that were blank and now aren't

`lt_order.tempid` and `BookedByUserID` resolve to the assigned clinician and the internal booker on 100% of filled orders; both were coming back empty. An extension conversation without the clinician's name is most of the way to useless. The same join was missing on Closed. On Bullhorn, `hiring_manager` comes from the placement's client contact, which resolves on 100% of live seats — `jo.reportToClientContactID` is never populated and would have looked like the obvious choice.

What stays blank is blank at the source: Bullhorn has no department, unit or cost-centre column on either the job order or the placement, and Symplr `lt_order` carries no rate, so 13-week extension value is Bullhorn-only.

## Onboarding on non-MSP

Seats starting in the -30/+45 day window, grouped ON TRACK / DELAYED START / CANCELED on the same rules MSP uses.

Bullhorn measures start-date slip properly, which B4 cannot. `EditHistoryPlacement` records every change to `dateBegin` with old and new values, so the planned start is what the first edit replaced and the slip is a real day count — B4 carries only a Yes/No `Delayed Starts` flag. On the live window: 30 of 62 seats have measurable movement, median slip 1 day, longest 364.

`onboardingStatus` is a genuine progress field on this book (Initiated 4,270, Completed 1,506, In Progress 169, Cancelled 298 over a year) with no MSP equivalent.

Symplr can't measure movement at all: no audit trail on `lt_order`, `temp_confirm_date` and `client_confirm_date` both 0% populated, and `StatusChangeLog` keyed on WorkerID rather than the order. Those rows carry `movement_tracked: false` and a null day count, so the UI shows "not tracked" rather than a zero that would read as *started on time*.

### The planned start is the first edit, not the earliest date

`MIN(oldValue)` looks like the original start and answers a different question — the earliest date ever *proposed*. The two diverge whenever a start is pulled earlier before being pushed back. One seat in thirty on the live window, but it reported a 392-day slip where the truth is 364. The planned start is now the value the chronologically first edit replaced.

**3.0.0** - IMPACT Meeting Workflow

The facilitated IMPACT meeting, merged into the app we already ship rather than replacing it with the marketing prototype's shell. PR #58 carries additive backend work plus native views; the prototype's own UI is dropped.

### The meeting layer

Start IMPACT Meeting scopes a session to a health system, walks the chosen stages in order, records every mutation made along the way, and saves to `impactmgr.meetings` so a meeting can be reopened later. Past meetings list from the same table, with the recap stored exactly as it was sent.

Meeting state lives in memory during the session, so the persistence path matters more than it looks: the stage scope is saved (not just progress), a failed final save keeps the meeting open rather than discarding the only copy, and an unfinished meeting can be resumed from setup — reusing its id so the MERGE keeps updating one row.

### Ten tabs, one layout

Closed · Open Jobs · Extensions · Onboarding · Priority Jobs · Trends · Per Diem · Revenue · Contracts · Pending.

`viewToggle` was a hand-rolled list where every tab appeared in three places; it is now driven by a single `View.VIEWS` table, because adding three tabs to the old shape meant remembering all three spots — the same drift that kept losing the GHR/Affiliate guards.

Extensions, Onboarding and Closed render in the app's own table + tile idiom, with frozen headers, per-column sort and filter, coloured legend bands and six KPI cards.

The scaffolded donut charts are gone. Checked against the v24.6 reference, which puts its charts (6-week `lineChart`s) in the **stage overview modals**, not on the table views: Extensions' Runway and Decision Mix and Onboarding's Start Status and Vendor Split had no counterpart at all. Runway also duplicated the coloured legend bands, which show the same split and are clickable, and Decision Mix could only ever render one grey ring until decisions are recorded. Closed keeps **Account Capture**, the one chart with a basis in the reference (`accountCapturePie` — "Closed Market Share / Agency starts", GHR vs Affiliates); its "Why It Ended" companion went, since the grouped table already says that.

Extension Search now has its own always-visible bar above the Extensions table rather than living inside the collapsible Analysis strip — it is a control, not analysis, and collapsing Analysis hid it. All ten tabs put their KPI cards in the shared `#kpiContainer` above the tab row, so nothing shifts between tabs. **The page scrolls on desktop.** It used to be pinned to the viewport (`body ... md:h-screen md:overflow-hidden`), so every table was `flex-1` — it got whatever height was left after the app header, filter bar, KPI row, tab heading and Analysis strip. On Closed that was four rows in a sliver with its own scrollbar, and no way to scroll the page to reclaim the space. The page now scrolls, the wrappers size to their content, and each table is a `70vh` window with its frozen header sticking inside it — so scrolling down moves the chrome off-screen and leaves the table filling most of the display. All three read B4 + VNDLY with no `is_non_msp` branch, so they are MSP-only and a request for one bounces to Open Jobs rather than opening a dead view.

Row-detail sub-tabs on List rows: Levers (still the default) plus Rate, Pipeline and Placements. Pipeline splits GHR vs Affiliate by stage. GM% is the configured margin rate or the per-job override; the bill/pay split the new endpoints return is an MSP *fee* split and is never labelled as margin.

### Data corrections found on the way

Most of these came from checking the queries against live data rather than reading them.

- **Onboarding start slip is now real.** `STAGING_VNDLY_JOBS` holds the start as of the application and keeps it when the work order's start moves, so it serves as a planned start (96.6% control match). Getting its grain right exposed three defects in one CTE: the join fanned out across every seat on a requisition, `hours_per_week` was therefore the largest value on the whole req rather than this seat's (177 reqs differ across seats), and `days_delayed` was hardcoded null on VNDLY while referencing a dropped alias on B4 — which made the entire B4 branch invalid and silently swallowed.
- **Per Diem open orders were filtered to the past.** Unfilled requisitions start in the future; the drill-down applied the worked-shift date cap, hiding 91 of 104 live open orders and all three Per Diem ones.
- **The Pipeline interview stage read a field that is almost never populated.** B4 carries no interview date at all and VNDLY fills one on 1.3% of submissions, so interview is a stage users record in the app — and reading the raw date discarded all of them.
- **Placements read fields that don't exist** on the stats payload, and compared job title against care type, so the pane was usually empty.
- Margin now has one read path over two stores, with the configured default marked as an assumption rather than presented as a decision.
- Date-only values stop shifting a day, and "not tracked" is shown wherever a source genuinely cannot answer — never a confident zero.

Where a source can't support something the prototype showed, the app says so instead of inventing it.

**2.2.10** - Non-MSP: division rollups (Travel/Planet→Nursing, Human Services→Education, LTC→Non-Acute)

Extends the 2.2.9 alias map with the remaining org changes Bullhorn's `correlatedCustomText1` hasn't caught up with:

| Legacy value | Rolls into |
|---|---|
| `Acute`, `Travel Nursing`, `Planet Healthcare` | `Nursing` |
| `Human Services` | `Education` |
| `Plymouth Meeting LTC` | `Non-Acute` |

The last two also align Bullhorn with the vocabulary Symplr already emits — `symplr_systems.build_division_case_expr` returns exactly `Education` / `Non-Acute` — so both sources finally share one division taxonomy instead of two disjoint ones.

`Workforce Solutions` (the MSP division) is deliberately left as its own value pending a decision on whether it should appear on the non-MSP instance at all.

**2.2.9** - Non-MSP: Acute division rolls into Nursing

Acute was merged into Nursing organizationally, but ~4,000 Bullhorn job orders still carry the old `correlatedCustomText1` value. It therefore appeared as its own Division option — effectively dead — while its teams (`Acute Team 1-5`, `TX Acute`) sat stranded away from the rest of Nursing.

New `Utils.DIVISION_ALIASES` + `normalizeDivision()`, applied at **ingest** (the job mapper and `normalizeTrendRow`) rather than at each comparison, so the dropdown, the matcher and the Division→Team cascade all see one canonical value with no further call sites to keep in sync. Comma-separated legacy values are mapped element-wise and de-duplicated, so `Acute,Nursing` collapses to `Nursing` rather than listing it twice. Future org changes are a one-line addition to the alias map.

**2.2.8** - Non-MSP: Active/Declines column headers drop the "GHR / Agency" label

The 2.2.5 AV strip fixed the per-row values but not the column headers. `Active` and `Declines` are static markup in the List table head, outside the JS render path the non-MSP guard lives in, so they kept advertising a `GHR / Agency` split above a single number. Now hidden on non-MSP. The row values and the List KPI cards were already handled.

**2.2.7** - Non-MSP: Specialty filter uses the structured value; Division→Team and Profession→Specialty cascade

- **Specialty filtering keys off `dbo.Specialty`, not the job title.** `job.specialty` is `jo.title` — free text with one variant per posting, so as a dropdown it was unusable and shared nothing with Trend, which uses the structured value. The card keeps the job title (that's the useful label); filtering and the dropdown now go through `Utils.jobSpecialtyKey()`, which prefers the structured name and falls back to the title so MSP is unaffected. List and Trend finally mean the same thing by "specialty".
- **Cascading option lists within each hierarchy.** Picking a Division narrows the Team dropdown to that Division's teams; picking a Profession narrows Specialty. The two hierarchies stay independent of each other — Division never constrains Profession — and picking a Team or Specialty directly without its parent still works, since these remain plain AND filters and the cascade only trims which options are offered. Assignments are folded in alongside open jobs so the lists don't collapse on tabs with no open orders.
- **Fixed an exclude-guard bug** in `updateAllFilterOptions`: the team constraint sat inside the `divisions` guard, so recomputing the Division dropdown silently dropped the team filter too.

Verified against live Bullhorn: 422,525 of 422,797 job orders carry exactly one category (269 carry two, 3 carry more), so the single-deterministic-row `OUTER APPLY` added in 2.2.5/2.2.6 is effectively lossless — no comma-list splitting needed. All 2,147 specialties have a populated `parentCategoryID`.

**2.2.6** - Non-MSP: real Specialty via JobOrderSpecialties (closes audit §F5 on Trend)

Completes the association work started in 2.2.5. Specialty is a to-many association like category — `dbo.JobOrderSpecialties` → `dbo.Specialty` — resolved with the same single-deterministic-row `OUTER APPLY`.

- **Trend's Specialty stopped duplicating Profession.** It read `p.customText1`, which on a placement *is* the profession, so the Specialty and Profession dropdowns showed identical values (NON_MSP_FILTER_AUDIT.md §F5). Now prefers the job's real specialty, falling back to the old value so nothing empties out.
- **GetPositions `subspecialty`** now carries the real specialty instead of the unmapped `customText2`. `specialty` stays `jo.title` — that's the job title the list card displays. Note the frontend does not currently read `subspecialty`, so this is data-only until we decide whether the non-MSP Specialty filter should switch from job title to the structured value.

**2.2.5** - Non-MSP: Division fixed at the source, Team filter, profession via Category, AV strip on List

The v2.2.3 division whitelist made the Division filter return **nothing** on the List view. Root cause was two wrong columns, not the matching logic.

- **Division is now job-level.** `record.division` came from `cc.customTextBlock1` — a comma-separated list of every GHR team servicing the CLIENT, which is why one client showed 4,403 RN placements tagged `Allied,Nursing,RevCycle Workforce,United`. Bullhorn's field mapping puts Division on the JOB (`correlatedCustomText1`), single-valued and clean (`Planet Healthcare`, `Travel Nursing`, `Allied`, `Nursing`, `RevCycle Workforce`, `Acute`, `Search`, `United`, `Locum Tenens`, `Technology`, `Texas`). Falls back to the client tag list only where the job carries no division, so legacy rows stay filterable.
- **Profession was reading an unmapped column.** `GetPositions` used `jo.customText1 AS profession`, but on a job order profession is a to-many association — `View_JobOrder` has no `categoryID` column at all; the mirror splits it into `dbo.JobOrderCategories` → `dbo.Category`. So profession was NULL for nearly every job order. Now resolved via `OUTER APPLY` taking one deterministic category. On a *placement* `customText1` genuinely is the profession, so Trend keeps it as fallback — that's why Trend was always better populated than the List.
- **The profession-keyword whitelist is deleted** (`DIVISION_PROFESSION_KEYWORDS` + the 3rd argument, 7 call sites). It existed to recover the real team from a client-level division tag by guessing from profession. With a job-level division there is nothing to guess. It was also broken both ways: `'ma '`/`'pa '` carried trailing spaces so a profession of exactly `MA`/`PA` could never match, while plain substring matching let `'rn'` match `CRNA` and `'do'` match `Endoscopy Tech` — the same cross-division bleed the whitelist was added to stop. And its `if (!prof) continue` branch is what turned sparse profession data into an empty filter.
- **New Team filter** (`correlatedCustomText5`), non-MSP only, hidden when empty — `Buffalo Nursing`, `Blue Bell Nursing`, `Travel Team 1-5`, `Acute Team 1-5`, `RevCycle Coders`. Team is sparsely populated, so an unset team does NOT pass a team selection; otherwise picking Buffalo would return Buffalo plus ~193k untagged rows. Deliberately unlike the Region rule, where NULL passes because Bullhorn region is universally NULL.
- **List actives/declines drop the GHR/AV split on non-MSP** — single total, headers `Subs`/`Declines` instead of `Subs (G/A)`/`Dec (G/A)`. Same 2-of-N drift as the Trend tab.
- **Quick-attach people picker searches Graph on non-MSP.** The hardcoded `CONSTANTS.TEAM_MEMBERS` chips are the MSP pod; on non-MSP they were simply the wrong names. MSP keeps the chips; non-MSP gets a debounced typeahead against the existing `/api/search-users` Graph endpoint. Both paths now share one `applyTagSelection(mode, name)` helper.

Known gap: job-order **specialty** (`specialty_categoryID`) is likewise not a column on the view and has no obvious job-level link table, so `subspecialty` still reads the unmapped `customText2` and stays mostly NULL.

**2.2.4** - Non-MSP §F3: System rolls up to parent, Facility stays specific

Closes §F3 from NON_MSP_FILTER_AUDIT.md. Both source systems have a parent/child chain we weren't using — Bullhorn's `parentClientCorporationID` on `View_ClientCorporation` and Symplr's `MasterClientID` on `profile_client`. Sub-orgs like `Cone Health OrthoCare Greensboro` / `Cone Health Behavioral Health Hospital` were their own systems, so the System filter dropdown mostly duplicated the Facility one on non-MSP.

- **Bullhorn**: `build_system_case_expr`'s fallback changed from `cc.name` to `ISNULL(pcc.name, cc.name)`. The 8 hardcoded rollups still take precedence (Cone Health / Orlando Health / etc.); auto-discovered clients now roll up one level via `parentClientCorporationID`. ~70% of in-scope Bullhorn clients have a parent, so this collapses a lot.
- **Symplr**: `build_system_case_expr` returns `ISNULL(m.clientname, pc.clientname)` — `m` = the MasterClient parent when the client is a sub-org, NULL when it's a top-level master. ~30% of in-scope Symplr clients have a master. Sub-orgs like `DCIU ECE - Aston` / `DCIU ECE - Wallingford` now roll up to System=`DCIU ECE` with Facility keeping the specific sub-org.
- Every non-MSP query now joins the parent alongside the client: `LEFT JOIN dbo.profile_client m ON pc.MasterClientID = m.recordid` for Symplr, `LEFT JOIN dbo.View_ClientCorporation pcc ON cc.parentClientCorporationID = pcc.clientCorporationID` for Bullhorn. Applied across all 6 non-MSP endpoints — 12 Symplr + 7 Bullhorn join sites.
- Facility stays as `cc.name` / `pc.clientname` — dropdowns are now meaningfully different.

Live verification: 118 distinct Symplr facilities collapse to 99 systems after rollup.

**2.2.3** - Non-MSP filters: Division profession intersection + hidden-system MSP-only guard

Two filter bugs on the non-MSP side. Full audit in [NON_MSP_FILTER_AUDIT.md](NON_MSP_FILTER_AUDIT.md).

- **§F1 Division filter over-matched via client-level tags.** Bullhorn `record.division` is `cc.customTextBlock1` — a comma-separated list of which GHR teams service the CLIENT, not the team that placed THIS worker. Selecting "RevCycle Workforce" pulled in RNs at any client whose tag list included RevCycle (most non-MSP clients do). `Utils.matchesSelectedDivisions` now also requires the placement's profession to match a keyword whitelist per division (Rev Cycle needs Coder/CDI/HIM Specialist/etc.; Nursing needs RN/LPN/CNA; Allied needs OT/PT/SLP/therapist/tech/etc.; Locum Tenens needs CRNA/Anesthesiologist/NP/PA). Divisions without a whitelist (Planet Healthcare, Search, Workforce Solutions, Human Services, Acute, United, Technology, Education, Non-Acute) keep the old client-list-only match — those are business-line labels rather than roles. Signature updated across 7 call sites to thread `profession` through.
- **§F2 Hidden-system MSP keywords cross-contaminated non-MSP.** `Utils.isHiddenHealthSystem` iterated `CONSTANTS.HEALTH_SYSTEM_MAPPINGS` (MSP-managed table from Settings → Health Systems), so any non-MSP client name containing "jefferson" or "sunrise" got silently dropped. Now short-circuits to `false` when `dataSource === 'non_msp'` — non-MSP has no equivalent hide taxonomy.

Open items §F3–F7 documented in the audit file (Facility duplicates System on non-MSP, Category mixes engagement mode + role, Specialty duplicates Profession, Region filter is Symplr-state-only, `matchesStatsCategoryFilter` broad-keyword-matches). None ship this pass — they need UI decisions.

**2.2.2** - Symplr scope filter now renders as a flat IN-list (fixes v2.2.0 Trend + Financials timeouts)

v2.2.0 shifted Symplr scope from a hardcoded rollup to auto-discovery, but `build_scope_filter` still built a correlated subquery that expanded MasterClientID AND filtered `r.regionname NOT LIKE '%MSP%'` on every row of the outer query. On the aggregate ORDERS-based queries (Trend + Financials + Hours + YoY) this measured **~66 seconds** against live data — past the SWA Free 45s gateway limit, so those tabs surfaced as "backend call failure" to users.

Fix: move both the MasterClientID expansion and the MSP-region exclusion into `resolve_scope_master_ids` (Python side), so it runs ONCE at scope resolution time. `build_scope_filter` then renders as a flat `col IN (list)` — SQL Server can seek the clustered index on `profile_client.recordid`.

Measured against live DB: the aggregate ORDERS COUNT(*) query drops from **65.9s to 1.1s** (~60× faster). Identical result set (11,396 rows). The lt_order-based trend query also drops from 332ms to 93ms.

No functional change for users — same scope, same MSP exclusion, same data. Just doesn't time out.

**2.2.1** - Hide flag now hides from dropdowns too

Systems marked `hidden` in Settings → Health Systems were already excluded from every row-level filter check via `Utils.isHiddenHealthSystem`, but the System and Facility filter dropdowns kept populating with them — so a user could pick a hidden system from the dropdown and get zero results. Filter dropdown population (LOAD_DATA and REFRESH_UI paths) now runs the same hidden-system check: hidden systems drop from the System dropdown, and facilities whose resolved system is hidden drop from the Facility dropdown. Applies to both MSP and non-MSP. Applies to Jefferson + Sunrise today; anything future users hide via Settings picks up automatically.

**2.2.0** - Symplr: drop hardcoded rollup, auto-discovery by region, MSP exclusion, expanded service line

The Symplr side of the non-MSP dashboard was scoped to 3 hardcoded school-district masters (Reading SD / Allentown SD / DCIU). Everyone else — KenCrest, Presbyterian Senior Living, Bancroft NeuroHealth, ~10 other school districts — was invisible. Symplr now works like Bullhorn: any client with active work is pulled in automatically. Manual allowlist covers edge cases.

- **Scope is auto-discovered.** `discover_active_client_ids(symplr_cursor)` in `shared_code/symplr_systems.py` queries `dbo.lt_order` for any `status='filled'` placement in the last 90 days, joined to `profile_client` + `regions` — returns every client with active work whose region name does NOT contain 'MSP'. Manual `source='symplr'` allowlist entries in `impactmgr.bullhorn_client_allowlist` still expand via MasterClientID.
- **Division is derived from region.** Every Symplr row's division is `CASE WHEN r.regionname LIKE '%Education%' THEN 'Education' ELSE 'Non-Acute' END` — so "Education Nursing", "Education Para", "Education Therapy", "DE Education", "FL Education" all bucket to Education; everything else (PA Nursing, NJ Nursing, DE Nursing - Non-Acute, etc.) → Non-Acute.
- **MSP-flavored regions excluded from non-MSP scope.** Regions like `GHR Education MSP`, `GHR Non-Acute MSP`, `GHR MSP` are filtered out at scope time — they belong on the MSP dashboard (~140 placements affected). `build_scope_filter` enforces this even for manual allowlist entries as a data-integrity guard.
- **`build_system_case_expr` returns `pc.clientname`.** Each in-scope client is its own row on the Account view instead of collapsing to hardcoded 'Reading School District' / 'Allentown School District' / 'DCIU'. Aggregate queries wrap with `MAX({system_case})` — `pc.clientname` is a plain column ref rather than the old inline CASE that was safe in GROUP BY on its own.
- **Every Symplr query joins `dbo.regions r` alongside `dbo.profile_client pc`.** Required by the new region-derived division / MSP-exclusion logic. Applied across GetTrendData, GetStatsData, GetPositions, GetHoursData, GetFinancialData, GetYoYTrendData — 12 join sites total.
- **`symplr_service_line_case()` taxonomy expanded** to cover ~140 rows/wk previously falling into 'Other': `Para`, `DSP`, `LPN,RN` / `RN,LPN` combos, `Registered Behavior Technician`, `BCBA`, `Social Worker School`, `SW`, `DON`, `ADON`, `NHA`, `Job Coach`, `Special Ed Teacher`, `Sub Teacher`, `Teacher`, `SLPA`, `School Psych`, `Cert School RN`, and more. Post-refactor bucketing verified against live data.

Live snapshot (2026-08-03): auto-discovered Symplr scope resolves to ~200 clients across Education + Non-Acute divisions (vs 3 clients before). Education: ~204 placements post-MSP-exclusion, Non-Acute: ~83.

Deferred: wire Symplr into the MSP-side endpoints so `GHR Education MSP` + `GHR Non-Acute MSP` regions show up on the MSP dashboard. The exclusion here keeps them off non-MSP but they still need a home.

**2.1.0** - Trend tab: Projected Headcount line on the chart

Closes Tier 3 §3a from TREND_TAB_FOLLOWUPS.md.

The projection engine — `buildProjection`, `holtSmooth`, `buildBlendedProjection`, `PENDING_CONVERSION`, `projectionData` / `ghrProjectionData` / `affProjectionData` — was being computed every render and the outputs never rendered anywhere. Wiring them onto the chart makes the ~150 lines earn their keep:

- **New "Projected Headcount" line on the Category view chart** (long-dashed grey — matches the Total series color, dashed to visually distinguish from actuals + pipeline). Data source is `projectionData` = Holt-smoothed trend blended with forward pipeline + expected-conversion of pending (`PENDING_CONVERSION = 0.7`), weighted 75% at +1 week, 50% at +2, 25% at +3, 0% at +4 — near-term reflects known-quantity pipeline, far-term defers to trend.
- **Overlay datasets** (used by Account, PM, Division views) now include the projection line alongside Total + Pipeline. Same three reference series stay steady across Group By switches.
- **Legend groups projections properly** — `parseLabel` recognizes "Projected Headcount", "GHR Projection", "Affiliate Projection" and slots them into the Total / GHR / Affiliate groups as a "Projection" variant.
- **Non-MSP: projection dropped** on the chart because the pending-conversion component is 0 (no submission funnel per BULLHORN_PORT_SPEC §5), which would leave the projection line collapsed onto Total Pipeline.
- **Dead code cleanup**: `linearRegression` was defined but never called — removed. `totalFillProjection` / `ghrFillProjection` / `affFillProjection` were computed but never used — removed alongside the `projectSeries` helper that only fed them. Fill Rate mode itself needs redefinition per §5b before its projection variants are worth adding.
- The audit called out two misleading comments promising "Trend Projection" output that didn't exist. Both replaced with accurate descriptions of what's actually rendered.

Note on numbers: the projection line inherits any bias in the lookback — see §2 residual on VNDLY terminal end dates (fixed to use last-spend week in an earlier commit), and the various filter parity fixes in v2.0.3 / v2.0.4. Should be sanity-checked against a few weeks of real data before it's trusted for planning decisions.

**2.0.5** - Trend tab: non-MSP category taxonomy + Division axis replaces PM view

Closes Tier 3 items §3b and §3c from TREND_TAB_FOLLOWUPS.md.

- **§3b Non-MSP categories bucket into clinical service lines**. Trend was leaving every Bullhorn `employmentType` and Symplr `nursetype` value in the raw form, so the "Nursing" / "Allied" / "Advanced Practices" / "Non-Clinical" groupings collapsed to a single "Other" catch-all row on non-MSP. Two service-line CASE expressions now normalize server-side: `symplr_service_line_case()` factored out of GetFinancialData into `shared_code/symplr_systems.py` (Nursing / Allied / Non-Clinical / Other), and a new `BULLHORN_SERVICE_LINE_CASE` in GetTrendData that maps `p.customText1` (profession — RN, Coder, CRNA, OR Tech, etc.) into the same buckets with a fallback to `employmentType` (Travel / PRN / Local / Remote / Permanent) so unmapped rows still land in a labelled bucket. Both trend queries emit `service_line` alongside raw `category`; the addRow closure prefers `service_line` on non-MSP. Live verification: 9,910 Nursing / 3,455 Allied / 2,780 Non-Clinical / 1,481 Advanced Practices / ~2,200 in engagement-type fallbacks.
- **§3b bonus — Profession filter uses the populated column**. v2.0.3 wired `jo.customText1 AS profession` but that field is NULL on the vast majority of job orders older placements attach to, so filtering by profession from the dropdown would have silently dropped most trend rows. Now `COALESCE(NULLIF(jo.customText1, ''), p.customText1)` — placement-level customText1 carries the real value (RN, Coder, CRNA, Social Worker, etc.) when the job order's is empty.
- **§3c Division axis replaces PM view on non-MSP**. The PM view was showing every non-MSP row as "Unassigned" because `pmMappings` is an MSP-only admin table. New Division axis accumulates weekly `divGhr` / `divAff` / `divSysGhr` / `divSysAff` sets in `weekData` — Bullhorn division is comma-separated per client, so a worker on an "Allied,Nursing,RevCycle Workforce" client contributes to all three buckets (noted in the Total-row tooltip). PM button in the Group By toolbar swaps to "Division" on non-MSP; MSP unchanged. Division chart datasets, breakdown table, restore-view logic, and defensive reset (if user is on PM/Vendor and switches to non-MSP, or on Division and switches to MSP) all wired.

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