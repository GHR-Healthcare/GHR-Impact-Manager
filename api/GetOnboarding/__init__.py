import azure.functions as func
import pyodbc
import os
import json
from shared_code.auth import require_allowed_domain
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


def _b4_rows(cursor, lookback, lookahead, include_affiliate):
    """B4 seats. Movement is reconstructed from dbo.HIST_B4HealthOrder, which
    snapshots each contract per warehouse load (RUN_ID). DISTINCT Start_Date
    per Contract_ID gives the move count; earliest snapshot vs current gives
    how far it slipped.

    That history comes from Bullhorn placement matching and Bullhorn is GHR's
    own ATS, so it is GHR-only: measured over this window it covers 97% of GHR
    seats but under 5% of affiliate ones. Where it is absent, movement is
    unknown — never zero.
    """
    agency_filter = '' if include_affiliate else f'AND {B4_GHR_PREDICATE}'
    cursor.execute(f'''
        WITH hist AS (
            SELECT
                LTRIM(RTRIM(Contract_ID))   AS cid,
                COUNT(DISTINCT Start_Date)  AS distinct_starts,
                MIN(Start_Date)             AS first_start,
                MAX(Start_Date)             AS last_start
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
            CAST(h.first_start AS DATE)                 AS original_start,
            CAST(o.End_Date AS DATE)                    AS end_date,
            NULL                                        AS onboarded_date,
            CASE WHEN h.cid IS NULL THEN NULL ELSE
                (h.distinct_starts - 1)
                -- History only covers loads taken so far, so a start changed
                -- since the last load shows as a single distinct value that no
                -- longer matches the live row — count that as one more move,
                -- otherwise very recent slips read as "on track".
                + CASE WHEN o.Start_Date <> h.last_start THEN 1 ELSE 0 END
            END                                         AS move_count,
            CASE WHEN h.cid IS NULL THEN 0 ELSE 1 END   AS movement_tracked,
            DATEDIFF(DAY, h.first_start, o.Start_Date)  AS days_delayed,
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

    The tradeoff: the work order carries [Original End Date] but there is no
    Original Start Date anywhere in the VNDLY staging tables, so how far a
    start slipped is NOT computable here. What is knowable is that a delayed
    start was flagged and why. days_delayed is therefore null on this side and
    move_count counts flagged delay events — a floor on real movement, not a
    measured count. The UI must not present the two sides as the same measure.
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
            -- STAGING_VNDLY_JOBS holds ~664 rows over ~387 distinct [Job Id];
            -- joining it raw fans work orders out, so collapse it first. The
            -- key is the int [Job Id]; [JobSystemKey] is an nvarchar business
            -- key ('CUH-3-294') and will not join.
            SELECT [Job Id] AS job_id,
                   MAX(TRY_CAST([Standard Hours Per Week] AS DECIMAL(10,2))) AS hours_per_week
            FROM dbo.STAGING_VNDLY_JOBS WITH (NOLOCK)
            GROUP BY [Job Id]
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
            NULL                                         AS original_start,
            TRY_CAST(w.[End Date] AS DATE)               AS end_date,
            TRY_CAST(w.[Onboarded Date] AS DATE)         AS onboarded_date,
            ISNULL(m.delay_events, 0)                    AS move_count,
            -- The modifications feed exists for every work order, so no row
            -- genuinely means no flagged delay (unlike B4, where a missing
            -- history row means unmatched).
            1                                            AS movement_tracked,
            NULL                                         AS days_delayed,
            DATEDIFF(DAY, CAST(GETDATE() AS DATE), TRY_CAST(w.[Start Date] AS DATE)) AS days_until_start,
            CASE WHEN ISNULL(m.delay_events, 0) > 0 THEN 'Yes' ELSE 'No' END AS delayed_flag,
            m.delay_reason                               AS delay_reason,
            w.[Resource Manager]                         AS account_manager,
            TRY_CAST(w.[Bill Rate] AS DECIMAL(10,2))     AS bill_rate,
            TRY_CAST(w.[Pay Rate] AS DECIMAL(10,2))      AS pay_rate,
            j.hours_per_week                             AS hours_per_week
        FROM dbo.STAGING_VNDLY_WORKORDERS w WITH (NOLOCK)
        LEFT JOIN mods m ON m.wo = w.WOSystemKey
        LEFT JOIN jobs j ON j.job_id = w.[Job Id]
        WHERE TRY_CAST(w.[Start Date] AS DATE)
                BETWEEN DATEADD(DAY, ?, CAST(GETDATE() AS DATE))
                    AND DATEADD(DAY, ?, CAST(GETDATE() AS DATE))
            {vendor_filter}
    ''', -abs(lookback), lookahead)
    return [dict(zip([c[0] for c in cursor.description], r)) for r in cursor.fetchall()]


def _finalize(rows):
    out = []
    for r in rows:
        for k in ('current_start', 'original_start', 'end_date', 'onboarded_date'):
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
        else:
            r['delay_category'] = 'Delayed Start' if (r.get('delayed_flag') == 'Yes') else None

        tracked = bool(r.get('movement_tracked'))
        moves = r.get('move_count')
        delayed = r.get('days_delayed')
        status = (r.get('contract_status') or '')
        cancelled = status in B4_CANCELLED_STATUSES or status in VNDLY_CANCELLED_STATUSES

        # Stage grouping, mirroring the four buckets the Onboarding view
        # renders. Untracked rows fall through to ON TRACK so they stay
        # visible, but carry movement_tracked=false and null metrics so the UI
        # shows "not tracked" rather than a confident zero.
        if cancelled:
            group = 'CANCELED'
        elif tracked and (delayed or 0) >= 7:
            group = 'DELAYED START'
        elif tracked and (moves or 0) > 0:
            group = 'START DATE CHANGED'
        else:
            group = 'ON TRACK'

        r['group'] = group
        r['moved_multiple'] = bool(tracked and (moves or 0) >= 2)
        r['movement_tracked'] = tracked
        # VNDLY knows a delay was flagged but not how far it slipped, so the
        # two sides are not the same measure and the UI is told which it has.
        r['delay_measure'] = 'days' if r.get('source_system') == 'B4' else 'flagged'
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
