import azure.functions as func
import pyodbc
import os
import json
from shared_code.auth import require_allowed_domain


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
                COUNT(*)                                        AS ext_events,
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


def _serialize(rows):
    out = []
    for r in rows:
        for k in ('start_date', 'end_date'):
            if r.get(k) is not None:
                r[k] = r[k].isoformat() if hasattr(r[k], 'isoformat') else str(r[k])
        for k in ('bill_rate', 'pay_rate', 'hours_per_week'):
            if r.get(k) is not None:
                r[k] = float(r[k])
        # NOT gross margin. B4's Pay_Rate is a fixed share of Awarded_Rate —
        # 11,170 awarded rows sit at exactly 95% and 5,177 at 93% — so this is
        # the MSP vendor fee tier, i.e. what the agency receives after the MSP
        # cut, not what the clinician is paid. Real margin needs clinician pay,
        # which lives in Bullhorn and is not joined on the MSP path. Exposed
        # under its own name so nothing downstream mistakes it for margin.
        bill, pay = r.get('bill_rate'), r.get('pay_rate')
        r['agency_receipt_pct'] = round(pay / bill * 100, 1) if bill and pay and bill > 0 else None
        r['margin_pct'] = None
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
