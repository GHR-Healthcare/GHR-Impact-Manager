import azure.functions as func
import pyodbc
import os
import json
from shared_code.auth import require_allowed_domain
from shared_code.data_source import is_non_msp
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
