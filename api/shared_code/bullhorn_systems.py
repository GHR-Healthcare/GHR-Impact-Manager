"""
Bullhorn account rollup + dynamic scope resolution.

The non-MSP dashboard's Bullhorn scope is now the UNION of three sources:

  1. Hardcoded rollup (BULLHORN_SYSTEM_ROLLUP below): maps specific
     clientCorporationIDs to a display "system_name" (e.g. Cone Health's
     4 IDs all roll up to one "Cone Health" row).
  2. Manual allowlist stored in impactmgr.bullhorn_client_allowlist: IDs
     leaders explicitly forced in from the Settings UI, regardless of
     current headcount. These render under their raw View_ClientCorporation.name.
  3. Auto-active: any clientCorporationID with an on-assignment placement
     right now (status IN ('Approved','Onboarding') AND today between
     dateBegin/dateEnd) AND whose client is tagged with at least one
     non-MSP division on cc.customTextBlock1. Also render under raw
     cc.name unless mapped in (1).

Symplr (Education) is a separate source and lives in symplr_systems.py.

NON_MSP_DIVISIONS is the whitelist that gates auto-scope. Without it,
"any active client" pulls in ~400 clients including Education (Symplr's
territory), untagged historical clients, and internal / LTC-location
buckets that aren't part of the non-MSP book.
"""

# Division tokens that count a client as "non-MSP" for auto-scope purposes.
# A client is auto-included only if its cc.customTextBlock1 (a comma-
# separated list) contains AT LEAST ONE of these tokens.
#
# Deliberately EXCLUDED tokens that appear in the data but aren't non-MSP:
#   - Education        (Symplr's territory, not Bullhorn's)
#   - (null / '')      (untagged records, mostly historical)
#   - GHR Internal     (internal ops, not client-facing)
#   - Travel Nursing / Texas / Texas Nursing / PRN Allied / Plymouth Meeting LTC /
#     Pittsburgh LTC   (legacy or location-specific tags, low volume)
NON_MSP_DIVISIONS = {
    'Allied',
    'Nursing',
    'RevCycle Workforce',
    'United',
    'Locum Tenens',
    'Technology',
    'Search',
    'Workforce Solutions',
    'Planet Healthcare',
    'Acute',
    'Human Services',
}

# system_name → list of Bullhorn clientCorporationIDs that roll up to it.
# Order here determines the default sort order in the Trend / breakdown tables.
# Anything NOT in this map that gets scoped in (via allowlist or headcount)
# renders under cc.name — the CASE expression's ELSE branch handles that.
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
    """Flat list of every hardcoded rollup clientCorporationID.

    Kept for backward compatibility. The effective scope now also includes
    the manual allowlist and auto-active clients — use resolve_scope_client_ids
    from within an endpoint that has DB connections available.
    """
    return list(CLIENT_ID_TO_SYSTEM.keys())


def get_manual_allowlist_ids(app_conn):
    """
    Read manual-add BULLHORN client IDs from impactmgr.bullhorn_client_allowlist.

    Filters by source='bullhorn' so Symplr allowlist entries in the same
    table don't leak into Bullhorn scope. Handles both the pre-`source`
    schema (no column → all rows counted, matching the original behavior)
    and the post-`source` schema (WHERE source='bullhorn').

    Returns an empty set if:
      - app_conn is None (DB not configured — dev/local)
      - the table doesn't exist yet (first run before ensure_schema)
      - any query error (logged, non-fatal — we fall back to hardcoded + auto)
    """
    if app_conn is None:
        return set()
    try:
        cursor = app_conn.cursor()
        cursor.execute("""
            IF EXISTS (
                SELECT 1 FROM sys.tables
                WHERE name = 'bullhorn_client_allowlist' AND schema_id = SCHEMA_ID('impactmgr')
            )
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM sys.columns
                    WHERE object_id = OBJECT_ID('impactmgr.bullhorn_client_allowlist')
                      AND name = 'source'
                )
                    SELECT client_id FROM impactmgr.bullhorn_client_allowlist WHERE source = 'bullhorn'
                ELSE
                    SELECT client_id FROM impactmgr.bullhorn_client_allowlist
            END
            ELSE
                SELECT TOP 0 CAST(NULL AS INT) AS client_id
        """)
        return {int(row[0]) for row in cursor.fetchall() if row[0] is not None}
    except Exception as e:
        # Non-fatal — hardcoded rollup + auto-active still keep the dashboard populated.
        print(f"get_manual_allowlist_ids: swallowed error, returning empty set: {e}")
        return set()


def discover_active_client_ids(bullhorn_cursor):
    """
    Any clientCorporationID with a placement that's on-assignment RIGHT NOW
    AND whose client is tagged with at least one non-MSP division.

    Matches how KPIs count headcount: status IN (Approved, Onboarding) with
    today falling between dateBegin and dateEnd. Cheap single query on an
    indexed status column.

    Division filter uses STRING_SPLIT against NON_MSP_DIVISIONS. Without it,
    the auto-scope would balloon from ~90 non-MSP accounts to ~400 including
    Education-only and untagged historical records.
    """
    try:
        # Build the whitelist as a comma-separated SQL literal. All tokens
        # are hardcoded, safe to inline.
        divisions_sql = ', '.join(
            "'" + d.replace("'", "''") + "'" for d in sorted(NON_MSP_DIVISIONS)
        )
        bullhorn_cursor.execute(f"""
            SELECT DISTINCT p.clientCorporationID
            FROM dbo.View_Placement p
            INNER JOIN dbo.View_ClientCorporation cc
                ON p.clientCorporationID = cc.clientCorporationID
            WHERE p.isDeleted = 0
              AND p.status IN ('Approved','Onboarding')
              AND p.dateBegin <= CAST(GETDATE() AS DATE)
              AND (p.dateEnd IS NULL OR p.dateEnd >= CAST(GETDATE() AS DATE))
              AND cc.customTextBlock1 IS NOT NULL
              AND cc.customTextBlock1 <> ''
              AND EXISTS (
                  SELECT 1
                  FROM STRING_SPLIT(cc.customTextBlock1, ',') s
                  WHERE LTRIM(RTRIM(s.value)) IN ({divisions_sql})
              )
        """)
        return {int(row[0]) for row in bullhorn_cursor.fetchall() if row[0] is not None}
    except Exception as e:
        print(f"discover_active_client_ids: swallowed error, returning empty set: {e}")
        return set()


def resolve_scope_client_ids(bullhorn_cursor, app_conn=None):
    """
    The full effective scope: hardcoded rollup ∪ manual allowlist ∪ auto-active.

    Call this once at the top of every non-MSP endpoint, then pass the
    resulting set into build_scope_filter(). Both DB connections should be
    from the caller's already-open connections so we don't churn extra logins.
    """
    return (
        set(CLIENT_ID_TO_SYSTEM.keys())
        | get_manual_allowlist_ids(app_conn)
        | discover_active_client_ids(bullhorn_cursor)
    )


def build_system_case_expr(column_name='p.clientCorporationID', fallback_name_column='cc.name'):
    """
    SQL CASE that maps clientCorporationID → rolled-up system_name. IDs not
    in the hardcoded rollup fall through to `fallback_name_column` (defaults
    to cc.name — every non-MSP query already JOINs View_ClientCorporation cc,
    so this Just Works).

    Callers that don't join cc must pass their own fallback_name_column, or
    pass 'NULL' to preserve the old behavior of returning NULL for unmapped IDs.
    """
    parts = ['CASE']
    for entry in BULLHORN_SYSTEM_ROLLUP:
        ids = ', '.join(str(i) for i in entry['client_ids'])
        # system_name is hard-coded, safe to inline. Single-quote-escape in case
        # a future entry contains an apostrophe (e.g. "Children's").
        name_safe = entry['system_name'].replace("'", "''")
        parts.append(f"    WHEN {column_name} IN ({ids}) THEN '{name_safe}'")
    parts.append(f'    ELSE {fallback_name_column}')
    parts.append('END')
    return '\n'.join(parts)


def build_scope_filter(column_name='p.clientCorporationID', client_ids=None):
    """
    SQL fragment `{col} IN (...)` restricting placements to in-scope non-MSP
    accounts.

    Pass `client_ids` (a set/list) computed via resolve_scope_client_ids to
    get the full dynamic scope. Omit `client_ids` for legacy behavior
    (hardcoded rollup only) — useful for tests or when the app DB is offline.

    If the effective set is empty, returns '1 = 0' so the query returns
    zero rows rather than accidentally matching everything.
    """
    if client_ids is None:
        client_ids = all_in_scope_client_ids()
    ids_list = sorted(set(int(i) for i in client_ids if i is not None))
    if not ids_list:
        return '1 = 0'
    ids_str = ', '.join(str(i) for i in ids_list)
    return f'{column_name} IN ({ids_str})'
