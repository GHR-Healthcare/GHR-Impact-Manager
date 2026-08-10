# Non-MSP Filter Audit (post-v2.2.0)

Last updated: 2026-08-10

The v2.2.0 Symplr auto-discovery rewrite (~200 clients vs 3 hardcoded) surfaced
several filter-logic gaps that were latent when the non-MSP scope was tiny.
Ranked by user-visible impact.

## Fixed in v2.2.3

### §F1. Division filter over-matched via client-level tags
Bullhorn `record.division` comes from `cc.customTextBlock1` — a comma-separated
list of which GHR teams service the CLIENT, not the team that placed THIS
worker. Filtering by "RevCycle Workforce" pulled in RNs at any client whose tag
list included RevCycle. Now `Utils.matchesSelectedDivisions` also requires the
profession to match a keyword whitelist per division. Divisions without a
whitelist (Planet Healthcare, Search, Workforce Solutions, Human Services,
Acute, United, Technology, Education, Non-Acute) fall back to the raw
client-list match — either the vocabulary is too varied or the division is a
business-line label rather than a role.

### §F2. Hidden-system MSP keywords cross-contaminated non-MSP
`Utils.isHiddenHealthSystem` iterated `CONSTANTS.HEALTH_SYSTEM_MAPPINGS`
(MSP-managed table from Settings → Health Systems), so any non-MSP client name
containing an MSP hidden keyword (e.g. "jefferson", "sunrise") got silently
dropped. Now short-circuits to `false` when `dataSource === 'non_msp'`.

---

## Known open issues (ranked by impact)

### §F3. Facility ≈ System on non-MSP — dropdowns duplicate

- Bullhorn: `record.system` = mapped health system (Cone Health / Orlando
  Health / etc. via `BULLHORN_SYSTEM_ROLLUP`), `record.facility` = `cc.name`.
  Different values ✓
- Symplr: `record.system` = `pc.clientname` (auto-discovery), `record.facility`
  = `pc.clientname`. **Same value.**

So the Facility filter dropdown on non-MSP is mostly a duplicate of the System
dropdown. Options:
1. Hide Facility filter on non-MSP.
2. Emit a different value for Symplr facility (e.g. `pc.clientname2` if
   sub-org info exists, or hide the value entirely).
3. Merge System + Facility into one filter on non-MSP.

### §F4. Category filter mixes vocabularies on non-MSP

- Bullhorn `category` = `p.employmentType` (Travel / PRN / Remote / Local /
  Permanent). This is an **engagement mode**, not a role.
- Symplr `category` = `lt.nursetype` (RN / LPN / SLP / PCA / Para / etc.).
  This is a **role**.

The two are orthogonal, and the dropdown mashes them together, so filtering
by "RN" hides all Bullhorn rows (they have no RN entry) and vice versa. The
Trend tab's Category BREAKDOWN already switched to `service_line` (v2.0.5)
which normalizes both — but the Category FILTER still uses raw `category`.
Options:
1. Change the filter to work off `service_line` on non-MSP (matches how the
   Trend breakdown groups).
2. Split into two filters (Engagement + Role) on non-MSP.
3. Rename to make the intent clearer on non-MSP.

### §F5. Specialty duplicates Profession on both sources

Bullhorn:
- `record.specialty` = `p.customText1` (profession — RN, Coder, CRNA)
- `record.profession` = `COALESCE(NULLIF(jo.customText1,''), p.customText1)` —
  usually the same value

Symplr:
- `record.specialty` = `lt.specialty` (the Symplr specialty text)
- `record.profession` = `lt.specialty` — literally the same field

So Specialty and Profession dropdowns show identical values on both sources.
Fix candidates:
- Use `p.customText2` for Bullhorn specialty (sub-specialty per spec §6 —
  Inpatient, Outpatient, ER, Coding, Call Center, etc.).
- Hide the Specialty filter entirely on non-MSP and rely on Profession.

### §F6. Region filter is Symplr-only on non-MSP

Bullhorn `region` is `NULL` (spec-declared). Symplr `region` is `pc.state`.
v2.0.3's fix ("NULL region passes any region filter") means picking a region
narrows Symplr rows but doesn't drop Bullhorn — which is the least-bad
behavior. But the dropdown values still look like a Bullhorn-agnostic geo
filter, when it's really a Symplr-state filter. Options:
- Rename to "State" on non-MSP.
- Emit `p.customText19` (city, state per spec §6) as Bullhorn region if
  populated.

### §F7. `matchesStatsCategoryFilter` broad-keyword-matches

`matchesStatsCategoryFilter(specialty, selectedCategories)` matches on
`CONSTANTS.NURSING_KEYWORDS` / `ALLIED_KEYWORDS`, so selecting "Nursing"
catches every specialty containing any nursing-adjacent word. Fine on MSP
where categories map cleanly to those buckets, but on non-MSP where category
IS the role name (RN / LPN), the broad match over-includes.

---

## Data audit — Symplr in-scope after v2.2.0

Sample values from live DB (2026-08-10):

- Auto-discovered clients: ~200 (was 3 hardcoded)
- Division distribution: ~204 Education / ~83 Non-Acute post-MSP-exclusion
- Nursetype distribution: PCA (1,523), Para (1,092), LPN (795), RN (712),
  DSP (359), SLP (358), LPN,RN (222), CNA (184), Registered Behavior
  Technician (115), OT (108), PT (79), + ~30 more values

---

## Recommendation

Ship §F1 + §F2 immediately (already done in v2.2.3). Then batch §F3–F7 as
a filter-UX pass — most are UI decisions that need user input rather than
just code.
