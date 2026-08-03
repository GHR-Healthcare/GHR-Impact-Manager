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
