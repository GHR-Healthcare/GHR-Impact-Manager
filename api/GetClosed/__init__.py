import azure.functions as func
import pyodbc
import os
import json
from shared_code.auth import require_allowed_domain
from shared_code.vndly_reasons import canonical_reason, reason_category
from shared_code.data_source import (
    is_non_msp, get_bullhorn_conn, get_symplr_conn, get_appdb_conn,
)
from shared_code.bullhorn_systems import (
    build_system_case_expr, build_scope_filter, resolve_scope_client_ids,
)
from shared_code.symplr_systems import (
    build_system_case_expr as symplr_system_case_expr,
    build_scope_filter as symplr_scope_filter,
    resolve_scope_master_ids as symplr_resolve_scope,
)
from shared_code.credentials import (
    normalize as normalize_credential,
    service_line as credential_service_line,
    normalize_state,
)


B4_GHR_PREDICATE = "(o.Agency LIKE '%GHR%' OR o.Agency LIKE '%Planet Healthcare%')"
VNDLY_GHR_PREDICATE = "(w.[Vendor Name] LIKE '%GHR%' OR w.[Vendor Name] LIKE '%Planet Healthcare%')"

CLOSED_LOOKBACK_DAYS = 7

# Non-MSP runs a wider default window than MSP's 7 days. MSP's Closed stage
# asks "who won this seat", which is answerable one week at a time. Non-MSP
# asks "what is our fill rate and how fast are we filling", and a week of
# resolved Symplr orders is ~150 rows — small enough that a single slow week
# swings the rate by double digits. Thirty days is still recent enough to act on.
NON_MSP_LOOKBACK_DAYS = 30

# Bullhorn job-order statuses that represent a resolved outcome.
#
# 'Archive' is deliberately absent. It is the single largest status on the
# book — 38,062 orders in 180 days — and *not one of them has ever carried a
# placement*. They spread evenly across major systems (HUP, Cleveland Clinic,
# OSU Wexner, Strong Memorial), which is the signature of an inbound job feed
# that is ingested and auto-archived rather than worked. Counting them as
# unfilled reports a 5% fill rate against a real 52%.
#
# Live statuses (Accepting Candidates, On Hold, Credit Hold, Offered) are
# absent for the opposite reason: they have no outcome yet.
BULLHORN_RESOLVED_STATUSES = ('Placed', 'Filled', 'Closed', 'Cancelled')

# Symplr void reasons meaning the order was never a fillable opportunity —
# the shift evaporated or was logged in error. Left in the denominator they
# understate fill rate by ~7 points (63.9% raw vs 70.9% on true opportunities).
SYMPLR_NON_OPPORTUNITY_REASONS = {'scheduling error', 'census dropped'}

# The full void vocabulary, measured over a year: 13 values, closed set.
# Mapped exhaustively rather than by prefix matching so a new reason shows up
# as unmapped instead of being silently folded into a neighbouring bucket.
#
#   (blank) 680 | Internal Staff 382 | Scheduling Error 316 | Competition 234
#   Census Dropped 178 | Unable to Fill 58 | Other 19 | No Show 2 | Call-out 2
#   Temp late and sent home 2 | DNR 1 | NSNC 1 | Picked up another shift 1
#
# 'Clinician fell through' is its own bucket because those orders were booked
# and then failed — a service failure, not a sourcing failure. They still
# count as unfilled (the shift went uncovered) but conflating them with
# "no candidate found" would misdirect the conversation.
SYMPLR_REASON_CATEGORY = {
    'filled by competition':    'Lost to competitor',
    'filled by internal staff': 'Client filled internally',
    'unable to fill':           'No qualified candidate',
    'scheduling error':         'Demand withdrawn',
    'census dropped':           'Demand withdrawn',
    'no show':                  'Clinician fell through',
    'call-out':                 'Clinician fell through',
    'temp late and sent home':  'Clinician fell through',
    'picked up another shift':  'Clinician fell through',
    'nsnc':                     'Clinician fell through',
    'dnr':                      'Clinician fell through',
    # Distinct from the blank case below: someone chose 'Other', which is not
    # the same as leaving the field empty. Folding them together made
    # lossBreakdown disagree with coverage.unfilledWithoutReason by exactly
    # the count of explicit 'Other' rows.
    'other':                    'Other (recorded)',
}

# Bullhorn's own close-reason vocabulary (View_JobOrder.reasonClosed).
#
# Coverage is the caveat that matters: this field has been populated on
# 1.3%-3.8% of closed orders every year for six years. It is not an abandoned
# field, it is one almost nobody fills in — 23 'Lost to Competition' in 2026
# against 6,941 closed orders. So it labels individual rows and must never be
# used to compute a competitive *rate* on this book.
#
# 'Duplicate', 'Data Cleanup - Admin' and 'System Error' are data artifacts
# rather than lost work, and join Symplr's Scheduling Error as orders that
# never belonged in a fill-rate denominator.
BULLHORN_REASON_CATEGORY = {
    'lost to competition':      'Lost to competitor',
    'filled by hctec partners': 'Lost to competitor',
    'wash - filled internally': 'Client filled internally',
    'no interest':              'No qualified candidate',
    'cancelled by client':      'Demand withdrawn',
    'canceled by client':       'Demand withdrawn',
    'wash - lost funding':      'Demand withdrawn',
    'duplicate':                'Data artifact',
    'data cleanup - admin':     'Data artifact',
    'system error':             'Data artifact',
    'filled':                   None,   # not a loss; the order was filled
    'won:contract':             None,
    'other':                    'Other (recorded)',
}

# Reasons on either book meaning the order was never a real opportunity.
NON_OPPORTUNITY_CATEGORIES = {'Demand withdrawn', 'Data artifact'}

COMPETITIVE_LOSS_CATEGORY = 'Lost to competitor'

# How many entries each competitive-loss rollup returns. Anything dropped is
# reported alongside it rather than silently truncated.
COMPETITIVE_ROLLUP_LIMIT = 15


# Minimum seats before a role-level split is worth drawing. Below this the
# percentages swing on one placement, so the view falls back to the account.
MARKET_SHARE_MIN_SEATS = 6


def _market_share(cursor):
    """Vendor split of the live book, by account and by account+role.

    This is what the reference drew as marketSharePie -- "vendor share for
    comparable roles at the account". Its own version could not have been
    shipped: pieHeadcounts derived headcount from the job id modulo 7 and read
    start dates out of a hardcoded array. The shape was right, the data was
    scaffolding. Both VMSs carry the real thing.

    Keyed on health system rather than facility. The two sources do not agree
    on what a facility is -- B4's Facility is the hospital, VNDLY's Default
    Work Site Name is a unit ('Nursing Float-6211') -- so a facility key
    silently splits one account in two. Health system is also the level the
    question is actually asked at.

    Returns both granularities. Role level answers "how much of their RN
    demand do we hold"; the account level is the fallback where a role has too
    few seats for a percentage to mean anything.
    """
    cursor.execute('''
        WITH seats AS (
            SELECT LTRIM(RTRIM(o.Health_System))  AS hs,
                   LTRIM(RTRIM(o.Position_Type))  AS role_,
                   LTRIM(RTRIM(o.Agency))         AS vendor
            FROM dhc.B4HealthOrder o WITH (NOLOCK)
            WHERE o.Contract_Status NOT LIKE 'Closed%'
              AND o.Health_System IS NOT NULL
              AND o.Position_Type IS NOT NULL
              AND o.Agency IS NOT NULL
            UNION ALL
            SELECT LTRIM(RTRIM(w.[Health System])),
                   LTRIM(RTRIM(COALESCE(w.[Job Title], w.[Title]))),
                   LTRIM(RTRIM(w.[Vendor Name]))
            FROM dbo.STAGING_VNDLY_WORKORDERS w WITH (NOLOCK)
            WHERE w.[Current Status] = 'Active'
              AND w.[Health System] IS NOT NULL
              AND COALESCE(w.[Job Title], w.[Title]) IS NOT NULL
              AND w.[Vendor Name] IS NOT NULL
        )
        SELECT hs, role_, vendor, COUNT(*) AS seats
        FROM seats
        WHERE hs <> '' AND role_ <> '' AND vendor <> ''
        GROUP BY hs, role_, vendor
    ''')
    by_role, by_account = {}, {}
    for hs, role_, vendor, seats in cursor.fetchall():
        by_role.setdefault(f'{hs}|{role_}', {})
        by_role[f'{hs}|{role_}'][vendor] = by_role[f'{hs}|{role_}'].get(vendor, 0) + seats
        by_account.setdefault(hs, {})
        by_account[hs][vendor] = by_account[hs].get(vendor, 0) + seats

    def shape(d):
        out = {}
        for key, vendors in d.items():
            total = sum(vendors.values())
            if not total:
                continue
            ranked = sorted(vendors.items(), key=lambda kv: -kv[1])
            ghr = sum(n for v, n in ranked if _is_ghr(v))
            out[key] = {
                'total': total,
                'ghr': ghr,
                'ghrPct': round(100.0 * ghr / total, 1),
                # Vendors are returned named. The UI masks them under Redact
                # Vendor Info; withholding them here would break the panel for
                # the people allowed to see it.
                'vendors': [{'name': v, 'seats': n, 'isGhr': _is_ghr(v)} for v, n in ranked],
            }
        return out

    return {'minSeats': MARKET_SHARE_MIN_SEATS,
            'byRole': shape(by_role), 'byAccount': shape(by_account)}


def _is_ghr(vendor):
    v = (vendor or '').lower()
    return 'ghr' in v or 'planet healthcare' in v


def _b4_rows(cursor, lookback):
    """Closed outcomes still recorded in B4Health.

    Anchoring is the hard part. Awarded_Date covers every awarded contract on
    both sides, but cancellations and never-awarded seats carry no close date
    at all.

    A history fallback via HIST_B4HealthOrder + the BH RUN_DIM used to date
    some of those, but it was dropped: that history is populated by Bullhorn
    placement matching, and MSP data must come from B4 and VNDLY only.
    Deriving from a Bullhorn-sourced table in the warehouse is the same
    dependency as querying the mirror, just with an extra hop. Measured over
    the full table:

        GHR WON        11,898 rows —     0 undated
        AFFILIATE WON   7,433 rows —     0 undated
        CANCELED        7,204 rows — 6,204 undated  (86%)
        MISSED          2,043 rows — 2,015 undated  (99%)

    So B4 can date its wins and mostly cannot date its losses. Undated rows
    are excluded from the window (they cannot be placed in a week) but counted
    and reported, because silently dropping them would overstate capture rate.
    """
    cursor.execute('''
        SELECT
            'B4'                                        AS source_system,
            LTRIM(RTRIM(o.Contract_ID))                 AS id,
            LTRIM(RTRIM(ISNULL(o.First_Name, '') + ' ' + ISNULL(o.Last_Name, ''))) AS clinician,
            o.Health_System                             AS health_system,
            o.Facility                                  AS facility,
            o.Unit                                      AS unit,
            o.Position_Type                             AS role,
            o.Program                                   AS program,
            o.Contract_Status                           AS status,
            o.Agency                                    AS agency,
            CASE WHEN ''' + B4_GHR_PREDICATE + ''' THEN 'GHR' ELSE 'Affiliate' END AS source,
            CAST(o.Awarded_Date AS DATE)                AS closed_on,
            CASE WHEN o.Awarded_Date IS NOT NULL THEN 'awarded_date' ELSE NULL END AS close_date_source,
            o.Unfilled_Reason                           AS outcome_reason,
            o.Account_Manager                           AS account_manager,
            CAST(o.Start_Date AS DATE)                  AS start_date,
            TRY_CAST(o.Awarded_Rate AS DECIMAL(10,2))   AS bill_rate,
            TRY_CAST(o.Pay_Rate AS DECIMAL(10,2))       AS pay_rate,
            TRY_CAST(o.Hours_per_Peek AS DECIMAL(10,2)) AS hours_per_week
        FROM dhc.B4HealthOrder o WITH (NOLOCK)
        WHERE o.Contract_Status LIKE 'Closed%'
    ''')
    rows = [dict(zip([c[0] for c in cursor.description], r)) for r in cursor.fetchall()]
    for r in rows:
        status = r.get('status') or ''
        if status == 'Closed And Awarded':
            r['group'] = 'GHR WON' if r.get('source') == 'GHR' else 'AFFILIATE WON'
        elif status == 'Closed Not Awarded':
            r['group'] = 'MISSED'
        else:
            r['group'] = 'CANCELED'
    return rows


def _vndly_rows(cursor, lookback):
    """Closed outcomes on VNDLY.

    Better anchored than B4: a converted seat carries [Onboarded Date], and
    everything else carries [Last Modified], so every closed row can be placed
    in a week — 0 undated across all buckets.

    Statuses that are still in play (Applied, Interviewing, Offer Released,
    Ready to Onboard, Verification In Progress) are not outcomes and are
    excluded rather than bucketed.
    """
    cursor.execute('''
        SELECT
            'VNDLY'                                      AS source_system,
            CAST(w.WOSystemKey AS NVARCHAR(50))           AS id,
            LTRIM(RTRIM(ISNULL(w.[Contractor First Name], '') + ' ' + ISNULL(w.[Contractor Last Name], ''))) AS clinician,
            w.[Health System]                             AS health_system,
            COALESCE(w.[Default Work Site Name], w.[Job Site]) AS facility,
            w.[Organization Unit]                         AS unit,
            COALESCE(w.[Job Title], w.[Title])            AS role,
            w.[Busines Unit - Name]                       AS program,
            w.[Current Status]                            AS status,
            w.[Vendor Name]                               AS agency,
            CASE WHEN ''' + VNDLY_GHR_PREDICATE + ''' THEN 'GHR' ELSE 'Affiliate' END AS source,
            COALESCE(TRY_CAST(w.[Onboarded Date] AS DATE),
                     TRY_CAST(w.[Last Modified] AS DATE)) AS closed_on,
            CASE WHEN TRY_CAST(w.[Onboarded Date] AS DATE) IS NOT NULL THEN 'onboarded_date'
                 WHEN TRY_CAST(w.[Last Modified] AS DATE) IS NOT NULL  THEN 'last_modified'
                 ELSE NULL END                            AS close_date_source,
            w.[End Reason]                                AS outcome_reason,
            w.[Resource Manager]                          AS account_manager,
            TRY_CAST(w.[Start Date] AS DATE)              AS start_date,
            TRY_CAST(w.[Bill Rate] AS DECIMAL(10,2))      AS bill_rate,
            TRY_CAST(w.[Pay Rate] AS DECIMAL(10,2))       AS pay_rate,
            NULL                                          AS hours_per_week,
            CASE WHEN TRY_CAST(w.[Onboarded Date] AS DATE) IS NOT NULL THEN 1 ELSE 0 END AS converted
        FROM dbo.STAGING_VNDLY_WORKORDERS w WITH (NOLOCK)
        WHERE w.[Current Status] NOT IN
              ('Applied', 'Interviewing', 'Offer Released', 'Ready to Onboard',
               'Verification In Progress')
    ''')
    rows = [dict(zip([c[0] for c in cursor.description], r)) for r in cursor.fetchall()]
    for r in rows:
        status = r.get('status') or ''
        if r.get('converted') and status in ('Active', 'Ended'):
            r['group'] = 'GHR WON' if r.get('source') == 'GHR' else 'AFFILIATE WON'
        elif status in ('Cancelled', 'Offer Declined', 'Withdrawn'):
            r['group'] = 'CANCELED'
        else:
            # Rejected / Ended by Job Close — the seat went elsewhere.
            r['group'] = 'MISSED'
        r.pop('converted', None)
        raw = r.get('outcome_reason')
        r['outcome_reason'] = canonical_reason(raw)
        r['outcome_category'] = reason_category(raw)
    return rows


def _bullhorn_closed_rows(cursor, app_conn, lookback):
    """Resolved Bullhorn job orders — the non-MSP Closed stage.

    Two things here are not what they look like.

    First, **fill is placement existence, not status**. Bullhorn carries a
    'Filled' status, and it lies: of 269 orders marked Filled in 180 days,
    only 12 have a placement attached. 'Placed' is the reliable one (2,204 of
    2,279). Rather than trust either label, this derives the outcome from
    whether a placement actually exists, which is also what makes the close
    date trustworthy.

    Second, the close anchor is `dateLastModified`, not `dateClosed`.
    `dateClosed` is present in the schema and populated on exactly zero
    resolved orders. `dateLastModified` covers 100%, and a filled order
    anchors on its placement date instead, which is the more truthful moment.
    """
    scope_ids = resolve_scope_client_ids(cursor, app_conn)
    system_case = build_system_case_expr('jo.clientCorporationID')
    scope_filter = build_scope_filter('jo.clientCorporationID', client_ids=scope_ids)
    status_list = ', '.join("'" + s + "'" for s in BULLHORN_RESOLVED_STATUSES)

    cursor.execute(f'''
        SELECT
            'Bullhorn'                                    AS source_system,
            CAST(jo.jobOrderID AS NVARCHAR(50))           AS id,
            ISNULL(pl.clinician, '')                      AS clinician,
            ({system_case})                               AS health_system,
            ISNULL(cc.name, '')                           AS facility,
            NULL                                          AS unit,
            jo.title                                      AS role,
            cat.cred                                      AS credential_raw,
            spec.name                                     AS specialty_raw,
            jo.status                                     AS status,
            CAST(jo.dateAdded AS DATE)                    AS opened_on,
            CAST(COALESCE(pl.first_placed, jo.dateLastModified) AS DATE) AS closed_on,
            CASE WHEN pl.n > 0 THEN 'placement' ELSE 'last_modified' END AS close_date_source,
            NULLIF(LTRIM(RTRIM(jo.reasonClosed)), '')     AS outcome_reason,
            LTRIM(RTRIM(ISNULL(u.firstName, '') + ' ' + ISNULL(u.lastName, ''))) AS account_manager,
            CAST(jo.startDate AS DATE)                    AS start_date,
            TRY_CAST(jo.clientBillRate AS DECIMAL(10,2))  AS bill_rate,
            TRY_CAST(jo.payRate AS DECIMAL(10,2))         AS pay_rate,
            TRY_CAST(jo.hoursPerWeek AS DECIMAL(10,2))    AS hours_per_week,
            ISNULL(pl.n, 0)                               AS placement_count,
            CASE WHEN pl.n > 0
                 THEN CAST(DATEDIFF(HOUR, jo.dateAdded, pl.first_placed) AS FLOAT) / 24.0
            END                                           AS days_to_fill,
            jo.state                                      AS region
        FROM dbo.View_JobOrder jo WITH (NOLOCK)
        LEFT JOIN dbo.View_ClientCorporation cc WITH (NOLOCK)
               ON cc.clientCorporationID = jo.clientCorporationID
        -- build_system_case_expr() rolls facilities up to their parent
        -- corporation, so its generated CASE references `pcc`. Without this
        -- join the query fails to bind rather than silently mis-grouping.
        LEFT JOIN dbo.View_ClientCorporation pcc WITH (NOLOCK)
               ON pcc.clientCorporationID = cc.parentClientCorporationID
        LEFT JOIN dbo.View_CorporateUser u WITH (NOLOCK)
               ON u.corporateUserID = jo.ownerID
        OUTER APPLY (
            SELECT COUNT(*) AS n,
                   MIN(p.dateAdded) AS first_placed,
                   MIN(LTRIM(RTRIM(ISNULL(cnd.firstName, '') + ' ' + ISNULL(cnd.lastName, '')))) AS clinician
            FROM dbo.View_Placement p WITH (NOLOCK)
            LEFT JOIN dbo.View_Candidate cnd WITH (NOLOCK) ON cnd.candidateID = p.candidateID
            WHERE p.jobOrderID = jo.jobOrderID
        ) pl
        OUTER APPLY (
            SELECT TOP 1 COALESCE(NULLIF(cty.name, ''), cty.occupation) AS cred
            FROM dbo.JobOrderCategories jc WITH (NOLOCK)
            INNER JOIN dbo.Category cty WITH (NOLOCK) ON jc.categoryID = cty.categoryID
            WHERE jc.jobOrderID = jo.jobOrderID AND jc.isDeleted = 0 AND cty.isDeleted = 0
        ) cat
        OUTER APPLY (
            SELECT TOP 1 sp.name
            FROM dbo.JobOrderSpecialties js WITH (NOLOCK)
            INNER JOIN dbo.Specialty sp WITH (NOLOCK) ON js.specialtyID = sp.specialtyID
            WHERE js.jobOrderID = jo.jobOrderID AND js.isDeleted = 0 AND sp.isDeleted = 0
        ) spec
        WHERE jo.isDeleted = 0
          AND jo.status IN ({status_list})
          AND {scope_filter}
          AND COALESCE(pl.first_placed, jo.dateLastModified)
              >= DATEADD(DAY, -{int(abs(lookback))}, GETDATE())
    ''')
    cols = [c[0] for c in cursor.description]
    rows = [dict(zip(cols, r)) for r in cursor.fetchall()]
    for r in rows:
        reason = (r.get('outcome_reason') or '').strip().lower()
        # An unmapped reason keeps its own label rather than being bucketed,
        # so a value someone starts writing tomorrow stays visible.
        category = (BULLHORN_REASON_CATEGORY.get(reason, (r.get('outcome_reason') or '').strip())
                    if reason else None)

        if (r.get('placement_count') or 0) > 0:
            r['group'] = 'FILLED'
        elif category in NON_OPPORTUNITY_CATEGORIES or (r.get('status') or '') == 'Cancelled':
            # Cancelled orders are non-opportunities by default. Where a
            # reason exists it can also promote an order *out* of the
            # denominator — a 'Duplicate' closed order was never real work —
            # but with the field populated ~2% of the time, status remains
            # the primary signal.
            r['group'] = 'CANCELED'
        else:
            r['group'] = 'UNFILLED'
        r['opportunity'] = r['group'] != 'CANCELED'
        r['outcome_category'] = category if r['group'] != 'FILLED' else None
    return rows


def _symplr_closed_rows(cursor, app_conn, lookback):
    """Resolved Symplr shift orders — the non-MSP Closed stage.

    Velocity anchors on `BookedByDT`, not `date_start`. Symplr orders are
    routinely entered *after* the shift has begun (same-day and retroactive
    logging), so date_entered -> date_start averages **negative four days**
    and is not a fill measure at all. `BookedByDT` is populated on 100% of
    filled orders and never precedes entry.

    `voidreason` is the other reason this source is worth having: it
    separates orders GHR lost (Filled by Competition, Unable to Fill, Filled
    by Internal Staff) from orders that stopped existing (Scheduling Error,
    Census Dropped). Only the former belong in a fill-rate denominator.
    """
    symplr_master_ids = symplr_resolve_scope(app_conn, symplr_cursor=cursor)
    sys_case = symplr_system_case_expr('lt.clientid')
    scope = symplr_scope_filter('lt.clientid', master_ids=symplr_master_ids)

    cursor.execute(f'''
        SELECT
            'Symplr'                                      AS source_system,
            CAST(lt.lt_orderid AS NVARCHAR(50))           AS id,
            -- Resolves on 100% of filled orders; blank on voids, which never
            -- had a worker assigned.
            NULLIF(LTRIM(RTRIM(ISNULL(t.firstname, '') + ' ' + ISNULL(t.lastname, ''))), '') AS clinician,
            ({sys_case})                                  AS health_system,
            pc.clientname                                 AS facility,
            NULL                                          AS unit,
            LTRIM(RTRIM(ISNULL(lt.nursetype, '') + ' — ' + ISNULL(lt.specialty, ''))) AS role,
            lt.nursetype                                  AS credential_raw,
            lt.specialty                                  AS specialty_raw,
            lt.status                                     AS status,
            CAST(lt.date_entered AS DATE)                 AS opened_on,
            CAST(COALESCE(lt.BookedByDT, lt.voiddt, lt.datetimemodified) AS DATE) AS closed_on,
            CASE WHEN lt.BookedByDT IS NOT NULL THEN 'booked'
                 WHEN lt.voiddt     IS NOT NULL THEN 'voided'
                 ELSE 'modified' END                      AS close_date_source,
            NULLIF(LTRIM(RTRIM(lt.voidreason)), '')       AS outcome_reason,
            NULLIF(LTRIM(RTRIM(ISNULL(bu.firstname, '') + ' ' + ISNULL(bu.lastname, ''))), '') AS account_manager,
            CAST(lt.date_start AS DATE)                   AS start_date,
            -- lt_order carries no rate: only ratecode / rateSheetID, which are
            -- keys into a rate sheet this app does not read. Left null rather
            -- than guessed, so the UI shows a gap instead of a wrong number.
            NULL                                          AS bill_rate,
            NULL                                          AS pay_rate,
            COALESCE(
                NULLIF(TRY_CAST(lt.HoursPerWeek AS DECIMAL(10,2)), 0),
                CAST((DATEDIFF(MINUTE, TRY_CAST(lt.shiftstarttime AS time),
                                       TRY_CAST(lt.shiftendtime AS time))
                      + CASE WHEN TRY_CAST(lt.shiftendtime AS time)
                                  <= TRY_CAST(lt.shiftstarttime AS time)
                             THEN 1440 ELSE 0 END) / 60.0
                     * TRY_CAST(lt.DaysPerWeek AS FLOAT) AS DECIMAL(10,2))
            )                                             AS hours_per_week,
            CASE WHEN lt.status = 'filled' THEN 1 ELSE 0 END AS placement_count,
            CASE WHEN lt.status = 'filled' AND lt.BookedByDT IS NOT NULL
                 THEN CAST(DATEDIFF(HOUR, lt.date_entered, lt.BookedByDT) AS FLOAT) / 24.0
            END                                           AS days_to_fill,
            pc.state                                      AS region
        FROM dbo.lt_order lt WITH (NOLOCK)
        LEFT JOIN dbo.profile_client pc ON lt.clientid = pc.recordid
        LEFT JOIN dbo.profile_client m  ON pc.MasterClientID = m.recordid
        LEFT JOIN dbo.profile_temp t WITH (NOLOCK) ON t.recordid = lt.tempid
        LEFT JOIN dbo.users bu WITH (NOLOCK) ON bu.userid = lt.BookedByUserID
        WHERE lt.status IN ('filled', 'void')
          AND {scope}
          AND COALESCE(lt.BookedByDT, lt.voiddt, lt.datetimemodified)
              >= DATEADD(DAY, -{int(abs(lookback))}, GETDATE())
    ''')
    cols = [c[0] for c in cursor.description]
    rows = [dict(zip(cols, r)) for r in cursor.fetchall()]
    for r in rows:
        reason = (r.get('outcome_reason') or '').strip().lower()
        is_void = (r.get('status') or '') == 'void'
        # 40% of voids (150 of 371 in a 30-day window) carry no reason at all.
        # They fall through to UNFILLED, which counts them against fill rate.
        # That is the conservative reading — an unexplained void is assumed to
        # be a miss rather than assumed away — but it is reported in coverage
        # so the rate is never quoted without it.
        r['reason_missing'] = is_void and not reason
        if (r.get('status') or '') == 'filled':
            r['group'] = 'FILLED'
        elif reason in SYMPLR_NON_OPPORTUNITY_REASONS:
            r['group'] = 'CANCELED'
        else:
            r['group'] = 'UNFILLED'
        r['opportunity'] = r['group'] != 'CANCELED'
        if not is_void:
            r['outcome_category'] = None
        elif not reason:
            r['outcome_category'] = 'Reason not recorded'
        else:
            # An unmapped reason keeps its own label rather than being bucketed,
            # so a value Symplr starts writing tomorrow is visible instead of
            # disappearing into 'Other'.
            r['outcome_category'] = SYMPLR_REASON_CATEGORY.get(
                reason, (r.get('outcome_reason') or '').strip())
    return rows


def _competitive_rollups(rows):
    """Where GHR is losing work to competing agencies.

    Both books contribute, but not equally, and the difference is not a
    detail. Symplr records a void reason on ~60% of voids; Bullhorn's
    `reasonClosed` has run at 1.3%-3.8% for six years. So a Bullhorn
    competitive loss is real when present and close to meaningless when
    absent, and the counts here are a floor on that book rather than a
    measurement. `reasonCoverage` carries the per-source rate so the number
    is never read as complete.
    """
    losses = [r for r in rows if r.get('outcome_category') == COMPETITIVE_LOSS_CATEGORY]

    def rollup(key):
        counts = {}
        for r in losses:
            name = (r.get(key) or '').strip() or '(unspecified)'
            counts[name] = counts.get(name, 0) + 1
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return {
            'top': [{'name': n, 'count': c} for n, c in ranked[:COMPETITIVE_ROLLUP_LIMIT]],
            # Never truncate silently — a top-15 that hides 40 more facilities
            # reads as "these are the facilities" when it isn't.
            'omitted': max(0, len(ranked) - COMPETITIVE_ROLLUP_LIMIT),
            'distinct': len(ranked),
        }

    # Share of true opportunities lost specifically to a competitor, which is
    # the number that says whether GHR is being outbid or simply short of
    # candidates. Denominator matches the fill-rate denominator.
    opportunities = sum(1 for r in rows if r.get('opportunity'))

    # What share of non-filled orders carry any reason at all, per source.
    # Without this the competitive counts look like a measurement of the whole
    # book instead of a floor drawn from whatever was recorded.
    reason_coverage = {}
    for r in rows:
        if r.get('group') == 'FILLED':
            continue
        src = r.get('source_system') or '?'
        stat = reason_coverage.setdefault(src, {'unfilled': 0, 'withReason': 0})
        stat['unfilled'] += 1
        if (r.get('outcome_reason') or '').strip():
            stat['withReason'] += 1
    for stat in reason_coverage.values():
        stat['pct'] = (round(100.0 * stat['withReason'] / stat['unfilled'], 1)
                       if stat['unfilled'] else None)

    return {
        'total': len(losses),
        'sources': sorted({r.get('source_system') for r in losses if r.get('source_system')}),
        'reasonCoverage': reason_coverage,
        'pctOfOpportunities': (
            round(100.0 * len(losses) / opportunities, 1) if opportunities else None
        ),
        'byHealthSystem': rollup('health_system'),
        'byFacility': rollup('facility'),
        'byCredential': rollup('profession'),
        'byServiceLine': rollup('service_line'),
    }


def _non_msp_payload(lookback):
    """Closed stage for non-MSP: historical orders with a fill outcome.

    The MSP notion of capture rate — GHR's share of a seat against affiliate
    agencies — has no meaning here. Non-MSP is GHR's direct book, with
    'GHR' hardcoded as the only agency, so there is no share to compute.
    What the meeting needs instead is fill rate and velocity, which both
    books support.
    """
    rows, errors = [], []

    for label, getter, fn in (
        ('Bullhorn', get_bullhorn_conn, _bullhorn_closed_rows),
        ('Symplr', get_symplr_conn, _symplr_closed_rows),
    ):
        conn = None
        app_conn = None
        try:
            conn = getter()
            if conn is None:
                errors.append(f'{label}: not configured')
                continue
            cursor = conn.cursor()
            cursor.execute('SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED')
            app_conn = get_appdb_conn()
            rows.extend(fn(cursor, app_conn, lookback))
        except Exception as e:
            print(f'Closed(non_msp): {label} branch failed: {e}')
            import traceback
            traceback.print_exc()
            errors.append(f'{label}: {e}')
        finally:
            if app_conn is not None:
                app_conn.close()
            if conn is not None:
                conn.close()

    return _aggregate_non_msp(rows, lookback, errors)


def _aggregate_non_msp(rows, lookback, errors):
    """Normalize and roll up already-fetched rows.

    Split out from the fetch so the arithmetic can be exercised against real
    rows without a database handle — the jsdom harness on this project has a
    long history of passing while silently skipping the whole data path.
    """
    for r in rows:
        for k in ('closed_on', 'opened_on', 'start_date'):
            if r.get(k) is not None and hasattr(r[k], 'isoformat'):
                r[k] = r[k].isoformat()
        for k in ('bill_rate', 'pay_rate', 'hours_per_week', 'days_to_fill'):
            if r.get(k) is not None:
                r[k] = round(float(r[k]), 2)
        raw_cred = r.pop('credential_raw', None)
        r['profession'] = normalize_credential(raw_cred)
        r['service_line'] = credential_service_line(raw_cred)
        r['specialty'] = (r.pop('specialty_raw', None) or '')
        r['region'] = normalize_state(r.get('region'))
        r['agency'] = 'GHR'
        rate, hrs = r.get('bill_rate'), r.get('hours_per_week')
        r['value_13wk'] = round(rate * hrs * 13, 2) if rate and hrs else None

    rows.sort(key=lambda r: r.get('closed_on') or '', reverse=True)

    counts = {}
    for r in rows:
        counts[r['group']] = counts.get(r['group'], 0) + 1

    loss_breakdown = {}
    for r in rows:
        cat = r.get('outcome_category')
        if cat:
            loss_breakdown[cat] = loss_breakdown.get(cat, 0) + 1

    filled = counts.get('FILLED', 0)
    unfilled = counts.get('UNFILLED', 0)
    denominator = filled + unfilled
    fill_times = [r['days_to_fill'] for r in rows if r.get('days_to_fill') is not None]

    payload = {
        'rows': rows,
        'coverage': {
            'windowDays': abs(lookback),
            'counts': counts,
            # Fill rate deliberately excludes CANCELED. Those orders stopped
            # existing (Scheduling Error, Census Dropped, cancelled reqs)
            # rather than going unfilled, and leaving them in the denominator
            # understates the rate by roughly seven points.
            'fillRatePct': round(100.0 * filled / denominator, 1) if denominator else None,
            'fillRateDenominator': denominator,
            'excludedNoOpportunity': counts.get('CANCELED', 0),
            # Voids with no recorded reason, counted as unfilled. If this is a
            # large share of the denominator the fill rate is a floor, not a
            # point estimate.
            'unfilledWithoutReason': sum(1 for r in rows if r.get('reason_missing')),
            # Why orders went unfilled, not just how many.
            'lossBreakdown': loss_breakdown,
            'competitive': _competitive_rollups(rows),
            'avgDaysToFill': round(sum(fill_times) / len(fill_times), 2) if fill_times else None,
            'medianDaysToFill': (
                round(sorted(fill_times)[len(fill_times) // 2], 2) if fill_times else None
            ),
            # Symplr orders carry no rate, so 13-week value is Bullhorn-only.
            'rowsWithValue': sum(1 for r in rows if r.get('value_13wk') is not None),
            'errors': errors,
        },
    }
    print(f"Closed(non_msp): {len(rows)} rows in {abs(lookback)}d {counts}; "
          f"fill {payload['coverage']['fillRatePct']}% of {denominator}; "
          f"errors: {errors or 'none'}")
    return payload


def main(req: func.HttpRequest) -> func.HttpResponse:
    auth_error = require_allowed_domain(req)
    if auth_error:
        return auth_error

    non_msp = is_non_msp()
    default_lookback = NON_MSP_LOOKBACK_DAYS if non_msp else CLOSED_LOOKBACK_DAYS
    try:
        lookback = int(req.params.get('days') or default_lookback)
    except (TypeError, ValueError):
        lookback = default_lookback

    if non_msp:
        try:
            return func.HttpResponse(
                json.dumps(_non_msp_payload(lookback), default=str),
                mimetype='application/json', status_code=200)
        except Exception as e:
            print(f'Closed(non_msp) error: {e}')
            import traceback
            traceback.print_exc()
            return func.HttpResponse(
                json.dumps({'error': str(e)}),
                mimetype='application/json', status_code=500)

    conn = None
    try:
        conn = pyodbc.connect(
            f"DRIVER={{ODBC Driver 17 for SQL Server}};"
            f"SERVER={os.environ['DB_HOST']};"
            f"DATABASE={os.environ['POSITIONS_DB']};"
            f"UID={os.environ['DB_USER']};"
            f"PWD={os.environ['DB_PASSWORD']};"
            f"TrustServerCertificate=yes"
        )
        cursor = conn.cursor()
        cursor.execute('SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED')

        import datetime
        cutoff = datetime.date.today() - datetime.timedelta(days=abs(lookback))

        all_rows, errors = [], []
        for label, fn in (('B4', _b4_rows), ('VNDLY', _vndly_rows)):
            try:
                all_rows.extend(fn(cursor, lookback))
            except Exception as e:
                print(f"Closed: {label} branch failed: {e}")
                import traceback
                traceback.print_exc()
                errors.append(f'{label}: {e}')

        # Undated rows cannot be placed in a week. Track them per source and
        # bucket instead of dropping them silently — B4 dates its wins but not
        # its losses, so an unreported exclusion would inflate capture rate.
        undated = {}
        rows = []
        for r in all_rows:
            for k in ('closed_on', 'start_date'):
                if r.get(k) is not None and hasattr(r[k], 'isoformat'):
                    r[k] = r[k].isoformat()
            for k in ('bill_rate', 'pay_rate', 'hours_per_week'):
                if r.get(k) is not None:
                    r[k] = float(r[k])
            # See note in GetExtensions: MSP fee split, not gross margin.
            bill, pay = r.get('bill_rate'), r.get('pay_rate')
            r['agency_receipt_pct'] = round(pay / bill * 100, 1) if bill and pay and bill > 0 else None
            r['margin_pct'] = None
            rate, hrs = r.get('bill_rate'), r.get('hours_per_week')
            r['value_13wk'] = round(rate * hrs * 13, 2) if rate and hrs else None

            if not r.get('closed_on'):
                key = f"{r['source_system']}/{r['group']}"
                undated[key] = undated.get(key, 0) + 1
                continue
            if r['closed_on'] >= cutoff.isoformat():
                rows.append(r)

        rows.sort(key=lambda r: r.get('closed_on') or '', reverse=True)

        counts = {}
        for r in rows:
            counts[r['group']] = counts.get(r['group'], 0) + 1

        # Vendor share of the live book, for the row-detail pie. Best effort:
        # the stage is perfectly usable without it, so a failure here must not
        # take the whole response down.
        market_share = {}
        try:
            market_share = _market_share(cursor)
        except Exception as e:
            print(f'Closed: market share rollup failed: {e}')
            errors.append(f'marketShare: {e}')

        payload = {
            'rows': rows,
            'marketShare': market_share,
            'coverage': {
                'windowDays': abs(lookback),
                'counts': counts,
                # Rows excluded because the source records no close date.
                # Heavily skewed to B4 losses, so capture rate computed from
                # `counts` alone reads high — the UI is expected to surface this.
                'undatedExcluded': undated,
                'errors': errors,
            },
        }
        print(f"Closed: {len(rows)} rows in {abs(lookback)}d {counts}; "
              f"undated excluded {undated}; errors: {errors or 'none'}")
        return func.HttpResponse(
            json.dumps(payload, default=str),
            mimetype="application/json",
            status_code=200,
        )
    except Exception as e:
        print(f"Closed error: {e}")
        import traceback
        traceback.print_exc()
        return func.HttpResponse(
            json.dumps({'error': str(e)}),
            mimetype="application/json",
            status_code=500,
        )
    finally:
        if conn is not None:
            conn.close()
