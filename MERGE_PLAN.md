# Merging `feature/msp-impact-prototype` into `main`

Measured 2026-09-11, `main` at `4d13943`, prototype at `4a4b6f9`.

The short version: **this is not "flip `impactUi` and ship."** Sixteen methods
differ between the branches in code that production MSP runs *ungated*, and the
drift runs in both directions — each branch has code the other does not.

---

## Why a plain `git merge` is the wrong tool

The branches were kept in sync by **committing the same work twice**, once on
each side, rather than by merging. Their merge base is `a1a4e3c`, ancient, and
each side carries ~24 commits since it — many the same change under the same
message with a different SHA.

A `git merge main` into the prototype for a one-line README entry produced
conflicts in four separate regions of `index.html` plus two in `README.md`, none
of them related to the change being merged. Expect the real merge to conflict on
essentially every shared change.

---

## What actually differs

Method-level comparison of the `View` / `Utils` object literals:

| | count |
|---|---|
| Methods on `main` | 141 |
| Methods on the prototype | 147 |
| **Exist only on `main`** | **0** |
| Exist only on the prototype | 6 |
| Differ in body | 29 |

Nothing exists only on `main`, so no method would be *lost*. But bodies differ,
and that is where the risk is.

### The 29 split into two nearly-disjoint groups

**19 pre-existing** — the divergence that predates this session:

```
applyTagSelection   contracts        getFilteredJobs   hotJobs
impactUi            initFilters      kpis              legend
legendBands         matchesStatsCategoryFilter         normalizeRow
pending             renderDropdown   renderTags        searchTagUsers
trend               updateAllFilterOptions             updateFilterUI
viewToggle
```

**11 changed this session**, plus 6 new — all of it feature-branch work held
back from `main` deliberately (2.18.1 – 2.23.0):

```
changed: closed  contracts  detailPipelinePane  detailPlacementsPane
         detailRatePane  extensionDetail  jobDetailHtml  jobRowCells
         list  onboardingDetail  renderKpiCards
added:   channelOf  endedEarly  extStepEvidence  rateGmInline
         rateMarketPosition  sourceLink
```

Only `contracts` appears in both lists, and that is an artefact of the
extraction overshooting into the initial-state literal — not a real overlap.

---

## The finding that matters

**Only 3 of the 19 pre-existing divergences are behind the `impactUi` gate**
(`impactUi` itself, `kpis`, `updateAllFilterOptions`). The other **16 are
ungated**, meaning production MSP runs them as-is. Merging the prototype's
versions would change production MSP behaviour in all sixteen — including the
largest:

| method | changed lines | note |
|---|---|---|
| `viewToggle` | 143 | **both directions** — see below |
| `contracts` | 80 | |
| `trend` | 38 | both directions |
| `renderTags` | 27 | |
| `hotJobs` | 23 | |
| `pending` | 22 | |
| `legend` | 17 | |

### Both branches have code the other lacks

`viewToggle` is the clearest case. `main` carries 78 lines the prototype does
not — the logic that hides Per Diem and Pending on the non-MSP instance
(BULLHORN_PORT_SPEC §5, §8) so users cannot navigate to dead views. The
prototype carries 65 lines `main` does not — a `VIEWS`-based redirect that
bounces off a hidden tab before anything renders it.

Taking either side wholesale loses real work. `trend` is the same story at
smaller scale.

---

## Recommended approach

Reconcile method by method rather than merging the trees.

1. **Keep `main`'s `impactUi`.** It returns `dataSource === 'non_msp'`; the
   prototype's returns `true`. This is the branch's one deliberate divergence
   and the single line that decides whether production MSP sees the redesign.
2. **Take the prototype wholesale for the 17 gated-or-new items** (the 11
   changed this session plus the 6 new). They are either new methods nothing
   else calls or surfaces production MSP does not reach.
3. **Reconcile the 16 ungated ones by hand**, one at a time, each verified
   against the 2.5.0 production baseline (`c846d34`). `viewToggle` and `trend`
   need both sides' behaviour, not one side's.
4. **Re-run the pre-commit gate after each**, which already asserts that
   production MSP's renderers still match the baseline.

Do not do this in one commit.

---

## Before shipping

- Set `APP_VERSION` on **both** Static Web Apps. They sit at 2.18.0 while the
  branch is at 2.23.0 — deliberately, since the prototype shares app settings
  with production MSP and bumping early would make production claim a version it
  is not running.
- Optionally set `SOURCE_URL_B4`, `SOURCE_URL_VNDLY`, `SOURCE_URL_BULLHORN`,
  `SOURCE_URL_SYMPLR` to switch on the source link-outs (2.20.0). Unset, they
  render nothing.
- See also `EXTENSION_MILESTONE_SOURCES.md` for the one outstanding upstream ask.
