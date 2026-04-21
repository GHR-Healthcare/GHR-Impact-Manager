import azure.functions as func
import pyodbc
import os
import json


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
                    p.dateBegin,
                    p.dateEnd,
                    p.status
                FROM dbo.View_Placement p
                LEFT JOIN dbo.View_Candidate c ON p.candidateID = c.id
                LEFT JOIN dbo.View_ClientCorporation cc ON p.clientCorporationID = cc.id
                WHERE p.isDeleted = 0
                    AND p.status NOT IN ('Cancellation', 'Archive')
                    AND (p.dateEnd IS NULL OR p.dateEnd >= DATEADD(DAY, -30, GETDATE()))
            ''')
            for row in bh_cursor.fetchall():
                pid, first, last, client, db, de, status = row
                bh_records.append({
                    'placementID': pid,
                    'worker_name': f"{first or ''} {last or ''}".strip(),
                    'normalized_name': normalize_name(first, last),
                    'client_name': client or '',
                    'dateBegin': format_date(db),
                    'dateEnd': format_date(de),
                    'status': status or ''
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
        # Match by normalized name; pick best Bullhorn record per
        # assignment by start-date proximity when multiple candidates.
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
        matches = []  # exact date matches (useful count)

        matched_bh_ids = set()

        for name, a_list in assignments_by_name.items():
            b_list = bullhorn_by_name.get(name, [])
            if not b_list:
                missing_in_bullhorn.extend(a_list)
                continue

            # Sort both by startDate for best pairing
            for a in a_list:
                a_sd = date_key(a.get('startDate'))

                def start_distance(b):
                    return abs((date_key(b.get('dateBegin')) or '0000-00-00').__hash__() -
                               (a_sd or '0000-00-00').__hash__())

                # Pick the bullhorn record closest by startDate that hasn't been matched
                candidates = [b for b in b_list if b['placementID'] not in matched_bh_ids]
                if not candidates:
                    # All already claimed — treat as missing
                    missing_in_bullhorn.append(a)
                    continue

                # Prefer exact start-date match, else closest by string compare
                best = None
                for b in candidates:
                    if date_key(b.get('dateBegin')) == a_sd:
                        best = b
                        break
                if best is None:
                    best = candidates[0]
                matched_bh_ids.add(best['placementID'])

                start_match = date_key(a.get('startDate')) == date_key(best.get('dateBegin'))
                end_match = date_key(a.get('endDate')) == date_key(best.get('dateEnd'))

                pair = {
                    'worker_name': a['worker_name'],
                    'assignment': a,
                    'bullhorn': best,
                    'start_match': start_match,
                    'end_match': end_match,
                }
                if start_match and end_match:
                    matches.append(pair)
                else:
                    date_mismatches.append(pair)

        for name, b_list in bullhorn_by_name.items():
            for b in b_list:
                if b['placementID'] not in matched_bh_ids:
                    missing_in_b4vndly.append(b)

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
