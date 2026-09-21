# Dan's UX and Functional Feedback — September 2026

Transcribed from `GHR_Impact_Manager_UI_Feedback.pdf` (6 pages, gitignored: the
repo root is served by the Static Web App and would expose it publicly).
Status column is mine, as of 2026-09-21.

## Core design principle

> Maximize the number of actionable records visible on screen. Use space for
> decisions, ownership, financial impact, and the next required action.

1. **Reduce vertical waste** — compress headers, filters, repeated explanatory content
2. **Clarify the action** — pending decision, owner, due date, next step immediately visible
3. **Make totals contextual** — summary metrics follow the selected module, filters and visible population

---

## 01 / Extensions — increase assignment visibility, compress the workflow

| Ask | Status |
|---|---|
| Move End Date From/To and Extension Status into the aging-filter row | open |
| Remove the separate Extension Search container; add a visible Clear Filters | open |
| Compress the Extensions title and subtext into one line | open |
| Short dates (`9/18/26`), reduced row padding | open |
| Widen Assignment so facility and position stay readable | open |
| Pull Status and decision fields together, remove unused gaps | open |
| Compact System Match badges, detail on hover/expansion | open |
| Reformat Client Decision into a compact task area: decision, owner, due date, next action | **partly** — owner and next action exist; not yet one compact area |
| Pull Bill Rate, GM %, GM/hour dynamically from source | **done** (2.24.4 — they were reading `$0.00`) |
| Same aging logic across summary cards, legend, Days Left, Urgency | open — needs an audit |

## 02 / Closed Jobs — reduce summary-card sprawl

| Ask | Status |
|---|---|
| Keep all summary KPIs on one row | **done** (2.26.2) |
| Remove Monthly and Annualized Revenue Captured | **done** — parked as comments |
| Retain revenue captured, capture rate, days to close, revenue missed, ended early, bid activity | **5 of 6** — bid activity has no data source |
| Use recovered space for more visible records | **done** — 244px → 106px |
| Rate Rank must populate for every job with a valid comparison group | **partly** — fixed in 2.24.4; needs verifying across cohorts |
| Benchmark within the correct category (Nursing, Allied, Non-Clinical) | open — currently facility+specialty, not workforce cohort |
| Display "No Comparable Rates" with a reason | **partly** — says it, without a reason |
| Outcomes standardized as GHR Won / Affiliate Won / Canceled / Missed | **done** |

## 03 / Closed Job Detail — three panels into two

Section 1 (Financial + Comparative): revenue captured/missed, bill rate and outcome,
days to close and comparable median, rate rank, outcome, owner.
Section 2 (Market Share + Bids): GHR share and competitor distribution, total/GHR/competitive
bids, winning supplier and bid-to-fill, how the job changed account capture.

**Status:** open. Bids and winning supplier have no data source.
Success test: the expanded record explains financial result, competitive position and
outcome drivers without repeating information.

## 04 / Open Jobs — replace the fragmented detail with a usable funnel

| Ask | Status |
|---|---|
| Remove the redundant MSP box; keep a small badge in the header | open |
| Full width for a funnel or horizontal bar chart | open |
| Stages: Submitted, Under Review, Interview, Offer, Accepted, Declined | **partly** — Accepted added (2.20.0); no "Under Review" stage exists |
| Stage counts, conversion rates, and where each decline occurred | **partly** — counts and conversion done; decline stage not captured |
| Candidate activity below the visualization, independently scrollable | open |
| Show multiple candidates at once | open |
| Candidate, supplier, current stage, last activity date, decline reason | **partly** |
| "Data Not Available" when a stage event is not captured — never a misleading zero | open — **this is a recurring theme; see cross-module** |

## 05 / Cross-module requirements

| Requirement | Acceptance test | Status |
|---|---|---|
| Contextual footer — Revenue Won/Missed/Open Exposure follow module, filters, population | Changing a filter or tab recalculates all three without refresh | needs verifying |
| Space efficiency | First screen shows materially more actionable records at standard desktop | in progress |
| Action ownership — every record shows decision, owner, due date, next action | No task requires opening multiple panels | **partly** — Extensions only |
| Data integrity — missing source events distinguished from true zero | Unavailable data shows a reason or "Data Not Available", never a misleading zero | **partly** — fixed in several places (bill rate, financials, RTO), not systematic |
| Benchmark logic — comparable rates use the appropriate workforce and job cohort | Nursing, Allied and Non-Clinical never blended | open |

## Priority

**P1** — compress filters, headers, redundant containers; make footer totals contextual across every module.
**P2** — complete stage, bid, financial and benchmark data; restructure expanded views around actions and outcomes.

> Target outcome: users should see more records, understand what requires action, and
> trust that every metric reflects the exact view currently on screen.

---

## Blocked on data, not design

Three of his asks have no source today and need a data decision before any UI work:

- **Bid activity** (total / GHR / competitive bids) — nothing in `GetClosed` or anywhere in the app
- **Winning supplier** on affiliate wins — `agency` carries the winner but is not surfaced as such
- **"Under Review"** as a distinct pipeline stage — not in `Utils.INTERVIEW_STAGES`

## The theme worth pulling out

"Never represent missing data as zero" appears in three separate sections. It is the same
defect class as the bill rate reading `$0.00`, Financials spinning forever on an empty
result, and Priority Jobs' Avg Bill Rate reading `—`. Worth treating as a standing rule
rather than a per-screen fix.

