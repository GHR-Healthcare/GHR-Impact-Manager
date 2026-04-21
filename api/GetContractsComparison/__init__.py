import azure.functions as func
import pyodbc
import os
import json
from datetime import datetime


# Max days between start dates for an assignment/placement to still be considered
# the same contract (anything further apart is treated as two separate contracts).
MATCH_THRESHOLD_DAYS = 60


def normalize_name(first, last):
    """Normalize to 'firstname lastname' lowercase, stripped, for cross-system matching."""
    first_s = (first or '').strip().lower()
    last_s = (last or '').strip().lower()
    joined = f"{first_s} {last_s}".strip()
    return ' '.join(joined.split())


def format_date(d):
    if d is None:
        return None
    if hasattr(d, 'isoformat'):
        return d.isoformat()
    return str(d)


def date_key(s):
    """First 10 chars of an ISO date string (YYYY-MM-DD) for comparison."""
    return (s or '')[:10]


def days_between(s1, s2):
    """Absolute days between two ISO date strings. Returns large number if either missing."""
    k1 = date_key(s1)
    k2 = date_key(s2)
    if not k1 or not k2:
        return 10 ** 9
    try:
        d1 = datetime.strptime(k1, '%Y-%m-%d')
        d2 = datetime.strptime(k2, '%Y-%m-%d')
        return abs((d1 - d2).days)
    except Exception:
        return 10 ** 9


def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    Compares Bullhorn placements against B4/VNDLY assignments.
    Returns three buckets: date mismatches, missing in Bullhorn, missing in B4/VNDLY.
    Matching is by normalized (first + last) name.
    """
    try:
        # ===========================================================
        # B4 + VNDLY assignments from positions DB
        # ===========================================================
        pos_conn = pyodbc.connect(
            f"DRIVER={{ODBC Driver 17 for SQL Server}};"
            f"SERVER={os.environ['DB_HOST']};"
            f"DATABASE={os.environ['POSITIONS_DB']};"
            f"UID={os.environ['DB_USER']};"
            f"PWD={os.environ['DB_PASSWORD']};"
            f"TrustServerCertificate=yes"
        )
        pos_cursor = pos_conn.cursor()

        assignment_records = []

        # B4
        try:
            pos_cursor.execute('''
                SELECT
                    First_Name, Last_Name, Facility, Health_System,
                    Start_Date, End_Date, Contract_Status
                FROM dhc.B4HealthOrder
                WHERE Contract_Status = 'Closed And Awarded'
                    AND Start_Date IS NOT NULL
                    AND Start_Date >= DATEADD(YEAR, -2, GETDATE())
                    AND (End_Date IS NULL OR End_Date >= DATEADD(DAY, -30, GETDATE()))
            ''')
            for row in pos_cursor.fetchall():
                first, last, facility, system, sd, ed, status = row
                assignment_records.append({
                    'source': 'B4',
                    'worker_name': f"{first or ''} {last or ''}".strip(),
                    'normalized_name': normalize_name(first, last),
                    'facility': facility or '',
                    'system': system or '',
                    'startDate': format_date(sd),
                    'endDate': format_date(ed),
                    'status': status or ''
                })
        except Exception as e:
            print(f"Error loading B4 assignments: {e}")

        # VNDLY
        try:
            pos_cursor.execute('''
                SELECT
                    [Contractor First Name], [Contractor Last Name],
                    [Default Work Site Name], [Health System],
                    [Start Date], [End Date], [Current Status]
                FROM dbo.STAGING_VNDLY_WORKORDERS
                WHERE [Current Status] = 'Active'
                    AND [Start Date] IS NOT NULL
                    AND [Start Date] >= DATEADD(YEAR, -2, GETDATE())
                    AND ([End Date] IS NULL OR [End Date] >= DATEADD(DAY, -30, GETDATE()))
            ''')
            for row in pos_cursor.fetchall():
                first, last, facility, system, sd, ed, status = row
                assignment_records.append({
                    'source': 'VNDLY',
                    'worker_name': f"{first or ''} {last or ''}".strip(),
                    'normalized_name': normalize_name(first, last),
                    'facility': facility or '',
                    'system': system or '',
                    'startDate': format_date(sd),
                    'endDate': format_date(ed),
                    'status': status or ''
                })
        except Exception as e:
            print(f"Error loading VNDLY assignments: {e}")

        pos_conn.close()

        # ===========================================================
        # Bullhorn placements from mirror DB
        # ===========================================================
        bh_records = []
        try:
            bh_conn = pyodbc.connect(
                f"DRIVER={{ODBC Driver 17 for SQL Server}};"
                f"SERVER={os.environ['BULLHORN_HOST']};"
                f"DATABASE={os.environ['BULLHORN_DB']};"
                f"UID={os.environ['BULLHORN_USER']};"
                f"PWD={os.environ['BULLHORN_PASSWORD']};"
                f"TrustServerCertificate=yes;"
                f"Encrypt=yes"
            )
            bh_cursor = bh_conn.cursor()
            bh_cursor.execute('''
                SELECT
                    p.placementID,
                    c.firstName,
                    c.lastName,
                    cc.name AS client_name,
                    CAST(p.dateBegin AS DATE) AS dateBegin,
                    CAST(p.dateEnd AS DATE) AS dateEnd,
                    p.status,
                    LTRIM(RTRIM(ISNULL(recr.firstName,'') + ' ' + ISNULL(recr.lastName,''))) AS recruiter,
                    LTRIM(RTRIM(ISNULL(am.firstName,'')   + ' ' + ISNULL(am.lastName,''))) AS account_manager
                FROM dbo.View_Placement p
                LEFT JOIN dbo.View_Candidate c          ON p.candidateID = c.candidateID
                LEFT JOIN dbo.View_ClientCorporation cc ON p.clientCorporationID = cc.clientCorporationID
                LEFT JOIN dbo.View_CorporateUser recr   ON p.ownerID = recr.userID
                LEFT JOIN dbo.View_JobOrder jo          ON p.jobOrderID = jo.jobOrderID
                LEFT JOIN dbo.View_CorporateUser am     ON jo.ownerID = am.userID
                WHERE p.isDeleted = 0
                    AND p.status IN ('Approved', 'Onboarding', 'Cleared', 'Pending Start', 'Started')
                    AND p.dateBegin IS NOT NULL
                    AND p.dateBegin >= DATEADD(YEAR, -2, GETDATE())
                    AND (p.dateEnd IS NULL OR p.dateEnd >= DATEADD(DAY, -30, GETDATE()))
            ''')
            for row in bh_cursor.fetchall():
                pid, first, last, client, db, de, status, recruiter, am = row
                bh_records.append({
                    'placementID': pid,
                    'worker_name': f"{first or ''} {last or ''}".strip(),
                    'normalized_name': normalize_name(first, last),
                    'client_name': client or '',
                    'dateBegin': format_date(db),
                    'dateEnd': format_date(de),
                    'status': status or '',
                    'recruiter': (recruiter or '').strip(),
                    'account_manager': (am or '').strip(),
                })
            bh_conn.close()
        except Exception as e:
            print(f"Error loading Bullhorn placements: {e}")
            import traceback
            traceback.print_exc()
            return func.HttpResponse(
                json.dumps({'error': f'Bullhorn query failed: {e}'}),
                mimetype="application/json",
                status_code=500
            )

        # ===========================================================
        # Match by normalized name; within a name group, greedy-pair
        # assignments to Bullhorn placements by start-date proximity.
        # Pairs more than MATCH_THRESHOLD_DAYS apart are NOT paired —
        # they fall through to the "missing" buckets on each side.
        # ===========================================================
        assignments_by_name = {}
        for r in assignment_records:
            key = r['normalized_name']
            if key:
                assignments_by_name.setdefault(key, []).append(r)

        bullhorn_by_name = {}
        for r in bh_records:
            key = r['normalized_name']
            if key:
                bullhorn_by_name.setdefault(key, []).append(r)

        date_mismatches = []
        missing_in_bullhorn = []
        missing_in_b4vndly = []
        matches = []
        matched_bh_ids = set()

        all_names = set(assignments_by_name.keys()) | set(bullhorn_by_name.keys())

        for name in all_names:
            a_list = assignments_by_name.get(name, [])
            b_list = bullhorn_by_name.get(name, [])

            if not b_list:
                missing_in_bullhorn.extend(a_list)
                continue
            if not a_list:
                missing_in_b4vndly.extend(b_list)
                continue

            # Score every pairing by start-date distance
            pairs = []
            for i, a in enumerate(a_list):
                for j, b in enumerate(b_list):
                    dist = days_between(a.get('startDate'), b.get('dateBegin'))
                    pairs.append((dist, i, j))
            pairs.sort(key=lambda t: t[0])

            used_a = set()
            used_b = set()
            for dist, i, j in pairs:
                if dist > MATCH_THRESHOLD_DAYS:
                    break
                if i in used_a or j in used_b:
                    continue
                a = a_list[i]
                b = b_list[j]
                start_match = date_key(a.get('startDate')) == date_key(b.get('dateBegin'))
                end_match = date_key(a.get('endDate')) == date_key(b.get('dateEnd'))
                pair = {
                    'worker_name': a['worker_name'],
                    'assignment': a,
                    'bullhorn': b,
                    'start_match': start_match,
                    'end_match': end_match,
                    'start_distance_days': dist,
                }
                if start_match and end_match:
                    matches.append(pair)
                else:
                    date_mismatches.append(pair)
                used_a.add(i)
                used_b.add(j)
                matched_bh_ids.add(b['placementID'])

            for i, a in enumerate(a_list):
                if i not in used_a:
                    missing_in_bullhorn.append(a)
            for j, b in enumerate(b_list):
                if j not in used_b:
                    missing_in_b4vndly.append(b)

        # Deterministic ordering so refreshes don't reshuffle rows
        date_mismatches.sort(key=lambda m: (
            (m.get('worker_name') or '').lower(),
            date_key(m.get('assignment', {}).get('startDate')),
            m.get('bullhorn', {}).get('placementID') or 0,
        ))
        missing_in_bullhorn.sort(key=lambda r: (
            (r.get('worker_name') or '').lower(),
            date_key(r.get('startDate')),
            r.get('source') or '',
        ))
        missing_in_b4vndly.sort(key=lambda r: (
            (r.get('worker_name') or '').lower(),
            date_key(r.get('dateBegin')),
            r.get('placementID') or 0,
        ))

        return func.HttpResponse(
            json.dumps({
                'date_mismatches': date_mismatches,
                'missing_in_bullhorn': missing_in_bullhorn,
                'missing_in_b4vndly': missing_in_b4vndly,
                'counts': {
                    'bullhorn_total': len(bh_records),
                    'assignments_total': len(assignment_records),
                    'exact_matches': len(matches),
                    'date_mismatches': len(date_mismatches),
                    'missing_in_bullhorn': len(missing_in_bullhorn),
                    'missing_in_b4vndly': len(missing_in_b4vndly),
                }
            }, default=str),
            mimetype="application/json",
            status_code=200
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
