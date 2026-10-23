"""Stage 5 of the extension workflow: daily approvals and daily accepted
extensions, for the panel at the top of the Extensions tab.

No snapshotting. Both numbers are already stored as dates, so the series is a
GROUP BY rather than something that has to be accumulated going forward:

  B4    Offer_Date   = the client approving      (the hospital offering to the
                                                  agency, which is why the
                                                  AGENCY is what declines next)
        Date_Awarded = accepted. This is also the spec's "count accepted
                       extensions only when the modification moves to Work
                       Order" -- an awarded submission IS that conversion.

  VNDLY the modifications feed, which carries real transition history but needs
        handling. See _vndly_daily.

The two books are returned separately and NOT summed. B4's dates are real
events; VNDLY's statuses are a different and looser vocabulary, and only B4 can
distinguish client from vendor. Adding them would merge two definitions of
"accepted" into one misleading number.
"""
import azure.functions as func
import pyodbc
import os
import json
import time

from shared_code.auth import require_allowed_domain

# Same GHR scoping the extensions tab uses, so the panel and the list below it
# cannot disagree about whose extensions these are.
B4_GHR = "(cs.Agency_Name LIKE '%GHR%' OR cs.Agency_Name LIKE '%Planet Healthcare%')"
VNDLY_GHR = "(w.[Vendor Name] LIKE '%GHR%' OR w.[Vendor Name] LIKE '%Planet Healthcare%')"

DEFAULT_DAYS = 30
MAX_DAYS = 180


def _b4_daily(cursor, days):
    """Approvals and acceptances per day, from the submission dates.

    Scoped to extensions (Parent_Contract_Assignment_ID is set) -- a first
    placement being awarded is not an extension being accepted.
    """
    cursor.execute(f"""
        SELECT CONVERT(VARCHAR(10), d, 23) AS day,
               SUM(approvals) AS approvals,
               SUM(accepted)  AS accepted
        FROM (
            SELECT CAST(cs.Offer_Date AS DATE) AS d, 1 AS approvals, 0 AS accepted
            FROM dhc.B4Health_Contract_Submissions cs WITH (NOLOCK)
            WHERE cs.Parent_Contract_Assignment_ID IS NOT NULL
              AND cs.Offer_Date >= DATEADD(DAY, -?, CAST(GETDATE() AS DATE))
              AND {B4_GHR}
            UNION ALL
            SELECT CAST(cs.Date_Awarded AS DATE), 0, 1
            FROM dhc.B4Health_Contract_Submissions cs WITH (NOLOCK)
            WHERE cs.Parent_Contract_Assignment_ID IS NOT NULL
              AND cs.Date_Awarded >= DATEADD(DAY, -?, CAST(GETDATE() AS DATE))
              AND {B4_GHR}
        ) z
        GROUP BY d
        ORDER BY d
    """, days, days)
    return [dict(zip([c[0] for c in cursor.description], r)) for r in cursor.fetchall()]


def _vndly_daily(cursor, days):
    """Per-day status events from the work-order modifications feed.

    Three things this feed will get wrong if taken at face value, all measured
    2026-10-12 across the 1,035 'Date Extension' rows:

    1. DISTINCT is mandatory. One event on CUH-3-14 is stored six times
       identically; 1,035 raw rows are 813 distinct events. Counting rows
       overstates a day's activity by roughly a quarter.
    2. [Revision Number] is NOT a time ordering -- revision 8 precedes
       revision 7 on that same work order -- so ordering is by [Last Modified]
       alone. [Is Latest Modification] is 'Yes' on nearly everything and cannot
       be trusted either.
    3. 417 of the rows carry no Status at all. They are real modifications that
       never got one, so they are excluded rather than bucketed as unknown;
       the series is built on the 478 events that have a status.

    The vocabulary is None / Submitted / Accepted / Cancelled. There is no
    client-versus-vendor distinction anywhere in it, which is why this returns
    its own column names instead of pretending to match B4's states.
    """
    cursor.execute(f"""
        SELECT CONVERT(VARCHAR(10), lm, 23) AS day,
               SUM(CASE WHEN st = 'Submitted' THEN 1 ELSE 0 END) AS submitted,
               SUM(CASE WHEN st = 'Accepted'  THEN 1 ELSE 0 END) AS accepted,
               SUM(CASE WHEN st = 'Cancelled' THEN 1 ELSE 0 END) AS cancelled
        FROM (
            SELECT DISTINCT
                   CAST(m.WOSystemKey AS NVARCHAR(100))  AS wo,
                   LTRIM(RTRIM(m.[Status]))              AS st,
                   CAST(m.[Last Modified] AS DATE)       AS lm
            FROM dbo.STAGING_VNDLY_WORKODER_MODIFICATIONS m WITH (NOLOCK)
            LEFT JOIN dbo.STAGING_VNDLY_WORKORDERS w WITH (NOLOCK)
                   ON w.WOSystemKey = m.WOSystemKey
            WHERE LTRIM(RTRIM(m.[Reason for Modification])) = 'Date Extension'
              AND NULLIF(LTRIM(RTRIM(m.[Status])), '') IS NOT NULL
              AND m.[Last Modified] >= DATEADD(DAY, -?, CAST(GETDATE() AS DATE))
              AND (w.WOSystemKey IS NULL OR {VNDLY_GHR})
        ) z
        GROUP BY lm
        ORDER BY lm
    """, days)
    return [dict(zip([c[0] for c in cursor.description], r)) for r in cursor.fetchall()]


def main(req: func.HttpRequest) -> func.HttpResponse:
    auth_error = require_allowed_domain(req)
    if auth_error:
        return auth_error

    try:
        days = int(req.params.get('days') or DEFAULT_DAYS)
    except (TypeError, ValueError):
        days = DEFAULT_DAYS
    days = max(1, min(days, MAX_DAYS))

    conn = None
    started = time.time()
    payload = {'days': days, 'b4': [], 'vndly': [], 'errors': []}
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

        # Independent: one book failing should not blank the other, the same
        # way the extensions tab reports a partial book rather than nothing.
        for name, fn in (('b4', _b4_daily), ('vndly', _vndly_daily)):
            try:
                payload[name] = fn(cursor, days)
            except Exception as e:
                print(f"extension-metrics {name}: {e}")
                payload['errors'].append(f"{name}: {e}")

        payload['ms'] = int((time.time() - started) * 1000)
        return func.HttpResponse(json.dumps(payload, default=str),
                                 mimetype='application/json', status_code=200)
    except Exception as e:
        print(f"extension-metrics failed: {e}")
        return func.HttpResponse(json.dumps({'error': str(e)}),
                                 mimetype='application/json', status_code=500)
    finally:
        if conn is not None:
            conn.close()
