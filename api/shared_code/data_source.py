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
    return pyodbc.connect(
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={os.environ['BULLHORN_HOST']};"
        f"DATABASE={os.environ['BULLHORN_DB']};"
        f"UID={os.environ['BULLHORN_USER']};"
        f"PWD={os.environ['BULLHORN_PASSWORD']};"
        f"TrustServerCertificate=yes;"
        f"Encrypt=yes"
    )


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
    return pyodbc.connect(
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={host};"
        f"DATABASE={db};"
        f"UID={user};"
        f"PWD={pwd};"
        f"TrustServerCertificate=yes;"
        f"Encrypt=yes"
    )
