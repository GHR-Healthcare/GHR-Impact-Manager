import azure.functions as func
import pyodbc
import os
import json
import time
from shared_code.auth import require_allowed_domain
from shared_code.data_source import is_non_msp, get_bullhorn_conn
from shared_code.bullhorn_systems import BULLHORN_SYSTEM_ROLLUP
from shared_code.symplr_systems import SYMPLR_SYSTEM_ROLLUP


def _non_msp_mappings_response():
    """
    Non-MSP instance: union the Bullhorn book (8 accounts) and Symplr Education
    book (3 accounts) into one mappings list. 11 total. Shape mirrors the MSP
    shape so the frontend can read system_name without branching. POST is
    rejected — the rollup is code-managed.
    """
    mappings = []
    idx = 0
    for entry in BULLHORN_SYSTEM_ROLLUP:
        mappings.append({
            'id': idx + 1,
            'system_name': entry['system_name'],
            'client_ids': entry['client_ids'],
            'source': 'bullhorn',
            # Division for Bullhorn is per-placement (View_Placement.customTextBlock1)
            # rather than per-account, so the rollup config doesn't carry it.
            # Frontend builds the division filter dropdown from actual record data.
            'division': entry.get('division'),
            'keywords': [],
            'sort_order': idx,
            'perdiem_breakout': 0,
            'hidden': 0,
            'relationship': '',
            'incumbent': '',
        })
        idx += 1
    for entry in SYMPLR_SYSTEM_ROLLUP:
        mappings.append({
            'id': idx + 1,
            'system_name': entry['system_name'],
            # Symplr config uses 'master_ids' (v1.7.7 rename) — surface under
            # the same 'client_ids' name as Bullhorn so the frontend doesn't
            # branch by source.
            'client_ids': entry['master_ids'],
            'source': 'symplr',
            'division': entry.get('division'),
            'keywords': [],
            'sort_order': idx,
            'perdiem_breakout': 0,
            'hidden': 0,
            'relationship': '',
            'incumbent': '',
        })
        idx += 1
    return func.HttpResponse(
        json.dumps({'mappings': mappings, 'source': 'non_msp'}),
        mimetype="application/json",
        status_code=200,
    )


# What the card is allowed to display. 'Direct' means GHR works with the
# system directly; 'MSP' means GHR runs the program; '3rd Party' means someone
# else's MSP sits in between -- and that is when the incumbent matters.
VALID_RELATIONSHIPS = ('MSP', 'Direct', '3rd Party')


# Bullhorn is the system of record for how GHR reaches a client. FieldMaps
# decodes the custom fields that hold it:
#
#   Placement.customText59  "Relationship"     GHR MSP | Direct Contract | Third Party
#   Placement.customText57  "MSP/Group Name"   the incumbent, when there is one
#
# customText59 is populated on 98.6% of placements in the last two years
# (7,508 / 4,263 / 3,587 against 213 blank), which is why this is derived
# rather than typed in. The per-system Settings fields still win where set --
# they exist for the cases this cannot see.
RELATIONSHIP_MAP = {
    'ghr msp':         'MSP',
    'direct contract': 'Direct',
    'third party':     '3rd Party',
}

# customText57 mirrors the relationship back when nobody sits in the middle.
# On a Direct account there is no MSP at all, so these are not incumbents and
# naming one would be wrong.
NON_INCUMBENTS = {
    'direct contract', 'perm contract', 'no msp', 'none', 'n/a',
}

# On a GHR MSP account the incumbent is us, which is worth saying rather than
# leaving blank -- the question the card answers is "who holds this program",
# and "nothing" is not an answer. Bullhorn spells it a few ways, and 'Preview'
# is a sandbox artifact that shows up against Grand View.
GHR_INCUMBENT_ALIASES = {
    'ghrhealthcare', 'ghrhealthcare preview', 'ghr healthcare', 'ghr',
}

RELATIONSHIP_LOOKBACK_YEARS = 2

# How long a derivation is reused before Bullhorn is asked again.
#
# This endpoint runs on every page load, and the derivation adds a second
# database -- the Bullhorn mirror, over the network -- to that path. The query
# itself is 47ms; the connection setup is the part worth avoiding. Which MSP
# holds an account changes on the order of months, so re-deriving per request
# buys nothing. Cached in module scope, so it lives per worker and a restart
# or a scale-out re-derives naturally.
RELATIONSHIP_CACHE_SECONDS = 15 * 60
_relationship_cache = {'at': 0.0, 'value': None}


def _derive_relationships(mappings):
    """Relationship and incumbent per health system, from Bullhorn placements.

    Matching is by the same keywords the app already uses to group facilities
    into systems, so a system resolves here exactly as it does everywhere else.
    The winner is the most-placed relationship rather than the most recent: a
    single stray placement should not relabel an account, and these books carry
    them (one 'Third Party' against 1,741 'GHR MSP' at Cooper).

    Best effort. If Bullhorn is unreachable this returns nothing and the
    stored values stand on their own -- the badge is worth omitting, never
    worth blocking the page for.
    """
    now = time.time()
    if (_relationship_cache['value'] is not None
            and now - _relationship_cache['at'] < RELATIONSHIP_CACHE_SECONDS):
        return _relationship_cache['value']

    try:
        conn = get_bullhorn_conn()
    except Exception as e:
        print(f'system-mappings: Bullhorn unavailable, skipping derivation: {e}')
        return {}
    if conn is None:
        return {}
    try:
        cursor = conn.cursor()
        cursor.execute('SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED')
        cursor.execute(f'''
            SELECT LOWER(cc.name) AS client_name,
                   NULLIF(LTRIM(RTRIM(p.customText59)), '') AS relationship,
                   NULLIF(LTRIM(RTRIM(p.customText57)), '') AS incumbent,
                   COUNT(*) AS n
            FROM dbo.View_Placement p WITH (NOLOCK)
            JOIN dbo.View_ClientCorporation cc WITH (NOLOCK)
              ON cc.clientCorporationID = p.clientCorporationID
            WHERE p.dateAdded >= DATEADD(YEAR, -{RELATIONSHIP_LOOKBACK_YEARS}, GETDATE())
              AND NULLIF(LTRIM(RTRIM(p.customText59)), '') IS NOT NULL
              AND cc.isDeleted = 0
            GROUP BY LOWER(cc.name), NULLIF(LTRIM(RTRIM(p.customText59)), ''),
                     NULLIF(LTRIM(RTRIM(p.customText57)), '')
        ''')
        rows = cursor.fetchall()
    except Exception as e:
        print(f'system-mappings: relationship derivation failed: {e}')
        return {}
    finally:
        try:
            conn.close()
        except Exception:
            pass

    out = {}
    for m in mappings:
        name = m.get('system_name') or ''
        kws = m.get('keywords') or []
        if isinstance(kws, str):
            kws = [k.strip() for k in kws.split(',')]
        kws = [k.lower().strip() for k in kws if k and k.strip()]
        if not name or not kws:
            continue
        rel_votes, inc_votes = {}, {}
        for client_name, relationship, incumbent, n in rows:
            if not any(k in client_name for k in kws):
                continue
            rel = RELATIONSHIP_MAP.get((relationship or '').lower().strip())
            if rel:
                rel_votes[rel] = rel_votes.get(rel, 0) + n
            inc = (incumbent or '').strip()
            if inc and inc.lower() not in NON_INCUMBENTS:
                if inc.lower() in GHR_INCUMBENT_ALIASES:
                    inc = 'GHR'
                inc_votes[inc] = inc_votes.get(inc, 0) + n
        if not rel_votes:
            continue
        best_rel = max(rel_votes.items(), key=lambda kv: kv[1])[0]
        # Name the holder on every account that has one. Previously this was
        # gated to 3rd Party, which meant that on the MSP book -- where ten of
        # the eleven live systems are GHR MSP -- the incumbent was blank
        # essentially everywhere, and the field looked broken.
        #
        # Direct is the one case with genuinely nobody in the middle, so it
        # stays blank by construction: its only customText57 values are the
        # placeholders filtered out above.
        best_inc = max(inc_votes.items(), key=lambda kv: kv[1])[0] if inc_votes else ''
        # A stray third-party name on a GHR MSP account is an exception, not
        # the incumbent; the account is held by GHR.
        if best_rel == 'MSP':
            best_inc = 'GHR' if (not best_inc or best_inc == 'GHR') else best_inc
        # Direct means nobody is in the middle, so naming a holder contradicts
        # the badge beside it. Real vendor names do leak through here -- a
        # handful of Trinity Health placements carry Trustaff -- and rendering
        # "Direct - via Trustaff" would be worse than saying nothing.
        elif best_rel == 'Direct':
            best_inc = ''
        out[name] = {'relationship': best_rel, 'incumbent': best_inc}
    # Only a successful derivation is cached. A failure returns {} above
    # without poisoning the cache, so the next request retries rather than
    # serving fifteen minutes of blank badges.
    _relationship_cache['at'] = now
    _relationship_cache['value'] = out
    return out


def ensure_schema(cursor):
    cursor.execute("""
        IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'impactmgr')
            EXEC('CREATE SCHEMA impactmgr')
    """)
    cursor.execute("""
        IF NOT EXISTS (
            SELECT 1 FROM sys.tables
            WHERE name = 'system_mappings' AND schema_id = SCHEMA_ID('impactmgr')
        )
        CREATE TABLE impactmgr.system_mappings (
            id                INT IDENTITY(1,1) PRIMARY KEY,
            keywords          NVARCHAR(MAX) NOT NULL,
            system_name       NVARCHAR(200) NOT NULL,
            sort_order        INT NOT NULL DEFAULT 0,
            perdiem_breakout  BIT NOT NULL DEFAULT 0,
            hidden            BIT NOT NULL DEFAULT 0
        )
    """)
    # Additive migrations. Relationship and incumbent answer "how do we reach
    # this system, and through whom" -- MSP / Direct / 3rd Party, and the
    # incumbent MSP when it isn't us. Both are configured per system rather
    # than derived: dhc.crosswalk carries OPPORTUNITY_TYPE and EXISTING_MSP,
    # but only for 7 of the 20 systems the app reports on, it has no '3rd
    # Party' value at all, and what it does hold is free text ('UnKnown',
    # 'No (but using Qualivis as VMS)'). It seeds this; it cannot drive it.
    cursor.execute("""
        IF NOT EXISTS (
            SELECT 1 FROM sys.columns
            WHERE object_id = OBJECT_ID('impactmgr.system_mappings') AND name = 'relationship'
        )
        ALTER TABLE impactmgr.system_mappings ADD relationship NVARCHAR(20) NULL
    """)
    cursor.execute("""
        IF NOT EXISTS (
            SELECT 1 FROM sys.columns
            WHERE object_id = OBJECT_ID('impactmgr.system_mappings') AND name = 'incumbent'
        )
        ALTER TABLE impactmgr.system_mappings ADD incumbent NVARCHAR(200) NULL
    """)


def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    GET: Retrieve all system mappings
    POST: Save/update system mappings (replaces all)
    Storage: ghrappdb.impactmgr.system_mappings
    """
    auth_error = require_allowed_domain(req)
    if auth_error:
        return auth_error

    # Non-MSP instance: rollup is code-managed (BULLHORN_SYSTEM_ROLLUP).
    # POST is a no-op — the mapping can't be edited from the UI while it's
    # hard-coded. If we ever move it to a DB table, this branch goes away.
    if is_non_msp():
        if req.method == 'POST':
            return func.HttpResponse(
                json.dumps({'error': 'Health system groupings can\'t be edited on the non-MSP dashboard. Use the Non-MSP Clients tab to add specific accounts.'}),
                mimetype="application/json",
                status_code=405,
            )
        return _non_msp_mappings_response()

    try:
        conn = pyodbc.connect(
            f"DRIVER={{ODBC Driver 17 for SQL Server}};"
            f"SERVER={os.environ['DB_HOST']};"
            f"DATABASE={os.environ['APPDB']};"
            f"UID={os.environ['DB_USER']};"
            f"PWD={os.environ['DB_PASSWORD']};"
            f"TrustServerCertificate=yes"
        )

        cursor = conn.cursor()
        ensure_schema(cursor)
        conn.commit()

        if req.method == 'GET':
            cursor.execute('''
                SELECT id, keywords, system_name, sort_order,
                       CASE WHEN perdiem_breakout = 1 THEN 1 ELSE 0 END AS perdiem_breakout,
                       CASE WHEN hidden = 1 THEN 1 ELSE 0 END AS hidden,
                       ISNULL(relationship, '') AS relationship,
                       ISNULL(incumbent, '')    AS incumbent
                FROM impactmgr.system_mappings
                ORDER BY sort_order, id
            ''')

            columns = [column[0] for column in cursor.description]
            mappings = []
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                row_dict['keywords'] = [k.strip() for k in row_dict['keywords'].split(',') if k.strip()]
                mappings.append(row_dict)

            conn.close()

            # Bullhorn is the source of truth for relationship and incumbent;
            # the stored columns are an override for what it cannot see. A
            # value typed in Settings therefore wins, and anything left blank
            # falls back to what the placements say.
            derived = _derive_relationships(mappings)
            for m in mappings:
                d = derived.get(m.get('system_name') or '')
                if not d:
                    continue
                if not (m.get('relationship') or '').strip():
                    m['relationship'] = d['relationship']
                    m['relationship_source'] = 'bullhorn'
                if not (m.get('incumbent') or '').strip():
                    m['incumbent'] = d['incumbent']

            return func.HttpResponse(
                json.dumps({'mappings': mappings}),
                mimetype="application/json",
                status_code=200
            )

        elif req.method == 'POST':
            try:
                body = req.get_json()
                mappings = body.get('mappings', [])
            except Exception:
                return func.HttpResponse(
                    json.dumps({'error': 'Invalid JSON body'}),
                    mimetype="application/json",
                    status_code=400
                )

            cursor.execute('DELETE FROM impactmgr.system_mappings')

            for idx, mapping in enumerate(mappings):
                keywords = mapping.get('keywords', [])
                system_name = mapping.get('system_name') or mapping.get('system', '')

                if isinstance(keywords, list):
                    keywords_str = ', '.join(keywords)
                else:
                    keywords_str = str(keywords)

                perdiem_breakout = 1 if mapping.get('perdiem_breakout') else 0
                hidden = 1 if mapping.get('hidden') else 0
                # Constrained to the three the card renders, so a typo can't
                # reach the badge. Anything unrecognised stores blank, which
                # the card treats as "not set" rather than showing a wrong
                # relationship confidently.
                relationship = str(mapping.get('relationship') or '').strip()
                if relationship not in VALID_RELATIONSHIPS:
                    relationship = ''
                incumbent = str(mapping.get('incumbent') or '').strip()[:200]

                cursor.execute('''
                    INSERT INTO impactmgr.system_mappings
                        (keywords, system_name, sort_order, perdiem_breakout, hidden, relationship, incumbent)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', keywords_str, system_name, idx, perdiem_breakout, hidden,
                     relationship or None, incumbent or None)

            conn.commit()
            conn.close()

            return func.HttpResponse(
                json.dumps({'success': True, 'count': len(mappings)}),
                mimetype="application/json",
                status_code=200
            )

        else:
            return func.HttpResponse(
                json.dumps({'error': 'Method not allowed'}),
                mimetype="application/json",
                status_code=405
            )

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return func.HttpResponse(
            json.dumps({'error': str(e)}),
            mimetype="application/json",
            status_code=500
        )
