"""
Symplr account rollup.

Maps Symplr profile_client.recordid → display name for the 3 non-MSP Education
accounts in the Symplr book. The current dashboard treats display name as a
"Health System" — same dimension that Bullhorn rollup feeds.

Symplr data model differs from Bullhorn:
  - lt_order  = long-term order (multi-week placement, one row per assignment)
  - orders    = shift-level rows under each lt_order, carry billrate/hours/etc.
  - profile_client.recordid is the client PK
  - profile_temp.recordid is the worker PK

Source: locked in 2026-06-XX based on customer ID lists provided by stakeholders.
84531 is "Delaware County Intermediate Unit - Early Intervention", a sub-rollup
that folds under DCIU.
"""

# system_name → list of Symplr profile_client.recordid that roll up to it.
# Order here determines the default sort order in the breakdown tables.
SYMPLR_SYSTEM_ROLLUP = [
    {'system_name': 'Reading School District',   'client_ids': [5890, 26917, 40136]},
    {'system_name': 'Allentown School District', 'client_ids': [5685, 34792, 34793]},
    {'system_name': 'DCIU',                      'client_ids': [5874, 14146, 84531]},
]


def _build_reverse_map():
    out = {}
    for entry in SYMPLR_SYSTEM_ROLLUP:
        for cid in entry['client_ids']:
            out[cid] = entry['system_name']
    return out


CLIENT_ID_TO_SYSTEM = _build_reverse_map()


def get_system_for_client_id(client_id):
    """Returns display name for a profile_client.recordid, or None if unmapped."""
    return CLIENT_ID_TO_SYSTEM.get(client_id)


def all_in_scope_client_ids():
    """Flat list of every profile_client.recordid in scope for the non-MSP dashboard."""
    return list(CLIENT_ID_TO_SYSTEM.keys())


def build_system_case_expr(column_name: str = 'lt.clientid') -> str:
    """
    Returns a SQL CASE WHEN ... expression that maps a Symplr client ID column
    to its rolled-up system_name.

    Example:
        cursor.execute(f'''
            SELECT {build_system_case_expr('lt.clientid')} AS health_system, ...
            FROM dbo.lt_order lt ...
        ''')
    """
    parts = ['CASE']
    for entry in SYMPLR_SYSTEM_ROLLUP:
        ids = ', '.join(str(i) for i in entry['client_ids'])
        name_safe = entry['system_name'].replace("'", "''")
        parts.append(f"    WHEN {column_name} IN ({ids}) THEN '{name_safe}'")
    parts.append('    ELSE NULL')
    parts.append('END')
    return '\n'.join(parts)


def build_scope_filter(column_name: str = 'lt.clientid') -> str:
    """
    Returns a SQL fragment to filter rows to in-scope Symplr Education accounts.

    Example:
        cursor.execute(f'''
            SELECT ... FROM dbo.lt_order lt
            WHERE {build_scope_filter('lt.clientid')}
              ...
        ''')
    """
    ids = ', '.join(str(i) for i in all_in_scope_client_ids())
    return f'{column_name} IN ({ids})'
