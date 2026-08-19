import azure.functions as func
import pyodbc
import os
import json
from shared_code.auth import require_allowed_domain
from shared_code.vndly_reasons import canonical_reason, reason_category


B4_GHR_PREDICATE = "(o.Agency LIKE '%GHR%' OR o.Agency LIKE '%Planet Healthcare%')"
VNDLY_GHR_PREDICATE = "(w.[Vendor Name] LIKE '%GHR%' OR w.[Vendor Name] LIKE '%Planet Healthcare%')"

CLOSED_LOOKBACK_DAYS = 7


def _b4_rows(cursor, lookback):
    """Closed outcomes still recorded in B4Health.

    Anchoring is the hard part. Awarded_Date covers every awarded contract on
    both sides, but cancellations and never-awarded seats carry no close date
    at all, and the HIST fallback only reaches GHR rows (that history comes
    from Bullhorn placement matching). Measured over the full table:

        GHR WON        11,898 rows —     0 undated
        AFFILIATE WON   7,433 rows —     0 undated
        CANCELED        7,204 rows — 6,204 undated  (86%)
        MISSED          2,043 rows — 2,015 undated  (99%)

    So B4 can date its wins and mostly cannot date its losses. Undated rows
    are excluded from the window (they cannot be placed in a week) but counted
    and reported, because silently dropping them would overstate capture rate.
    """
    cursor.execute('''
        WITH hist_close AS (
            SELECT LTRIM(RTRIM(h.Contract_ID)) AS cid,
                   MIN(CAST(CAST(d.Date_ID AS VARCHAR(8)) AS DATE)) AS closed_on
            FROM dbo.HIST_B4HealthOrder h WITH (NOLOCK)
            JOIN dbo.BH_PLACEMENT_RAW_TO_B4HealthOrder_RUN_DIM d WITH (NOLOCK)
                 ON d.RUN_ID = h.RUN_ID
            WHERE h.Contract_Status LIKE 'Closed%'
            GROUP BY LTRIM(RTRIM(h.Contract_ID))
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
            o.Contract_Status                           AS status,
            o.Agency                                    AS agency,
            CASE WHEN ''' + B4_GHR_PREDICATE + ''' THEN 'GHR' ELSE 'Affiliate' END AS source,
            CAST(COALESCE(o.Awarded_Date, hc.closed_on) AS DATE) AS closed_on,
            CASE WHEN o.Awarded_Date IS NOT NULL THEN 'awarded_date'
                 WHEN hc.closed_on IS NOT NULL   THEN 'history'
                 ELSE NULL END                          AS close_date_source,
            o.Unfilled_Reason                           AS outcome_reason,
            o.Account_Manager                           AS account_manager,
            CAST(o.Start_Date AS DATE)                  AS start_date,
            TRY_CAST(o.Awarded_Rate AS DECIMAL(10,2))   AS bill_rate,
            TRY_CAST(o.Hours_per_Peek AS DECIMAL(10,2)) AS hours_per_week
        FROM dhc.B4HealthOrder o WITH (NOLOCK)
        LEFT JOIN hist_close hc ON hc.cid = LTRIM(RTRIM(o.Contract_ID))
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


def main(req: func.HttpRequest) -> func.HttpResponse:
    auth_error = require_allowed_domain(req)
    if auth_error:
        return auth_error

    try:
        lookback = int(req.params.get('days') or CLOSED_LOOKBACK_DAYS)
    except (TypeError, ValueError):
        lookback = CLOSED_LOOKBACK_DAYS

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
            for k in ('bill_rate', 'hours_per_week'):
                if r.get(k) is not None:
                    r[k] = float(r[k])
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
