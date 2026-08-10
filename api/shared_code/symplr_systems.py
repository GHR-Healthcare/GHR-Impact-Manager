"""
Symplr scope resolution + display taxonomy.

v2.2.0: dropped the hardcoded rollup. Scope is now data-driven, parallel to
how Bullhorn works: any client with active filled work in the recent window
gets pulled in automatically. Leaders can force additional clients via the
manual allowlist (source='symplr' rows in impactmgr.bullhorn_client_allowlist).

Symplr data model:
  - lt_order       = long-term order (multi-week placement, one row per assignment).
                     "Filled" means a worker is on it.
  - orders         = shift-level rows under each lt_order, carry billrate / hours / etc.
  - profile_client = client PK is `recordid`; `MasterClientID` optionally points
                     at a parent (sub-orgs roll up to their master). The `region`
                     column stores the region ID as a string (odd schema).
  - regions        = the region lookup. `regionname` is the display value —
                     "Education Nursing", "PA Nursing", "GHR Education MSP", etc.
  - profile_temp   = the worker record. `recordid` is the worker PK.

Division rules for the non-MSP dashboard:
  - Any region name containing 'Education' → Education
  - Anything else → Non-Acute
  - Region name containing 'MSP' → EXCLUDED entirely (belongs on the MSP
    dashboard — GHR Education MSP + GHR Non-Acute MSP are managed differently)

System / Account axis: each in-scope client is its own row (uses
profile_client.clientname directly — no more hardcoded rollup names).
"""

# Kept for backward compat with the older Settings-modal system-mappings
# endpoint that shipped an initial-load list before auto-discovery. Not
# used by scope resolution anymore.
SYMPLR_SYSTEM_ROLLUP = [
    {'system_name': 'Reading School District',   'division': 'Education', 'master_ids': [5890, 26917, 40136]},
    {'system_name': 'Allentown School District', 'division': 'Education', 'master_ids': [5685, 34792, 34793]},
    {'system_name': 'DCIU',                      'division': 'Education', 'master_ids': [5874, 14146, 84531, 122454, 122455]},
]


def service_line_case(nursetype_col: str = 'lt.nursetype') -> str:
    """
    SQL CASE that buckets Symplr `nursetype` into MSP-parallel service lines
    (Nursing / Allied / Non-Clinical / Other).

    Expanded in v2.2.0 to cover the ~140 rows/wk that were previously falling
    into 'Other'. Values inventoried against live lt_order:
      Nursing      = RN, LPN, dual RN/LPN combos, cert school RN, DON/ADON
      Allied       = OT/PT/SLP/SLPA, therapy specialties, behavioral techs,
                     social workers (school + generic), psychologists
      Non-Clinical = PCA/CNA/Aide/Para/DSP, teachers/instructors, admin

    Callers pass the correct column reference (e.g. `lt.nursetype` under the
    lt_order alias `lt`, or `o.nursetype` under the orders alias `o`).
    """
    return f"""CASE
        WHEN {nursetype_col} IN (
            'RN', 'LPN', 'RN,LPN', 'LPN,RN', 'Cert School RN', 'DON', 'ADON'
        ) THEN 'Nursing'
        WHEN {nursetype_col} IN (
            'OT', 'PT', 'SLP', 'SLPA', 'Therapy', 'Behavior Therapist',
            'Registered Behavior Technician', 'BCBA',
            'Social Worker School', 'SW',
            'Psychologist School', 'School Psych', 'PSYCOL', 'Psychologist'
        ) THEN 'Allied'
        WHEN {nursetype_col} IN (
            'PCA', 'CNA', 'Aide', 'Paraprofessional', 'Para', 'DSP',
            'NHA', 'Job Coach',
            'Special Ed Teacher', 'Special Ed Instructor',
            'Sub Teacher', 'Teacher',
            'Hearing Impaired'
        ) THEN 'Non-Clinical'
        ELSE 'Other'
    END"""


# JOIN REQUIREMENTS FOR CALLERS
# =============================
# Every Symplr query joins the client table for facility + system:
#
#     LEFT JOIN dbo.profile_client pc ON <client_id_col> = pc.recordid
#
# Queries that use build_division_case_expr additionally join regions:
#
#     LEFT JOIN dbo.regions r ON r.regionid = TRY_CAST(pc.region AS INT)
#
# build_scope_filter no longer needs `r` — MasterClientID expansion + MSP
# exclusion happen once in resolve_scope_master_ids (Python) and the filter
# renders as a flat IN-list.


def build_system_case_expr(column_name: str = 'lt.clientid') -> str:
    """
    System axis on non-MSP = the client's own display name (pc.clientname).
    Caller must LEFT JOIN dbo.profile_client pc ON `column_name` = pc.recordid.
    `column_name` is retained for API compatibility with the older signature
    but ignored — the alias is always `pc`.
    """
    return "pc.clientname"


def build_division_case_expr(column_name: str = 'lt.clientid') -> str:
    """
    Division for non-MSP Symplr rows, derived from the client's region:
      - regionname LIKE '%Education%' → 'Education'
      - anything else                 → 'Non-Acute'

    Caller must join both profile_client (alias `pc`) AND regions (alias `r`).
    MSP-flavored regions ('GHR Education MSP', 'GHR Non-Acute MSP', etc.)
    are filtered out by build_scope_filter upstream and never reach here.

    `column_name` retained for API compat but ignored — expected alias is `r`.
    """
    return """CASE
        WHEN r.regionname LIKE '%Education%' THEN 'Education'
        ELSE 'Non-Acute'
    END"""


def all_in_scope_master_ids():
    """Legacy shim. Auto-discovery replaces the hardcoded rollup, but this is
    still called by GetSystemMappings on initial page load — return the same
    hardcoded list so the front-end's initial mappings response doesn't break."""
    out = []
    for entry in SYMPLR_SYSTEM_ROLLUP:
        out.extend(entry['master_ids'])
    return out


def get_manual_symplr_allowlist_ids(app_conn):
    """
    Symplr client IDs added manually via the Settings → Non-MSP Clients admin
    UI (source='symplr' rows in impactmgr.bullhorn_client_allowlist).

    Returns empty set if app DB is unreachable, the allowlist table doesn't
    exist yet, or the `source` column hasn't been added yet.
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


def discover_active_client_ids(symplr_cursor):
    """
    Every Symplr client with a filled lt_order in the recent window
    (last 90 days), MINUS any whose region name contains 'MSP' (those belong
    on the MSP dashboard).

    Uses lt_order.status = 'filled' as the active marker — same signal
    GetTrendData / GetHoursData / GetStatsData use to count worked shifts.

    Non-fatal on error: returns empty set so the endpoint can still return
    whatever's in the manual allowlist.
    """
    try:
        symplr_cursor.execute("""
            SELECT DISTINCT lt.clientid
            FROM dbo.lt_order lt
            INNER JOIN dbo.profile_client pc ON lt.clientid = pc.recordid
            LEFT JOIN dbo.regions r ON r.regionid = TRY_CAST(pc.region AS INT)
            WHERE lt.status = 'filled'
              AND lt.date_start IS NOT NULL
              AND (lt.date_end IS NULL OR lt.date_end >= DATEADD(DAY, -90, GETDATE()))
              AND (r.regionname IS NULL OR r.regionname NOT LIKE '%MSP%')
        """)
        return {int(row[0]) for row in symplr_cursor.fetchall() if row[0] is not None}
    except Exception as e:
        print(f"discover_active_client_ids (Symplr): swallowed error, returning empty set: {e}")
        return set()


def _expand_and_filter_allowlist(symplr_cursor, allowlist_ids):
    """
    Expand manual allowlist entries via MasterClientID AND drop any resulting
    client whose region name contains 'MSP'. Runs ONCE at scope-resolution
    time so that build_scope_filter can render as a flat IN-list (no
    correlated subquery). Only called when the allowlist is non-empty.
    """
    if not allowlist_ids or symplr_cursor is None:
        return set()
    try:
        ids_sql = ', '.join(str(int(i)) for i in allowlist_ids if i is not None)
        if not ids_sql:
            return set()
        symplr_cursor.execute(f"""
            SELECT pc.recordid
            FROM dbo.profile_client pc
            LEFT JOIN dbo.regions r ON r.regionid = TRY_CAST(pc.region AS INT)
            WHERE (pc.recordid IN ({ids_sql}) OR pc.MasterClientID IN ({ids_sql}))
              AND (r.regionname IS NULL OR r.regionname NOT LIKE '%MSP%')
        """)
        return {int(row[0]) for row in symplr_cursor.fetchall() if row[0] is not None}
    except Exception as e:
        print(f"_expand_and_filter_allowlist: swallowed error: {e}")
        return set()


def resolve_scope_master_ids(app_conn=None, symplr_cursor=None):
    """
    Effective Symplr scope: manual allowlist (expanded via MasterClientID +
    MSP-region filter) ∪ auto-discovered active clients.

    Both expansion and MSP exclusion happen HERE, once — build_scope_filter
    can then render a flat IN-list and let SQL Server use the clustered
    index on profile_client.recordid. Previously the expansion + MSP filter
    lived inside build_scope_filter as a correlated subquery evaluated per
    row of every trend/financial/positions query, which timed out the
    Trend and Financials tabs on the SWA Free 45s gateway limit.

    Name kept as `resolve_scope_master_ids` for backward compat with the
    existing endpoint call sites — the IDs it returns are the fully-
    resolved leaf client IDs, no longer just "masters".

    If `symplr_cursor` is None (e.g. endpoint failed to open Symplr conn),
    falls back to allowlist only (unexpanded). Empty set → scope filter
    renders 1=0 → no rows.
    """
    allowlist_ids = get_manual_symplr_allowlist_ids(app_conn)
    ids = _expand_and_filter_allowlist(symplr_cursor, allowlist_ids) if allowlist_ids else set()
    if symplr_cursor is not None:
        ids |= discover_active_client_ids(symplr_cursor)
    return ids


def build_scope_filter(column_name: str = 'lt.clientid', master_ids=None) -> str:
    """
    SQL fragment restricting rows to Symplr clients in scope.

    Pass `master_ids` (fully resolved by resolve_scope_master_ids) — this
    now renders as a flat IN-list with no correlated subquery, so SQL
    Server can seek the clustered index on the joined column. MSP exclusion
    + MasterClientID expansion happen once at scope resolution, not per
    row of each caller's query.
    """
    if master_ids is None:
        master_ids = []
    ids = sorted(set(int(i) for i in master_ids if i is not None))
    if not ids:
        return '1 = 0'
    return f'{column_name} IN ({", ".join(str(i) for i in ids)})'
