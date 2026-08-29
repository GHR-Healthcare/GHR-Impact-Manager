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


# GHR-owned vendors differ by VMS. B4Health writes 'GHR Allied' / 'GHR Acute'
# / 'GHR Travel'; VNDLY writes 'GHR Nursing' / 'GHR Allied' / 'GHR Search' /
# 'GHR Locum Tenens'. Both are matched on the 'GHR' stem, and Planet
# Healthcare is treated as GHR-family the same way GetPositions does.
B4_GHR_PREDICATE = "(o.Agency LIKE '%GHR%' OR o.Agency LIKE '%Planet Healthcare%')"
VNDLY_GHR_PREDICATE = "(w.[Vendor Name] LIKE '%GHR%' OR w.[Vendor Name] LIKE '%Planet Healthcare%')"

# Cancelled / never-awarded seats aren't extension candidates — there's no
# seat to extend.
B4_EXCLUDED_STATUSES = ('Closed And Cancelled', 'Closed Not Awarded')

# On VNDLY only a live seat can extend. Everything else is terminal
# ('Ended', 'Ended by Job Close', 'Rejected', 'Withdrawn', 'Offer Declined')
# or hasn't started ('Applied', 'Verification In Progress').
VNDLY_ACTIVE_STATUSES = ('Active',)

EXTENSION_HORIZON_DAYS = 45

# Non-MSP placement statuses that represent a seat still running, and so
# capable of being extended. Everything else in the window is terminal
# (Cancellation, Termination, Completed) or never started.
BULLHORN_LIVE_PLACEMENT_STATUSES = (
    'Approved', 'Started', 'Cleared', 'Onboarding', 'Pending Start',
)


def _urgency(days):
    """Mirrors the stage legend: deeper red = fewer days to secure a decision."""
    if days is None:
        return 'low'
    if days <= 7:
        return 'critical'
    if days <= 14:
        return 'high'
    if days <= 21:
        return 'medium'
    return 'low'


def _b4_rows(cursor, horizon, include_affiliate):
    """Extension candidates still being managed in B4Health."""
    status_list = ', '.join("'" + s.replace("'", "''") + "'" for s in B4_EXCLUDED_STATUSES)
    agency_filter = '' if include_affiliate else f'AND {B4_GHR_PREDICATE}'
    cursor.execute(f'''
        SELECT
            'B4'                                        AS source_system,
            LTRIM(RTRIM(o.Contract_ID))                 AS id,
            LTRIM(RTRIM(ISNULL(o.First_Name, '') + ' ' + ISNULL(o.Last_Name, ''))) AS clinician,
            o.Health_System                             AS health_system,
            o.Facility                                  AS facility,
            o.Unit                                      AS unit,
            o.Position_Type                             AS role,
            o.Care_Type                                 AS care_type,
            o.Program                                   AS program,
            o.Time_Type                                 AS time_type,
            CAST(o.Start_Date AS DATE)                  AS start_date,
            CAST(o.End_Date AS DATE)                    AS end_date,
            DATEDIFF(DAY, CAST(GETDATE() AS DATE), o.End_Date) AS days_left,
            CASE WHEN {B4_GHR_PREDICATE} THEN 'GHR' ELSE 'Affiliate' END AS source,
            o.Agency                                    AS agency,
            o.Contract_Status                           AS contract_status,
            o.Account_Manager                           AS account_manager,
            o.Hiring_Manager                            AS hiring_manager,
            o.Cost_Center                               AS cost_center,
            TRY_CAST(o.Awarded_Rate AS DECIMAL(10,2))   AS bill_rate,
            TRY_CAST(o.Pay_Rate AS DECIMAL(10,2))       AS pay_rate,
            TRY_CAST(o.Hours_per_Peek AS DECIMAL(10,2)) AS hours_per_week,
            -- B4 has no "original end date", so an extension is only visible
            -- through the parent-contract chain.
            CASE WHEN o.Parent_Contract_ID IS NOT NULL AND LTRIM(RTRIM(o.Parent_Contract_ID)) <> ''
                 THEN 1 ELSE 0 END                      AS is_extension,
            LTRIM(RTRIM(ISNULL(o.Parent_Contract_ID, ''))) AS parent_ref,
            -- Margin is not read from the data. The VMS records what the
            -- client is billed and what the vendor receives, never what the
            -- vendor pays its clinician — disclosure sits under 1% across every
            -- B4 and VNDLY table. Margin is the configured rate (DEFAULT_MARGIN,
            -- currently 26) or a per-job override, applied client-side to the
            -- bill rate. The rates below are what the calculation needs.
            0                                           AS extension_events,
            NULL                                        AS last_extension_at,
            NULL                                        AS extension_note,
            NULL                                        AS extension_by,
            -- B4 has no modifications feed, so no decision signal exists here.
            'Not tracked in B4'                         AS decision_state
        FROM dhc.B4HealthOrder o WITH (NOLOCK)
        WHERE o.End_Date BETWEEN CAST(GETDATE() AS DATE)
                             AND DATEADD(DAY, ?, CAST(GETDATE() AS DATE))
            AND o.Contract_Status NOT IN ({status_list})
            {agency_filter}
    ''', horizon)
    return [dict(zip([c[0] for c in cursor.description], r)) for r in cursor.fetchall()]


def _vndly_rows(cursor, horizon, include_affiliate):
    """Extension candidates on VNDLY.

    Richer than B4 in two ways: [Original End Date] makes an already-extended
    seat directly visible rather than inferred from a parent chain, and the
    modifications feed records explicit 'Date Extension' events.

    Hours aren't on the work order, so they come from the linked job.
    """
    status_list = ', '.join("'" + s.replace("'", "''") + "'" for s in VNDLY_ACTIVE_STATUSES)
    vendor_filter = '' if include_affiliate else f'AND {VNDLY_GHR_PREDICATE}'
    cursor.execute(f'''
        WITH ext_mods AS (
            -- Extension activity is an EVENT in the modifications feed, not a
            -- status column. It is filed inconsistently: usually under
            -- 'Date Extension', but 17 rows sit under 'Other', 'Ended in
            -- Error' and 'Assignment Completed' with the detail only in
            -- [Other Reason]. Matching on either catches all of them —
            -- filtering on the reason alone silently drops those.
            SELECT
                WOSystemKey                                     AS wo,
                -- The feed carries exact duplicates — 1,212 rows over 977
                -- distinct events, up to 7 copies of one change — so counting
                -- rows overstates activity. Measured on the extension signal:
                -- 274 rows for 238 real events, 13% inflation. Count the event,
                -- not the row.
                COUNT(DISTINCT CONCAT(
                    CONVERT(VARCHAR(19), [Last Modified], 120), '|',
                    ISNULL([Reason for Modification], ''), '|',
                    ISNULL([Other Reason], '')))                AS ext_events,
                MAX([Last Modified])                            AS last_ext_at,
                -- The free text is where the actual decision lives
                -- ('Extension offered for Chemistry unit - new end date
                -- 12/26/26'), so keep the most recent non-empty note.
                MAX(CASE WHEN NULLIF(LTRIM(RTRIM([Other Reason])), '') IS NOT NULL
                         THEN [Other Reason] END)               AS ext_note,
                MAX([Last Modified By])                         AS ext_by,
                MAX(CASE WHEN [Other Reason] LIKE '%offer%' THEN 1 ELSE 0 END) AS mentions_offer
            FROM dbo.STAGING_VNDLY_WORKODER_MODIFICATIONS WITH (NOLOCK)
            WHERE [Reason for Modification] = 'Date Extension'
               OR [Other Reason] LIKE '%exten%'
            GROUP BY WOSystemKey
        )
        SELECT
            'VNDLY'                                     AS source_system,
            CAST(w.WOSystemKey AS NVARCHAR(50))          AS id,
            LTRIM(RTRIM(ISNULL(w.[Contractor First Name], '') + ' ' + ISNULL(w.[Contractor Last Name], ''))) AS clinician,
            w.[Health System]                            AS health_system,
            COALESCE(w.[Default Work Site Name], w.[Job Site]) AS facility,
            w.[Organization Unit]                        AS unit,
            COALESCE(w.[Job Title], w.[Title])           AS role,
            NULL                                         AS care_type,
            w.[Busines Unit - Name]                      AS program,
            NULL                                         AS time_type,
            TRY_CAST(w.[Start Date] AS DATE)             AS start_date,
            TRY_CAST(w.[End Date] AS DATE)               AS end_date,
            DATEDIFF(DAY, CAST(GETDATE() AS DATE), TRY_CAST(w.[End Date] AS DATE)) AS days_left,
            CASE WHEN {VNDLY_GHR_PREDICATE} THEN 'GHR' ELSE 'Affiliate' END AS source,
            w.[Vendor Name]                              AS agency,
            w.[Current Status]                           AS contract_status,
            w.[Resource Manager]                         AS account_manager,
            w.[Hiring Manager]                           AS hiring_manager,
            NULL                                         AS cost_center,
            TRY_CAST(w.[Bill Rate] AS DECIMAL(10,2))     AS bill_rate,
            TRY_CAST(w.[Pay Rate] AS DECIMAL(10,2))      AS pay_rate,
            j.hours_per_week                             AS hours_per_week,
            -- An end date past the original is an extension, full stop.
            CASE WHEN TRY_CAST(w.[End Date] AS DATE) > TRY_CAST(w.[Original End Date] AS DATE)
                 THEN 1 ELSE 0 END                       AS is_extension,
            CONVERT(VARCHAR(10), TRY_CAST(w.[Original End Date] AS DATE), 120) AS parent_ref,
            -- See the B4 branch: margin is a configured rate, not a lookup.
            ISNULL(x.ext_events, 0)                      AS extension_events,
            CONVERT(VARCHAR(19), x.last_ext_at, 120)     AS last_extension_at,
            x.ext_note                                   AS extension_note,
            x.ext_by                                     AS extension_by,
            -- Decision state is only inferable from free text: ~50 of the 105
            -- work orders with extension activity say "offer". Anything else
            -- with an executed date change is treated as already extended.
            CASE WHEN x.wo IS NULL                       THEN 'None recorded'
                 WHEN x.mentions_offer = 1               THEN 'Offered'
                 WHEN TRY_CAST(w.[End Date] AS DATE) > TRY_CAST(w.[Original End Date] AS DATE)
                                                         THEN 'Extended'
                 ELSE 'Activity recorded' END            AS decision_state
        FROM dbo.STAGING_VNDLY_WORKORDERS w WITH (NOLOCK)
        LEFT JOIN ext_mods x ON x.wo = w.WOSystemKey
        -- STAGING_VNDLY_JOBS holds 664 rows across only 387 distinct [Job Id],
        -- so joining it raw fans work orders out — measured at 112 rows where
        -- the truth is 60. Collapse to one row per job before joining.
        -- Note the key is the int [Job Id] on both sides; [JobSystemKey] is an
        -- nvarchar business key ('CUH-3-294') and will not join to it.
        LEFT JOIN (
            SELECT [Job Id] AS job_id,
                   MAX(TRY_CAST([Standard Hours Per Week] AS DECIMAL(10,2))) AS hours_per_week
            FROM dbo.STAGING_VNDLY_JOBS WITH (NOLOCK)
            GROUP BY [Job Id]
        ) j ON j.job_id = w.[Job Id]
        WHERE TRY_CAST(w.[End Date] AS DATE) BETWEEN CAST(GETDATE() AS DATE)
                                                 AND DATEADD(DAY, ?, CAST(GETDATE() AS DATE))
            AND w.[Current Status] IN ({status_list})
            {vendor_filter}
    ''', horizon)
    return [dict(zip([c[0] for c in cursor.description], r)) for r in cursor.fetchall()]


def _bullhorn_ext_rows(cursor, app_conn, horizon):
    """Extension candidates on Bullhorn.

    This book can do something neither MSP source can: show the seat's actual
    extension history. `EditHistoryPlacement` records every change to
    `dateEnd` with its old and new value, so an extension is an edit where the
    end date moved *later* — not an inference from a parent-contract chain
    (B4) or a reason-text match (VNDLY). Count, latest date and the user who
    made it all come straight from the audit trail.

    Hours come from the job order. `View_Placement` carries `hoursPerDay` but
    no weekly figure, and multiplying by an assumed five-day week would invent
    precision the source does not have.
    """
    scope_ids = resolve_scope_client_ids(cursor, app_conn)
    system_case = build_system_case_expr('p.clientCorporationID')
    scope_filter = build_scope_filter('p.clientCorporationID', client_ids=scope_ids)
    status_list = ', '.join("'" + x + "'" for x in BULLHORN_LIVE_PLACEMENT_STATUSES)

    cursor.execute(f'''
        SELECT
            'Bullhorn'                                    AS source_system,
            CAST(p.placementID AS NVARCHAR(50))           AS id,
            LTRIM(RTRIM(ISNULL(cnd.firstName, '') + ' ' + ISNULL(cnd.lastName, ''))) AS clinician,
            ({system_case})                               AS health_system,
            ISNULL(cc.name, '')                           AS facility,
            NULL                                          AS unit,
            jo.title                                      AS role,
            NULL                                          AS care_type,
            cat.cred                                      AS credential_raw,
            spec.name                                     AS specialty_raw,
            p.employmentType                              AS time_type,
            CAST(p.dateBegin AS DATE)                     AS start_date,
            CAST(p.dateEnd AS DATE)                       AS end_date,
            DATEDIFF(DAY, CAST(GETDATE() AS DATE), p.dateEnd) AS days_left,
            'GHR'                                         AS source,
            'GHR'                                         AS agency,
            p.status                                      AS contract_status,
            LTRIM(RTRIM(ISNULL(u.firstName, '') + ' ' + ISNULL(u.lastName, ''))) AS account_manager,
            -- The placement's client contact resolves to a name on 100% of
            -- live seats in the window. jo.reportToClientContactID is never
            -- populated, so this is the only working source.
            NULLIF(LTRIM(RTRIM(ISNULL(cn.firstName, '') + ' ' + ISNULL(cn.lastName, ''))), '') AS hiring_manager,
            -- Bullhorn has no cost-centre or unit concept: View_JobOrder and
            -- View_Placement carry no department, unit or cost-centre column
            -- at all. Left null rather than filled with a stand-in.
            NULL                                          AS cost_center,
            TRY_CAST(p.clientBillRate AS DECIMAL(10,2))   AS bill_rate,
            TRY_CAST(p.payRate AS DECIMAL(10,2))          AS pay_rate,
            TRY_CAST(jo.hoursPerWeek AS DECIMAL(10,2))    AS hours_per_week,
            CASE WHEN ISNULL(ext.extended, 0) > 0 THEN 1 ELSE 0 END AS is_extension,
            NULL                                          AS parent_ref,
            ISNULL(ext.extended, 0)                       AS extension_events,
            CAST(ext.last_at AS DATE)                     AS last_extension_at,
            -- The audit trail records the dates, so the note is the move
            -- itself rather than free text nobody wrote.
            CASE WHEN ext.last_old IS NOT NULL AND ext.last_new IS NOT NULL
                 THEN 'End date moved ' + CONVERT(NVARCHAR(10), ext.last_old, 23)
                      + ' to ' + CONVERT(NVARCHAR(10), ext.last_new, 23)
            END                                           AS extension_note,
            ext.last_by                                   AS extension_by,
            -- Bullhorn has no VMS decision feed. Unlike VNDLY there is no
            -- modifications stream to derive intent from, so the decision is
            -- whatever the meeting records in the app.
            'Not tracked in Bullhorn'                     AS decision_state,
            jo.state                                      AS region
        FROM dbo.View_Placement p WITH (NOLOCK)
        LEFT JOIN dbo.View_Candidate cnd WITH (NOLOCK) ON cnd.candidateID = p.candidateID
        LEFT JOIN dbo.View_JobOrder jo WITH (NOLOCK)   ON jo.jobOrderID = p.jobOrderID
        LEFT JOIN dbo.View_ClientCorporation cc WITH (NOLOCK)
               ON cc.clientCorporationID = p.clientCorporationID
        LEFT JOIN dbo.View_ClientCorporation pcc WITH (NOLOCK)
               ON pcc.clientCorporationID = cc.parentClientCorporationID
        LEFT JOIN dbo.View_CorporateUser u WITH (NOLOCK) ON u.corporateUserID = p.ownerID
        LEFT JOIN dbo.View_ClientContact cn WITH (NOLOCK)
               ON cn.clientContactID = p.clientContactID
        OUTER APPLY (
            SELECT
                SUM(CASE WHEN TRY_CAST(e.newValue AS date) > TRY_CAST(e.oldValue AS date)
                         THEN 1 ELSE 0 END) AS extended,
                MAX(CASE WHEN TRY_CAST(e.newValue AS date) > TRY_CAST(e.oldValue AS date)
                         THEN e.dateAdded END) AS last_at
            FROM dbo.EditHistoryPlacement e WITH (NOLOCK)
            WHERE e.placementID = p.placementID AND e.columnName = 'dateEnd' AND e.isDeleted = 0
        ) ext_agg
        OUTER APPLY (
            SELECT TOP 1
                e.dateAdded AS last_at,
                TRY_CAST(e.oldValue AS date) AS last_old,
                TRY_CAST(e.newValue AS date) AS last_new,
                LTRIM(RTRIM(ISNULL(eu.firstName, '') + ' ' + ISNULL(eu.lastName, ''))) AS last_by,
                ext_agg.extended AS extended
            FROM dbo.EditHistoryPlacement e WITH (NOLOCK)
            LEFT JOIN dbo.View_CorporateUser eu WITH (NOLOCK) ON eu.corporateUserID = e.updatingUserID
            WHERE e.placementID = p.placementID AND e.columnName = 'dateEnd' AND e.isDeleted = 0
              AND TRY_CAST(e.newValue AS date) > TRY_CAST(e.oldValue AS date)
            ORDER BY e.dateAdded DESC
        ) ext
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
        WHERE p.dateEnd BETWEEN CAST(GETDATE() AS DATE)
                            AND DATEADD(DAY, {int(abs(horizon))}, CAST(GETDATE() AS DATE))
          AND p.status IN ({status_list})
          AND {scope_filter}
    ''')
    return [dict(zip([c[0] for c in cursor.description], r)) for r in cursor.fetchall()]


def _symplr_ext_rows(cursor, app_conn, horizon):
    """Extension candidates on Symplr.

    Only filled orders qualify: an open or void order has no seat to extend.
    These are genuine long-term assignments rather than per-diem shifts —
    the average span in the window is about 139 days.

    `original_lt_orderid` exists and looks like the parent-contract chain B4
    uses, but it is not populated in practice: 2 rows out of 4,735 in a year.
    It is read anyway, so a seat that *does* carry one is flagged, but it
    cannot be relied on to find prior extensions, and there is no audit trail
    on this source to fall back to. Extension history on Symplr is therefore
    whatever the app has recorded.
    """
    symplr_master_ids = symplr_resolve_scope(app_conn, symplr_cursor=cursor)
    sys_case = symplr_system_case_expr('lt.clientid')
    scope = symplr_scope_filter('lt.clientid', master_ids=symplr_master_ids)

    cursor.execute(f'''
        SELECT
            'Symplr'                                      AS source_system,
            CAST(lt.lt_orderid AS NVARCHAR(50))           AS id,
            -- lt_order.tempid is the assigned worker and BookedByUserID the
            -- internal booker; both resolve on 100% of filled orders in the
            -- window. An extension conversation without the clinician's name
            -- is most of the way to useless.
            NULLIF(LTRIM(RTRIM(ISNULL(t.firstname, '') + ' ' + ISNULL(t.lastname, ''))), '') AS clinician,
            ({sys_case})                                  AS health_system,
            pc.clientname                                 AS facility,
            NULL                                          AS unit,
            LTRIM(RTRIM(ISNULL(lt.nursetype, '') + ' — ' + ISNULL(lt.specialty, ''))) AS role,
            NULL                                          AS care_type,
            lt.nursetype                                  AS credential_raw,
            lt.specialty                                  AS specialty_raw,
            'Long-Term'                                   AS time_type,
            CAST(lt.date_start AS DATE)                   AS start_date,
            CAST(lt.date_end AS DATE)                     AS end_date,
            DATEDIFF(DAY, CAST(GETDATE() AS DATE), lt.date_end) AS days_left,
            'GHR'                                         AS source,
            'GHR'                                         AS agency,
            lt.status                                     AS contract_status,
            NULLIF(LTRIM(RTRIM(ISNULL(bu.firstname, '') + ' ' + ISNULL(bu.lastname, ''))), '') AS account_manager,
            NULL                                          AS hiring_manager,
            ISNULL(r.regionname, '')                      AS cost_center,
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
            CASE WHEN lt.original_lt_orderid IS NOT NULL
                  AND lt.original_lt_orderid <> 0
                  AND lt.original_lt_orderid <> lt.lt_orderid
                 THEN 1 ELSE 0 END                        AS is_extension,
            CASE WHEN lt.original_lt_orderid IS NOT NULL
                  AND lt.original_lt_orderid <> 0
                  AND lt.original_lt_orderid <> lt.lt_orderid
                 THEN CAST(lt.original_lt_orderid AS NVARCHAR(50)) END AS parent_ref,
            0                                             AS extension_events,
            NULL                                          AS last_extension_at,
            NULL                                          AS extension_note,
            NULL                                          AS extension_by,
            'Not tracked in Symplr'                       AS decision_state,
            pc.state                                      AS region
        FROM dbo.lt_order lt WITH (NOLOCK)
        LEFT JOIN dbo.profile_client pc ON lt.clientid = pc.recordid
        LEFT JOIN dbo.profile_client m  ON pc.MasterClientID = m.recordid
        LEFT JOIN dbo.regions r ON r.regionid = TRY_CAST(pc.region AS INT)
        LEFT JOIN dbo.profile_temp t WITH (NOLOCK) ON t.recordid = lt.tempid
        LEFT JOIN dbo.users bu WITH (NOLOCK) ON bu.userid = lt.BookedByUserID
        WHERE lt.status = 'filled'
          AND lt.date_end BETWEEN CAST(GETDATE() AS DATE)
                              AND DATEADD(DAY, {int(abs(horizon))}, CAST(GETDATE() AS DATE))
          AND {scope}
    ''')
    return [dict(zip([c[0] for c in cursor.description], r)) for r in cursor.fetchall()]


def _non_msp_rows(horizon):
    """Extension candidates across Bullhorn and Symplr."""
    rows, errors = [], []
    for label, getter, fn in (
        ('Bullhorn', get_bullhorn_conn, _bullhorn_ext_rows),
        ('Symplr', get_symplr_conn, _symplr_ext_rows),
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
            rows.extend(fn(cursor, app_conn, horizon))
        except Exception as e:
            print(f'Extensions(non_msp): {label} branch failed: {e}')
            import traceback
            traceback.print_exc()
            errors.append(f'{label}: {e}')
        finally:
            if app_conn is not None:
                app_conn.close()
            if conn is not None:
                conn.close()
    return rows, errors


def _apply_credentials(rows):
    """Same credential vocabulary the rest of non-MSP uses, so an RN filter
    matches RNs from both books."""
    for r in rows:
        raw = r.pop('credential_raw', None)
        r['profession'] = normalize_credential(raw)
        r['service_line'] = credential_service_line(raw)
        r['specialty'] = (r.pop('specialty_raw', None) or '')
        if r.get('last_extension_at') is not None and hasattr(r['last_extension_at'], 'isoformat'):
            r['last_extension_at'] = r['last_extension_at'].isoformat()
    return rows


def _serialize(rows):
    out = []
    for r in rows:
        for k in ('start_date', 'end_date'):
            if r.get(k) is not None:
                r[k] = r[k].isoformat() if hasattr(r[k], 'isoformat') else str(r[k])
        for k in ('bill_rate', 'pay_rate', 'hours_per_week'):
            if r.get(k) is not None:
                r[k] = float(r[k])
        # bill_rate/pay_rate are the MSP fee split — what the client is billed
        # vs what the agency receives — not a margin. Kept under a name that
        # cannot be mistaken for one. Margin itself is applied client-side from
        # the configured rate or a per-job override.
        bill, pay = r.get('bill_rate'), r.get('pay_rate')
        r['agency_receipt_pct'] = round(pay / bill * 100, 1) if bill and pay and bill > 0 else None
        rate, hrs = r.get('bill_rate'), r.get('hours_per_week')
        # 13-week forward value of the seat if it extends. Left null rather
        # than assuming a standard week when hours aren't known.
        r['extension_value_13wk'] = round(rate * hrs * 13, 2) if rate and hrs else None
        r['urgency'] = _urgency(r.get('days_left'))
        out.append(r)
    return out


def main(req: func.HttpRequest) -> func.HttpResponse:
    auth_error = require_allowed_domain(req)
    if auth_error:
        return auth_error

    try:
        horizon = int(req.params.get('days') or EXTENSION_HORIZON_DAYS)
    except (TypeError, ValueError):
        horizon = EXTENSION_HORIZON_DAYS
    include_affiliate = str(req.params.get('includeAffiliate', '')).lower() in ('1', 'true', 'yes')

    if is_non_msp():
        # No affiliate concept on this side: non-MSP is GHR's direct book, so
        # includeAffiliate is meaningless rather than merely unset.
        try:
            rows, errors = _non_msp_rows(horizon)
            rows = _serialize(_apply_credentials(rows))
            rows.sort(key=lambda r: (r.get('days_left') if r.get('days_left') is not None else 9999))
            bhn = sum(1 for r in rows if r['source_system'] == 'Bullhorn')
            syn = sum(1 for r in rows if r['source_system'] == 'Symplr')
            ext = sum(1 for r in rows if r.get('extension_events'))
            print(f"Extensions(non_msp): {len(rows)} rows (Bullhorn {bhn}, Symplr {syn}; "
                  f"{ext} previously extended; horizon {horizon}d; errors: {errors or 'none'})")
            return func.HttpResponse(
                json.dumps(rows, default=str),
                mimetype='application/json', status_code=200)
        except Exception as e:
            print(f'Extensions(non_msp) error: {e}')
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

        # B4 and VNDLY are unioned rather than deduped: during the transition
        # they cover different workers, and once a system is fully cut over B4
        # simply stops producing rows for it. Same treatment GetFinancialData
        # applies for the transitioned systems (RUMC, Holy Redeemer, Cooper).
        for label, fn in (('B4', _b4_rows), ('VNDLY', _vndly_rows)):
            try:
                rows.extend(fn(cursor, horizon, include_affiliate))
            except Exception as e:
                print(f"Extensions: {label} branch failed: {e}")
                import traceback
                traceback.print_exc()
                errors.append(f'{label}: {e}')

        rows = _serialize(rows)
        rows.sort(key=lambda r: (r.get('days_left') if r.get('days_left') is not None else 9999))
        b4n = sum(1 for r in rows if r['source_system'] == 'B4')
        vnn = sum(1 for r in rows if r['source_system'] == 'VNDLY')
        print(f"Extensions: {len(rows)} rows (B4 {b4n}, VNDLY {vnn}; horizon {horizon}d, "
              f"affiliate={include_affiliate}; errors: {errors or 'none'})")
        return func.HttpResponse(
            json.dumps(rows, default=str),
            mimetype="application/json",
            status_code=200,
        )
    except Exception as e:
        print(f"Extensions error: {e}")
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
