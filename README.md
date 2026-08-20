# GHR Impact Manager

## Version History

**2.4.11** - Coloured legend filters on all four main tabs, banded per tab

The legend chips existed only on Open Jobs. They now appear on Extensions, Onboarding and Closed too — with **bands that mean something on each tab**, following the reference's `LEGEND_PREDICATES` rather than copying one list everywhere.

| Tab | Bands |
|---|---|
| Open Jobs / Priority Jobs | `1-5` · `6-10` · `11-15` · `16+ Days` · `Perm` (aging — high is bad) |
| Extensions | `≤7` · `8-14` · `15-21` · `22+ Days Left` (runway — **low** is bad) |
| Onboarding | `On Track` · `Start Moved` · `Delayed` · `Cancelled` |
| Closed | `Ran To Term` · `Ended Early` · `Cancelled` · `Administrative` |

Reusing Open Jobs' five day-bands everywhere would have been wrong on three of the four tabs: on Extensions a *low* number is the urgent one, and Onboarding and Closed don't band by days at all.

- **Chips OR together**, as in the reference — picking two bands widens the view. Intersecting disjoint day-bands would always be empty.
- Open Jobs' chips still drive the same `ageMin`/`ageMax` the Days Age inputs write, so those two controls stay in sync; the other tabs carry a predicate, since their bands aren't a numeric range over one field.
- **Selections clear on tab change.** Band keys are per-view, so a selection carried across tabs would filter everything out with no visible chip explaining why.
- Trend, Per Diem, Revenue, Contracts and Pending have no bands defined, so the legend hides rather than showing chips that do nothing.

19 assertions across per-view band sets, OR semantics, null handling, outcome predicates, chip rendering and highlight state, and the empty case.

**2.4.10** - One layout across all ten tabs: KPI cards stop moving

The tabs had drifted into three different layouts. KPI cards sat **above** the tab row on List / Per Diem / Revenue (the shared `#kpiContainer`), **inside the panel below the tab row** on Trend / Closed / Extensions / Onboarding / Priority Jobs / Pending, and **inline at the top of the content** on Contracts — with two different card designs between them. Switching tabs moved the cards.

Now every view renders through `View.renderKpiCards()` into the shared container above the tab row. One card design, one position, no shift on tab change.

- The five per-wrapper tile containers are gone, along with the now-dead `stageTile()` helper and Contracts' local `kpiCard()`.
- Stage cards gained icons to match the existing card design instead of the plainer tile look.
- Trend's cards are next-7-day *deltas* rather than totals, and the shared card has no subtitle line — so the comparison week moved into the label (`Total Headcount vs Aug 20`) rather than being dropped.
- Stage panels reclaim the freed vertical space for their tables.

Card counts still vary by tab (4-6) because the metrics do, but card size and position are now identical everywhere.

**2.4.9** - Row-expansion audit: Placements pane was reading fields that don't exist

Audit of the drop-down detail panes across the list views — whether the data each needs actually resolves, and whether every edit reaches the database.

**Bug found and fixed.** The Placements pane read `r.worker_name` and `r.is_ghr` off `statsData.onAssignment`. Neither exists: `GetStatsData` returns `candidate_name` and an agency *string*, on both the MSP and non-MSP paths. So every placement rendered as "Unknown", every row was badged Affiliate, and GHR share was permanently 0%. Its match condition was wrong too — it compared the job **title** against an assignment's care type, which almost never matches, so the pane was usually empty even when comparable placements existed. It now matches on facility plus the job's *structured* specialty where one exists, falls back to facility only where it doesn't, and says which basis it used.

Also added `Utils.isGhrAgency()` rather than a fifth inline copy of the same `includes('ghr') || includes('planet healthcare')` test, and vendor names in that pane now honour `redactMode` like the Submission Log does.

**Mutations verified as persisting.** Levers, job margin, next step and AV-open-date go through `recordChange` → `/changes` (and are replayed on load). Extension decisions, revised starts, interview stages and contract GM overrides go to `workspace_state`. All eight write paths confirmed, all feeding the meeting recap.

**Header alignment.** Stage table headers now use the List tab's treatment (bold / slate-600 / wider tracking / matching padding), so the four tabs read as one table style. Deliberately *not* converted to a `<table>`: the List header, its job rows and its detail wrapper are all `grid grid-cols-12` with 23 `col-span` declarations, so converting the header alone would misalign it from its own rows, and converting all three risks visual regressions in the most-used view — against the "UI shouldn't change much" constraint. The four are already aligned on what matters: frozen header and sortable columns. Per-column filters exist only on the stage tabs because the shared filter bar applies just System and Facility there, whereas on Open Jobs it already applies eight dimensions plus ID search and a day range — strictly richer than a column filter would be.

11 assertions on the Placements pane: correct field reads, GHR/affiliate counting from the agency string (including Planet Healthcare), share maths, both match bases and the disclosure of which was used, redaction, and the empty state.

**2.4.8** - Frozen table headers with per-column sort and filter on the stage tabs; Start Meeting centred

- **Header row freezes.** Closed / Extensions / Onboarding now scroll their rows under a sticky header. Their wrappers were reworked so the KPI tiles stay fixed and the table area is the scroll container — a sticky `thead` sticks to its scrolling ancestor, and the old `overflow-x-auto` wrapper was quietly becoming that ancestor. Open Jobs already had a sticky header, so all four now behave the same.
- **Sort on every column**, with an indicator. Third click clears rather than cycling, so each view's own default ordering stays reachable. Nulls sort last in *both* directions — a missing value isn't "smallest", it's unknown, and burying it beats ranking it.
- **Per-column filters** in a second header row, case-insensitive substring. An empty result says "Nothing matches these column filters" rather than the generic empty state, so a filtered-to-nothing table doesn't read as a quiet week. A Reset link appears only once a sort or filter is active.
- **Grouping survives sorting.** Extensions and Onboarding keep their workflow-state groups and sort *within* them; the default days-left / move-count ordering applies only when no column sort is active.
- All of it lives in `stageShell`, so the three stage tabs share one implementation rather than three that drift apart.
- **Start Meeting moved to the centre** of the header, as the reference had it — it's the primary action, so it sits apart from the utility cluster. Past Meetings stays with the other utilities on the right.

17 assertions across sticky markup, sort direction and arrows, numeric vs text sorting, nulls-last in both directions, filter narrowing and its empty state, the reset affordance, and grouping being preserved under sort.

Known inconsistency: Open Jobs has sticky + sort but no per-column filters — its header is a CSS grid rather than a table, so the filter row doesn't transplant directly.

**2.4.7** - Interview stage overrides — the last WorkspaceState scope is wired

All five scopes (`lever`, `extension`, `onboarding`, `interview`, `margin`) now have a UI.

- Submissions in the List row's Submission Log get a stage select: `Submitted · Interview Requested · Interview Scheduled · Interview Complete · Offer Pending · Offer Accepted · Post-Offer Decline · Declined`, taking the reference's vocabulary.
- **Derived by default, overridable on top.** The source systems supply dates, from which a stage is inferable — but a recruiter often knows more than the dates do, and `Interview Requested` has no date field at all. So the derived value is the default, an override sits over it, and the row is tagged `auto` or `manual` so the two are never confused.
- **Editable for internal submissions only**, as in the reference. An affiliate's pipeline is theirs to report, not ours to overwrite — affiliate rows keep the read-only derived status.
- The select stops event propagation: the row itself opens the candidate-action modal, so without that, picking a stage would also open a modal.
- Saves feed the meeting recap like every other mutation.

12 assertions on the derivation precedence (declined beats offer beats interview), override behaviour, key format, and a nameless candidate.

**2.4.6** - Per-view KPI tiles on every tab, and date-only values stop shifting a day

- **Priority Jobs** and **Pending** were the last two tabs with no tiles. Priority Jobs shows jobs / openings / avg days open (flagging how many are 16+) / active submissions; Pending shows pipeline / submitted / offers out / ready-to-onboard with GHR share. Both render into their own wrapper, the pattern Trend and the stage views use, rather than the shared `#kpiContainer` which belongs to List / Per Diem / Revenue. Contracts already rendered its own cards inline. That completes per-view metrics across all ten tabs.
- Pending's buckets come from `statusBucket`, the same classifier the view itself uses, so the tiles can't disagree with the rows beneath them.

**Date fix.** `toDateOrNull` turns a date-only string like `"2026-08-20"` into UTC midnight, which local formatting renders as Aug 19 anywhere west of UTC. New `Utils.fmtDate()` takes the zone *from the value*: exactly-midnight-UTC is a date-only value and formats in UTC, anything carrying a real time component formats locally — because blanket-UTC would shift genuine timestamps the other way. Applied to the candidate submission and decline dates, and the Placements pane now shares the helper instead of hardcoding UTC.

Worth noting the sweep found fewer real problems than expected: of nine unguarded `toLocaleDateString()` calls, six were `new Date()` — a real instant with real local time, never at risk. Only the three formatting *source* dates needed changing.

6 assertions on the formatter across date-only strings, local datetimes, late-evening values that would cross midnight, Date objects and null.

**2.4.5** - Margin: one read path over two stores, and the default is marked as an assumption

Reconciles the two margin mechanisms. They turned out not to be duplicates — they cover different entity spaces, and one of them was silently lossy.

- An open **job's** margin already worked: `recordChange` writes `margin_update` and the loader replays it back onto `job.margin`. Kept as-is.
- A **contract** (an Extensions / Onboarding / Closed row) has no job behind it, and the replay does `if (!job) return` for ids missing from `jobMap` — so a contract-level override written to `/changes` would have been accepted and then dropped on reload. Those go to `workspace_state` scope `margin` instead.
- **`Utils.marginFor(id, job)`** unifies reading: job override → contract override → configured default → none, returning the source alongside the value so no caller needs to know which store applied.
- **`Utils.marginLabel()`** renders `31%` for a deliberate override and `~25%` for the configured default, taking the prototype's convention — an assumption applied tenant-wide shouldn't look like a decision made about this seat.
- Extensions and Onboarding details gain a Rate & GM block: bill rate and GM/hour read-only, GM% overridable, blank clears back to the default. Changes feed the meeting recap.
- Bill rate stays read-only throughout: it is source-owned. GM% is a rate we apply, which is why it is the only editable half — MSP carries no clinician pay rate, so there is no bill-minus-pay margin to read.

A junk `job.margin` now falls back to the tenant default rather than rendering nothing — caught by test, not by reading.

16 assertions across both stores, override vs default sourcing, numeric-id coercion, and unparseable values on either side.

**2.4.4** - Onboarding becomes actionable: revised start, delay category, context

Same split as Extensions — status badge in the row, editing in the expanded detail — following the reference.

- **Rows group as the reference does**: `CANCELED · DELAYED START · START DATE CHANGED · ON TRACK`, each header carrying its count and total start-date moves, sorted by move count.
- **`START DATE CHANGED` is derivable, not invented.** The reference compares each start against the one recorded at the previous meeting, and no source field carries that — but `workspace_state` already stores a revised start per seat, so the last *saved* start **is** the previously-recorded start. Movement is measured against it.
- **Detail** shows source-owned facts (current start, last recorded start, times moved, source status) beside the editable box: revised start, six delay categories, meeting context.
- **Context is required when the date moved.** Saving a moved start without a reason is refused rather than silently recorded — that's the reference's "Context required" prompt, enforced.
- **Feeds the meeting recap**: "Start for C RN · Cooper recorded as 2026-09-15 (Compliance) — moved +14d".

Three data limits stated in the app:
- **Original Start** reads `not in data` — neither feed carries one, which is why total slip from the first scheduled date isn't shown. The panel says so.
- **Times Moved** is B4-blind: only VNDLY reports start-change events, so B4 rows show `—` with a tooltip rather than `0`.
- A never-reviewed seat says `never reviewed` instead of implying it hasn't moved.

20 assertions across group derivation, slip maths and its red threshold, the context-required refusal, the never-reviewed path, and both data-gap notices.

**2.4.3** - Extensions become actionable: client decision, workflow checkpoints, persisted

Wires `WorkspaceState` to the Extensions tab, following the reference's shape: a read-only decision badge in the row, editing in the expanded detail.

- **Rows group by workflow state** — `NO DECISION · RTO · OFFERED · PENDING ACCEPTANCE · APPROVED · BACKFILL · EXCEPTION` — each header carrying its count and total contract value, sorted by days left within a group.
- **Detail owns the editing**: the three team checkpoints (Confirm Client / Gather RTO / Send Extension), a six-value Client Decision, and shared notes. `Extension Accepted` is deliberately **read-only** — the source systems own it. Team-owned checkpoints are editable, system-confirmed facts are not, the same discipline that keeps the MSP fee split from being labelled margin.
- **Persists to `impactmgr.workspace_state`** and reflects locally, so the badge and the row's group update without a reload. Last-saved-by and timestamp are shown.
- **Decisions land in the meeting recap** through the same log the job mutations use, so a recap reads "Extension decision “Approved” for A Nurse · Inspira" alongside lever and margin changes.

Two data gaps are **stated in the app** rather than quietly omitted, so they can be explained in a demo instead of looking like bugs:
- The Gather RTO step shows its owner as `Recruiter · not in data` — B4 and VNDLY carry an account manager and the client's hiring manager; neither is a GHR recruiter. The step is still real and checkable, only the attribution is missing.
- The linked-backfill panel from the reference is absent, with a line saying nothing in B4 or VNDLY ties an ending seat to its replacement job.

20 assertions across grouping (every decision and step path), badge tones, the two gap notices, group ordering, and tile counts reacting to saved state.

**2.4.2** - Meetings record what changed, not just which stages were walked

`LOG_MEETING_ACTION` existed but nothing called it, so a saved meeting listed the stages you visited and nothing about what you decided in them. Mutations now feed the meeting log through the one function they already all pass through.

- **`recordChange()` is the single funnel.** All six mutation sites already called it to persist; it now also feeds an in-progress meeting. No meeting running means nothing is recorded, so ordinary use is unaffected.
- **New `describeChange()`** turns a change type + payload into one readable line ("Pulled lever “Hot Job Promotion” on Registered Nurse #371922 · Inspira Medical (owner A Smith)"). One place knows how to word each type, so the recap and any future audit surface read the same way. Unknown types degrade to a sensible sentence rather than throwing.
- **Recap bookkeeping can't break a save** — the feed is wrapped, so a logging failure never surfaces as a failed mutation.
- **Retired `sessionLog`.** Its only reader was the AI Call Summary removed in 2.4.1, and it duplicated `job.history`, which is what the per-job history modal actually renders. The parallel array is gone; `job.history` stays.

11 assertions across all four change types, unknown-type fallback, truncation, and the meeting-running / not-running branches.

**2.4.1** - IMPACT meetings: Start Meeting, guided stages, past-meeting history

Ports the prototype's meeting layer into the existing app, and retires the two buttons it replaced.

- **Start Meeting** opens a scope picker (health system, recap recipients, which of the four stages to walk). Choosing a system sets the real System filter, so every tab is genuinely narrowed rather than the banner claiming a scope the data ignores.
- **A meeting drives the nav.** Stage order matches tab order, so "next stage" and "next tab" are one motion. The banner shows progress, lets you jump between stages, and follows the nav in reverse too — clicking a tab that is one of the meeting's stages moves the meeting's pointer, so the two can't disagree about what's on screen.
- **Saves continuously.** `POST /api/meetings` MERGEs on a timestamp id, so starting, each stage completion and finishing all update one row — a meeting interrupted halfway is still there, and shows as `in progress` in history.
- **Past meetings** are browsable via the history button: list, then detail with the stages completed, every action recorded, and the recap exactly as it was sent. Finishing copies the recap to the clipboard.
- **Removed**: the AI Impact Call Summary button (`GENERATE_SUMMARY`) and the Change History viewer (button, modal, `openHistoryModal`/`closeHistoryModal`/`compareVersions`/`restoreVersion` — 245 lines). Snapshot *saving* on the `/history` endpoint is untouched; only its viewer is gone. The AI Hot Job Summary button stays, and keeps the shared `#summaryText` container it depends on.

22 assertions across setup, stage walking, recap grouping, the POST envelope, and the history/detail round trip.

**2.4.0** - IMPACT stages merged into the existing app UI

Brings the new functionality from the marketing prototype into the app we already ship, rather than replacing the UI with it. The prototype's own shell is dropped; PR #58 carries only additive backend work plus these native views.

- **Tab bar is now ten tabs**: Closed · Open Jobs · Extensions · Onboarding · Priority Jobs · Trends · Per Diem · Revenue · Contracts · Pending. Hot Jobs → Priority Jobs, Financials → Revenue, Trend → Trends. `viewToggle` was a hand-rolled list where every tab appeared in three places (wrapper toggle, button class, non-MSP hide); it is now driven by a single `View.VIEWS` table, because adding three tabs to the old shape meant remembering all three spots — the same drift that kept losing the GHR/Affiliate guards.
- **Extensions / Onboarding / Closed** render natively in the app's table + tile idiom, each owning its KPI tiles inside its own wrapper (the pattern Trend already used) rather than competing for the shared `#kpiContainer`. Each loads independently, records its own error, and shows a banner instead of an empty table that reads like a quiet week.
- **Hidden on non-MSP.** All three read B4 + VNDLY directly with no `is_non_msp` branch, so they join Per Diem and Pending as MSP-only, and a request for one bounces to Open Jobs rather than opening a dead view.
- **Onboarding shows "not tracked", not zero**, where a source can't say how far a start slipped — B4 has no such field, and a confident `0d` would be a lie.
- **Row-detail sub-tabs** on the List rows: Levers (the existing panel, still the default) plus Rate, Pipeline and Placements. GM% is the configured margin rate or the per-job override; the bill/pay split the new endpoints return is an MSP *fee* split and is deliberately never labelled as margin.

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