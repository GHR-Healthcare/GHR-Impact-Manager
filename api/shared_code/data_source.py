"""
Data-source dispatch.

The Impact Manager runs in two flavors:
  - DATA_SOURCE=msp     → unifies B4 + VNDLY (current default)
  - DATA_SOURCE=non_msp → unifies Bullhorn + Symplr (top non-MSP accounts)

Same git branch, same `main`, deployed to two Azure Static Web Apps with
different env config. Each endpoint's main() reads get_data_source() and
dispatches to a source-specific implementation. The frontend reads the
value from /api/get-config to hide tabs that don't apply (Pending,
Per Diem on non-MSP).
"""
import os
import pyodbc


VALID_SOURCES = {'msp', 'non_msp'}


def _pin_datefirst(conn):
    """
    Pin the session's DATEFIRST to 7 (Sunday) so every DATEPART(WEEKDAY, ...)
    in the app buckets on a Sunday boundary regardless of the server or
    connection language default. The frontend assumes Sunday-start weeks,
    so a session running under us_english (which is Sunday=1) is the assumed
    case — but a connection string or server default in any other language
    would shift every week bucket by one or more days without this.
    Idempotent; safe to run on every connection.
    """
    try:
        cursor = conn.cursor()
        cursor.execute("SET DATEFIRST 7")
        cursor.close()
    except Exception as e:
        # Non-fatal — if the driver rejects it for some reason, the app still
        # runs (just at the mercy of the server default). Log for the record.
        print(f"_pin_datefirst: swallowed error: {e}")
    return conn


def get_data_source() -> str:
    """
    Returns 'msp' (default) or 'non_msp'. Unknown values fall back to 'msp'
    so a typo in App Settings doesn't take the dashboard down.
    """
    ds = (os.environ.get('DATA_SOURCE') or 'msp').strip().lower()
    return ds if ds in VALID_SOURCES else 'msp'


def is_msp() -> bool:
    return get_data_source() == 'msp'


def is_non_msp() -> bool:
    return get_data_source() == 'non_msp'


def get_bullhorn_conn():
    """pyodbc connection to the Bullhorn mirror DB. Used by every non_msp endpoint."""
    return _pin_datefirst(pyodbc.connect(
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={os.environ['BULLHORN_HOST']};"
        f"DATABASE={os.environ['BULLHORN_DB']};"
        f"UID={os.environ['BULLHORN_USER']};"
        f"PWD={os.environ['BULLHORN_PASSWORD']};"
        f"TrustServerCertificate=yes;"
        f"Encrypt=yes"
    ))


def get_appdb_conn():
    """
    pyodbc connection to the writable app-config DB (impactmgr schema).
    Used for tables owned by this app: changes, history_snapshots,
    system_mappings, bullhorn_client_allowlist.

    Returns None if DB_HOST / APPDB / DB_USER / DB_PASSWORD aren't configured
    (e.g. local dev without the app DB). Callers should treat that as
    "app-managed config unavailable, fall back to code-managed defaults".
    """
    host = os.environ.get('DB_HOST')
    db = os.environ.get('APPDB')
    user = os.environ.get('DB_USER')
    pwd = os.environ.get('DB_PASSWORD')
    if not all([host, db, user, pwd]):
        return None
    return _pin_datefirst(pyodbc.connect(
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={host};"
        f"DATABASE={db};"
        f"UID={user};"
        f"PWD={pwd};"
        f"TrustServerCertificate=yes"
    ))


def get_symplr_conn():
    """
    pyodbc connection to the Symplr DB. Returns None if Symplr env vars aren't
    configured — callers should handle that as "Symplr not available, skip
    that half of the union". Used by every non_msp endpoint alongside Bullhorn.
    """
    host = os.environ.get('SYMPLR_HOST')
    db = os.environ.get('SYMPLR_DB')
    user = os.environ.get('SYMPLR_USER')
    pwd = os.environ.get('SYMPLR_PASSWORD')
    if not all([host, db, user, pwd]):
        return None
    return _pin_datefirst(pyodbc.connect(
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={host};"
        f"DATABASE={db};"
        f"UID={user};"
        f"PWD={pwd};"
        f"TrustServerCertificate=yes;"
        f"Encrypt=yes"
    ))

# Health systems whose MSP contract has moved from B4 to VNDLY.
#
# Lived in GetExtensions until 2026-10-30, when three other endpoints turned
# out to need the same list and two of them had drifted -- GetStatsData
# carried a hand-written Richmond/Redeemer exclusion that had never been
# updated for Cooper or Inspira, and GetOnboarding had none at all.
#
# THE RULE, set 2026-10-30. After a cutover B4 still holds real HISTORY for
# the system, so revenue, order counts and closed work keep reading it. What
# B4 must no longer supply is anything ACTIONABLE -- live assignments, seats
# to onboard, extensions to chase. Those moved to VNDLY, and the B4 copy is a
# leftover that shows the same person twice and puts finished work back in
# somebody's queue.
#
# The failure is not that B4 goes quiet after a cutover. It does not: B4 keeps
# future-dated open orders alive indefinitely, and the stale copy carries the
# PRE-migration end date, so the wrong row sits directly beneath the right one.
#
# Measured over the live extensions window (2026-10-27) and the actionable
# stats surfaces (2026-10-30). On the worst system roughly two thirds of its
# B4 seats were the same clinician as a live VNDLY row, and its stale B4
# "active" assignments outnumbered anything else the filter removes -- against
# a newest real B4 shift over two months earlier. Figures per system are in
# the local working notes, not here.
#
# One entry changes nothing today -- that system has no open B4 orders at all
# -- and is listed so the next stale order cannot reintroduce the bug. Seats
# that are B4-only still drop off: they are orders left open in B4 after the
# cutover, and the fix for those is closing them in B4, not showing them
# here.
#
# MATCHED AGAINST B4's SPELLING, NOT VNDLY's. B4 calls RUMC "Richmond
# University Medical Center" and Redeemer "Holy Redeemer Hospital", so the
# entries that look obvious -- 'RUMC', 'Redeemer Health' -- match nothing and
# fail silently: the exclusion looks applied and does nothing. A list rather
# than a blanket B4 exclusion because B4 is still the system of record
# everywhere else; each cutover adds one line here.
#
# Keep in step with CONSTANTS.TRANSITIONED_SYSTEMS in index.html, which is the
# same list under the rollup names the frontend uses.
B4_MIGRATED_TO_VNDLY = ('Cooper', 'Inspira', 'Richmond', 'Redeemer')


def b4_migrated_filter(column: str) -> str:
    """SQL that is TRUE for a row belonging to a migrated system.

    Callers wrap it in NOT (...) on actionable queries and leave historical
    ones alone. LIKE rather than equality because the same system is spelled
    differently across feeds and a cutover should not need a mapping table.
    """
    return ' OR '.join(f"{column} LIKE '%{m}%'" for m in B4_MIGRATED_TO_VNDLY)


# B4 statuses that mean the seat is over. A cancelled or never-awarded order
# is finished work: nobody is starting, so there is nothing to onboard and
# nothing to extend. Lived in GetExtensions until 2026-11-03, when Onboarding
# turned out to have no status exclusion at all and was still listing
# cancelled seats as people to chase.
B4_TERMINAL_STATUSES = ('Closed And Cancelled', 'Closed Not Awarded')


def b4_has_clinician(alias: str = 'o') -> str:
    """SQL that is TRUE only when a B4 order has somebody in the seat.

    B4 keeps an order row from the moment it is raised, long before anyone is
    awarded it, and an unawarded order carries no name and no agency. Those
    rows are requisitions out to the market, not work in progress: there is
    nobody to extend and nobody to onboard.

    Reported 2026-11-03 from the UPHS desk as extensions showing against
    Lancaster and Princeton contracts that B4 reports as closed and not
    awarded -- empty orders, one of them explicitly "Assignment Completed -
    extension offered", i.e. the seat ended and nobody took the extension.

    Tested on the PRESENCE OF A NAME rather than on Contract_Status, because
    the two do not line up. Every 'Closed And Awarded' order has a clinician,
    but 'With Requests' splits both ways -- mostly empty orders, and a handful
    of genuinely filled seats whose extension request is still out. A status
    list would drop those real seats; this does not.
    """
    return (f"LTRIM(RTRIM(ISNULL({alias}.First_Name, '') "
            f"+ ISNULL({alias}.Last_Name, ''))) <> ''")
