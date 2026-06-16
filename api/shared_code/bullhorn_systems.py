"""
Bullhorn account rollup.

Maps Bullhorn clientCorporationID → display name for the 8 non-MSP accounts
in the Bullhorn book. The current dashboard treats display name as a
"Health System" — same dimension that B4/VNDLY rollup feeds on the MSP side.

Symplr (Education) book — Reading SD, Allentown SD, DCIU — is in a separate
data source and not in this file. Will be added under shared_code/symplr_systems.py
once we have a Symplr connection.

Source: locked in with stakeholders 2026-06-XX based on top-12 non-MSP
account list. Active-placement counts captured during ID verification:

  Orlando Health    : 144
  Solventum         :  61
  Montefiore        :  51
  Cone Health       :  38
  Memorial Hermann  :  28
  Lakeland Regional :  28
  University of Miami: 20
  CarolinaEast      :  15

Duke Health was on the original top-12 list but is dropped — 1 active
placement total, most volume in Completed/Termination status (closed
historical book, not actively serviced).
"""

# system_name → list of Bullhorn clientCorporationIDs that roll up to it.
# Order here determines the default sort order in the Trend / breakdown tables.
BULLHORN_SYSTEM_ROLLUP = [
    {'system_name': 'Orlando Health',       'client_ids': [8227]},
    {'system_name': 'Solventum',            'client_ids': [2566]},
    {'system_name': 'Montefiore',           'client_ids': [4116]},
    {'system_name': 'Memorial Hermann',     'client_ids': [8186]},
    {'system_name': 'Lakeland Regional',    'client_ids': [2936]},
    {'system_name': 'Cone Health',          'client_ids': [5127, 18726, 5245, 361020]},
    {'system_name': 'CarolinaEast Health',  'client_ids': [5154]},
    {'system_name': 'University of Miami',  'client_ids': [10324, 371546, 10325]},
]


# Flattened reverse-lookup: clientCorporationID → system_name.
# Used by every Bullhorn endpoint to label rows.
def _build_reverse_map():
    out = {}
    for entry in BULLHORN_SYSTEM_ROLLUP:
        for cid in entry['client_ids']:
            out[cid] = entry['system_name']
    return out


CLIENT_ID_TO_SYSTEM = _build_reverse_map()


def get_system_for_client_id(client_id):
    """Returns display name for a clientCorporationID, or None if unmapped."""
    return CLIENT_ID_TO_SYSTEM.get(client_id)


def all_in_scope_client_ids():
    """Flat list of every clientCorporationID in scope for the non-MSP dashboard."""
    return list(CLIENT_ID_TO_SYSTEM.keys())


def build_system_case_expr(column_name: str = 'p.clientCorporationID') -> str:
    """
    Returns a SQL CASE WHEN ... expression that maps clientCorporationID to
    its rolled-up system_name. Use this in any Bullhorn query that needs
    a 'health_system' column.

    Example:
        cursor.execute(f'''
            SELECT {build_system_case_expr()} AS health_system, ...
            FROM dbo.View_Placement p ...
        ''')
    """
    parts = ['CASE']
    for entry in BULLHORN_SYSTEM_ROLLUP:
        ids = ', '.join(str(i) for i in entry['client_ids'])
        # system_name is hard-coded data, safe to inline. Still single-quote-escape
        # in case future names contain apostrophes (e.g. "Children's").
        name_safe = entry['system_name'].replace("'", "''")
        parts.append(f"    WHEN {column_name} IN ({ids}) THEN '{name_safe}'")
    parts.append('    ELSE NULL')
    parts.append('END')
    return '\n'.join(parts)


def build_scope_filter(column_name: str = 'p.clientCorporationID') -> str:
    """
    Returns a SQL fragment to filter placements to in-scope non-MSP accounts.

    Example:
        cursor.execute(f'''
            SELECT ... FROM dbo.View_Placement p
            WHERE p.isDeleted = 0
              AND {build_scope_filter()}
              ...
        ''')
    """
    ids = ', '.join(str(i) for i in all_in_scope_client_ids())
    return f'{column_name} IN ({ids})'
