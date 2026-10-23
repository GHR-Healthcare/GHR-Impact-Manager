# Extension Milestones — Where Each One Comes From

Measured 2026-09-11 against the Bullhorn mirror (`DM_GeneralHealthCare_24187_EMS`)
and the warehouse (`ghrdhc`). Population throughout is **live seats ending in the
next 45 days**, which is the window the Extensions tab loads.

This exists to answer a question Dan raised: which of the extension checkpoints
can become a real process in the source systems, and which have to live in this
app because the systems have nowhere to put them.

---

## The ask: add `customTextBlock9` to the warehouse placement load

**One column, to an existing load.**

| | |
|---|---|
| **Table** | `ghrdhc.dbo.BH_PLACEMENT_RAW` |
| **Column** | `customTextBlock9` — labelled "Time Off" in Bullhorn's `FieldMaps` |
| **Source** | already present on `View_Placement` in the Bullhorn mirror |
| **Currently loaded** | `customTextBlock` 1, 2, 3, 4, 5 and 10 — 9 is skipped |

### Why it matters

This is the clinician's requested time off. On the non-MSP side we read it
straight from the mirror and it is populated on **351 of 726 live seats (48%)**.
On MSP we cannot see it at all, because MSP reads the warehouse copy and the
warehouse copy does not carry that column.

There is no second source to fall back on. VNDLY has **no time-off field of any
kind** — a search of every VNDLY table for `Time Off`, `PTO`, `Absence`, `Leave`,
`Vacation` or `Unavail` returns zero columns. So for MSP, requested time off is
information the business already has written down in Bullhorn and which the
Extensions tab is structurally unable to show.

### What it unblocks

The "Gather RTO" checkpoint currently asks a recruiter to go and collect
something that, roughly half the time, somebody already collected. With the
column loaded, MSP gets the same pre-fill non-MSP has today: the recorded text
appears beside the checkbox, and the recruiter confirms rather than re-gathers.

The app deliberately does **not** auto-complete the step from this field — 68 of
the 351 populated entries say "None needed", "N/A" or similar, and the field is
free text, so presence cannot be read as "done". It is shown, never parsed.

---

## Full milestone source map

| Milestone | Source | Coverage | Automatable |
|---|---|---|---|
| Is this an extension | `customText14` "Extension?" | 99% MSP, 100% non-MSP | **Yes** — already used |
| Client interest | `customText28` "Upcoming Extension?" | **0 of 726** | **No** |
| Gather RTO | `customTextBlock9` "Time Off" | 48% non-MSP, **0% MSP** | **Partly** — see ask above |
| Client approval | nothing | — | **No** |
| Extension sent | VNDLY `[Other Reason]` free text | 41 of 101 live VNDLY seats | **Yes**, VNDLY only |
| Finalised / accepted | end-date move in the source system | system-confirmed | **Yes** — already automatic |

### Notes on the entries above

**`customText14` is trustworthy and already in use.** Across the whole warehouse
it agrees with `PLACEMENT_DIM.IsExtension` on every single row — 8,773 Yes/True,
17,860 No/False, 25,019 null/null, no disagreements. They are the same field. MSP
therefore restates it from the dimension rather than joining for it again.

**`customText28` is the interesting negative.** Bullhorn already has a field
built for exactly the "client wants to extend" signal, and it is empty on all 726
live seats — nobody fills it in. If the team ever started using it, client
interest would stop being a manual checkbox. Recorded here as context; not
currently being pursued.

**Client approval has no field at all**, in either system. It stays manual
regardless of what happens upstream.

**Extension-sent is VNDLY-only.** VNDLY's modification feed carries free text
like "extension offer - new end date 12/26/26" and "Extension- RTO approved".
Bullhorn has no equivalent intent feed — its audit trail records the end-date
move itself, which is evidence the send already happened rather than evidence it
was sent.

---

## What the app does with all this

Team-owned checkpoints (`Confirm Client`, `Gather RTO`, `Send Extension`) are
checkboxes a person ticks, persisted per seat via `POST /workspace-state` under
scope `extension`. Acceptance stays system-confirmed and read-only.

Where a source system already knows the answer, the recorded text is shown beside
the checkbox with a "confirm to complete" prompt — see `View.extStepEvidence` in
`index.html`. Ticking stays a human's job on purpose: auto-completing on free
text would quietly advance the funnel on seats nobody has looked at.
