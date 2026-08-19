import azure.functions as func
import pyodbc
import os
import json
from shared_code.auth import require_allowed_domain


# GHR-owned agencies in B4Health. Verified against the live extension window:
# 'GHR Allied', 'GHR Acute', 'GHR Travel'. Everything else is an affiliate on
# the MSP panel, and 'Planet Healthcare' is GHR-family the same way the
# submission logic in GetPositions treats it.
GHR_AGENCY_PREDICATE = "(o.Agency LIKE 'GHR%' OR o.Agency LIKE '%Planet Healthcare%')"

# Contracts that are cancelled or were never awarded aren't extension
# candidates — there's no seat to extend.
EXTENSION_EXCLUDED_STATUSES = ('Closed And Cancelled', 'Closed Not Awarded')

# How far ahead the Extensions stage looks. Matches the stage copy: "GHR
# contracts ending within 45 days."
EXTENSION_HORIZON_DAYS = 45


def _extensions_data(cursor, horizon_days, include_affiliate):
    """Rows for the Extensions meeting stage: seats ending inside the horizon.

    Urgency is derived from days-left rather than stored, so it stays correct
    without a nightly job. The client *decision* (Offered / Pending Acceptance
    / Approved / No Decision) is deliberately NOT sourced here — B4Health has
    no such column. That state is captured during the IMPACT meeting and lives
    in the app DB, so this endpoint returns only what the source system knows.
    """
    status_list = ', '.join("'" + s.replace("'", "''") + "'" for s in EXTENSION_EXCLUDED_STATUSES)
    agency_filter = '' if include_affiliate else f'AND {GHR_AGENCY_PREDICATE}'

    cursor.execute(f'''
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
            CAST(o.Start_Date AS DATE)                  AS start_date,
            CAST(o.End_Date AS DATE)                    AS end_date,
            DATEDIFF(DAY, CAST(GETDATE() AS DATE), o.End_Date) AS days_left,
            CASE WHEN {GHR_AGENCY_PREDICATE} THEN 'GHR' ELSE 'Affiliate' END AS source,
            o.Agency                                    AS agency,
            o.Contract_Status                           AS contract_status,
            o.Account_Manager                           AS account_manager,
            o.Hiring_Manager                            AS hiring_manager,
            o.Cost_Center                               AS cost_center,
            TRY_CAST(o.Awarded_Rate AS DECIMAL(10,2))   AS awarded_rate,
            TRY_CAST(o.Hours_per_Peek AS DECIMAL(10,2)) AS hours_per_week,
            -- 13-week forward value of the seat if it extends.
            CAST(
                TRY_CAST(o.Awarded_Rate AS DECIMAL(10,2))
                * TRY_CAST(o.Hours_per_Peek AS DECIMAL(10,2))
                * 13 AS DECIMAL(12,2)
            )                                           AS extension_value_13wk,
            -- A seat that already carries a parent is itself an extension, so
            -- the relationship is worth surfacing in the meeting.
            CASE WHEN o.Parent_Contract_ID IS NOT NULL AND LTRIM(RTRIM(o.Parent_Contract_ID)) <> ''
                 THEN 1 ELSE 0 END                      AS is_extension,
            LTRIM(RTRIM(ISNULL(o.Parent_Contract_ID, ''))) AS parent_contract_id
        FROM dhc.B4HealthOrder o WITH (NOLOCK)
        WHERE o.End_Date BETWEEN CAST(GETDATE() AS DATE)
                             AND DATEADD(DAY, ?, CAST(GETDATE() AS DATE))
            AND o.Contract_Status NOT IN ({status_list})
            {agency_filter}
        ORDER BY o.End_Date ASC
    ''', horizon_days)

    columns = [c[0] for c in cursor.description]
    rows = []
    for row in cursor.fetchall():
        r = dict(zip(columns, row))
        for k in ('start_date', 'end_date'):
            if r.get(k) is not None:
                r[k] = r[k].isoformat() if hasattr(r[k], 'isoformat') else str(r[k])
        for k in ('awarded_rate', 'hours_per_week', 'extension_value_13wk'):
            if r.get(k) is not None:
                r[k] = float(r[k])
        days = r.get('days_left')
        # Mirrors the stage legend: deeper red = fewer days to secure a decision.
        r['urgency'] = (
            'critical' if days is not None and days <= 7 else
            'high'     if days is not None and days <= 14 else
            'medium'   if days is not None and days <= 21 else
            'low'
        )
        rows.append(r)
    return rows


def main(req: func.HttpRequest) -> func.HttpResponse:
    auth_error = require_allowed_domain(req)
    if auth_error:
        return auth_error

    try:
        horizon = int(req.params.get('days') or EXTENSION_HORIZON_DAYS)
    except (TypeError, ValueError):
        horizon = EXTENSION_HORIZON_DAYS
    # The stage is a delivery view (GHR only) by default; affiliate rows are
    # opt-in so the same endpoint can back a panel-wide comparison later.
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
        rows = _extensions_data(cursor, horizon, include_affiliate)
        print(f"Extensions: returning {len(rows)} rows (horizon {horizon}d, affiliate={include_affiliate})")
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
