import azure.functions as func
import pyodbc
import os
import json
from shared_code.auth import require_allowed_domain


GHR_AGENCY_PREDICATE = "(o.Agency LIKE 'GHR%' OR o.Agency LIKE '%Planet Healthcare%')"

# How far back/forward the Onboarding stage looks around today's date. The
# stage is about accepted offers moving toward an actual start, so it needs a
# window on both sides: recent starts that already happened (did they stick?)
# and upcoming ones (are they still moving?).
ONBOARDING_LOOKBACK_DAYS = 30
ONBOARDING_LOOKAHEAD_DAYS = 45

CANCELLED_STATUSES = ('Closed And Cancelled', 'Closed Not Awarded')


def _onboarding_data(cursor, lookback, lookahead, include_affiliate):
    """Rows for the Onboarding meeting stage.

    Start-date movement is reconstructed from dbo.HIST_B4HealthOrder, which
    snapshots each contract per load (RUN_ID). Counting DISTINCT Start_Date per
    Contract_ID yields the number of times a start moved; comparing the
    earliest snapshot to the current row yields how far it slipped.

    Verified against live data: 5,756 contracts never moved, 449 moved once,
    151 moved 2+ times (max 6). So both the "Moved 2+ Times" and "Avg Days
    Delayed" KPIs on this stage are computable rather than estimated.

    B4HealthOrder.Delayed_Starts is only a Yes/No flag, so it's returned as
    corroboration but the move count is what drives the stage.
    """
    status_list = ', '.join("'" + s.replace("'", "''") + "'" for s in CANCELLED_STATUSES)
    agency_filter = '' if include_affiliate else f'AND {GHR_AGENCY_PREDICATE}'

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
            LTRIM(RTRIM(o.Contract_ID))                 AS id,
            LTRIM(RTRIM(ISNULL(o.First_Name, '') + ' ' + ISNULL(o.Last_Name, ''))) AS clinician,
            o.Health_System                             AS health_system,
            o.Facility                                  AS facility,
            o.Unit                                      AS unit,
            o.Position_Type                             AS role,
            o.Care_Type                                 AS care_type,
            o.Program                                   AS program,
            o.Time_Type                                 AS time_type,
            CASE WHEN {GHR_AGENCY_PREDICATE} THEN 'GHR' ELSE 'Affiliate' END AS source,
            o.Agency                                    AS agency,
            o.Contract_Status                           AS contract_status,
            CAST(o.Start_Date AS DATE)                  AS current_start,
            CAST(h.first_start AS DATE)                 AS original_start,
            CAST(o.End_Date AS DATE)                    AS end_date,
            -- distinct_starts counts values seen in history, so moves =
            -- values - 1. History only covers snapshots taken so far, so a
            -- start changed since the last load shows as a single distinct
            -- value that no longer matches the live row — count that as one
            -- more move, otherwise very recent slips read as "on track".
            (ISNULL(h.distinct_starts, 1) - 1)
                + CASE WHEN h.last_start IS NOT NULL AND o.Start_Date <> h.last_start
                       THEN 1 ELSE 0 END                AS move_count,
            -- Positive = slipped later. Negative = pulled earlier.
            DATEDIFF(DAY, h.first_start, o.Start_Date)  AS days_delayed,
            DATEDIFF(DAY, CAST(GETDATE() AS DATE), o.Start_Date) AS days_until_start,
            o.Delayed_Starts                            AS delayed_flag,
            o.Delayed_Starts_Reasons                    AS delay_reason,
            o.Unfilled_Reason                           AS unfilled_reason,
            o.Account_Manager                           AS account_manager,
            o.Hiring_Manager                            AS hiring_manager,
            TRY_CAST(o.Awarded_Rate AS DECIMAL(10,2))   AS awarded_rate,
            TRY_CAST(o.Hours_per_Peek AS DECIMAL(10,2)) AS hours_per_week
        FROM dhc.B4HealthOrder o WITH (NOLOCK)
        LEFT JOIN hist h ON h.cid = LTRIM(RTRIM(o.Contract_ID))
        WHERE o.Start_Date BETWEEN DATEADD(DAY, ?, CAST(GETDATE() AS DATE))
                               AND DATEADD(DAY, ?, CAST(GETDATE() AS DATE))
            {agency_filter}
        ORDER BY ISNULL(h.distinct_starts, 1) DESC, o.Start_Date ASC
    ''', -abs(lookback), lookahead)

    columns = [c[0] for c in cursor.description]
    rows = []
    for row in cursor.fetchall():
        r = dict(zip(columns, row))
        for k in ('current_start', 'original_start', 'end_date'):
            if r.get(k) is not None:
                r[k] = r[k].isoformat() if hasattr(r[k], 'isoformat') else str(r[k])
        for k in ('awarded_rate', 'hours_per_week'):
            if r.get(k) is not None:
                r[k] = float(r[k])

        moves = r.get('move_count') or 0
        delayed = r.get('days_delayed') or 0
        status = (r.get('contract_status') or '')

        # Stage grouping, mirroring the four buckets the Onboarding view renders.
        if status in CANCELLED_STATUSES:
            group = 'CANCELED'
        elif delayed >= 7:
            group = 'DELAYED START'
        elif moves > 0:
            group = 'START DATE CHANGED'
        else:
            group = 'ON TRACK'
        r['group'] = group
        r['moved_multiple'] = moves >= 2
        rows.append(r)
    return rows


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
        rows = _onboarding_data(cursor, lookback, lookahead, include_affiliate)
        print(f"Onboarding: returning {len(rows)} rows (-{lookback}d/+{lookahead}d, affiliate={include_affiliate})")
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
