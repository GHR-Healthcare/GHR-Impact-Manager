"""
Symplr account rollup.

Maps Symplr profile_client master records → display name for the 3 non-MSP
Education accounts in the Symplr book. The dashboard treats display name as
a "Health System" — same dimension that Bullhorn rollup feeds.

Symplr data model:
  - lt_order  = long-term order (multi-week placement, one row per assignment)
  - orders    = shift-level rows under each lt_order, carry billrate/hours/etc.
  - profile_client.recordid is the client PK
  - profile_client.MasterClientID points at the parent recordid (NULL on masters)
  - profile_temp.recordid is the worker PK

Scope is defined as a list of "master" recordids per system. Expansion picks
up every profile_client whose recordid is in the list OR whose MasterClientID
is in the list — so future sub-orgs (e.g. new "DCIU ECE - ..." sites) get
included automatically without code changes.

DCIU specifically owns 5 masters:
  - 5874   Delaware County Intermediate Unit (master, legacy)
  - 14146  Delaware County Intermediate Unit (master, duplicate name)
  - 84531  Delaware County Intermediate Unit - Early Intervention (master)
  - 122454 DCIU School Age (master — parents ~30 "School Age" sub-orgs)
  - 122455 DCIU ECE (master — parents ~30 "ECE" / "EI" sub-orgs)
"""

# system_name → list of Symplr profile_client masters (parent recordids) that
# roll up to it. Children whose MasterClientID points at any of these masters
# are auto-included via the scope filter / case expression below.
# Order here determines the default sort order in the breakdown tables.
# `division` is the internal GHR team owning the book of business — used as a
# top-level filter dimension in the dashboard.
SYMPLR_SYSTEM_ROLLUP = [
    {'system_name': 'Reading School District',   'division': 'Education', 'master_ids': [5890, 26917, 40136]},
    {'system_name': 'Allentown School District', 'division': 'Education', 'master_ids': [5685, 34792, 34793]},
    {'system_name': 'DCIU',                      'division': 'Education', 'master_ids': [5874, 14146, 84531, 122454, 122455]},
]


def all_in_scope_master_ids():
    """Flat list of every profile_client master recordid in scope."""
    out = []
    for entry in SYMPLR_SYSTEM_ROLLUP:
        out.extend(entry['master_ids'])
    return out


def get_manual_symplr_allowlist_ids(app_conn):
    """
    Symplr master IDs added manually via the Settings → Non-MSP Clients admin UI
    (source='symplr' rows in impactmgr.bullhorn_client_allowlist).

    Returns empty set if app DB is unreachable, the allowlist table doesn't
    exist yet, or the `source` column hasn't been added yet (in which case
    every row is treated as Bullhorn, so no Symplr entries exist).
    """
    if app_conn is None:
        return set()
    try:
        cursor = app_conn.cursor()
        cursor.execute("""
            IF EXISTS (
                SELECT 1 FROM sys.columns
                WHERE object_id = OBJECT_ID('impactmgr.bullhorn_client_allowlist')
                  AND name = 'source'
            )
                SELECT client_id FROM impactmgr.bullhorn_client_allowlist WHERE source = 'symplr'
            ELSE
                SELECT TOP 0 CAST(NULL AS INT) AS client_id
        """)
        return {int(row[0]) for row in cursor.fetchall() if row[0] is not None}
    except Exception as e:
        print(f"get_manual_symplr_allowlist_ids: swallowed error, returning empty set: {e}")
        return set()


def resolve_scope_master_ids(app_conn=None):
    """
    Effective Symplr master-ID scope: hardcoded rollup ∪ manual allowlist
    (source='symplr' entries in the shared allowlist table). Call at the
    top of any Symplr endpoint that needs a wider-than-hardcoded scope.
    """
    return set(all_in_scope_master_ids()) | get_manual_symplr_allowlist_ids(app_conn)


def _expansion_subquery(master_ids):
    """SQL subquery that resolves to every recordid in scope of the given
    masters — the masters themselves plus any client whose MasterClientID
    points at one of them."""
    ids = ', '.join(str(i) for i in master_ids) if master_ids else 'NULL'
    return (
        f"SELECT recordid FROM dbo.profile_client "
        f"WHERE recordid IN ({ids}) OR MasterClientID IN ({ids})"
    )


def build_system_case_expr(column_name: str = 'lt.clientid') -> str:
    """
    Returns a SQL CASE WHEN ... expression that maps a Symplr client ID
    column to its rolled-up system_name, expanding via MasterClientID.

    Example:
        cursor.execute(f'''
            SELECT {build_system_case_expr('lt.clientid')} AS health_system, ...
            FROM dbo.lt_order lt ...
        ''')
    """
    parts = ['CASE']
    for entry in SYMPLR_SYSTEM_ROLLUP:
        name_safe = entry['system_name'].replace("'", "''")
        sub = _expansion_subquery(entry['master_ids'])
        parts.append(f"    WHEN {column_name} IN ({sub}) THEN '{name_safe}'")
    parts.append('    ELSE NULL')
    parts.append('END')
    return '\n'.join(parts)


def build_scope_filter(column_name: str = 'lt.clientid', master_ids=None) -> str:
    """
    SQL fragment restricting rows to Symplr accounts in scope. Uses the
    hardcoded rollup by default; pass `master_ids` (a set/list — typically
    from resolve_scope_master_ids) to include the manual allowlist too.

    Example:
        cursor.execute(f'''
            SELECT ... FROM dbo.lt_order lt
            WHERE {build_scope_filter('lt.clientid', master_ids=scope_master_ids)}
        ''')
    """
    if master_ids is None:
        master_ids = all_in_scope_master_ids()
    ids = sorted(set(int(i) for i in master_ids if i is not None))
    if not ids:
        return '1 = 0'
    sub = _expansion_subquery(ids)
    return f'{column_name} IN ({sub})'


def build_division_case_expr(column_name: str = 'lt.clientid') -> str:
    """
    Returns a SQL CASE WHEN ... expression that maps a Symplr client ID column
    to its division (the internal GHR team owning that book of business).
    Uses the same MasterClientID expansion as build_system_case_expr.
    """
    parts = ['CASE']
    for entry in SYMPLR_SYSTEM_ROLLUP:
        division_safe = entry['division'].replace("'", "''")
        sub = _expansion_subquery(entry['master_ids'])
        parts.append(f"    WHEN {column_name} IN ({sub}) THEN '{division_safe}'")
    parts.append('    ELSE NULL')
    parts.append('END')
    return '\n'.join(parts)
