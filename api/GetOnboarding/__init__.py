import azure.functions as func
import pyodbc
import os
import json
from shared_code.auth import require_allowed_domain
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
)
from shared_code.vndly_reasons import canonical_reason, reason_category


B4_GHR_PREDICATE = "(o.Agency LIKE '%GHR%' OR o.Agency LIKE '%Planet Healthcare%')"
VNDLY_GHR_PREDICATE = "(w.[Vendor Name] LIKE '%GHR%' OR w.[Vendor Name] LIKE '%Planet Healthcare%')"

# How far back/forward the Onboarding stage looks around today. The stage is
# about accepted offers moving toward an actual start, so it needs a window on
# both sides: recent starts that already happened (did they stick?) and
# upcoming ones (are they still moving?).
ONBOARDING_LOOKBACK_DAYS = 30
ONBOARDING_LOOKAHEAD_DAYS = 45

B4_CANCELLED_STATUSES = ('Closed And Cancelled', 'Closed Not Awarded')
# VNDLY terminal statuses that mean the seat never converted to a start.
VNDLY_CANCELLED_STATUSES = ('Rejected', 'Withdrawn', 'Offer Declined', 'Ended in Error')

# Non-MSP equivalents. A placement that cancelled inside the onboarding
# window is a fallout and belongs in the stage, not filtered out of it.
BULLHORN_CANCELLED_STATUSES = ('Cancellation', 'Termination')


def _b4_rows(cursor, lookback, lookahead, include_affiliate):
    """B4 seats in the window.

    B4 records whether a start was delayed and why — Delayed_Starts plus
    Delayed_Starts_Reasons (Health System Request, Vendor/Contractor Request,
    Compliance Incomplete) — natively, with no Bullhorn dependency.

    It does not record how far a start slipped or how many times it moved.
    That previously came from HIST_B4HealthOrder, which is populated by
    Bullhorn placement matching; MSP reads B4 and VNDLY only, so day-count and
    move-count return null rather than being reconstructed from a
    Bullhorn-sourced table.
    """
    agency_filter = '' if include_affiliate else f'AND {B4_GHR_PREDICATE}'
    cursor.execute(f'''
        WITH hist AS (
            SELECT LTRIM(RTRIM(Contract_ID))  AS cid,
                   COUNT(DISTINCT Start_Date) AS distinct_starts,
                   MIN(Start_Date)            AS first_start,
                   MAX(Start_Date)            AS last_start
            FROM dbo.HIST_B4HealthOrder WITH (NOLOCK)
            WHERE Start_Date IS NOT NULL
            GROUP BY LTRIM(RTRIM(Contract_ID))
        )
        SELECT
            'B4'                                        AS source_system,
            LTRIM(RTRIM(o.Contract_ID))                 AS id,
            LTRIM(RTRIM(ISNULL(o.First_Name, '') + ' ' + ISNULL(o.Last_Name, ''))) AS clinician,
            o.Health_System                             AS health_system,
            o.Facility                                  AS facility,
            o.Unit                                      AS unit,
            o.Position_Type                             AS role,
            o.Program                                   AS program,
            CASE WHEN {B4_GHR_PREDICATE} THEN 'GHR' ELSE 'Affiliate' END AS source,
            o.Agency                                    AS agency,
            o.Contract_Status                           AS contract_status,
            CAST(o.Start_Date AS DATE)                  AS current_start,
            -- The earliest start ever recorded for this contract, from B4's
            -- own history table.
            CAST(h.first_start AS DATE)                 AS planned_start,
            CAST(o.End_Date AS DATE)                    AS end_date,
            NULL                                        AS onboarded_date,
            -- HIST_B4HealthOrder is a history OF dhc.B4HealthOrder — all
            -- 6,440 of its contracts exist there, and it carries affiliate
            -- rows (267 contracts) as well as GHR. It was dropped on the
            -- reading that it was Bullhorn-derived. It is not: the BH-to-B4
            -- matching process also writes this history and tags rows with
            -- match metadata, but the rows are B4's own.
            --
            -- Only the history itself is needed here. Turning RUN_ID into a
            -- date requires the BH-named RUN_DIM; counting distinct
            -- Start_Date does not, so this stays inside B4.
            CASE WHEN h.cid IS NULL THEN NULL ELSE
                (h.distinct_starts - 1)
                -- A start changed since the last load shows as one distinct
                -- value that no longer matches the live row.
                + CASE WHEN o.Start_Date <> h.last_start THEN 1 ELSE 0 END
            END                                         AS move_count,
            CASE WHEN o.Delayed_Starts = 'Yes' THEN 1 ELSE 0 END AS delay_flagged,
            CASE WHEN h.cid IS NULL THEN NULL
                 ELSE DATEDIFF(DAY, h.first_start, o.Start_Date) END AS days_delayed,
            DATEDIFF(DAY, CAST(GETDATE() AS DATE), o.Start_Date) AS days_until_start,
            o.Delayed_Starts                            AS delayed_flag,
            o.Delayed_Starts_Reasons                    AS delay_reason,
            o.Account_Manager                           AS account_manager,
            TRY_CAST(o.Awarded_Rate AS DECIMAL(10,2))   AS bill_rate,
            TRY_CAST(o.Pay_Rate AS DECIMAL(10,2))       AS pay_rate,
            TRY_CAST(o.Hours_per_Peek AS DECIMAL(10,2)) AS hours_per_week
        FROM dhc.B4HealthOrder o WITH (NOLOCK)
        LEFT JOIN hist h ON h.cid = LTRIM(RTRIM(o.Contract_ID))
        WHERE o.Start_Date BETWEEN DATEADD(DAY, ?, CAST(GETDATE() AS DATE))
                               AND DATEADD(DAY, ?, CAST(GETDATE() AS DATE))
            {agency_filter}
    ''', -abs(lookback), lookahead)
    return [dict(zip([c[0] for c in cursor.description], r)) for r in cursor.fetchall()]


def _vndly_rows(cursor, lookback, lookahead, include_affiliate):
    """VNDLY work orders.

    VNDLY records modifications explicitly, with a categorised reason, rather
    than requiring snapshot inference — and unlike the B4 history it covers
    affiliate vendors too.

    There is still no Original Start Date on the work order itself — it carries
    [Original End Date] but no start equivalent. What we do have is
    STAGING_VNDLY_JOBS, which holds the start as of the APPLICATION and keeps
    it when the work order's start later moves. That gives a planned start for
    the seats the jobs feed covers (~82% of Active/Ended, far less of
    'Ended by Job Close'), and days_delayed is measured against it.

    move_count still counts flagged delay events, and is a FLOOR rather than a
    true count: measured against live data, 8 work orders moved to a later
    start with no 'Delayed Start' reason recorded at all. So a start can move
    without the modifications feed saying so, and the UI should not present
    move_count as authoritative.
    """
    vendor_filter = '' if include_affiliate else f'AND {VNDLY_GHR_PREDICATE}'
    cursor.execute(f'''
        WITH mods AS (
            SELECT
                WOSystemKey                                          AS wo,
                SUM(CASE WHEN [Reason for Modification] LIKE 'Delayed Start%'
                         THEN 1 ELSE 0 END)                          AS delay_events,
                MAX(CASE WHEN [Reason for Modification] LIKE 'Delayed Start%'
                         THEN [Reason for Modification] END)         AS delay_reason,
                COUNT(*)                                             AS total_mods
            FROM dbo.STAGING_VNDLY_WORKODER_MODIFICATIONS WITH (NOLOCK)
            GROUP BY WOSystemKey
        ),
        jobs AS (
            -- STAGING_VNDLY_JOBS is one row per (Health System, Work Order Id)
            -- — verified unique against live data, 748 of 748. It was being
            -- joined on [Job Id] alone, which fans out across every seat on a
            -- requisition; the MAX() that hid the fan-out also meant
            -- hours_per_week was the largest value on the whole req rather than
            -- this seat's. 177 reqs carry differing hours across their seats,
            -- so that was wrong, not merely imprecise.
            --
            -- [Start Date] here is the start as of the application, and it is
            -- retained when the work order's own start later moves — so it
            -- serves as a PLANNED start. Measured against live data on
            -- Active/Ended single-seat reqs: 453 of 469 with no delay logged
            -- match exactly (96.6%), while 6 of 8 with a delay logged show a
            -- later start. Differences are almost all whole shift-weeks
            -- (7/14/21/28/35 days), i.e. real movement rather than drift.
            --
            -- Not called "original": it is the start at application time, not
            -- necessarily the first ever scheduled.
            SELECT [Health System] AS hs,
                   [Work Order Id]  AS wo_id,
                   MAX(TRY_CAST([Standard Hours Per Week] AS DECIMAL(10,2))) AS hours_per_week,
                   MAX(TRY_CAST([Start Date] AS DATE))                       AS planned_start
            FROM dbo.STAGING_VNDLY_JOBS WITH (NOLOCK)
            WHERE [Work Order Id] IS NOT NULL
            GROUP BY [Health System], [Work Order Id]
        )
        SELECT
            'VNDLY'                                     AS source_system,
            CAST(w.WOSystemKey AS NVARCHAR(50))          AS id,
            LTRIM(RTRIM(ISNULL(w.[Contractor First Name], '') + ' ' + ISNULL(w.[Contractor Last Name], ''))) AS clinician,
            w.[Health System]                            AS health_system,
            COALESCE(w.[Default Work Site Name], w.[Job Site]) AS facility,
            w.[Organization Unit]                        AS unit,
            COALESCE(w.[Job Title], w.[Title])           AS role,
            w.[Busines Unit - Name]                      AS program,
            CASE WHEN {VNDLY_GHR_PREDICATE} THEN 'GHR' ELSE 'Affiliate' END AS source,
            w.[Vendor Name]                              AS agency,
            w.[Current Status]                           AS contract_status,
            TRY_CAST(w.[Start Date] AS DATE)             AS current_start,
            j.planned_start                              AS planned_start,
            TRY_CAST(w.[End Date] AS DATE)               AS end_date,
            TRY_CAST(w.[Onboarded Date] AS DATE)         AS onboarded_date,
            ISNULL(m.delay_events, 0)                    AS move_count,
            -- The modifications feed exists for every work order, so no row
            -- genuinely means no flagged delay (unlike B4, where a missing
            -- history row means unmatched).
            -- Slip against the planned start, where the jobs feed covers this
            -- seat. Null where it does not (~18% of Active/Ended work orders,
            -- and most 'Ended by Job Close'), so the UI can say "not tracked"
            -- rather than imply zero movement.
            DATEDIFF(DAY, j.planned_start, TRY_CAST(w.[Start Date] AS DATE)) AS days_delayed,
            DATEDIFF(DAY, CAST(GETDATE() AS DATE), TRY_CAST(w.[Start Date] AS DATE)) AS days_until_start,
            CASE WHEN ISNULL(m.delay_events, 0) > 0 THEN 'Yes' ELSE 'No' END AS delayed_flag,
            CASE WHEN ISNULL(m.delay_events, 0) > 0 THEN 1 ELSE 0 END AS delay_flagged,
            m.delay_reason                               AS delay_reason,
            w.[Resource Manager]                         AS account_manager,
            TRY_CAST(w.[Bill Rate] AS DECIMAL(10,2))     AS bill_rate,
            TRY_CAST(w.[Pay Rate] AS DECIMAL(10,2))      AS pay_rate,
            -- Hours come off the work order's own [Work Week], not the jobs
            -- feed: it is at the seat grain and present on 1532 of 1571 work
            -- orders (97.5%) versus 732 (47%) for the jobs feed, and the two
            -- agree on 717 of the 732 where both exist. The jobs value is only
            -- a fallback for the handful with no [Work Week].
            COALESCE(TRY_CAST(w.[Work Week] AS DECIMAL(10,2)),
                     j.hours_per_week)                   AS hours_per_week
        FROM dbo.STAGING_VNDLY_WORKORDERS w WITH (NOLOCK)
        LEFT JOIN mods m ON m.wo = w.WOSystemKey
        LEFT JOIN jobs j ON j.wo_id = w.[Work Order Id] AND j.hs = w.[Health System]
        WHERE TRY_CAST(w.[Start Date] AS DATE)
                BETWEEN DATEADD(DAY, ?, CAST(GETDATE() AS DATE))
                    AND DATEADD(DAY, ?, CAST(GETDATE() AS DATE))
            {vendor_filter}
    ''', -abs(lookback), lookahead)
    return [dict(zip([c[0] for c in cursor.description], r)) for r in cursor.fetchall()]


def _bullhorn_onb_rows(cursor, app_conn, lookback, lookahead):
    """Onboarding seats on Bullhorn.

    This branch measures start-date slip properly, which MSP's B4 side cannot.
    `EditHistoryPlacement` records every change to `dateBegin` with its old and
    new value, so the planned start is the oldest recorded `oldValue` and the
    slip is the distance from there to where the date sits now. B4 carries only
    a 'Delayed Starts' Yes/No flag; here the day count is real.

    Seats with no edit history are genuinely untracked rather than on time.
    They report a null day count and movement_tracked false, so the UI shows
    "not tracked" instead of a confident zero.

    `onboardingStatus` is a real progress field on this book (Initiated 4,270,
    Completed 1,506, In Progress 169, Cancelled 298; 23% blank over a year),
    with no MSP equivalent at all.
    """
    scope_ids = resolve_scope_client_ids(cursor, app_conn)
    system_case = build_system_case_expr('p.clientCorporationID')
    scope_filter = build_scope_filter('p.clientCorporationID', client_ids=scope_ids)

    cursor.execute(f"""
        SELECT
            'Bullhorn'                                    AS source_system,
            CAST(p.placementID AS NVARCHAR(50))           AS id,
            LTRIM(RTRIM(ISNULL(cnd.firstName, '') + ' ' + ISNULL(cnd.lastName, ''))) AS clinician,
            ({system_case})                               AS health_system,
            ISNULL(cc.name, '')                           AS facility,
            NULL                                          AS unit,
            jo.title                                      AS role,
            cat.cred                                      AS credential_raw,
            spec.name                                     AS specialty_raw,
            'GHR'                                         AS source,
            'GHR'                                         AS agency,
            p.status                                      AS contract_status,
            p.onboardingStatus                            AS onboarding_status,
            CAST(p.dateBegin AS DATE)                     AS current_start,
            -- The oldest recorded previous value is the start everyone first
            -- agreed to; with no history the current date is all there is.
            CAST(COALESCE(fp.first_planned, p.dateBegin) AS DATE) AS planned_start,
            CAST(p.dateEnd AS DATE)                       AS end_date,
            NULL                                          AS onboarded_date,
            ISNULL(h.moves, 0)                            AS move_count,
            CASE WHEN ISNULL(h.pushed, 0) > 0 THEN 1 ELSE 0 END AS delay_flagged,
            CASE WHEN fp.first_planned IS NULL THEN NULL
                 ELSE DATEDIFF(DAY, fp.first_planned, p.dateBegin) END AS days_delayed,
            DATEDIFF(DAY, CAST(GETDATE() AS DATE), p.dateBegin) AS days_until_start,
            CASE WHEN ISNULL(h.pushed, 0) > 0 THEN 'Yes' ELSE 'No' END AS delayed_flag,
            -- Bullhorn records no delay reason. terminationReason explains a
            -- fallout after the fact, the closest thing available, and is only
            -- meaningful on a cancelled seat.
            CASE WHEN p.status IN ('Cancellation', 'Termination')
                 THEN NULLIF(LTRIM(RTRIM(p.terminationReason)), '') END AS delay_reason,
            LTRIM(RTRIM(ISNULL(u.firstName, '') + ' ' + ISNULL(u.lastName, ''))) AS account_manager,
            TRY_CAST(p.clientBillRate AS DECIMAL(10,2))   AS bill_rate,
            TRY_CAST(p.payRate AS DECIMAL(10,2))          AS pay_rate,
            TRY_CAST(jo.hoursPerWeek AS DECIMAL(10,2))    AS hours_per_week,
            jo.state                                      AS region
        FROM dbo.View_Placement p WITH (NOLOCK)
        LEFT JOIN dbo.View_Candidate cnd WITH (NOLOCK) ON cnd.candidateID = p.candidateID
        LEFT JOIN dbo.View_JobOrder jo WITH (NOLOCK)   ON jo.jobOrderID = p.jobOrderID
        LEFT JOIN dbo.View_ClientCorporation cc WITH (NOLOCK)
               ON cc.clientCorporationID = p.clientCorporationID
        LEFT JOIN dbo.View_ClientCorporation pcc WITH (NOLOCK)
               ON pcc.clientCorporationID = cc.parentClientCorporationID
        LEFT JOIN dbo.View_CorporateUser u WITH (NOLOCK) ON u.corporateUserID = p.ownerID
        OUTER APPLY (
            SELECT COUNT(*) AS moves,
                   SUM(CASE WHEN TRY_CAST(e.newValue AS date) > TRY_CAST(e.oldValue AS date)
                            THEN 1 ELSE 0 END) AS pushed,
                   MIN(TRY_CAST(e.oldValue AS date)) AS earliest_ever_proposed
            FROM dbo.EditHistoryPlacement e WITH (NOLOCK)
            WHERE e.placementID = p.placementID AND e.columnName = 'dateBegin'
              AND e.isDeleted = 0
        ) h
        -- The planned start is the value the *first* edit replaced, ordered by
        -- when the edit happened. MIN(oldValue) is a different question -- the
        -- earliest date ever proposed -- and the two diverge whenever a start
        -- was pulled earlier before being pushed back. Measured on the live
        -- window that is 1 seat in 30, reporting a 392-day slip where the
        -- truth is 364.
        OUTER APPLY (
            SELECT TOP 1 TRY_CAST(e.oldValue AS date) AS first_planned
            FROM dbo.EditHistoryPlacement e WITH (NOLOCK)
            WHERE e.placementID = p.placementID AND e.columnName = 'dateBegin'
              AND e.isDeleted = 0
            ORDER BY e.dateAdded ASC
        ) fp
        OUTER APPLY (
            SELECT TOP 1 COALESCE(NULLIF(cty.name, ''), cty.occupation) AS cred
            FROM dbo.JobOrderCategories jc WITH (NOLOCK)
            INNER JOIN dbo.Category cty WITH (NOLOCK) ON jc.categoryID = cty.categoryID
            WHERE jc.jobOrderID = p.jobOrderID AND jc.isDeleted = 0 AND cty.isDeleted = 0
        ) cat
        OUTER APPLY (
            SELECT TOP 1 sp.name
            FROM dbo.JobOrderSpecialties js WITH (NOLOCK)
            INNER JOIN dbo.Specialty sp WITH (NOLOCK) ON js.specialtyID = sp.specialtyID
            WHERE js.jobOrderID = p.jobOrderID AND js.isDeleted = 0 AND sp.isDeleted = 0
        ) spec
        WHERE p.dateBegin BETWEEN DATEADD(DAY, -{int(abs(lookback))}, CAST(GETDATE() AS DATE))
                              AND DATEADD(DAY,  {int(abs(lookahead))}, CAST(GETDATE() AS DATE))
          AND {scope_filter}
    """)
    return [dict(zip([c[0] for c in cursor.description], r)) for r in cursor.fetchall()]


def _symplr_onb_rows(cursor, app_conn, lookback, lookahead):
    """Onboarding seats on Symplr.

    Movement cannot be measured here. There is no audit trail for lt_order,
    `temp_confirm_date` and `client_confirm_date` are both 0% populated, and
    `StatusChangeLog` is keyed on WorkerID rather than the order, so it tracks
    worker state and not this seat's onboarding. Every row therefore reports a
    null day count and movement_tracked false, rather than a zero that would
    read as "started on time".
    """
    symplr_master_ids = symplr_resolve_scope(app_conn, symplr_cursor=cursor)
    sys_case = symplr_system_case_expr('lt.clientid')
    scope = symplr_scope_filter('lt.clientid', master_ids=symplr_master_ids)

    cursor.execute(f"""
        SELECT
            'Symplr'                                      AS source_system,
            CAST(lt.lt_orderid AS NVARCHAR(50))           AS id,
            NULLIF(LTRIM(RTRIM(ISNULL(t.firstname, '') + ' ' + ISNULL(t.lastname, ''))), '') AS clinician,
            ({sys_case})                                  AS health_system,
            pc.clientname                                 AS facility,
            NULL                                          AS unit,
            LTRIM(RTRIM(ISNULL(lt.nursetype, '') + ' - ' + ISNULL(lt.specialty, ''))) AS role,
            lt.nursetype                                  AS credential_raw,
            lt.specialty                                  AS specialty_raw,
            'GHR'                                         AS source,
            'GHR'                                         AS agency,
            lt.status                                     AS contract_status,
            NULL                                          AS onboarding_status,
            CAST(lt.date_start AS DATE)                   AS current_start,
            CAST(lt.date_start AS DATE)                   AS planned_start,
            CAST(lt.date_end AS DATE)                     AS end_date,
            CAST(lt.BookedByDT AS DATE)                   AS onboarded_date,
            0                                             AS move_count,
            0                                             AS delay_flagged,
            NULL                                          AS days_delayed,
            DATEDIFF(DAY, CAST(GETDATE() AS DATE), lt.date_start) AS days_until_start,
            NULL                                          AS delayed_flag,
            NULLIF(LTRIM(RTRIM(lt.voidreason)), '')       AS delay_reason,
            NULLIF(LTRIM(RTRIM(ISNULL(bu.firstname, '') + ' ' + ISNULL(bu.lastname, ''))), '') AS account_manager,
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
            pc.state                                      AS region
        FROM dbo.lt_order lt WITH (NOLOCK)
        LEFT JOIN dbo.profile_client pc ON lt.clientid = pc.recordid
        LEFT JOIN dbo.profile_client m  ON pc.MasterClientID = m.recordid
        LEFT JOIN dbo.profile_temp t WITH (NOLOCK) ON t.recordid = lt.tempid
        LEFT JOIN dbo.users bu WITH (NOLOCK) ON bu.userid = lt.BookedByUserID
        WHERE lt.status IN ('filled', 'void')
          AND lt.date_start BETWEEN DATEADD(DAY, -{int(abs(lookback))}, CAST(GETDATE() AS DATE))
                                AND DATEADD(DAY,  {int(abs(lookahead))}, CAST(GETDATE() AS DATE))
          AND {scope}
    """)
    return [dict(zip([c[0] for c in cursor.description], r)) for r in cursor.fetchall()]


def _non_msp_rows(lookback, lookahead):
    """Onboarding seats across Bullhorn and Symplr."""
    rows, errors = [], []
    for label, getter, fn in (
        ('Bullhorn', get_bullhorn_conn, _bullhorn_onb_rows),
        ('Symplr', get_symplr_conn, _symplr_onb_rows),
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
            rows.extend(fn(cursor, app_conn, lookback, lookahead))
        except Exception as e:
            print(f'Onboarding(non_msp): {label} branch failed: {e}')
            import traceback
            traceback.print_exc()
            errors.append(f'{label}: {e}')
        finally:
            if app_conn is not None:
                app_conn.close()
            if conn is not None:
                conn.close()
    for r in rows:
        raw = r.pop('credential_raw', None)
        r['profession'] = normalize_credential(raw)
        r['service_line'] = credential_service_line(raw)
        r['specialty'] = (r.pop('specialty_raw', None) or '')
    return rows, errors


def _finalize(rows):
    out = []
    for r in rows:
        for k in ('current_start', 'planned_start', 'end_date', 'onboarded_date'):
            if r.get(k) is not None:
                r[k] = r[k].isoformat() if hasattr(r[k], 'isoformat') else str(r[k])
        for k in ('bill_rate', 'pay_rate', 'hours_per_week'):
            if r.get(k) is not None:
                r[k] = float(r[k])
        # See note above: this is the MSP fee split, not gross margin.
        bill, pay = r.get('bill_rate'), r.get('pay_rate')
        r['agency_receipt_pct'] = round(pay / bill * 100, 1) if bill and pay and bill > 0 else None
        r['margin_pct'] = None

        # VNDLY writes the same reason several ways ('Terminated - attendance'
        # vs 'Terminated-attendance'), which would split one reason across two
        # rows in any grouping. Canonicalise on read — the staging tables are
        # reloaded from VNDLY, so a warehouse-side fix would be overwritten.
        if r.get('source_system') == 'VNDLY':
            raw_reason = r.get('delay_reason')
            r['delay_reason'] = canonical_reason(raw_reason)
            r['delay_category'] = reason_category(raw_reason)
        elif r.get('source_system') in ('Bullhorn', 'Symplr'):
            # Non-MSP reasons are already single-vocabulary per book and are
            # about fallout rather than delay, so they pass through as written.
            r['delay_category'] = (r.get('delay_reason') or None)
        else:
            r['delay_category'] = 'Delayed Start' if (r.get('delayed_flag') == 'Yes') else None

        # Both VMSs say whether a start slipped and why. Only VNDLY says by how
        # much, and only for the seats its jobs feed covers. Grouping keys off
        # the flag both sides record, so it stays consistent across sources.
        flagged = bool(r.get('delay_flagged'))
        moves = r.get('move_count')
        delayed = r.get('days_delayed')
        status = (r.get('contract_status') or '')
        cancelled = (status in B4_CANCELLED_STATUSES
                     or status in VNDLY_CANCELLED_STATUSES
                     or status in BULLHORN_CANCELLED_STATUSES
                     # Symplr writes lowercase order states; a voided order in
                     # the onboarding window is a fallout, not a live seat.
                     or status == 'void')

        # Stage grouping, mirroring the four buckets the Onboarding view
        # renders. Untracked rows fall through to ON TRACK so they stay
        # visible, but carry movement_tracked=false and null metrics so the UI
        # shows "not tracked" rather than a confident zero.
        if cancelled:
            group = 'CANCELED'
        elif flagged:
            group = 'DELAYED START'
        else:
            group = 'ON TRACK'

        r['group'] = group
        # More than one logged delay event. A floor, not a true count — a start
        # can move with no 'Delayed Start' reason recorded.
        r['moved_multiple'] = bool(moves and moves > 1)
        r['delay_flagged'] = flagged

        # Which measure this row actually carries. VNDLY seats covered by the
        # jobs feed have a real day count against the planned start; B4 seats
        # and uncovered VNDLY seats have only the flag. The UI must render
        # 'flagged' rows as "not tracked" rather than as zero days.
        if delayed is not None:
            r['delay_measure'] = 'days'
            r['movement_tracked'] = True
        else:
            r['delay_measure'] = 'flagged'
            r['movement_tracked'] = False
        rate, hrs = r.get('bill_rate'), r.get('hours_per_week')
        r['value_13wk'] = round(rate * hrs * 13, 2) if rate and hrs else None
        out.append(r)
    return out


def main(req: func.HttpRequest) -> func.HttpResponse:
    auth_error = require_allowed_domain(req)
    if auth_error:
        return auth_error

    def _int_param(name, default):
        try:
            return int(req.params.get(name) or default)
        except (TypeError, ValueError):
            return default

    lookback = _int_param('lookback', ONBOARDING_LOOKBACK_DAYS)
    lookahead = _int_param('lookahead', ONBOARDING_LOOKAHEAD_DAYS)
    include_affiliate = str(req.params.get('includeAffiliate', '')).lower() in ('1', 'true', 'yes')

    if is_non_msp():
        try:
            rows, errors = _non_msp_rows(lookback, lookahead)
            rows = _finalize(rows)
            rows.sort(key=lambda r: (-(r.get('move_count') or 0), r.get('current_start') or ''))
            bhn = sum(1 for r in rows if r['source_system'] == 'Bullhorn')
            syn = sum(1 for r in rows if r['source_system'] == 'Symplr')
            tracked = sum(1 for r in rows if r.get('movement_tracked'))
            print(f"Onboarding(non_msp): {len(rows)} rows (Bullhorn {bhn}, Symplr {syn}; "
                  f"{tracked} with measurable movement; -{lookback}/+{lookahead}d; "
                  f"errors: {errors or 'none'})")
            return func.HttpResponse(
                json.dumps(rows, default=str),
                mimetype='application/json', status_code=200)
        except Exception as e:
            print(f'Onboarding(non_msp) error: {e}')
            import traceback
            traceback.print_exc()
            return func.HttpResponse(
                json.dumps({'error': str(e)}),
                mimetype='application/json', status_code=500)

    conn = None
    rows, errors = [], []
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

        # Unioned, not deduped — during the B4→VNDLY transition the two cover
        # different workers, and once a system is cut over B4 stops producing
        # rows for it. Same treatment GetFinancialData applies for RUMC, Holy
        # Redeemer and Cooper.
        for label, fn in (('B4', _b4_rows), ('VNDLY', _vndly_rows)):
            try:
                rows.extend(fn(cursor, lookback, lookahead, include_affiliate))
            except Exception as e:
                print(f"Onboarding: {label} branch failed: {e}")
                import traceback
                traceback.print_exc()
                errors.append(f'{label}: {e}')

        rows = _finalize(rows)
        rows.sort(key=lambda r: (-(r.get('move_count') or 0), r.get('current_start') or ''))
        b4n = sum(1 for r in rows if r['source_system'] == 'B4')
        vnn = sum(1 for r in rows if r['source_system'] == 'VNDLY')
        print(f"Onboarding: {len(rows)} rows (B4 {b4n}, VNDLY {vnn}; -{lookback}d/+{lookahead}d, "
              f"affiliate={include_affiliate}; errors: {errors or 'none'})")
        return func.HttpResponse(
            json.dumps(rows, default=str),
            mimetype="application/json",
            status_code=200,
        )
    except Exception as e:
        print(f"Onboarding error: {e}")
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
