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
