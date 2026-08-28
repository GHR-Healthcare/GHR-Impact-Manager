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
            NULL                                          AS outcome_reason,
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
        if (r.get('placement_count') or 0) > 0:
            r['group'] = 'FILLED'
        elif (r.get('status') or '') == 'Cancelled':
            r['group'] = 'CANCELED'
        else:
            r['group'] = 'UNFILLED'
        # Bullhorn job orders carry no cancellation-reason field, so unlike
        # Symplr there is no way to tell a cancelled order that was never
        # fillable from one GHR simply lost. All cancellations are treated as
        # non-opportunities, matching how Symplr's 'Census Dropped' is handled.
        r['opportunity'] = r['group'] != 'CANCELED'
        r['outcome_category'] = None
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
            ''                                            AS clinician,
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
            NULL                                          AS account_manager,
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
        r['outcome_category'] = (
            'Lost to competitor' if reason == 'filled by competition'
            else 'Client filled internally' if reason == 'filled by internal staff'
            else 'Demand withdrawn' if r['group'] == 'CANCELED'
            else 'Unfilled' if r['group'] == 'UNFILLED'
            else None
        )
    return rows


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

        payload = {
            'rows': rows,
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
