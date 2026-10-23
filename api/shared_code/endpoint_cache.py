"""
Payload cache for the endpoints whose cost does not belong on the request path.

WHY
---
Measured on the deployed non-MSP instance, the heavy endpoints were:

    trend-data      9.3s -> 16.8s once the scope widened   (2.1 -> 4.7 MB)
    financial-data  9.2s -> 19.9s, and intermittently 500  (2.8 -> 6.7 MB)
    hours-data      13.6s                                  (17 MB)

The 500 is the part that matters: those two are slow enough that a cold plan
tips them past the gateway, which is what the divisions saw as "Couldn't load
financial data".

Widening the non-MSP scope is what pushed them over, and the reason is written
up in the sibling ghr-salespulse app, which hit the same wall on a 1.76M-row
aggregate:

    "With ~974 corps in scope SQL Server stops seeking on
     IX_ClientContact_clientCorporationID and scans instead: measured 104
     SECONDS cold. A per-page OUTER APPLY was not a fix either (3s cold,
     26.7s warm -- just a different bad plan)."

That is our shape exactly: the trend query carries two OUTER APPLYs evaluated
per placement row. salespulse's answer was to stop optimising the query and
take it off the request path -- a cache table refreshed on a schedule, and a
trivial SELECT to serve it. This is the same pattern, generalised over a
payload rather than a single count, because what we need cached is a whole
response rather than one number.

WHAT IS SAFE TO CACHE HERE
--------------------------
Trend is a four-week lookback plus a four-week projection; Financials is
monthly billings. Neither moves meaningfully within a day, and both are read
to answer "how are we tracking", not "what is true this second". Anything a
user edits -- workspace state, extension decisions -- is NOT cached and never
should be.

FAIL-OPEN, ALWAYS
-----------------
Every function here swallows its own errors. A missing table, an unconfigured
app DB or a bad row must never turn a working endpoint into a failing one: the
caller simply computes live, which is what it did before this existed.
"""
import json
import os
from datetime import datetime, timezone

from shared_code.data_source import get_appdb_conn


# Serve a cached payload up to this old. Beyond it the caller computes live, so
# a scheduler that stops running degrades to today's behaviour rather than
# serving last month's numbers.
DEFAULT_MAX_AGE_SECONDS = 26 * 3600  # a day, plus room for a late refresh

_DDL = '''
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('impactmgr.endpoint_cache'))
BEGIN
    CREATE TABLE impactmgr.endpoint_cache (
        cache_key    NVARCHAR(120)  NOT NULL CONSTRAINT PK_endpoint_cache PRIMARY KEY,
        payload      NVARCHAR(MAX)  NOT NULL,
        bytes        INT            NULL,
        build_ms     INT            NULL,
        refreshed_at DATETIME2      NOT NULL CONSTRAINT DF_endpoint_cache_at DEFAULT SYSUTCDATETIME(),
        refreshed_by NVARCHAR(120)  NULL
    );
END
'''


def ensure_table(conn):
    """Idempotent create. Cheap enough to call before a write."""
    try:
        cur = conn.cursor()
        cur.execute(_DDL)
        conn.commit()
        return True
    except Exception as e:
        print(f'endpoint_cache: ensure_table failed: {e}')
        return False


# Bump when a cached payload's SHAPE changes -- a new field, a renamed one, a
# different structure. A deploy does not clear the cache table, so without this
# a shape change is invisible for up to 26 hours: the endpoint returns the old
# payload, built by the old code, and looks like the deploy did nothing. That is
# exactly what adding recorded_rto to extensions-data would have done.
#
# Changing this string makes every existing entry unreachable, so the next
# request to each endpoint rebuilds. Old rows age out on their own.
PAYLOAD_SHAPE = 'v13'


def cache_key(route, data_source, variant=None):
    """
    One entry per route per book -- MSP and non-MSP are different answers --
    and per `variant` where a parameter changes the payload.

    `variant` exists because caching only the bare URL cached a URL nobody
    asks for. MSP requests extensions-data and onboarding-data with
    ?includeAffiliate=1, so the default-only cache was never read on that
    book: those endpoints stayed at 35-41s while the uncalled default served
    in 265ms. Encode the parameter in the key instead of refusing to cache it.
    """
    base = f'{route}:{data_source}'
    if variant:
        base = f'{base}:{variant}'
    return f'{base}:{PAYLOAD_SHAPE}'


def read_cache(key, max_age_seconds=DEFAULT_MAX_AGE_SECONDS):
    """
    The cached payload as a dict, or None to mean 'compute it live'.

    None covers every failure: no app DB, no table, no row, stale row, or
    unparseable JSON. The caller cannot tell them apart and should not need to.
    """
    conn = None
    try:
        conn = get_appdb_conn()
        if conn is None:
            return None
        cur = conn.cursor()
        cur.execute(
            'SELECT payload, refreshed_at FROM impactmgr.endpoint_cache WHERE cache_key = ?',
            key)
        row = cur.fetchone()
        if not row or not row[0]:
            return None
        refreshed_at = row[1]
        if refreshed_at is not None:
            if refreshed_at.tzinfo is None:
                refreshed_at = refreshed_at.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - refreshed_at).total_seconds()
            if age > max_age_seconds:
                print(f'endpoint_cache: {key} is {int(age)}s old, past {max_age_seconds}s -- computing live')
                return None
        payload = json.loads(row[0])
        # Stamped so the UI can say how fresh this is rather than implying it
        # is live. The endpoints pass this straight through.
        #
        # Only an object can carry the stamp. extensions-data returns a bare
        # JSON array, and stamping it raised TypeError on every read -- which
        # the except below swallowed, so the cache wrote correctly, reported
        # itself healthy in cache_status, and never served a single request.
        # Fail-open is right here, but it hid this completely; the only visible
        # symptom was that a cached endpoint stayed as slow as an uncached one.
        if isinstance(payload, dict):
            payload['cachedAt'] = refreshed_at.isoformat() if refreshed_at else None
        return payload
    except Exception as e:
        print(f'endpoint_cache: read {key} failed, computing live: {e}')
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def write_cache(key, payload, build_ms=None, refreshed_by=None):
    """
    Store a payload. Best-effort: returns True/False and never raises, so a
    failed write cannot fail the request that produced the data.
    """
    conn = None
    try:
        conn = get_appdb_conn()
        if conn is None:
            return False
        ensure_table(conn)
        body = json.dumps(payload, default=str)
        cur = conn.cursor()
        # MERGE so a refresh updates in place, matching WorkspaceState.
        cur.execute('''
            MERGE impactmgr.endpoint_cache AS t
            USING (SELECT ? AS cache_key) AS s
              ON t.cache_key = s.cache_key
            WHEN MATCHED THEN UPDATE SET
                payload = ?, bytes = ?, build_ms = ?,
                refreshed_at = SYSUTCDATETIME(), refreshed_by = ?
            WHEN NOT MATCHED THEN INSERT
                (cache_key, payload, bytes, build_ms, refreshed_by)
                VALUES (?, ?, ?, ?, ?);
        ''', key, body, len(body), build_ms, refreshed_by,
             key, body, len(body), build_ms, refreshed_by)
        conn.commit()
        print(f'endpoint_cache: wrote {key} ({len(body)} bytes, built in {build_ms}ms)')
        return True
    except Exception as e:
        print(f'endpoint_cache: write {key} failed (non-fatal): {e}')
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def cache_status():
    """Every cached entry, for a refresh endpoint or a health check."""
    conn = None
    try:
        conn = get_appdb_conn()
        if conn is None:
            return []
        cur = conn.cursor()
        cur.execute('''SELECT cache_key, bytes, build_ms, refreshed_at, refreshed_by
                       FROM impactmgr.endpoint_cache ORDER BY cache_key''')
        return [{'key': r[0], 'bytes': r[1], 'buildMs': r[2],
                 'refreshedAt': r[3].isoformat() if r[3] else None,
                 'refreshedBy': r[4]} for r in cur.fetchall()]
    except Exception as e:
        print(f'endpoint_cache: status failed: {e}')
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
