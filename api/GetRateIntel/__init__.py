import azure.functions as func
import pyodbc
import os
import json
from shared_code.auth import require_allowed_domain
from shared_code.data_source import is_non_msp
from shared_code.credentials import (
    normalize as normalize_credential,
    service_line as credential_service_line,
)


# How far back the peer set reaches. 90 days is what the feedback asked for on
# comparable placements, and it keeps the payload at ~22k rows.
PEER_LOOKBACK_DAYS = 90

# Rates outside this band are data entry errors, not market signal. One RN
# Case Management req carried $1,100/hr -- a weekly or monthly figure typed
# into an hourly field -- which alone lifted its group's average from roughly
# $90 to $144 and made every other req in that group look far below market.
# bt_Outlier did not catch it, so the bound is enforced here as well.
RATE_MIN, RATE_MAX = 20, 400


def _peer_rows(cursor):
    """Individual job-order rates, for ranking client-side.

    Ships the peers rather than a precomputed rank on purpose. Filters in this
    app are client-side -- they narrow already-loaded rows and never refetch --
    so a rank computed server-side would be locked to one scope and would stop
    agreeing with the System picker the moment anyone touched it. With the
    peers in hand the client can rank within whatever the active filters leave,
    and the same number follows every filter for free.

    Source is BH_BILL_RATE_TRENDS_OUTLIERS_FACT, which despite the name holds
    the whole population: 35.07M rated rows over 288,348 job orders, of which
    only 7% carry bt_Outlier. The flag marks which rates are outliers; it does
    not filter the table.

    Two filters are not optional. Group_Context and Sensitivity_Level pin the
    grain: the same job order appears once per grouping context (six of them)
    and once per sensitivity level (ten), so without them every job counts
    dozens of times and the "of N" in a rank is meaningless.
    """
    cursor.execute(f'''
        SELECT DISTINCT
            o.jobOrderID                                   AS job_id,
            o.Profession                                   AS profession,
            o.Specialty                                    AS specialty,
            CAST(o.clientBillRate AS DECIMAL(9,2))         AS rate,
            ISNULL(NULLIF(LTRIM(RTRIM(c.ParentAccount)), ''), c.FacilityName) AS account,
            -- Week start, Sunday-based, matching the DATEFIRST 7 the app pins
            -- for every other week bucket and the Saturday period ends in the
            -- rate trend tables.
            DATEADD(DAY, -((DATEPART(WEEKDAY, o.dateAdded_date) + 5) % 7),
                    o.dateAdded_date)                      AS week_start,
            CAST(o.bt_Outlier AS INT)                      AS is_outlier
        FROM dbo.BH_BILL_RATE_TRENDS_OUTLIERS_FACT o WITH (NOLOCK)
        LEFT JOIN dbo.CLIENT_DIM c WITH (NOLOCK)
               ON c.Source_Client_ID = o.clientCorporationID
        WHERE o.Group_Context = 'Profession,Specialty'
          AND o.Sensitivity_Level = 1
          AND o.clientBillRate BETWEEN {RATE_MIN} AND {RATE_MAX}
          AND o.dateAdded_date >= DATEADD(DAY, -{PEER_LOOKBACK_DAYS}, CAST(GETDATE() AS DATE))
    ''')
    cols = [c[0] for c in cursor.description]
    out = []
    for row in cursor.fetchall():
        r = dict(zip(cols, row))
        raw = r.get('profession')
        out.append({
            'jobId': str(r['job_id']),
            # Normalised through the same vocabulary the rest of the app uses,
            # so an RN peer matches an RN job whichever book named it.
            'profession': normalize_credential(raw) or (raw or ''),
            'serviceLine': credential_service_line(raw),
            'specialty': (r.get('specialty') or '').strip(),
            'account': (r.get('account') or '').strip(),
            'week': r['week_start'].isoformat() if hasattr(r['week_start'], 'isoformat') else str(r['week_start']),
            'rate': float(r['rate']) if r['rate'] is not None else None,
            'outlier': bool(r.get('is_outlier')),
        })
    return out


def main(req: func.HttpRequest) -> func.HttpResponse:
    auth_error = require_allowed_domain(req)
    if auth_error:
        return auth_error

    # Bullhorn job orders are the only rate population wide enough to rank
    # against, and they cover both books' roles. Non-MSP reads the same set.
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
        peers = _peer_rows(cursor)

        payload = {
            'peers': peers,
            'coverage': {
                'lookbackDays': PEER_LOOKBACK_DAYS,
                'rateBounds': [RATE_MIN, RATE_MAX],
                'rows': len(peers),
                'accounts': len({p['account'] for p in peers if p['account']}),
                'weeks': len({p['week'] for p in peers}),
                'outliersFlagged': sum(1 for p in peers if p['outlier']),
                # The client needs these to label a rank honestly rather than
                # guessing at thresholds the server chose.
                'minPeers': 3,
                'ordinalThreshold': 10,
                'dataSource': 'non_msp' if is_non_msp() else 'msp',
            },
        }
        print(f"RateIntel: {len(peers)} peer rows, "
              f"{payload['coverage']['accounts']} accounts, "
              f"{payload['coverage']['outliersFlagged']} flagged")
        return func.HttpResponse(json.dumps(payload, default=str),
                                 mimetype='application/json', status_code=200)
    except Exception as e:
        print(f'RateIntel error: {e}')
        import traceback
        traceback.print_exc()
        return func.HttpResponse(json.dumps({'error': str(e)}),
                                 mimetype='application/json', status_code=500)
    finally:
        if conn is not None:
            conn.close()
