# GHR Impact Manager

## Version History

### 2.48.1 - The actual cause of the dead YoY overlay

Parallelising and caching in 2.48.0 was not enough — the endpoint still returned a 500,
because **one branch alone exceeded the gateway**. Measured each in turn:

| | |
|---|---|
| Symplr `lt_order` half | 2.4s |
| Symplr **orderless** half | **60.7s** |

The orderless branch joined `profile_client`, `profile_temp` and `regions` across all
**817k** orders and aggregated afterwards. Grouping the orders first and joining the
lookups to the much smaller result is **6.9s**, for byte-identical output — 76,406 week
rows, 2,782 distinct workers.

The derived table is aliased `o` so the injected `{sys_case_orders}`,
`{division_case_orders}` and `{scope_orders}` expressions, all built against
`'o.customerid'`, still bind.

### 2.48.0 - The non-MSP prior-year overlay was dead, and its filters lied (GH #50)

Found while verifying #50: **`yoy-trend-data` was returning a 500 on non-MSP** — 45.2s
against the 45s gateway — so the prior-year overlay never loaded on that book at all.
MSP answered in 9.1s and was fine. The same uncached code is on `main`, so production
non-MSP has the same dead overlay.

- **Fixed by running the two sources concurrently and caching the result.** Bullhorn and
  Symplr ran one after the other with nothing cached, and a sixty-week historical series
  does not move within a day. Each branch gets its own connection
- **The endpoint also dropped its `errors` list from the payload**, the same defect
  `financial-data` had in #86 — a failed source rendered the overlay with half the book
  and no sign of it. Now returned, and a partial result is never cached
- **#50 -- the two prior-year filter guards disagreed with each other, silently.** The
  payload is a weekly aggregate by system / category / facility / division / region /
  vendor_type and carries **no profession or specialty column**. The profession guard
  short-circuited on `undefined` and kept 100% of rows, so a filtered current year was
  compared against an unfiltered prior year; the specialty guard compared `''` against
  the selection, matched nothing, and made the overlay vanish instead. Both short-circuit
  the same way now, and the tab states plainly that the prior-year line is not filtered
  by profession or specialty when such a filter is active — rather than leaving the
  reader to infer it from a wrong delta

### 2.47.1 - Rate-Intel weeks were off by a week, and free-text arithmetic hardened (GH #92, #49, #65)

- **#92 -- Rate Intel bucketed weeks to MONDAY while every other weekly series in the app
  buckets to SUNDAY**, despite the comment directly above it saying "Sunday-based". Worse
  than an off-by-one: for a Sunday the old expression returned the *previous* Monday, six
  days earlier — a different week entirely. Verified across a full week of dates; all
  seven disagreed with `GetTrendData` / `GetHoursData` / `GetFinancialData` /
  `GetYoYTrendData`. Any client-side comparison of a rate week against a trend week was
  joining the wrong bucket
- **#92 also: this endpoint never pinned `DATEFIRST`.** It opens a raw `pyodbc.connect`
  rather than going through `data_source._pin_datefirst`, so `DATEPART(WEEKDAY)` depended
  on the server's language default. `SET DATEFIRST 7` now, as every other MSP endpoint
  does
- **#49, #65 -- free-text columns multiplied before being cast.** `clientBillRate` and
  `hoursPerDay` are free text in the Bullhorn views;
  `TRY_CAST(p.hoursPerDay * 5 AS DECIMAL)` casts the *product*, so the multiply runs
  first and one unparseable value aborts the whole Bullhorn branch —
  `GetFinancialData` had no `TRY_CAST` at all. Each column is cast before the arithmetic
  now, in `GetFinancialData`, `GetTrendData` and `GetHoursData`.
  **Latent, not active:** 0 bad values across 52,026 live placements, so this is
  hardening against a silent partial book rather than a figure that was wrong

### 2.47.0 - Three number-correctness fixes, verified against the source data (GH #64, #47, #48)

- **#64 -- a soft-deleted placement counted as a fill.** The placement-existence apply in
  `GetClosed` had no `p.isDeleted = 0`, unlike every other `View_Placement` query in the
  repo, so a deleted placement made a resolved order report FILLED and supplied the
  `first_placed` that days-to-close is anchored on. Measured on the live mirror over 30
  days: 1,049 resolved orders, 368 counted FILLED, 365 with a live placement — **3 were
  filled only by a deleted row**
- **#47 -- Financials and Trend disagreed, and neither was right.** Over 13 months of
  Symplr orders:

  | bucket | rows | revenue |
  |---|---|---|
  | worked (`hours > 0`) | 287,625 | $85,001,816 |
  | never ran (`hours = 0`) | 528,035 | **$141,958 phantom** |
  | credit/reversal (`hours < 0`) | 1,351 | **−$400,541 real** |

  Financials counted all three, so it carried the phantom revenue. Trend gated on
  `> 0`, so it discarded $400k of genuine credits. The two tabs differed by $258k. Both
  now use `<> 0`, which drops the orders that never ran and keeps the reversals — more
  correct than either was, not merely consistent. The issue proposed adopting Trend's
  `> 0`, which would have silently overstated revenue by $400k
- **#48 -- half the Symplr book reported headcount 0.** `COUNT(DISTINCT lt.tempid)` is
  null for per-shift orders with no `lt_order` row: 414,452 rows carrying $43.8M. Those
  groups showed real billings beside a headcount of zero, so any $/head figure was wrong
  or divided by zero. Falling back to `o.filledby` turns 149,309 orderless worked rows
  from headcount **0** into **2,536** distinct workers

### 2.46.0 - Meeting attribution, and the allowlist stops wiping itself (GH #88, #68, #41)

- **#88 -- meetings stay team-wide; what was missing was the author of an edit.**
  Confirmed as the intended model: meetings are between the whole team, and
  division/department scoping is a later concern. So the control is attribution, as it
  is for settings. `created_by` was set on insert only, so an edit left `updated_at`
  with nobody against it; there is now an `updated_by` column (additive migration, same
  idiom as the others in that file), set on both branches of the MERGE, and each save
  joins the shared `impactmgr.changes` timeline
- **#68 -- a POST with no `allowlist` key deleted every forced client.** The handler
  replaces the whole table, and an absent key defaulted to `[]`, so `{}` wiped the
  configuration for both sources and committed. Those ids drive scope resolution across
  every non-MSP endpoint, so the accounts simply vanished from the dashboard. An absent
  key is now a 400; send `[]` explicitly to clear. A non-numeric `client_id` is
  **rejected** rather than silently dropped, for the same reason -- a payload whose ids
  were all malformed used to clear the table and report success
- **#41 -- closed as accepted.** Browser-side redaction is the agreed model

### 2.45.0 - Record who changed settings (GH #52, #81)

Settings are open by design — anyone who can sign in may edit the client allowlist, the
system mappings and the PM mappings. So the control is **attribution, not
authorisation**: every config write now lands in `impactmgr.changes` against the
signed-in principal, giving one queryable timeline of who changed what and when.

- **`shared_code/audit.py`** holds the one implementation. It is the mirror
  `WorkspaceState` has used for a while, factored out so the config endpoints record
  changes the same way instead of each inventing its own
- **Newly attributed:** `GetSystemMappings`, `GetPMMappings`, `SaveHistory`. The audit
  row is written **before** the delete in the replace-all handlers, so a failure
  part-way still leaves the attempt attributed
- **`GetClientAllowlist`** already stored `added_by` per row; it now also appears on the
  shared timeline, and its `_user_from_req` prefers the verified principal email so this
  endpoint attributes the same way the others do
- **Fixes the other half of #69.** `WorkspaceState`'s local copy built the row id as
  `scope:entity:timestamp` with no length cap against an `NVARCHAR(100)` column, so a
  long entity id overflowed and — the audit write being best-effort — the row was
  dropped without a trace. The shared helper trims the entity id and keeps the timestamp
  that makes it unique
- Audited every endpoint that writes: all nine now attribute. (`GetPositions` matched the
  first scan on the comment `# Update counts`, not SQL — it is read-only)

### 2.44.1 - The #46 fix was correct and far too slow

A regression I shipped in 2.43.2 and did not catch, because I verified the fix's
**correctness** and never its **cost**.

- The new effective-end expression was two correlated subqueries evaluated per work
  order: **29.8s** on its own. `trend-data` returned a **500** on its first cold rebuild
  — the 45s gateway. It looked fine immediately after the deploy only because the cache
  was still serving a payload built by the old code; the shape bump in 2.43.3 forced the
  rebuild and exposed it
- Pre-aggregated into `#vndly_wo_spend` / `#vndly_name_spend` once per request and
  joined: **1.6s**, with identical coverage (410 of 1,354 terminal work orders) and the
  same 44 corrected end dates. The rendered query was executed against the warehouse
  before shipping this time, not just reasoned about
- This is the third time in this codebase that a correlated construct has had to be
  materialised. SQL Server re-runs a correlated subquery and inlines a CTE, so any set
  consulted once per row needs a temp table — the same fix the extensions endpoint
  needed twice in 2.41.1 and 2.41.2

### 2.44.0 - Security batch from the issue backlog (GH #16, #23, #27, #42, #87)

- **#27, #87 -- audit attribution came from the request body.** `SaveChange` and
  `ReviewedContractsRows` stored whatever `user` the caller sent. On the browser side
  that value is `getCurrentUser()`, which can be a name typed into a `prompt()` and kept
  in `localStorage`, or the literal string `Anonymous` -- so the change log was
  self-asserted and any caller could attribute a change to anyone. Both now use
  `current_user_email(req)`, which re-reads the auth header. `Meetings` already did this;
  `GetClientAllowlist` reads the SWA principal headers
- **#16 -- the privacy-mode password fell back to the literal `'2026'`** when
  `PRIVACY_PASSWORD` was unset, so a misconfigured deploy would ship a guessable code
  that un-redacts competitor and affiliate worker names. It is set on all four
  environments, so nothing operational sat behind that default. Now fails closed,
  compares with `hmac.compare_digest` (a plain `==` leaks match length through timing on
  what is otherwise an unthrottled oracle), logs failed attempts with the caller, and no
  longer echoes exception text. Real throttling needs shared state these stateless
  functions do not have -- noted on the issue rather than pretended
- **#42 -- `SearchUsers` interpolated the query into a Graph `$search` term.** The term
  is quoted, so `a" OR "mail:ceo` closed the quote and ran an arbitrary directory search
  under the app's `User.Read.All` permission. Graph has no escape for a quote inside a
  search term, so quotes, backslashes, parentheses and the boolean operators are stripped
  instead. `o'brien` still searches correctly
- **#23 -- toast messages were interpolated into `innerHTML`.** Callers pass
  server-derived strings in (`"Could not save: " + e.message`), making every toast a sink
  for whatever text an API returned. The message is set with `textContent` now

### 2.43.3 - Bump the cache shape for #86's new field

- `financial-data` gained an `errors` field in 2.43.2 and `PAYLOAD_SHAPE` was not
  bumped, so MSP kept serving the pre-change payload from cache and the field was
  absent on that book — the precise failure the shape version exists to prevent.
  Caught by checking the live response rather than assuming the deploy was enough

### 2.43.2 - Triage of the three remaining high-severity issues (GH #46, #63, #86)

Verified each against the live data before changing any figure. One was a false
positive, two were real.

- **#63 is a false positive — and the code's own comment caused it.** The report said
  the Symplr void-reason map never matches, so competitive losses count as zero. The
  live book disagrees: `Filled by Competition` maps to `Lost to competitor` on every
  row, Symplr contributes 44 of 58 unfilled with a reason (75.9% coverage), and nothing
  falls through unmapped. The map keys were right; the **inventory comment above them**
  abbreviated the values to `Internal Staff` / `Competition`, which is what the scan
  read. Comment corrected to the real vocabulary
- **#86 is real.** Both MSP source blocks caught, logged and continued without recording
  anything, and the non-MSP path collected an `errors` list and then dropped it from the
  payload. One failed source rendered the tab with half the billings at HTTP 200 and no
  indication — a wrong money figure that looks authoritative. Both paths report their
  errors now, a partial result is never cached, and the Financials tab shows the same
  amber partial-load banner the Trend tab already uses
- **#46 is real, but its suggested fix would have made it worse.** The spend subquery
  matched contractor first+last name with no work-order or date correlation, so a
  worker's latest billing week was applied to every terminal work order they ever had.
  Correlating strictly by work order — what the issue proposes — corrects 12 of 1,354
  but strips the end date from **177** others, which this query then reads as "still
  running". The fix is work-order correlation **with a bounded same-name fallback**
  (limited to that work order's own window, 14-day grace). Measured: coverage 410 vs 409
  for name-only, and **44 overstated ends corrected, averaging 67 days**. Applied to
  both the Trend and prior-year endpoints

### 2.43.1 - Robustness batch from the issue backlog (10 issues, no behaviour change)

Fixes that only affect failure paths -- crashes on bad input, leaked connections and
leaked error detail. Nothing here moves a number on a working request.

- **#30, #29 (SaveChange)** -- `id`/`timestamp`/`jobId`/`type` were indexed directly, so
  a body missing any of them, a non-object body, or invalid JSON raised
  KeyError/ValueError and surfaced as a 500 carrying the raw driver message. Validated
  up front with a 400 naming the missing fields; the exception text is logged, not
  returned
- **#69 (SaveChange)** -- `changes.id` is `NVARCHAR(100)`, and a longer id was a
  truncation error rather than a saved row. Rejected with a 400
- **#67 (Meetings)** -- `limit` was clamped only at the top, so `?limit=-5` produced
  `SELECT TOP -5` and a 500. Clamped at both ends
- **#43 (ReviewedContractsRows)** -- `row_key` is `NVARCHAR(500)`; a longer key hit
  "String or binary data would be truncated" as a 500. The key is concatenated from
  worker/facility/date fields, so a long facility name was enough. Now a 400
- **#89 (GetOnboarding)** -- the movement-history line carries a free-text user name, so
  a Bullhorn user whose name contains a newline shifted every field and
  `date.fromisoformat` raised, **taking down the whole endpoint for every seat**. An
  unreadable line is skipped
- **#35, #95 (GetFinancialData, GetPerDiemData)** -- the month regex checked shape only,
  accepting `2025-13` and `9999-99`, which failed conversion inside SQL Server where the
  handler swallowed it and returned 200 with nothing. The UI said "no activity" for what
  was a bad request. Month is now `01`-`12`
- **#45 (GetYoYTrendData, GetPendingData)** -- `Access-Control-Allow-Origin: *` on
  authenticated data endpoints, inconsistent with every other endpoint. Removed
- **#53, #40 (GetClientAllowlist)** -- the POST deletes every row before re-inserting and
  had no rollback and no close on the failure path. pyodbc defaults to
  `autocommit=False` so the transaction *was* rolled back -- but only whenever the leaked
  connection happened to be collected. Explicit rollback, and closed in a `finally`
- **#72, #29 (GetContractsComparison)** -- `pos_conn` was closed only on the happy path,
  so any raise after it opened leaked it. Closed in a `finally`, and the error response
  no longer returns raw exception text

### 2.43.0 - Two security findings from the issue backlog (GH #26, #78)

- **#26 (high) -- the domain allowlist could be bypassed through the display-name
  claim, and this was live on `main`.** `EMAIL_CLAIM_TYPES` included the
  `.../identity/claims/name` claim and `_extract_email` returned the first candidate
  containing an `@`. The provider accepts any Microsoft account and the holder of a
  personal one sets their own display name, so an account named
  `someone@ghrhealthcare.com` passed the gate and reached every `/api/*` endpoint
  whenever `userDetails` itself carried no `@`. Display name is user-controlled text,
  not an identity. The claim is gone and explicit claims are now checked **before**
  `userDetails`. Verified against both attack shapes and three legitimate sign-in
  shapes, including the email-only-in-userDetails case
- **#78 (high) -- partly stale, partly real.** The report said there is no HTML escaping
  anywhere; there are two helpers (`View.esc`, 127 uses, and `_escapeAttr`) and the
  reproduction it gives, the Non-MSP client allowlist, is fully escaped. But the
  **system-mappings table was not**: `system_name` went into `innerHTML` as a text node
  and into a `value=""` attribute with no escaping at all, and `incumbent` escaped only
  the double quote. Operator-entered through the mappings admin endpoint, so it was
  stored XSS for anyone opening Settings. All three now go through `_escapeAttr`

### 2.42.2 - "Comparable Median 0 days" was measuring data entry

The Closed detail read **"This Job 56 days vs Comparable Median 0 days"** across 20
peers -- a comparison that tells a reviewer nothing.

- `days_to_close` is `DATEDIFF(HOUR, job added, first placement) / 24`, so a record
  keyed in retroactively -- job order and placement entered together -- scores **0**.
  That is data-entry timing, not recruiting speed
- Measured on the live non-MSP book: of 881 closed rows only 248 carry a number at all,
  and **99 of those (40%) are exactly 0**, with another 38 under a day. The median came
  out 0.38 and rendered as "0 days". Excluding sub-day entries gives **7 days** against
  the same peers
- The comparable set now requires at least a full day. The row's **own** days-to-close
  is untouched -- the filter governs only what it is measured against -- and the
  "Avg Days to Close" KPI uses the same rule, since it was being pulled toward zero by
  the same rows

### 2.42.1 - Settle the rate-intel scoping question

`GetRateIntel` pools every Bullhorn job order as a peer, MSP included. That has been
flagged twice as a possible correctness problem; it is not one, and the evidence is now
in the query.

- MSP is **2,501 of 43,011 peer rows (5.8%)** over the 90-day window
- The global gap -- MSP $74.65 against non-MSP $90.77 -- is a **profession-mix effect,
  not a rate gap**. Ranking happens within profession + specialty + category, and inside
  the same bucket MSP is mostly *higher*: RN/ER/Travel $91.94 vs $86.51, RN/OR/Travel
  $100.90 vs $93.21, RRT/Hospital/Travel $91.14 vs $84.04
- Scoping it out would move ranks by 1-3% of each bucket, in the direction of making
  seats look better paid, and would gut the thin Local/PRN pools where MSP supplies most
  of the comparables -- RN/Med/Surg Tele/Local is 92 MSP rows against 23 non-MSP, an 80%
  reduction. Still above `minPeers` (3), but far noisier
- **Decision: leave it pooled.** Recorded in the query comment so it is not re-opened

### 2.42.0 - Cache the last three uncached endpoints

- **`stats-data`** was the slowest endpoint on either book -- **6.9s warm on non-MSP**,
  1.9MB -- and uncached. It takes no request parameters, so the whole response caches
  with no default-window guard. The non-MSP branch returns its own response, so it
  warms the cache explicitly: a read in front of both books with a write on only one is
  how the extensions cache failed silently in 2.38.1
- **`rate-intel`** is the biggest payload in the app at 4.8MB, 2.7s on MSP and 4.3s on
  non-MSP, also parameterless and now cached
- **`financial-data` had no caching on MSP at all.** The cache block lives inside
  `_non_msp_financial()`, so that book rebuilt on every request (3.2s warm) while the
  endpoint appeared to have caching. The same oversight the trend endpoint had in
  2.39.0. The default-range guard is hoisted so both books share it

### 2.41.3 - Modal pass after the dropdown-to-popup switch

Walked every modal on both apps at 1440, 1280 and 1100px.

- **The body gave no sign it scrolled.** The modal caps at 92vh, so on a laptop the
  detail is often taller than the box and the last row was sliced mid-label -- it read
  as a rendering fault rather than as "there is more below". A pure-CSS scroll cue now
  shows a soft shadow at the top only when content is above and at the bottom only when
  content is below: the `local` gradients travel with the content and mask the shadow at
  each end, the `scroll` radials stay put. No JS, no scroll listener
- **Closed and Pending stacked to one column across the whole 1024-1280 band** --
  ordinary laptop width -- because their grids broke at `xl`. The modal is `max-w-1200`
  and still ~976px wide at a 1024px viewport, so there is room for two and three panels
  from `lg`. Lowering the breakpoint turns a modal that scrolled into one that fits
- The list modal's own grids are left at `xl`: that renderer is shared with production
  MSP's legacy in-place expand, and this is not a production-MSP change

### 2.41.2 - The last of the per-row work in the extensions build

- **The five B4 extension signals were correlated `EXISTS`**, one set per row: **10.0s
  for 444 rows**, against **1.6s** pre-aggregated into `#b4_ext` / `#b4_chain` for the
  same answers (15 offer-pending, 72 accepted, 9 declined, 87 awarded, 38 chain-pending)
- With the crosswalk fix in 2.41.1 this is the third correlated construct replaced in
  this endpoint. The pattern is consistent: SQL Server re-runs a correlated subquery and
  inlines a CTE, so any set consulted once per row has to be materialised

### 2.41.1 - The extensions build was correlated per row

The cold build was 35-41s and had twice returned a 500 at the 45s gateway. It scaled
at roughly **5.5s fixed + 62ms per row** -- the signature of per-row work, not a slow
base query.

- **The B4 Bullhorn crosswalk was a correlated `OUTER APPLY`**, re-evaluated once per
  row: **13.1s for 444 rows**, against **2.7s** pre-resolved into a temp table for
  identical output (the same 292 links). The RTO lookup had the same shape and is
  resolved the same way
- **My earlier timings missed this** because they used `SELECT COUNT(*)`, which lets
  the optimiser skip the SELECT list entirely. The cost only appears when the columns
  are actually produced -- 3.8s counting, 13.1s returning
- Same fix the VNDLY link needed in 2.41.0, for the same reason: SQL Server inlines a
  CTE and re-runs a correlated subquery, so a set that is used once per row has to be
  materialised

### 2.41.0 - VNDLY seats reach Bullhorn

Every VNDLY seat on the Extensions tab read "No Bullhorn record joins to this seat" --
0 of 90 linked, no recruiter, no System Match. The bridge was there all along:
**`BH_PLACEMENT_RAW.customText56` holds the VNDLY work order** (`WO00270`), matching
`STAGING_VNDLY_WORKORDERS.[Work Order Id]`.

- **It cannot be joined on its own.** `[Work Order Id]` restarts per tenant: of 973
  ids, 376 are unique to one tenant but **370 appear in two and 227 in three**, so a
  bare join would have sent the majority of seats to the wrong health system. The
  `WOSystemKey` prefix names the tenant (CUH / IHN / RH / RUMC) and the B4 crosswalk
  maps a Bullhorn client to the same health systems, so the real key is the work order
  id *plus that tenant's clients*. The two systems spell the names differently --
  VNDLY's "Redeemer Health" and "RUMC" against the crosswalk's "Holy Redeemer Hospital"
  and "Richmond University Medical Center" -- so the mapping is explicit
- **Result:** 106 of 298 work orders in the window reach a placement, and 97 of those
  resolve to a single candidate. The extra placements on the other 9 are a seat that
  changed hands; the clinician-name comparison picks the right one, and where even that
  disagrees the System Match panel reports the mismatch rather than hiding it. Those
  seats now carry a recruiter and a real end-date comparison
- **Built as a temp table, not a CTE.** SQL Server inlines a CTE, so as one the link
  set was re-evaluated once per work order and the query took **103 seconds**. Built
  once into `#bh_wo` it is **3.4s** for the same 106 links
- The old comment asserting no link exists is kept and corrected: what it tested was
  right, its conclusion was not. Every attempt had looked for a VNDLY-side key pointing
  at Bullhorn, never a custom field on the Bullhorn side carrying a VNDLY id

### 2.40.1 - The cache was caching a URL nobody asks for

- **MSP requests `extensions-data` and `onboarding-data` with `?includeAffiliate=1`**,
  and the cache only stored the bare default -- so on MSP the cache was never read.
  Measured on the live prototype: the uncalled default served in **265ms** while the
  URL the app actually calls took **41s, then 35s**, close enough to the 45s gateway
  timeout to fail outright. The 21s-to-250ms win reported in 2.38.1/2.39.0 was real for
  the default URL and did not reach the MSP app
- The parameter is part of the key now rather than a reason to refuse caching, so both
  variants are cached. `cache_key` takes a `variant`

### 2.40.0 - RTO from both VMS books; the source can overtake a hand-set status

- **RTO now reaches the app.** It read 0 on all 404 seats, and the "Gather RTO"
  checkpoint said "not in data" for everyone. The Bullhorn path cannot supply it --
  `BH_PLACEMENT_RAW` holds Blocks 1-5 and 10, so `customTextBlock9` never reaches the
  warehouse -- but both VMS books record it themselves. **B4** keeps it on the contract
  submission (18 of the 314 GHR seats in the live window); **VNDLY** keeps it on the
  contractor, reached through the work-order cross-reference (58 of 296). Shown as
  written -- "Approved RTO: 6/1-6/5" -- rather than parsed into dates it may not be
- **A hand-set designation no longer outranks the source forever.** "Awaiting Client
  Approval" exists because no VMS can see that the PMO has the extension in front of
  the client; it fills a gap, it does not overrule the VMS. Letting it win
  unconditionally meant a seat set by hand kept that status permanently -- VNDLY could
  report the offer out to the vendor, or B4 record the extension accepted, and the tab
  would still read "Awaiting Client" because someone typed it once. The two are now
  compared by position in the pipeline: if the source has moved past the hand-set
  value, the source wins and the record says so. The selection is kept, not discarded,
  and takes effect again if the source falls back behind it
- **Cache keys carry a payload-shape version.** A deploy does not clear the cache
  table, so a shape change was invisible for up to 26 hours -- the endpoint kept
  serving the old payload built by the old code, which is exactly what adding
  `recorded_rto` would have done

### 2.39.2 - Two defects the modal pass turned up

- **Lever labels rendered straight across their card border.** Two lever panels sit
  side by side and each splits into two cards, so a card is ~140px and its label box
  ~86px after the checkbox and padding -- "Requirements" needs ~100px at that size.
  The labels break now instead of overflowing
- **Onboarding's movement panel contradicted the tiles beside it.** With an empty moves
  list it showed, in green, "No start-date movement recorded" -- while "Total Moves 3"
  and "Net Slip +28 days" sat immediately above. An empty list does not mean the start
  never moved: the source can count moves without itemising them. It now says so, and
  only claims no movement when the count agrees

### 2.39.1 - Modal layout: remove the dead space

Walked every modal on both apps and measured where the room was going.

- **Extensions was the worst.** The editable form sat inside the right column, which
  made that column ~690px of content against ~300px on the left: the left ended with
  roughly **370px of dead white space** while the right overflowed and scrolled (126%
  of the available height). The columns are now 5/7 rather than 50/50, and the form is
  lifted out to full width below both -- so the form gets the whole 1200px, the right
  column loses the height it was lending it, and neither column scrolls. Client
  Decision, Extension Designation and Shared Notes now sit three across instead of
  wrapping in a half-width column
- **The List modal's Submission Log reserved a fixed 224px** (`h-56`) whether it held
  one submission or twenty, so a job with a single submission showed ~180px of empty
  box. It sizes to its content now and only scrolls past the cap
- **Pending's middle panel promised "who else is submitted" and showed two counts.**
  That left it short against the panels either side -- the dead space and the
  unanswered question were the same problem. It now lists the peers by name with their
  agency, from data already loaded for the counts

### 2.39.0 - Cache the three remaining slow endpoints

Warm measurements before this change: onboarding-data **15.8s** (MSP), trend-data
**14.0s** (MSP), closed-data **10.3s** (non-MSP).

- **onboarding-data and closed-data** now read and warm the endpoint cache, default
  window only. Both take parameters that change the result -- lookback/lookahead and
  from/to/days -- so a key on the route alone would serve the default to a caller who
  asked for something else
- **trend-data's MSP branch** is cached too. It takes no parameters at all, so unlike
  the others there is no default window to guard. The existing cache block sat inside
  `_non_msp_trend()`, which only ever runs on the non-MSP path -- so MSP had never been
  cached despite the endpoint appearing to have caching
- **Caveat:** the scheduler (`RefreshCache`) still only knows `trend-data` and
  `financial-data` on non-MSP. The new keys are warmed by the first live request after
  expiry, so that one caller still pays the full build

### 2.38.1 - The extensions cache was writing and never serving

- `read_cache` stamped `cachedAt` onto the payload unconditionally. `extensions-data`
  returns a bare JSON **array**, so that raised `TypeError` on every read -- and the
  fail-open `except` swallowed it and computed live. The cache wrote correctly, 845KB,
  and reported itself healthy in `cache_status`; the only visible symptom was that a
  cached endpoint stayed exactly as slow as an uncached one. Only an object can carry
  the stamp, so only an object gets it now

### 2.38.0 - Extensions endpoint: parallel feeds and a cache

- **extensions-data was returning a 500.** Measured warm at 21-23s, and the first
  request after a deploy took **43.6s** -- past the 45s SWA gateway timeout, so the tab
  got an error rather than a slow answer. The B4 and VNDLY branches were running
  sequentially on one cursor despite being independent reads
- **The two branches now run concurrently**, each on its own connection, as
  `GetTrendData` and `GetFinancialData` already do. A pyodbc connection cannot be
  shared across threads, and sharing the single cursor was the serialisation being
  fixed
- **Cached through `shared_code/endpoint_cache`**, default window only. `horizon` and
  `includeAffiliate` both change the result, so a key on the route alone would hand a
  45-day default to a caller who asked for 90 -- the exact bug the financial-data cache
  shipped with
- **Header: the book subtitle moved back under the title.** The density pass had put it
  on the title line, where it sat beside the version number and read as part of it
- **"Symplr Education" is now just "Symplr".** Non-Acute comes through the same source,
  so naming only Education understated the book

### 2.37.1 - Scope the B4 extension report to GHR

- The Network Activity Report is registry-level and lists every vendor in the program
  -- AppleOne, LanceSoft, BAYADA, Triage and the rest -- which is not GHR's book. The
  three designation lookups now require a GHR agency. No row changes today: all 96
  extensions on GHR seats in the window are already GHR's own, because the tab is fed
  from Bullhorn and an affiliate's extension never reaches it. It is there so an
  affiliate picking up a GHR seat's extension can never read as GHR extending it

### 2.37.0 - Extension designations, from VNDLY and B4

Aligns the Extensions tab to the Extension Criteria spec. Nothing was removed: the
45-day Bullhorn window, the Client Decision lever and the workflow grouping all stay
as they were, and the designation is added alongside them.

- **A designation per seat, derived from the VMS rather than typed in.** Where the
  extension has actually got to, on its own axis from the Client Decision. The decision
  is GHR's position on whether to extend; the designation is how far the request has
  got with the client and the vendor. A seat can be "Approved to Offer" internally and
  still sit at "Pending Extension Review" externally, and that gap is the thing worth
  seeing. One function, `Utils.extensionStage`, owns it for every surface
- **Both VMS books feed it.** VNDLY: the latest `Date Extension` modification on the
  work order, from `STAGING_VNDLY_WORKODER_MODIFICATIONS` -- the "Pending Modifications
  - Awaiting Vendor" report, read from staging instead of exported by hand. `Submitted`
  means awaiting vendor, `Accepted` means done. B4: `dhc.B4HealthExtensionsOrder`, the
  Network Activity Report, with the contract parent/child chain as a second,
  independent read
- **The B4 report's AssignmentID is the child contract, not the seat that is ending.**
  Joined straight to `Contract_ID` it matched 205 of 205 rows and was wrong every time:
  none of the 29 pending offers landed on an order ending inside 45 days, because the
  child carries the *extended* end date. The hop through `Parent_Contract_ID` puts the
  designation on the seat a reviewer is looking at. The check that it is right: the
  report finds 87 accepted in the window, and the contract chain independently finds
  the same 87
- **"Awaiting Client Approval" is the manual lever the spec asks for.** It has no VMS
  signal -- it means the PMO has put the extension in front of the client -- so it is
  set by hand and applied over whatever is derived. A dot on the badge marks any
  designation set by hand, so a stale manual value cannot pass for live data
- **A fifth designation the spec did not list: Extension Declined.** The B4 report
  carries it on 10 live seats. Folding those into "Pending Extension Review" would have
  put a settled decision back on the chase list
- **Group by Designation or Workflow**, defaulting to Designation. The original
  workflow grouping is the only view of what the team has done rather than what the
  systems report, so it stays one click away

Live counts at release: 87 accepted, 28 awaiting vendor and 10 declined from the B4
report; 3 awaiting vendor and 88 accepted from VNDLY.

### 2.36.6 - One day is "1 day"

- Four surfaces printed a raw day count and read "1 days": the Pending stage age, the
  Closed days-to-close tile, the comparable-median tile and the comparison bars.
  `Utils.days(n)` now owns rounding and the plural, and all four call it, so they
  cannot drift apart

### 2.36.5 - Pending opens the modal; days-to-close reads as days

- **Pending was the last tab still expanding in place.** Every other tab on the
  redesigned UI opens the row-detail modal, so a reviewer working down the list got
  a different interaction on this one surface. `pendingDetailBody` is now split out
  of its `<tr>` wrapper -- the same treatment `extensionDetailBody` and
  `onboardingDetailBody` already had -- and the modal renders it. Pending rows have
  no id of their own, so the record is matched on the composite `__key` the table
  builds. Gated on `impactUi()` at the same switch point as `TOGGLE_ROW`, so
  production MSP keeps its legacy in-place expand and is provably untouched
- **"Days to Close 55.88"** rendered the raw fraction with no unit, directly beside a
  tile reading "0 days". Rounded to whole days and labelled, in the tile, the
  comparison bars and the Closed table cell
- **"Comparable Median" was clipped to "Comparable Medi…"** in the bar chart -- the
  label column was 104px. Widened to 132px

### 2.36.4 - Restore the 778 Closed rows 2.36.3 hid on non-MSP

- **Making Closed expandable on non-MSP silently dropped 88% of the table.** One flag
  was doing two unrelated jobs: gating whether a row opens, *and* choosing the group
  vocabulary. Turning it on in 2.36.3 also switched non-MSP to the MSP group names
  (`GHR WON` / `AFFILIATE WON` / `MISSED`), but non-MSP rows carry
  `FILLED` / `UNFILLED` / `CANCELED` -- so 778 of 883 rows matched no group and
  vanished from the page. The API was returning all 883 the whole time. The group
  list is a property of the book and is now keyed on the data source; expandability
  is independent of it
- **How it surfaced:** the re-audit counted 105 rendered rows against 883 from the
  endpoint, and 105 was *exactly* the CANCELED count -- the signature of a group
  filter, not of a lost query

### 2.36.3 - UI pass across both prototypes
Found by walking every tab on both prototype instances and opening a row modal on each.

- **Closed rows now open on non-MSP too.** They were hard-gated to MSP because the
  detail's second section is vendor share and bid activity, which do not exist on the
  non-MSP book. Rather than withhold the whole record, that section is dropped on
  non-MSP and section one -- revenue, bill rate, rate rank, days to close against
  comparables, outcome, owner -- takes the full width. It is just as meaningful on
  direct business
- **Billings headline used the month in progress.** On 1 October it read
  **"Total Billings (Oct-26) $0 ▼100%"** -- a day-old month compared against a full
  September. It now uses the last *complete* month and says so. The tables below still
  show every month including the partial one; only the summary has to compare like with
  like
- **The filter row crushed instead of wrapping.** Eleven `flex-1` controls with no
  minimum width squeezed until `Min`/`Max` rendered as "Mi"/"Ma". A 132px floor makes
  the row wrap, which is what `flex-wrap` was there for
- **"Start IMPACT Meeting" wrapped to two lines** in the cramped header; now
  `whitespace-nowrap shrink-0`
- **The system/facility cell was five stacked lines**, making it the tallest cell in
  every table and holding Open Jobs to four rows on a 900px screen. The relationship
  badge moves onto the system line, where it reads as an attribute of the system

### 2.36.0 - Density pass, money formatting, and dropdown filters
From the 2026-09-30 feedback list.

- **One money formatter, and one of the five was wrong.** The Closed KPI formatter had
  no millions branch at all, so annualised revenue rendered as **`+$112428.3k`** -- a
  number that reads as a hundred thousand when it means a hundred *million*. Reported
  from the call as "9348K instead of 9.35M". All seven call sites now share
  `Utils.fmtMoney`
- **Millions start at 950,000, not 1,000,000**, per the same feedback: a figure a
  whisker under a million reading as `$950K` understates it to anyone scanning. 950k-999k
  shows as `$1.00M`; 948k still shows as `$948K`
- **Whitespace and headers.** Title `text-3xl` -> `text-xl`, logo 9x9 -> 7x7, subtitle off
  its own row; KPI cards `min-h-100px` -> `72px` with tighter padding and type. **Chrome
  above the list: 568px -> 488px, rows visible 5 -> 6** at 1440x900 (1 -> 6 across the
  session)
- **"Extra text" moved to a hover.** Each stage tab's explanatory sentence is now an info
  icon beside the title -- still readable, no longer costing a line on every render
- **Per-column filters become dropdowns** where the vocabulary is bounded: System Match,
  Decision and Urgency join AM and Recruiter. This needed a `filterValue` hook, because
  System Match *sorts* on a numeric severity and Urgency on a raw lowercase band -- a
  dropdown built from those lists sort keys ("3", "critical") rather than the words in
  the column
- **GHR Vendor Performance hidden** on Per Diem (`SHOW_GHR_VENDOR_CHART = false`). Gated,
  not deleted: it is 160 lines of working chart with a regression overlay mirroring the
  recruiting team's tracker, and "or make it a column" suggests it may return

Not in this release: the **Extensions workflow** item (reading Bullhorn notes for
candidate RTO, an in-app extension process, syncing to VNDLY). That is a feature, not a
tweak, and is waiting on the call notes.

### 2.35.0 - Row details open in a modal, with prev/next and an explicit save
- **The inline row dropdown becomes a modal** on Extensions, Onboarding, Closed and Open
  Jobs. The dropdown pushed every row below it down the page and squeezed the detail into
  the width of a table already carrying twelve columns. The modal gets the viewport, the
  list stays still underneath, and subtabs have somewhere to live
- **Prev / Next step through the list**, with an "N of M" counter. The order is read from
  the DOM rather than recomputed, so it follows the active sort, filters and grouping --
  Next means "the next one you can actually see". Arrow keys work; Esc closes
- **Save and Cancel are explicit.** Edits live in the inputs until saved, so closing a
  dirty record asks before discarding rather than losing the work silently
- **Autosave stands down while the modal is open.** This was a real conflict, not a
  theoretical one: a delegated `change` listener saved on every edit, so changing Client
  Decision wrote to the database *before* the user could press Cancel, and Cancel reverted
  nothing. Verified by tracing dispatches -- two `SAVE_EXTENSION` calls became one. The
  inline dropdown and production MSP still autosave, unchanged
- **One write path, not two.** The modal reuses each view's own save, which reads the same
  inputs wherever they are rendered, so the two presentations cannot drift
- The detail bodies were split out of their `<tr>` wrappers (`extensionDetailBody`,
  `onboardingDetailBody`) so the same markup serves both. `toggleStageRow` is untouched,
  so the dropdown still works and is one line away
- Closed and Open Jobs have no editable state, so their footer says the record is
  read-only instead of offering a Save that would do nothing
- Production MSP keeps its in-place expand: the switch is inside the `TOGGLE_ROW` handler,
  after the `!impactUi()` early return, so it cannot reach that build

### 2.34.0 - Trend and Financials come off the request path
- **A payload cache in `impactmgr.endpoint_cache`, refreshed on a schedule**, following
  the pattern the sibling `ghr-salespulse` app already uses for its placement and
  contact-count caches. The request path becomes one `SELECT` instead of a 17-20s
  aggregate
- **Why those two.** Measured on the deployed non-MSP instance: `trend-data` 9.3s ->
  16.8s once the scope widened, `financial-data` 9.2s -> 19.9s and intermittently
  **HTTP 500** -- the "Couldn't load financial data" the divisions were seeing. Both are
  four-week / monthly aggregates that don't move within a day
- **The diagnosis came from salespulse's own notes**, which hit this wall first: *"with
  ~974 corps in scope SQL Server stops seeking on the index and scans instead: measured
  104 SECONDS cold. A per-page OUTER APPLY was not a fix either -- just a different bad
  plan."* That is our shape exactly; the trend query carries two OUTER APPLYs evaluated
  per placement row, and widening the scope pushed it over the same cliff
- `POST /api/cache/refresh` rebuilds; `GET` reports what is cached and how old. Auth is
  a signed-in user **or** `CACHE_REFRESH_API_KEY`, because a timer has no email domain
  to check -- the same split salespulse uses
- **Fail-open throughout.** A missing table, an unconfigured app DB or an unparseable
  row all mean "compute live", which is exactly today's behaviour. A cache older than
  26 hours is ignored, so a stopped scheduler degrades rather than serving stale numbers
- **A partial build never overwrites a good cache** -- if one source errors, the previous
  payload stands rather than a half-empty month being served as fact
- Only the **default** 13-month window is cached: `fromMonth`/`toMonth` produce a
  different answer, and a cache keyed on route alone would have handed the default range
  to someone who asked for one quarter

### 2.33.1 - Trend and Financials stop running their two sources one after the other
- **The two source branches now run concurrently.** Bullhorn and Symplr hit different
  databases on their own connections and share no state, so sequencing them only ever
  cost wall-clock. Measured on the deployed non-MSP instance: `trend-data` **9.3s**,
  `financial-data` **9.2s** or an intermittent **HTTP 500** -- the "Couldn't load
  financial data" the divisions were seeing. The endpoint now costs the slower branch
  rather than the sum
- **The scope resolution behind the filter was dead work.** With the wide scope,
  `build_scope_filter` ignores the client list, but every endpoint still ran a discovery
  query *and opened a second connection to the app database* to build it. Skipped
  entirely now; nine endpoints stop paying for it
- **Verified the wide scope did not make the slow query slower** -- a fair concern, since
  it replaced a selective `IN (~340 ids)` with `NOT IN (47)`. Timed against the real
  query: 6.8s wide vs 7.8s narrow warm, so it is marginally *faster*
- **Honest about what is left.** The main Bullhorn trend query is still ~2.5s warm and
  far worse cold (17.8s on a cold plan, 6.5s warm, against a 4.2s connect baseline). It
  carries four LEFT JOINs and two OUTER APPLYs evaluated per placement row. That cold
  variance is what tips `financial-data` into a 500, and it is not fixed by this change

### 2.33.0 - The non-MSP book is every client except GHR's own MSP accounts
- **The book was scoped to clients with a placement running today.** Any account GHR is
  actively recruiting for but hasn't yet placed anyone at was excluded entirely -- so the
  app showed **1,336 of 6,298 open reqs** inside the same 45-day window it already applies
- Divisions that sell searches rather than fill seats were worst hit, because they rarely
  have an active placement to be discovered by. **Search showed 2 reqs out of 58**;
  Locums 30 of 254; Allied 249 of 1,569; Nursing 991 of 4,268
- **The 45-day cutoff was never the problem and stays.** 6,332 of the 6,350 reqs it lets
  through were modified in the last 30 days -- live work, not stale postings. Of the reqs
  over a year old, only 41 of 825 have been touched in a month, and those stay filtered
- **Headcount is unaffected by construction.** A client with no active placement
  contributes no heads, so Trend, Financials and headcount move **1,145 -> 1,157 (1.01x)**
  while Open Jobs moves 4.8x. Clients go 333 -> 1,274, which widens the System and
  Facility pickers
- Rendered as `NOT IN (47 MSP ids)` rather than `IN (~10k ids)`: the honest expression of
  the rule, and a far better query plan. One change in `build_scope_filter`, so all nine
  non-MSP endpoints inherit it. `WIDE_NON_MSP_SCOPE = False` restores the old behaviour
- Reconciled against analytics first: their 543 open Locums jobs / 1,048 openings is
  `Accepting Candidates`, company-wide, no date limit. Measured here as 545 / 1,043

### 2.32.7 - Per Diem crashed on open
- Ported from main 2.21.4. `ReferenceError: sysAssignments is not defined` took the whole
  Per Diem view down on open: the movement block read a variable declared inside the
  earlier `groups.forEach`, a different callback that closes well before the use. Now
  declared in the `activeGroups.forEach` scope that needs it

### 2.32.6 - P1 compression: Open Jobs pipeline is a funnel, full width
- **The funnel is now a funnel.** The Pipeline pane's bars were "GHR Share by Stage" --
  always full width, showing the GHR/affiliate split and hiding the shape, which is the
  one thing a funnel is for. Bar *length* is now reach against the widest stage, so it
  narrows; the fill inside keeps the split the old bars carried. Verified against **236
  real submissions** on the busiest open Bullhorn job: 236 → 1 → 1 → 0 → 0, narrowing
  monotonically
- **Full width, with candidate activity beneath it.** The pane was two half-width
  columns, where the bars were too short to read a shape off and the activity list was a
  narrow column
- **Every candidate, in its own scroller.** The list was capped at 12 with a line saying
  how many were withheld; it now renders all of them (236 in the check) in a 286px
  scroller over 11,563px of content, so the funnel stays in view while the list is read
- Horizontal overflow measured at 0px; no page errors

### 2.32.5 - P1 compression: Extensions shows five records instead of one
- Measured at 1440x900, the standard desktop the feedback's acceptance test names.
  **Before: 681px of chrome and 126px rows, fitting one record. After: 568px and
  65px rows, fitting five.** Every number below was measured, not estimated
- **Extension Search was a bordered panel with a heading, a description and stacked
  labels for three controls** -- 74px plus margins. Now one inline strip with the
  labels beside the controls, and Clear Filters is always visible once a filter is
  set, since a filtered table that looks empty is otherwise indistinguishable from
  an empty one
- **The aging legend wrapped to two lines** (94px for four chips) because it was
  sized `flex-1` against the tab bar and its labels repeated "Days Left", which the
  column header already says. Now `≤7 days / 8-14 days / ...` on one line, 38px
- **Row height 126px → 65px.** Next Action was the only cell driving it, measured at
  79px: the owner name wrapped and the sentence ran to three lines. Name truncates,
  sentence clamps to one line with the full text on hover. System Match's note
  clamps to two
- **Short dates** (`9/21/26`, not `2026-09-21`), title and subtitle on one line, and
  `py-2` → `py-1.5` on the row
- **Assignment finally readable.** It carried `max-w-0`, which forced it to the
  narrowest possible box, so every facility read "Lancaster G...". Widening it
  naively pushed the table 310px past its container -- a horizontal scroll the
  feedback explicitly rules out -- so the table now declares percentage widths and
  uses the fixed-layout path `stageShell` already had. **Horizontal overflow is 0px
  at both 1440 and 1680.** The column-filter row needed `min-w-0` in that mode: its
  `min-w-[4.5rem]` alone put the scrollbar back
- Onboarding and Closed are unaffected -- they stay auto-layout, verified at 0px
  overflow with no page errors

### 2.32.4 - Why a rate has no rank, in the row's own terms
- **Benchmark cohorts verified, not changed.** The concern was that Nursing, Allied and
  Non-Clinical were being blended. They are not: every ladder rung keys on `profession`,
  which is finer than service line, so an RN is only ranked against RNs. Measured over
  **182,810 rate rows in 814 peer buckets -- zero buckets mix service lines**, and every
  profession resolves to one. The original note described the older facility+specialty
  ladder
- **The real gap was the reason text, and it named the wrong cause.** Three distinct
  causes collapsed into one message:
  - `Number(r.bill_rate)` is NaN on an open job, whose rate is a display string on
    `billRate` -- so every open job claimed "No bill rate on this record" while showing
    one. Now reads through `Utils.rateNum`, as `rateRank` itself already did
  - A job with no credential can reach no ladder rung at all. Over 81,574 open Bullhorn
    jobs, **26,109 (32%) carry no category** -- the single biggest cause, and it was
    being reported as "fewer than 3 comparable job orders", a statement about the peer
    pool rather than about the job
  - Genuinely too few peers, the only case the old text actually fitted
- All 219 distinct credential values in the live book normalise, so the mapping is
  complete; the unrankable jobs are missing source data, and now say so

### 2.32.3 - An expired session says so, instead of looking like a broken endpoint
- Ported from main 2.21.2. A guard on `window.fetch` catches an `/api/` call that SWA
  redirected to the sign-in page and raises a session-expired error, instead of letting
  `JSON.parse` fail on the sign-in HTML with `Unexpected token '<'`

### 2.32.2 - MSP accounts the hand-kept list had missed
- Ported from main 2.21.1. 18 Bullhorn clients added to `MSP_CLIENT_IDS`, taking
  1,667 open jobs (Penn Lancaster General and the rest) off the non-MSP side
- Derived from `dbo.BH_PLACEMENT_RAW_TO_B4HealthOrder`, the warehouse's own link
  between a Bullhorn placement and a B4 order, rather than from client names

### 2.32.1 - Tab row stays put; hero cards stop claiming zero
- Ported from main 2.21.0. The aging legend is `display:none` on views with no
  bands, and with `justify-between` a lone child falls to the left -- the tab bar
  sat 446px further left on those tabs. Measured before and after: 446px, now 0
- Pending Offers and Post-Offer Declines say *Not recorded* instead of `0` where no
  stage or offer date exists, with the reason in the tooltip. `0` claims "no offers
  are outstanding", which is not the same statement as "we don't know"

### 2.31.0 - Closed Job Detail: three panels into two
- **Section 1 (Financial & Comparative)** and **Section 2 (Market Position & Bids)**,
  replacing three panels. The old Panel C repeated Panel A almost entirely -- Rate Rank,
  Days to Close, Comparable Median and Revenue each appeared twice under different
  sub-labels, which reads as two measures rather than one shown twice
- **One revenue figure, labelled by its sign.** `revenue` is signed at the source, so a
  record is captured *or* missed, never both. The three-panel layout could show
  "Revenue Captured $0" above "Revenue Outcome -$48,000" -- the same number stated twice
  and contradicting itself once
- **Bid activity on the record**: total, GHR and competitive bids, plus the winning
  supplier. Bid-to-fill has no source -- neither VMS records a first-bid timestamp, and
  Days to Close measures a different span -- so it says "Data Not Available" with the
  reason rather than reusing a number that would look right and be wrong
- **Owner appears on the record**, not only in a column the reader has scrolled past by
  the time the panel is open
- GHR Market Rank moved to Section 2. It is a market measure, and sitting it beside Rate
  Rank invited the two to be read as the same ranking
- Panel C's code kept as a comment rather than deleted
- Verified by rendering against live data -- real closed B4 orders with their real bid
  counts at accounts carrying a live vendor panel, plus rows at accounts without one.
  No label appears twice in the expanded record, across GHR Won / Affiliate Won / Missed
  and both market-share branches

### 2.30.1 - Due chip stays in one piece
- The derived due phrase broke across two lines in the narrow Next Action column
  ("7d" above "overdue"), reading as two separate values. Kept whole

### 2.30.0 - Derived action due dates, and one aging ladder behind all of them
- **Due dates are derived from Days Left, not entered.** A decision is due when the
  seat would otherwise enter the critical band, so the owner keeps the last 7 days
  to execute rather than to decide. Because the due date *is* Days Left offset, it
  cannot drift from the Days Left or Urgency columns beside it
- **Three aging ladders collapsed into one.** The legend bands and the API's urgency
  agreed (critical <=7, high <=14, medium <=21, low beyond), but `extensionUrgencyLabel`
  used <=14 High / <=30 Medium -- so a seat 25 days out was *low* on the server, in
  the legend's green band, and *Medium* in its own Urgency column. All of it now
  runs through `Utils.agingBand`
- The extension panel's Urgency tile reads the same call as the Urgency column
  instead of the raw server field, so a seat whose systems disagree no longer says
  High in the column and Medium in the panel
- **A compact task area on Extensions and Onboarding** -- decision, owner, due date
  and next action on one line, in the row and repeated at the head of the detail
  panel. No task needs a second panel opened to find out who owns it or when it is due
- **Onboarding gained an owner, a next action and a due date**, which it never had.
  Its deadline counts down to the start date rather than an end date, same offset and
  same bands: the last week before a start is for onboarding, not for discovering it
  slipped. The action comes from the API's own `next_action`, so the row and the panel
  can't disagree; the recorded AM still shows even when the ball has moved to the
  client or the recruiter
- Both Next Action / Owner columns now sort by when the action is due rather than
  alphabetically by owner. Records with no date to derive from sort last -- unknown,
  not urgent
- Verified in a browser against 70 live extension seats and 60 live upcoming starts
  sampled across the full 45-day horizon, so all four bands are exercised: every row's
  due phrase, Days Left and Urgency agree, and no row lost its AM

### 2.29.0 - Under Review in the funnel, and stages say when they aren't recorded
- **Under Review** added between Submitted and Interview. It's VNDLY's shortlist,
  and on that book it's where the volume actually sits: **611 submissions
  shortlisted, all dated**, against 20 of 1,356 with a recorded interview date.
  Without it those candidates showed as merely Submitted and the interview step
  looked near-empty -- reading as a broken pipeline rather than an unrecorded stage
- **Stages now declare when the source doesn't record them.** The cumulative count
  assumes stages are sequential -- hold an offer, so you must have interviewed. On
  VNDLY that's false: 474 offers against 19 interview dates. Counting it that way
  had the funnel assert 474 interviews from 19 records, which is a misleading zero
  pointed the other way
- Each stage now also counts *direct* evidence. Where a stage has none but a later
  one has volume, the row is marked "not captured" with the inference explained.
  Verified across four shapes: VNDLY flags Interview, Bullhorn flags Under Review
  (no shortlist concept), a complete funnel flags nothing, and a genuinely empty
  funnel isn't mislabelled as a gap
- Decline reasons needed no work -- `Hospital_Decline_Reason`, `Offer_Decline_Reason`
  and VNDLY's `Rejected`/`Withdrawal Reason` were already flowing into candidates

### 2.28.1 - Don't spend a card saying a measure doesn't apply here
- Bid Activity and Ended Early are now **omitted** on non-MSP rather than rendered
  as a dash. Non-MSP is direct business -- GHR is the only agency on the req -- so
  competitive bids don't exist there, and Bullhorn's order-level `reasonClosed`
  can't determine an early end. A card whose content is "not applicable" is the
  opposite of the stated principle of spending space on actionable numbers
- The distinction: **inapplicable** → omit the card; **applicable but not captured**
  → show it with a reason. "Never a misleading zero" governs the second, not the first
- `renderKpiCards` drops falsy entries, so any card can opt out of a book
- Result: MSP shows six cards, non-MSP four real ones, both on one row

### 2.28.0 - Bid activity, from submissions tables nothing was reading
- Dan's sixth Closed KPI is built. `dhc.B4Health_Contract_Submissions` and
  `dbo.STAGING_VNDLY_SUBMISSIONS` carry one row per agency submission with the
  vendor name, so total / GHR / competitive bids are all countable. Nothing in the
  app was reading either table
- Coverage: 2,534 of 2,764 closed B4 orders (92%) carry bids -- 7,737 of them, 4,280
  GHR. VNDLY 100%, 1,713 bids
- **Counted over distinct bid groups, never over rows.** A VNDLY job's bids repeat on
  every one of its work orders -- 1,498 work orders sit across just 404 Job Ids, up
  to 49 on one job -- so a row-wise sum inflated bids 7.5×, from 1,713 to 12,766.
  B4 is 1:1 and unaffected either way, which is exactly how a row-wise sum would
  have looked plausible. `bid_group` is emitted so the client can aggregate honestly
- Non-MSP shows "not recorded on this book", not 0: Bullhorn is direct business and
  GHR is the only agency on the req, so nought would read as "nobody bid"
- The Closed KPI row is now the six he specified, still on one row

### 2.27.1 - One variance in the Rate pane, and the facility strip keeps its job
- The variance chip added in 2.21.0 is removed. Market Position now states variance
  against 337 all-accounts peers; a second figure from five placed rates invited
  reconciling numbers that answer different questions
- The **strip stays**. It answers what nothing else does, and what the IT notes call
  the most important use case: what seats at *this* facility actually filled at. A
  $38 opening against ~$45 of recent fills explains why a job isn't filling; the
  market ladder can't say that. Relabelled "What This Facility Actually Pays" so the
  two sections read as different questions rather than rival answers

### 2.27.0 - Market position, derived from rate data that was already there
- **Market rate is live.** `BH_BILL_RATE_TRENDS_OUTLIERS_FACT` already carries the
  population and `GetRateIntel` already ships it, so the Rate pane now reads
  `Current $100 · Market $84 · +18.8% · #5 of 23` -- the exact form the feedback
  asked for, with no new fetch. Dan had deferred Rate to P3 on the assumption the
  market data wasn't ready; 476K rows of `Client_Bill_Rate_AVG` run to Sep 2026
- Market is the tightest **all-accounts** rung with enough peers; the rank stays the
  **account** ladder, matching his split of "Market" from "Internal Ranking".
  Median not mean -- one outlier in a small bucket moves an average off every real job
- **The rate ladder's top two rungs had never fired.** `GetRateIntel` emits peers
  with an `account` key; `rateKeyOf` read `health_system || facility`, which peers
  don't have, so every peer keyed to `''` on that field and both account rungs were
  permanently empty. Every rank silently fell through to all-accounts -- correctly
  labelled, so it read as right. **77% of jobs (4,637 of 6,000) now rank against
  their own account; previously none did**
- `rateRank` read only `bill_rate`, so it returned null for every open job (they
  carry `billRate`, and as a display string). Now uses `Utils.rateNum` on both
- `rate_category` is emitted by `GetPositions` for all four sources, so an open job
  and a closed one rank on the same axis. 1,050 of 1,052 live Bullhorn open jobs
  resolve one; the two that don't are Permanent and correctly carry no rank
- `rate_category()` moved to `shared_code/rate_scope.py` -- two copies would drift,
  and a rank meaning one thing on Closed and another on Open Jobs is worse than none

### 2.26.2 - Closed KPIs back to one row; VNDLY client; Dan's feedback tracked
- The Closed KPI row had seven cards in a six-column grid, so the seventh wrapped
  and left a band of dead space above the records. Monthly and Annualized Revenue
  Captured are parked as comments -- they are `weekly x 4.33` and `weekly x 52`,
  carrying nothing the first card doesn't. Measured: 7 cards / 2 rows / 244px →
  5 cards / 1 row / 106px
- Dan's sixth card, Bid Activity, is not built: no bid field exists in `GetClosed`
  or anywhere in the app. It needs the data before the card
- `api/shared_code/vndly_api.py` — multi-tenant VNDLY client for writing requested
  time off to the contractor's `RTO` custom field. Each MSP client is its own
  tenant with its own host and token; the warehouse pools them, so writes resolve
  the tenant from `[Health System]`. **`[Health System Id]` cannot be used** — it
  is numbered per tenant and collides: Cooper and Inspira are both `3`
- `DAN_UI_FEEDBACK.md` transcribes the September feedback with current status per
  item. The PDF itself is gitignored -- the repo root is served by the Static Web
  App, and a file there with no matching route would be publicly fetchable

### 2.26.1 - Carry the bottom-bar hide fix onto this branch
- `el.hidden = true` cannot hide an element carrying Tailwind's `flex` class, so on
  main the bar stayed laid out at 56px across production MSP's job list. Inert here
  (`impactUi()` is always true) but carried so merging can't reintroduce it
- The bar is also hidden in the markup now and revealed by `View.revenueBar()`,
  which removes the blank strip that flashed on every load. Verified the bar still
  appears on both books on this branch

### 2.26.0 - Extensions can be recorded here when the source can't hold them
- **New End Date**, **Requested Time Off**, an **Is An Extension** override and a
  **Reason** field (shown for Declined / Backfill Required), all saving through the
  existing `workspace-state` path alongside decision, notes and the checkpoints
- The source systems mostly cannot hold this. Of 497 MSP seats ending in the next
  45 days, only the 115 VNDLY ones have a modification feed to write an extension
  to, and 57 carry one; B4's 382 have nowhere at all. RTO is worse -- the warehouse
  copy of the placement omits `customTextBlock9`, so MSP cannot even read it
- Recorded values feed the UI rather than being write-only: typed RTO shows beside
  the Gather RTO checkpoint as "Recorded here", and the extension override drives
  "Previously Extended" with a "set here" marker
- The RTO placeholder shows what the source already has, so nobody retypes it

### 2.25.0 - Non-MSP: MSP accounts removed, divisions merged to the core seven
Requested by the divisions taking the non-MSP instance live (Matthew Kyle, Daniel
Matteson, 2026-09-16).
- **MSP account data removed.** Auto-scope admitted any client whose *client-level*
  division tag list contained a non-MSP token -- and MSP health systems are tagged
  across divisions. 29 of the 371 auto-scoped clients were MSP accounts carrying
  540 live heads, which is exactly why Allied showed MSP heads: Hospital of the
  University of Pennsylvania alone contributed 132, tagged
  "Allied,Nursing,RevCycle Workforce". Now subtracted from the final scope, so a
  manual allowlist entry cannot re-admit one
- **Core divisions.** `RevCycle Workforce` → Rev Cycle; `Search` + `United`
  (United Anesthesia) + `Locum Tenens` → Search and Locums; `Human Services` →
  Education was already in place. The filter now offers the core seven
- `statsData` rows were never run through `normalizeDivision`, and they feed the
  Division filter, so the raw tokens showed up beside their own merged names
- `Other` (untagged) and `GHR Internal` are still listed rather than hidden --
  599 of 4,379 open reqs carry no division, and dropping them silently is the
  failure this taxonomy exists to prevent

### 2.24.4 - Bill rate was a string everywhere it was used as a number
- `job.billRate` is a display string (`"$58/hr"`, `"$1,200"`) -- `cleanBillRate()`
  formats it at ingest. Four places called `Number()` on it and got `NaN`
- The Rate pane showed **Bill Rate $0.00** and **GM / Hour —** on a job billing
  $58, and printed **"Rate Rank: no comparable rates"** directly above its own line
  reading "Against 92 placed rates at Orlando Health"
- Priority Jobs' **Avg Bill Rate** KPI read "—" always. `placedPeers`' open-jobs
  fallback could never find a rate, so it never actually fell back
- One `Utils.rateNum()` now parses it, the same way the column sorters already did
- The rank strip counted only strictly-higher peers, so a tied rate ranked at the
  top of its block rather than the middle -- and disagreed with the variance chip
  beside it: "#78 of 92" next to "#86 of 93" for one rate against one peer set.
  Both now use one definition
- Verified in a browser: Bill Rate $58.00, GM/Hour $14.50, #86 of 93, -9.4%

### 2.24.3 - Financials could spin forever; Contracts could fail opaquely
- `financials()` treated an empty array as "still loading", and both failure paths
  set `financialData = []` -- so a financials request that 500'd or threw span
  "Loading financial data from database..." for as long as the tab stayed open,
  and a period with genuinely no rows looked identical. Verified against main: an
  HTTP 500 and an empty result both spin indefinitely
- Now three states: loading, failed (with the reason), and loaded-but-empty
- `new Set(rBody.keys || [])` breaks if `/api/reviewed-contracts` ever answers with
  an array -- `rBody.keys` is then `Array.prototype.keys`, a *function*, which is
  truthy, so `|| []` never fires and `new Set(fn)` throws. It surfaced as
  "Couldn't load contracts comparison" with nothing useful in the console

### 2.24.2 - A mechanical check for production-MSP bleed
- `node tools/msp-bleed-check.js [refA] [refB]` renders index.html AS production
  MSP at two git refs and diffs what the page actually produces: visible tabs and
  their labels, body classes, the Open Jobs layout tree, KPI labels, whether the
  legacy list is what's on screen, and console errors
- The "don't change production MSP" rule has been enforced by memory and failed
  twice -- the standing revenue bar reached production, and a gating wrapper broke
  scrolling on the job list. Both were invisible in a diff and obvious in a browser
- Serves the files locally and stubs `/api/*`, because the deployed sites require
  an authenticated role on `/` and a headless browser just lands on the Entra ID
  sign-in page
- Exit code is the number of differences, so it can gate a push

### 2.24.1 - Sorting and column filters were dead on every stage table
- `label:${JSON.stringify('Assignment ID')}` emits `label:"Assignment ID"` inside a
  double-quoted HTML attribute, so the parser ended the attribute at that first
  inner quote and the handler was cut mid-expression. Every click and keystroke
  threw "Unexpected end of input"
- Sorting and per-column filtering therefore never worked on Extensions,
  Onboarding, Closed or Open Jobs. The interview-stage dropdown in the job detail
  passed a candidate name the same way and was broken too
- Fixed by escaping the interpolation, so the parser decodes the quotes back
  inside the handler. Apostrophes in a name or label are now safe as well
- Verified in a browser against 60 real placements: filtering narrows 60 rows to
  1 with every visible row matching, and sorting reorders all 60 both ways

### 2.24.0 - Extensions filters AM and Recruiter by dropdown
- Both columns were free-text filters. On the non-MSP book they are effectively
  fully populated -- 661 of 661 seats carry an AM, 660 carry a recruiter -- across
  71 and 78 distinct people, which is more names than anyone can spell from memory
- Dropdown matching is exact, not substring: picking "Ann Lee" no longer also
  returns "Ann Leeson". Typed filters elsewhere still match on substring
- Options are built from the loaded rows before any column filter applies, so
  choosing a name never empties the list you chose it from
- A "— none —" option where a column has blanks, so "who has no recruiter
  assigned" is askable
- The column reads AM on non-MSP and stays PM on MSP, matching what each book
  calls the role
- `filter: 'select'` is generic; any stage column can opt in

### 2.23.1 - Carry the scrolling hotfix onto this branch
- `legacyListWrap` gets the same `flex-1 min-h-0 flex flex-col` shipped to main in
  2.18.1. Inert here -- `impactUi()` is always true on this branch, so the wrapper
  is never shown -- but without it, merging this branch would reintroduce the bug

### 2.23.0 - Channel is a column on non-MSP, not just a badge
- Who holds the program (direct, or in behind someone else's MSP, and which one)
  was a badge you could read but not sort or filter by. It is now a real column
  on Open Jobs, through the same stageApply the stage tabs use
- Non-MSP only: on an MSP account the answer is always GHR, so the column would
  be a stack of identical cells
- The column's sort key and the rendered cell both call `View.channelOf`, so what
  you sort on is exactly what you see. Unset systems show a dash rather than
  asserting a channel nobody recorded
- The facility column gives up the width, so the table still totals 100% and does
  not reintroduce a horizontal scroll

### 2.22.0 - Closed gets a drillable "Ended Early"
- New KPI counting seats that ended before they were meant to, clickable to narrow
  the table to exactly those rows, with a banner and a "Show all" so a filtered
  table can't be mistaken for the full one
- Two conditions, both required: the row must be one somebody actually filled, and
  its reason must name a real early end. An unfilled order the hospital cancelled
  is a lost order, not a seat that ended early
- Reasons are matched on normalised text -- the same reason is typed several ways
  ("Terminated - attendance" and "Terminated-attendance"). Checked against all 33
  distinct reason values in the live data
- Non-MSP shows "—", not 0. Bullhorn's `reasonClosed` is order-level (Filled, Lost
  to Competition, Cancelled by Client), so "no early ends" would be a claim the
  data can't support
- KPI cards can now carry a click target and an active state

### 2.21.0 - Rate says where it sits: "+7.8% · #3 of 8"
- The Rate pane drew a rank strip but never stated the position in words. It now
  gives variance against the peer median and an explicit rank
- Variance is measured against the median, not the mean: these are small samples
  at a single facility, where one outlier drags an average somewhere no real seat sits
- Ties take a mid-rank, and this job's own rate is counted in the denominator when
  it is not already one of the peers -- ranking against a single placed rate used
  to produce "#2 of 1"

### 2.20.0 - Pipeline gains an Accepted stage and a link back to the source
- The funnel ran Submitted → Interview → Offer and stopped, so it said an offer
  went out but never how many were taken. Accepted is now its own stage: it used
  to share a rank with Offer Pending and Post-Offer Decline
- Each record can link back to the system it came from. The URL templates come
  from config (`SOURCE_URL_B4`, `SOURCE_URL_VNDLY`, `SOURCE_URL_BULLHORN`,
  `SOURCE_URL_SYMPLR`), since the hosts are tenant-specific -- an unset source
  renders no link rather than a broken one, and only http(s) is ever emitted
- The unreferenced `bucket()` helper is commented out rather than removed; it is
  still the right shape for a terminal-state breakdown

### 2.19.0 - Placements defaults to the last 90 days
- The Placements pane showed only seats live at that moment, so a facility that
  turned over last month read as untouched. It now defaults to a 90-day window --
  active seats plus anything that ended in the last 90 days -- with an
  "Active only" toggle to get the old view back
- Ended rows are chipped as such, carry their source status on hover, and sort
  after the active ones, most recent first
- `recentlyEnded` is a new key on `stats-data`, deliberately separate from
  `onAssignment`: the Contracts tab and the headline counts read that one, and
  widening it would have moved numbers across the app
- Status lists had to be measured, not assumed. Ended seats carry Completed /
  Termination / Cancellation on Bullhorn and Ended / Ended by Job Close on VNDLY,
  none of which appear in the active-status lists, so a date-only change would
  have returned almost nothing. Pipeline outcomes on seats nobody worked
  (Rejected, Withdrawn, Offer Declined, Closed Not Awarded) are excluded

### 2.18.3 - Rate & GM condensed into the record
- Rate & GM was a full-width strip below the detail panels on Extensions and
  Onboarding; it now sits inside the record itself, beside the decision it informs
- Same numbers, same override field, same save path -- only the placement changed.
  The old full-width block is kept in place, unused, if a wider one is wanted back
- A seat with no bill rate on file now shows a dash instead of $0.00. Symplr
  onboarding rows carry no bill rate at all, so every one of them was claiming
  $0.00 bill and $0.00 GM/hour. A genuine zero still renders as $0.00

### 2.18.2 - Extension checkpoints show what the source already knows
- "Gather RTO" and "Send Extension" now display the recorded text from the source
  system beside the checkbox, so the team confirms rather than re-gathers
- The box is still ticked by a person. 68 of the 351 seats with recorded time off
  say "None needed" or "N/A", and the field is free text, so presence cannot mean
  the step is done -- auto-completing would advance the funnel on untouched seats
- "None needed" is reported as such rather than quoted back as if it were a date
- MSP shows nothing for RTO, and structurally cannot: the warehouse placement copy
  omits customTextBlock9 and VNDLY has no time-off field at all. Written up with
  the measurements in EXTENSION_MILESTONE_SOURCES.md

### 2.18.1 - The extension note shown is the latest one
- VNDLY's extension note and its author were picked with `MAX()` over text, which
  is alphabetical rather than chronological. 91 of the 149 work orders carrying an
  extension note have more than one, and 14 of them (9%) were showing a superseded
  end date; one was credited to the wrong person
- Extension milestones now carry what the source systems already know: Bullhorn's
  own "Extension?" flag, and the clinician's recorded time off on the non-MSP side
  (35% of live seats), so the team is not asked for either a second time
- Client interest and client approval have no source anywhere -- Bullhorn's
  "Upcoming Extension?" field is empty on every row -- and stay manual by necessity
- Feature-branch only. `GetExtensions` backs a tab production MSP has had since
  2.5.0, so this ships with the prototype merge rather than ahead of it

### 2.18.0 - Headcount moves explain themselves
- Trend's category breakdown now says what caused each headcount move rather than
  only how large it was
- Open Jobs' row dropdown rebuilt to Dan's current design (prototype and non-MSP;
  production MSP keeps the renderer it has always had)
- One ID cell and one system cell shared across all four stage tables, so the
  columns line up tab to tab
- Expanding a row centres the dropdown itself, measured after it reflows, instead
  of scrolling to the top of the row and cutting off the contents

### 2.17.0 - Production MSP gated off the redesign

main serves both production MSP and non-MSP, and only the stage tabs were gated. Everything on a shared surface — Open Jobs, the job detail, Trend, the footer — therefore reached production MSP, which was to stay exactly as it was until the prototype branch merges. `Utils.impactUi()` is now the single switch every new surface asks: `dataSource === 'non_msp'` on main, `true` on the prototype, which is that branch's one deliberate divergence.

Production MSP's Open Jobs grid renderer and row builder were restored from 2.5.0 and verified identical. They live alongside the stage-table versions rather than replacing them. Also gated back: the KPI row, the standing bottom bar, the Trend movement annotation, the Facilities dropdown change, the doubled-specialty collapse, and row centring.

Two things would have broken MSP outright. `rowUpdate` and `TOGGLE_ROW` had been rewritten for the table and would have silently failed on div-based rows, taking every lever, margin and interview edit with them. And `s.sort` with its `SORT` reducer had been deleted as dead weight when Open Jobs moved to `stageApply` — the restored renderer reads `s.sort.key` on its first line, so every load threw until they came back.

The pre-commit check now asserts those two renderers still match their 2.5.0 baseline, so drift on that surface is caught mechanically.

### 2.16.1 - One people picker, and assignees get a stable identity

*(Renumbered from 2.13.0. Two sessions numbered in parallel and both reached 2.13.0 — this one shipped on the prototype branch, the other on main for Open Jobs. The content is unchanged.)*

The lever/margin tag is *who the action is assigned to*, recorded separately from who performed it (`changes.user_name`, which is the Azure AD email and is 100% consistent across all 4,649 rows). The assignee was the inconsistent half: of 2,030 lever completions, **1,115 carried a display name and 915 an email**, so the same person appears twice and the records can't be grouped by assignee.

Both code paths already had the email and dropped it. MSP rendered chips from a hardcoded team list and passed only `m.name`; the Graph dropdown showed `u.mail` but passed `displayName || mail`.

That list had also drifted — five addresses on `@ghresources.com` rather than `@ghrhealthcare.com`, and one with the surname misspelt (`jdirekes`) — so it could not serve as an identity source. Rather than repair it, MSP now uses the same Graph search non-MSP already had: one code path, an authoritative source, and no second list to maintain.

A selection now carries both — the display name in the visible field, the email on a data attribute — and `lever_update`, `margin_update` and the open-to-AVs lever all persist `userEmail` beside `user`. `change_data` is free-form JSON so no API change was needed. Rows written before this carry no email; the replay path defaults it to empty and still renders the name.

The tradeoff: MSP loses one-click chips for type-to-search.

### 2.16.0 - Open Jobs fits its container

Column widths as percentages so the table stops needing a horizontal scroll: an unsized column under `table-fixed` swallows every remaining pixel, which left a band of white space while ID and Category truncated. B4 writes many specialties doubled ("CT TECH - CT TECH") on 62 of its 91 open orders; the halves are collapsed where they match exactly.

Two KPIs could only ever read zero. `interviewDerivedStage` never returns 'Post-Offer Decline', so counting that stage matched nothing; a decline carrying an offer date is one. And `onboardingSlip` measured against a start recorded at the previous meeting, returning null until someone had saved one — it now falls back to the slip the source reports.

### 2.15.0 - Pending detail panels

Clinician Status, Pipeline Mix and Next Action. Stage age is days since submission; the blocker is whatever the source recorded. Pipeline Mix counts other submissions against the same facility and position. No feed carries an owner, so that tile says so rather than naming one.

### 2.14.0 - Movement History

Every `dateBegin` edit from `EditHistoryPlacement` with its old value, new value and author. An edit setting the start to the value it already held is dropped. Only Bullhorn keeps a per-edit trail; B4 and VNDLY say the per-move history is unavailable rather than showing an empty timeline.

### 2.13.0 - Open Jobs on the shared stage table

Open Jobs rendered a div per job under a hand-built header with its own sort. It now renders through `stageShell` with the reference's ten columns, gaining sortable headers and column filters. `buildRowHtml` split into `jobRowCells` and `jobDetailHtml`; the `SORT` action and `s.sort` were removed as superseded — which 2.17.0 had to undo.

### 2.12.2 - Reviewed contracts were all attributed to one person


### 2.11.1 - Sweep for things built but never wired

Two things this session were fully built and never called — `GetRateIntel` shipped as an endpoint nothing fetched, and three row-detail panes existed only on the prototype. So the codebase was swept for the same shape of defect.

Every one of the 26 API routes is now referenced by the client. Of 120 View methods, one was genuinely unreachable: `updateFacilityOptions()`, which rebuilt the facility dropdown from open jobs filtered by system alone — ignoring division, team, profession, category and assignments entirely. That is the behaviour 2.10.0 replaced, so leaving it in place invited a silent regression by anyone who called it. Removed.

Also verified rather than assumed: no duplicated DOM ids across the 124 in static markup (past merge conflicts have produced them), and the remaining hand-written `colspan` values all match their table headers — 13, 8 and 8 against headers of 13, 8 and 8.

### 2.11.0 - Explainable headcount movement

Headcount per week is a set of distinct workers on assignment, so a change between two weeks is the difference between two sets — who appeared and who dropped out. The size of the swing was already on screen; the names behind it were not, which is what made a fall of six impossible to act on. Each headcount cell now carries the change from the previous column, and hovering it names who started and who ended.

The comparison runs across the **displayed** weeks, not the built series. The activity and completeness gates drop weeks out of the middle, so comparing each week to its neighbour in the full series would show a change that does not reconcile with the two numbers either side of it. Checked against the identity it relies on — |A| − |B| = |A∖B| − |B∖A| — over 5,000 random series: the annotation always equals the difference between the two headcounts shown.

Clinician names appear here. Redaction hides vendor-side identity and these are GHR's own workers, so they show; the tooltip caps the list so a swing of forty does not become an unreadable wall.

### 2.10.0 - Non-MSP: Division narrows System, and the VMS fee surfaces

**Division did not narrow System, and the reason was one unasked question.** The Systems dropdown builds its options from open jobs *and* from assignments, so a system that is filled but has no open order stays selectable. Open jobs were checked against the whole non-MSP hierarchy; assignments were checked only against category and facility. So choosing a Division left behind every system whose sole presence was an assignment in some other division. Assignments carry `division`, `team` and `profession` like everything else and are now asked the same questions.

Facilities had a larger version of the same gap: that dropdown never looked at assignments at all, so a facility with assignments but no open orders was unreachable while its system stayed selectable. Fixing it required hoisting the shared assignment list out of the Systems block — it had been declared inside it, so the Facilities block referencing it would have thrown at runtime. `node --check` cannot see that; only reading the scope can.

**VMS Fee.** Bullhorn's `FieldMaps` decodes Job Posting `correlatedCustomFloat3` as "VMS Fee %" — the cut a third party's VMS takes when GHR delivers through someone else's programme, which is exactly the account the Relationship badge already labels "3rd Party". It has no MSP counterpart: there GHR runs the programme and the bill/pay split is the fee, already carried as `agency_receipt_pct`.

It is populated on 41,454 of 513,198 job orders (8.1%) averaging 4.88%, and on 317 of the 6,325 currently open (5%) averaging 4.45%. Shown only where recorded, never as a zero — a zero would read as "this VMS takes nothing" rather than "nobody recorded one". The Rate pane shows the fee, the per-hour amount and the rate net of it, and says plainly that GM is calculated on the gross rate: the fee is a fact from the job, GM is a rate we apply, and folding one into the other would blur that line.

Incumbent and Channel were already delivered — `Utils.systemRelationship()` puts the relationship and the incumbent holder on the job card. Worth noting `CLIENT_DIM.ParentAccount` sometimes holds "MSP" or "Direct" instead of an account name, which is a data-entry artifact rather than a channel field, and is why a 1,920-row "MSP" bucket shows up among the rate peers.

### 2.9.0 - The row-detail panes: real rates, real funnel math, one definition of declined

Three feedback items — Rate, Pipeline and Placements — were all about the job row-detail panes.

**The rate was never missing from the data, only from the query.** The Rate pane ranked a job against other *open jobs* at the facility, with a comment explaining that "active placements carry no rate in the stats feed". They carry no rate because every one of the six assignment queries in `GetStatsData` left its source's rate column unselected. B4 has `Awarded_Rate`, VNDLY `[Bill Rate]`, Bullhorn `clientBillRate` — populated on 100% of active MSP assignments (741 B4, 328 VNDLY) and 97% of non-MSP (1,844 of 1,901). Rate Rank now ranks against placed rates, falling back to open jobs only where a facility has fewer than three placements, and the note says which basis was used.

That also lets the Placements pane do the GHR-versus-affiliate rate comparison its own footnote had been claiming for. There are 199 affiliate B4 seats and 76 VNDLY carrying rates, so the comparison has substance. Rates stay visible under redaction: it hides vendor *identity*, and a rate is not an identity.

**The funnel was counting the wrong thing.** Stages were bucketed by *furthest stage reached*, so "Submitted" meant "submitted and went no further" — a job with 10 submissions of which 4 interviewed showed Submitted 6, and no conversion rate could be read between the rows. Each stage now counts everyone who reached at least that far, with step-to-step conversion. Depth is only claimed where it is recorded: a post-offer decline demonstrably reached Offer and counts at every stage, while a plain decline carries no record of how far it got and counts only as a submission rather than being credited with an interview it may never have had.

**"Declined" meant two different things in the same row.** `c.isDeclined` is the source flag and never moves when someone records a decline in the app, while `interviewStage()` honours that override. The pipeline counted declines through the stage and the candidate list filtered on the flag, so the Declined count and the Declined list could disagree — clicking a count of five could show three. A single `Utils.isDeclinedNow()` now answers it, used by the list, the pipeline, the candidate row styling, the aged-submission KPI, the hot-jobs email and the decline analysis. The count is also clickable now, which is what surfaced the mismatch.

### 2.8.0 - Onboarding compliance, blocker and next action

The feedback asked Onboarding for a compliance status, a blocker and an action owner. None of it was in the app; most of it was in Bullhorn.

`BH_PLACEMENT_RAW` (and `View_Placement` on the mirror) carry a full credentialling set: `onboardingStatus`, `totalRequirements`, `incompleteRequirements`, `requirementCompleted`, `expiringCredentials`. MSP reaches them through the same warehouse crosswalk System Match uses; non-MSP reads them off the placement directly. Coverage on seats starting in the window is good on both sides — a status on 80% of the 435 MSP seats and 83% of 1,040 non-MSP, a requirement count on 90% and 99%, something outstanding on 76% and 71%.

**`requirementCompleted` is a percentage, despite the count-like name** — total 59, requirementCompleted 95, incomplete 3, and 3 of 59 outstanding is indeed 95% done. The field actually called `onboardingPercentComplete` is populated on every row of both the warehouse copy and the mirror and is **always 0**, so reading the obvious one would have shown every clinician at 0% complete.

The verdict combines the packet status with what is outstanding, because neither alone is the answer: a seat can read "Initiated" with nothing left to do, or "Completed" with an expiring credential. It is computed server-side so both instances agree.

**No action owner was invented.** Bullhorn has a `credentialSpecialistUserID` field and it is null on every row of both the warehouse copy and the mirror, so there is no compliance owner to name. Instead the recorded owner keeps the ball and gains a next action read off the seat's actual state — clear the outstanding requirements, renew the expiring credential, record why the start moved. The recruiter is named alongside where known (86% MSP, 100% non-MSP). VNDLY and Symplr track no credentialling at all and read "not tracked" rather than "clear": an unknown packet is not a finished one.

Also fixed: detail and group rows were spanning hand-written column counts that had gone stale — Extensions' group header spanned 8 of 12 columns and Onboarding's detail 9 of 11. Each stage now records its own width as it renders, which also handles Closed correctly, where Market Share is MSP-only and the column count differs between instances.

### 2.7.0 - Rate Rank shipped, and the tie bug that nearly went with it

`GetRateIntel` had been built and never called: the endpoint shipped, nothing fetched it, and Closed still had no RATE RANK. It is now wired, and the column is live — the last reference column that was missing.

**Ties were being ignored, and that was not a detail.** Ranking counted only peers with a strictly higher rate, so a seat tied with 75 others at $38 was reported as "#1 of 76". Bill rates cluster hard on round numbers, so this was the common case, not an edge: the median closed seat came out at the **93rd percentile** while its rate was exactly the peer median, and 53% of rows read p90+. Mid-ranking ties moves the median to **48** and the spread to 10% / 15% / 52% / 23% across the quartile bands, which agrees with the independent check that the median GHR rate is 1.00x the peer median.

Three vocabularies had to be reconciled first, and none of them met:

- **Category.** Peers use Bullhorn's `employmentType` (Travel / Local / Remote) — an engagement type. B4's `Program` fuses engagement type with service line ("Travel Nursing", "Local Contract Allied Health"), and VNDLY's `Labor Type` is mostly service line. The engagement type is now read back out of the programme name, but only where the name states it. "Contract (Nursing)", the largest B4 bucket at 6,871 rows, could be travel or local and is left unmapped rather than guessed — a Local seat ranked against Travel peers reads as underpaid however well it is priced. Derivable on 16,749 of 29,106 orders (58%). Non-MSP needs no derivation: its Closed rows carry the same `employmentType` column the peers are built from.
- **Specialty.** B4 does have one, under `Care_Type` ("Medical Surgical", "ICU"), which was not being selected. It matches a peer specialty on 6,504 of 29,106 orders (22%).
- **Account.** Only **3 of 18** B4 health systems match a peer account name exactly — "Penn Medicine" against "University of Pennsylvania Health System" — and every near-miss is a false friend: "Cooper University Healthcare" and "University of Maryland Medical System" share only the word "University". No fuzzy bridge is attempted. The rung simply does not fire, and the rank resolves one rung wider and says which scope it used.

A fourth ladder rung, `category + profession`, was added for this. Without it MSP could rank one seat in eight; with it, 49% of recent closed seats rank (37% at category+profession, 8% at account+category+profession, 4% with specialty). Still category-first — the widest rung is all accounts *within* one engagement type, never across the book.

Two smaller fixes: `normalize()` could not resolve "RN III" (its contained-match fallback needs an alias longer than three characters and "rn" is two), so 1,508 B4 rows fell through to unmapped names and matched no peers; a trailing seniority level is now stripped, leaving "Level II Trauma" alone. And the rank counts are computed by binary search over the already-sorted peer array rather than two linear scans, since this runs per visible row per render and again for the sort key.

### 2.6.0 - System Match, Recruiter, and what `IsExtension` actually means

The two columns Extensions was missing both shipped. Both needed the same thing: a way to find a seat's counterpart record in the other system.

**`PLACEMENT_DIM.IsExtension` does not mean what its name suggests.** It marks a placement that *is* an extension of a prior assignment, not one that *has been* extended. Checked against the chain rule the feedback describes — same clinician, same client, a prior placement ending within 14 days of this one's start — 7,545 of the 8,745 flagged placements chain (86%), against 8.7% of the unflagged ones. That is also why the flag and the date signal barely overlap: only 871 records both carry `IsExtension` and show `DateEnd > DateOriginalEnd`, because a flagged placement is a *new record* whose own end date has not moved. They answer different questions, so both ship: `seat_is_continuation` for the chain and `is_extension` for a pushed-out end date.

Neither is the Extensions tab's driver. The tab lists active seats approaching contract end; the flags supply the seat's history.

**System Match** compares the same fact as the VMS and the ATS each record it. `BH_PLACEMENT_RAW_TO_B4HealthOrder` links a B4 contract to a Bullhorn placement and resolves 287 of the 313 live GHR seats in the 45-day window (92%).

The crosswalk is used for the link and nothing else. Its own status and date columns are a snapshot frozen at load time: 3,002 of its 4,179 newest rows (72%) disagree with the live placement status and 1,257 (30%) with the live end date. Reading them as facts produced 78 phantom end-date mismatches and made 270 working seats look stuck in "Pending Start" — live, 272 of the 287 read "Approved". Every compared value is now read from `PLACEMENT_DIM`.

Only fields that are genuinely the same fact are compared. Status is not one of them: B4's `Contract_Status` describes the requisition and reads "Closed And Awarded" on every live seat, while Bullhorn's describes the placement lifecycle, so comparing the strings would flag all 287 as mismatched. The Lifecycle row asks the answerable question instead — has one system closed a seat the other still runs?

| Field | Mismatches (of 287 linked) |
|---|---|
| End Date | 36 |
| Start Date | 17 |
| Clinician | 2 |
| Lifecycle — ended in ATS, live in VMS | 10 |

**Recruiter** is reached through the same crosswalk and is named on all 287 linked seats.

VNDLY gets neither, and says so rather than showing a blank: no identifier reaches from a VNDLY work order to a Bullhorn placement. `VMSReqID` looked like the bridge and is a B4 contract number (5,068 of its 5,069 values resolve to B4, none to a work order); `STAGING_VNDLY_CONTRACTOR_XREF.[Client Contractor]` looked like a Bullhorn candidate ID and is the client's own contractor number (zero of 283 match). Non-MSP has one system of record, so the panel shows the seat's own audit trail instead — `EditHistoryPlacement` records every end-date move with its old value, new value and author, which is stronger evidence than a comparison would be.

Also: the Extensions group header spanned 8 columns against a 12-column table, and KPI cards silently dropped any `sub` line they were given.

### 2.6.1 - Standing revenue bar

The reference's footer, live on every tab: Revenue Won, Revenue Missed, Open Exposure. Won and Missed read the Closed rows' signed revenue, which the API decides -- a GHR win positive, an affiliate win or unfilled seat negative, a cancellation zero -- so both instances agree without the client re-deriving the rule. All three follow the active filters.

Open Exposure is 13 weeks of bill rate x weekly hours across open seats. Seats whose source carries no weekly hours are counted in neither the money nor the seat total and are reported as excluded, the same discipline `extension_value_13wk` uses. Coverage is good: all 86 open B4 orders carry hours and 40 of 49 active VNDLY jobs do.

Two things this turned up. `shift_hours` was being folded into a display string (`shift`) and the number thrown away, so nothing downstream could multiply it. And the exposure filter was written as `jobs.filter(j => !j.filled)` against a `filled` property no job has -- every branch of GetPositions already filters to open demand at source, so the filter was inert and read as though it were not.

### Extensions and Onboarding realigned to the reference

Both tabs now carry the reference's columns and its three-line assignment block — system eyebrow, facility, specialty — with Source and Agency moved under the clinician so a row reads the same on every stage tab.

| | Reference | Now |
|---|---|---|
| Extensions | 10 columns | 10, plus 13-Wk Value |
| Onboarding | 10 columns | 10 |
| Closed | 10 columns | 9, less Rate Rank |

**Rate Rank** on Closed is the one reference column still absent, pending the Rate pane wiring.

**2.5.0** - Rate intelligence: real ranking from the rate trend tables

The prototype's Rate pane could not have shipped — its `rateRank` came from a mock field and its comparable median was `days * 0.82 + 4`. The feedback said to leave Rate out until market data was validated. It turns out the data was already in `ghrdhc`.

`BH_BILL_RATE_TRENDS_OUTLIERS_FACT` holds the full population despite its name: 35.07M rated rows across 288,348 job orders, of which only 7% carry `bt_Outlier`. The flag marks which rates are outliers; it does not filter the table. Two filters are mandatory — `Group_Context` and `Sensitivity_Level` — because each job order appears once per grouping context (six) and per sensitivity level (ten), so without them every job counts dozens of times and a rank's denominator is meaningless.

**The endpoint ships peer rates, not ranks.** Filters in this app are client-side; they narrow loaded rows and never refetch. A rank computed server-side would be locked to one scope and would stop agreeing with the System picker the moment anyone used it. With the peers in hand the client ranks within whatever the active filters leave, so the ranking follows the System picker — and facility, category and division — with no extra control and no refetch. 21,824 rows over 90 days across 657 accounts.

Ranking is **category-first**, and the ladder never leaves the category — taking the tightest rung that clears a three-peer floor:

| Scope | Coverage |
|---|---|
| account + category + profession + specialty | 35% |
| account + category + profession | 62% |
| category + profession + specialty (all accounts) | 94% |

What would have been "book-wide" is the third rung: all accounts *within* the category, never a comparison across categories. That matters because Travel is 20,588 jobs against Local's 1,108 and pays structurally more, so ranking a Local req against Travel peers would mark every Local req underpaid however well it is priced. Category comes from `employmentType` (Travel / Local / Remote / PRN), which lines up with the CATEGORY column on Closed.

The same reasoning rules out service line as a ranking tier, despite its 68% coverage: an RN at $95 and a CNA at $35 are both Nursing, so the CNA would rank last regardless of pricing.

Below ten peers the display is a percentile rather than an ordinal. "#1 of 3" and "#1 of 149" read as equally strong claims and are not.

A $20–400/hr sanity bound is enforced. One RN Case Management req carried $1,100/hr — a weekly figure in an hourly field — which alone lifted its group's average from about $90 to $144 and made every other req in that group look far below market. `bt_Outlier` did not catch it; inside the bound only 97 of 21,824 rows are flagged, so the bound is doing nearly all the work.

Weeks are Sunday-based, matching the `DATEFIRST 7` the app pins elsewhere and the Saturday period ends in the rate trend tables.

**2.4.0** - Legend filter buttons on non-MSP, and the source badge tells the truth

**The source badge said B4 on the non-MSP side.** It tested `sourceSystem === 'VNDLY'` and labelled everything else `B4`, so Bullhorn and Symplr rows both claimed to come from a system that supplies none of that instance's data. All four sources now have their own badge — **B4**, **V**, **BH**, **SY** — and an unrecognised value shows its own initials rather than being relabelled. The `|| 'B4'` default on the row mapping went too; every query on both sides selects `source_system` explicitly, so it was only ever a guard, and a guard that lies is worse than none.

**The legend filter existed with no way to reach it.** `LEGEND_BANDS`, `legendBands()`, `legendMatch()` and a `TOGGLE_LEGEND_BAND` reducer case were all ported with the stage views, and `stageFilter()` has been consulting a band selection ever since — but nothing rendered a chip to dispatch it. The legend is now clickable on non-MSP, and shows on the stage views there instead of being hidden. Original MSP keeps the legend it has always had; it gets the interactive one through the prototype build.

**Three of the four Closed bands could never match a row.** They tested for `lifecycle|complete`, `terminat|resign` and `admin`, none of which any endpoint has ever produced — the real outcomes are `GHR WON` / `AFFILIATE WON` / `MISSED` / `CANCELED` on MSP and `FILLED` / `UNFILLED` / `CANCELED` on non-MSP. Closed now has a band list per instance, matching the vocabulary each side actually reports. This was invisible until a chip existed to click.

**2.3.9** - Market share on the Closed row detail

The last prototype feature worth porting. The reference drew this as `marketSharePie` — vendor share for comparable roles at the account — but its version could not have shipped: `pieHeadcounts` derived headcount from *the job id modulo 7* and read start dates from a hardcoded array. The shape was right and the data was scaffolding. Both VMSs carry the real figures.

Closed rows now expand, matching Extensions and Onboarding, onto a donut of who holds the live demand at that account:

```
Cooper · Registered Nurse      109 seats   GHR 85.3%
Inspira · Registered Nurse      85 seats   GHR 80.0%
RUMC · Registered Nurse         50 seats   GHR 62.0%
Cooper · Respiratory Therapist   9 seats   GHR 44.4%
```

That last line is the point of the feature: GHR hold under half the respiratory demand at Cooper, behind Nurse Staffing LLC.

**Keyed on health system, not facility.** The two sources disagree about what a facility is — B4's `Facility` is the hospital, VNDLY's `Default Work Site Name` is a unit (`Nursing Float-6211`) — so a facility key silently splits one account in two. Health system is also the level "share at the account" is asked at.

It prefers the seat's own role and falls back to the whole account when that role has fewer than six seats, always labelling which it is showing: "GHR hold 85%" means something very different across 109 seats than across 3. Vendor names follow Redact Vendor Info. Non-MSP rows do not expand — every seat there is GHR, so the split would be one 100% slice.

The other missing prototype feature, `canSeeAffiliateExtensions`, is deliberately not built. It gated affiliate rows behind an Admin role, and the app has no roles — the config carries only `anonymous` and `authenticated`, and custom roles from AAD groups need Standard tier. Redact Vendor Info already covers the same concern by masking identities rather than hiding rows, and now reaches the stage views.

**2.3.8** - MSP stage tabs show the whole vendor panel

Extensions and Onboarding were returning GHR seats only. Both endpoints take an `includeAffiliate` parameter, it defaults off, and the UI had never passed it — so the tabs were quietly hiding **29%** of the extensions window and **44%** of the onboarding window. On the B4 side of Onboarding there are more affiliate starts than GHR ones (428 against 408).

That was a default nobody revisited rather than a decision. On MSP, GHR runs the panel: an affiliate start that is slipping is still the programme's problem, and an affiliate seat ending is potentially a seat to win. Closed already returned both, because capture rate is GHR measured against affiliates and could never have been GHR-only.

Non-MSP is unaffected — `'GHR'` is the only agency there, so the flag is meaningless and stays off.

**An Agency column comes with it,** because showing affiliate rows without labelling them would be worse than hiding them. `Source` was already taken by B4/VNDLY, which is the system of record rather than the vendor. Rows now carry a GHR or AV badge, with the vendor name in the tooltip, following Redact Vendor Info. The GHR/AV distinction itself is not masked: knowing a seat is not ours is the point of showing it, and it identifies nobody.

The column sorts and filters on the displayed label rather than the raw vendor. Keyed on the vendor, someone could type a name into the Agency filter with redaction on and infer the masked value from which rows matched.

**2.3.7** - Redact Vendor Info is MSP-only

The button masks the names of clinicians placed by *other* agencies. Non-MSP is GHR's direct book with no vendor panel, so every row is already ours and the toggle could never change anything — every redaction site tests `!isGhrAgency` before masking. It is hidden there now, and hiding it strands nothing, since nothing was being masked to begin with.

**The stage views applied no redaction at all**, and now do. Across 1,245 lines of Closed / Extensions / Onboarding there was not one reference to `redactMode` — the four IMPACT tables were the one part of the app the privacy toggle never reached. It went unnoticed because those views only carry GHR rows, and GHR is never masked, so the button would have silently stopped meaning what it says the moment an affiliate row appeared. All three now render clinician names through `View.stageWorker`, using the same `isGhrAgency` test as the rest of the app.

A sweep of every view confirms the rest were already covered. `perDiem` and `trend` reference `worker_name` but never display it — it is a deduplication key and a headcount counter there, so there is nothing to mask.

**2.3.6** - List views load in parallel

Six of the loads behind the list views — stats, revenue, per diem, hours, trend and pending — were awaited one after another, so the page waited for the *sum* of their latencies before those tabs had anything in them. They share no data; each writes its own state keys. The requests now start together and each response is still handled in place by the same code, so wall time is the slowest of the six rather than their total.

The database was never the bottleneck here, which is worth recording: the MSP tables are small and scan in 0–5ms (`B4HealthOrder` 29,061 rows in 5ms, the VNDLY staging tables in under 1ms). The cost is per-request — function invocation plus a fresh `pyodbc` connection each time — which is exactly what overlapping the requests hides.

Relationship derivation is now cached for 15 minutes in module scope. It had added a second database to the page-load path: the query is only 47ms, but the Bullhorn connection setup is not, and which MSP holds an account changes on the order of months. Only successful derivations are cached, so a Bullhorn outage retries on the next request instead of serving fifteen minutes of blank badges.

Direct accounts no longer name a holder. Real vendor names leak into `customText57` on a handful of them — some Trinity Health placements carry Trustaff — and "Direct · via Trustaff" contradicts the badge beside it.

**2.3.5** - Leaked comment fix, relationship on non-MSP, one chart per side

**A comment was rendering as visible text on the page.** Porting the meeting modals across branches sliced them starting mid-comment, so the `<!--` opener was left behind and only the tail came over — `stage tabs in order, records what was decided… -->` sat in the markup as content. Repaired, and the file is now checked for orphaned comment tails alongside the other structural checks.

Relationship and incumbent now show on non-MSP too, not just the MSP prototype: the badge on the card, the two fields in Settings, and the Bullhorn derivation behind them.

**The incumbent rule was wrong.** It only named a holder on Third Party accounts, on the reasoning that `customText57` echoes the relationship back elsewhere. But ten of the eleven live MSP systems are GHR MSP, so the field was blank almost everywhere and looked broken. The card's question is *who holds this program*, and "nothing" is not an answer — GHR MSP accounts now read **MSP · via GHR**, third-party ones **3RD PARTY · via Symmetry**. Direct stays blank by construction, since there genuinely is no one in the middle.

Charts are back in line with the reference: one per side. MSP keeps Account Capture, which the reference draws via `accountCapturePie` in its stage trends slot. Non-MSP keeps Fill Rate, that stage's headline number. "Why We Lost" is gone — it had no counterpart in the reference and the table's Reason column already says it.

**2.3.4** - New favicon

`impact-manager.ico` replaces the old icon. The `<link>` tag points at it and `favicon.ico` is updated to the same bytes, because browsers still request `/favicon.ico` implicitly — from bookmarks, and for the tab before this page's HTML has been parsed. Both paths therefore serve the same image rather than the old one lingering wherever the link tag isn't consulted.

The href carries a `?v=` cache-buster. Favicons are cached hard enough that a stale one routinely outlives a deploy.

**2.3.3** - IMPACT meeting: review pass, three fixes

A pass over the meeting flow and its persistence. The shape held up — `impactmgr.meetings` has every column including the additive `stages` and `recap_html`, the MERGE upserts on `meeting_id` so repeated saves update one row rather than forking, `created_by` is set only on insert so the original owner survives, and two real meetings saved by actual users show correct rows with a stored recap. Three defects did surface.

**Stage jumps were never persisted.** `GOTO_MEETING_STAGE` moved `stageIdx` in memory but never saved, so `stage` in the database only advanced on Complete. Closing the tab mid-meeting resumed at the last *completed* stage rather than where the user actually was.

**Resuming a finished-but-unclosed meeting went back to the beginning.** `stageIdx === stages.length` is the legitimate "all stages done, not yet closed" state, and indexing it yields `undefined`; the fallback was `list[0]`. There is a real row in this state. It now lands on the last stage, which is where the user left off — the banner already rendered this correctly as Save & Close.

**Lever, margin and next-step changes were missing from the recap.** Actions were only logged from the four stage editors (interview, margin override, onboarding, extension decision). Everything done through the job action modal — the levers especially — went to `sessionLog` and the `changes` table but never reached the meeting record. `pushLog` is the single choke point for all of it, so logging there covers every modal action at once. `LOG_MEETING_ACTION` no-ops when no meeting is running, so this changes nothing outside a meeting, MSP included.

Checked and found correct: every DOM id the meeting code touches exists, no dispatch lacks a reducer case, the resume offer is scoped to the signed-in user, the meeting's health-system scope really does drive `stageFilter`, and a failed stage load renders an amber error banner rather than an empty table. `workspace_state` is empty, which is expected — those editors have only ever been reachable from a preview environment.

**2.3.2** - GHR logo in the header, instant tooltips on the icon buttons

The header's lucide `activity` pulse is replaced by the GHR logo, on both instances.

Every icon-only control in the header now has an instant hover tooltip. They already carried `title`, but a native tooltip waits well over a second and is easy to miss on a button whose whole meaning is an icon. `data-tip` renders immediately in the app's own styling; `title` is dropped where `data-tip` replaces it so the two don't stack, and `aria-label` keeps each control named for screen readers.

Covered: connection status, instance switch, settings, sign out, AI hot job summary, AI impact call summary, redact vendor info, change history, past IMPACT meetings, and start IMPACT meeting. Which of those are present depends on the instance — MSP shows AI summary and change history, non-MSP shows past meetings and start meeting in their place. Controls inside modals are left alone; they have visible text labels.

**2.3.1** - Non-MSP header: the meeting takes the retired buttons' place

Non-MSP retires the same two header buttons the prototype did — AI Impact Call Summary and Change History. Meeting History takes the Change History slot in the utility cluster, and Start IMPACT Meeting sits centred at the top of the header rather than in a strip above the tabs.

MSP keeps all four of its original buttons and shows neither meeting control. The swap is done in `viewToggle()` off `dataSource`, so the only edits to MSP's own markup are two added `id` attributes — inert, and needed to address the buttons at all. Against the pre-2.3.0 MSP baseline `index.html` still differs by five lines, two of which are those ids.

One detail worth recording: hiding these with `hidden` alone leaves them `display:block` when un-hidden, which stacks the icon above the label. Each element toggles `hidden` and its intended `flex` together.

**2.3.0** - Non-MSP: Closed, Extensions and Onboarding stages

The IMPACT stage views ship to the non-MSP instance. MSP is deliberately untouched: its versions of these stages stay on the feature branch for review before they reach main.

Closed answers a different question on each side. MSP asks who won a seat against affiliate agencies; non-MSP is GHR's direct book with no vendor panel, so it reports fill rate and velocity over historical orders instead, with a Why We Lost breakdown. Extensions reads real extension history from `EditHistoryPlacement` rather than inferring it. Onboarding measures actual start-date slip, which B4 cannot do.

Per Diem open orders were capped at today, which hid 91 of 104 live open orders and left the Per Diem drill-down permanently empty. That cap now looks three months ahead by default; an explicit month range is still honoured exactly as before. This is the one MSP-facing change in this release, and it is a fix rather than new behaviour — it affects only the `B4HEALTHOPENORDER` query, not billings, headcount or worked shifts.

### Keeping MSP unchanged, provably

The port was built up from main rather than stripped down from the feature branch, so anything untouched cannot regress. Against main, `index.html` is 15 hunks of which **13 are pure additions**. The two that modify existing lines are inert on MSP: one adds `|| STAGE_VIEWS.includes(view)` to a legend-visibility test that can never be true there (those views bounce to List), and the other adds one property to the `filters` object.

The API side was checked the same way, by which function each change lands in. Every `GetPositions` and `GetStatsData` change sits inside a Bullhorn or Symplr function, except four `_apply_service_line()` calls on the MSP path — and that helper is a no-op there, acting only on rows carrying `credential_raw`, which only the non-MSP queries select.

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