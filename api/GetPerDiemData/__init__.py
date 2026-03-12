import azure.functions as func
import pyodbc
import os
import json
import re
from datetime import datetime

# Mapping of B4 health system names to VNDLY health system names
# Used to detect overlap and prefer VNDLY data when available
B4_TO_VNDLY_SYSTEM_MAP = {
    'Richmond University Medical Center': 'RUMC',
    'Holy Redeemer Hospital': 'Redeemer Health',
}

def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    Returns per diem data for the Per Diem tab:
    - assignments: Per diem workers on assignment (from B4Health + VNDLY)
    - shifts: Actual shifts worked by per diem workers (from B4HealthESR + VNDLY Spend)

    Frontend computes weekly metrics from this raw data:
    - Total Active Headcount, Actives Worked, % Worked, Total Shifts, Shifts/Nurse Avg
    """
    try:
        # Optional date range params (format: YYYY-MM)
        from_month = req.params.get('from')
        to_month = req.params.get('to')

        # Validate date format to prevent injection
        date_pattern = re.compile(r'^\d{4}-\d{2}$')
        if from_month and not date_pattern.match(from_month):
            from_month = None
        if to_month and not date_pattern.match(to_month):
            to_month = None

        # Default: 6 months back from current month
        if from_month:
            date_from = f"'{from_month}-01'"
        else:
            date_from = "DATEADD(MONTH, -3, DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1))"

        if to_month:
            date_to = f"DATEADD(MONTH, 1, '{to_month}-01')"
        else:
            date_to = "DATEADD(DAY, 1, GETDATE())"

        conn = pyodbc.connect(
            f"DRIVER={{ODBC Driver 17 for SQL Server}};"
            f"SERVER={os.environ['DB_HOST']};"
            f"DATABASE={os.environ['POSITIONS_DB']};"
            f"UID={os.environ['DB_USER']};"
            f"PWD={os.environ['DB_PASSWORD']};"
            f"TrustServerCertificate=yes"
        )

        cursor = conn.cursor()
        assignments = []
        shifts = []

        # ============================================================
        # B4Health - Active Per Diem Assignments
        # Filter by Program directly on B4HealthOrder
        # ============================================================
        try:
            cursor.execute('''
                SELECT DISTINCT
                    'B4' AS source_system,
                    Contract_ID AS contract_id,
                    CONCAT(First_Name, ' ', Last_Name) AS worker_name,
                    Health_System AS system,
                    Facility AS facility,
                    Agency AS agency,
                    Start_Date AS startDate,
                    End_Date AS endDate
                FROM dhc.B4HealthOrder
                WHERE Contract_Status = 'Closed And Awarded'
                    AND Program LIKE '%Per Diem%'
                    AND Start_Date IS NOT NULL
                    AND Health_System NOT LIKE '%Richmond University%'
                    AND Health_System NOT LIKE '%Redeemer%'
            ''')

            columns = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                if row_dict.get('startDate'):
                    row_dict['startDate'] = row_dict['startDate'].isoformat() if hasattr(row_dict['startDate'], 'isoformat') else str(row_dict['startDate'])
                if row_dict.get('endDate'):
                    row_dict['endDate'] = row_dict['endDate'].isoformat() if hasattr(row_dict['endDate'], 'isoformat') else str(row_dict['endDate'])
                assignments.append(row_dict)
        except Exception as e:
            print(f"Error loading B4 per diem assignments: {e}")

        # ============================================================
        # B4Health - Shifts Worked by Per Diem Workers
        # Uses B4HealthESR, filtered by Program directly
        # ============================================================
        try:
            cursor.execute(f'''
                SELECT
                    'B4' AS source_system,
                    [Employee] AS worker_name,
                    [Work Date] AS shift_date,
                    [Health System] AS system,
                    [Agency Name] AS agency
                FROM dhc.B4HealthESR
                WHERE [Health System] IS NOT NULL
                    AND [Work Date] >= {date_from}
                    AND [Work Date] < {date_to}
                    AND [Program] LIKE '%Per Diem%'
                    AND [Health System] NOT LIKE '%Richmond University%'
                    AND [Health System] NOT LIKE '%Redeemer%'
            ''')

            columns = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                if row_dict.get('shift_date'):
                    row_dict['shift_date'] = row_dict['shift_date'].isoformat() if hasattr(row_dict['shift_date'], 'isoformat') else str(row_dict['shift_date'])
                shifts.append(row_dict)
        except Exception as e:
            print(f"Error loading B4 per diem shifts: {e}")

        # ============================================================
        # VNDLY - Active Per Diem Assignments (from Work Orders)
        # Try joining to STAGING_VNDLY_JOBS for category filtering
        # ============================================================
        try:
            cursor.execute('''
                SELECT DISTINCT
                    'VNDLY' AS source_system,
                    CAST(w.[Work Order Id] AS NVARCHAR(50)) AS contract_id,
                    CONCAT(w.[Contractor First Name], ' ', w.[Contractor Last Name]) AS worker_name,
                    w.[Health System] AS system,
                    w.[Default Work Site Name] AS facility,
                    w.[Vendor Name] AS agency,
                    w.[Start Date] AS startDate,
                    w.[End Date] AS endDate
                FROM dbo.STAGING_VNDLY_WORKORDERS w
                LEFT JOIN dbo.STAGING_VNDLY_JOBS j
                    ON w.[Job Id] = j.[Job Id]
                WHERE w.[Current Status] = 'Active'
                    AND w.[Start Date] IS NOT NULL
                    AND (
                        j.[Job Category] LIKE '%Per Diem%'
                        OR w.[Job Title] LIKE '%Per Diem%'
                    )
            ''')

            columns = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                if row_dict.get('startDate'):
                    row_dict['startDate'] = row_dict['startDate'].isoformat() if hasattr(row_dict['startDate'], 'isoformat') else str(row_dict['startDate'])
                if row_dict.get('endDate'):
                    row_dict['endDate'] = row_dict['endDate'].isoformat() if hasattr(row_dict['endDate'], 'isoformat') else str(row_dict['endDate'])
                assignments.append(row_dict)
        except Exception as e:
            print(f"Error loading VNDLY per diem assignments: {e}")
            # Fallback: try without the join in case Job Id column doesn't exist on workorders
            try:
                cursor.execute('''
                    SELECT DISTINCT
                        'VNDLY' AS source_system,
                        CAST(w.[Work Order Id] AS NVARCHAR(50)) AS contract_id,
                        CONCAT(w.[Contractor First Name], ' ', w.[Contractor Last Name]) AS worker_name,
                        w.[Health System] AS system,
                        w.[Default Work Site Name] AS facility,
                        w.[Vendor Name] AS agency,
                        w.[Start Date] AS startDate,
                        w.[End Date] AS endDate
                    FROM dbo.STAGING_VNDLY_WORKORDERS w
                    WHERE w.[Current Status] = 'Active'
                        AND w.[Start Date] IS NOT NULL
                        AND w.[Job Title] LIKE '%Per Diem%'
                ''')

                columns = [column[0] for column in cursor.description]
                for row in cursor.fetchall():
                    row_dict = dict(zip(columns, row))
                    if row_dict.get('startDate'):
                        row_dict['startDate'] = row_dict['startDate'].isoformat() if hasattr(row_dict['startDate'], 'isoformat') else str(row_dict['startDate'])
                    if row_dict.get('endDate'):
                        row_dict['endDate'] = row_dict['endDate'].isoformat() if hasattr(row_dict['endDate'], 'isoformat') else str(row_dict['endDate'])
                    assignments.append(row_dict)
            except Exception as e2:
                print(f"Error loading VNDLY per diem assignments (fallback): {e2}")

        # ============================================================
        # VNDLY - Shifts Worked by Per Diem Workers
        # Uses STAGING_VNDLY_SPEND filtered to Per Diem workers
        # ============================================================
        try:
            cursor.execute(f'''
                SELECT
                    'VNDLY' AS source_system,
                    CONCAT(s.[Contractor First Name], ' ', s.[Contractor Last Name]) AS worker_name,
                    s.[Item Date] AS shift_date,
                    s.[Health System] AS system,
                    s.[Vendor Company Name] AS agency
                FROM dbo.STAGING_VNDLY_SPEND s
                WHERE s.[Health System] IS NOT NULL
                    AND s.[Item Date] >= {date_from}
                    AND s.[Item Date] < {date_to}
                    AND EXISTS (
                        SELECT 1 FROM dbo.STAGING_VNDLY_WORKORDERS w
                        WHERE w.[Current Status] = 'Active'
                            AND CONCAT(w.[Contractor First Name], ' ', w.[Contractor Last Name])
                                = CONCAT(s.[Contractor First Name], ' ', s.[Contractor Last Name])
                            AND w.[Health System] = s.[Health System]
                            AND (
                                w.[Job Title] LIKE '%Per Diem%'
                                OR EXISTS (
                                    SELECT 1 FROM dbo.STAGING_VNDLY_JOBS j
                                    WHERE j.[Job Id] = w.[Job Id]
                                        AND j.[Job Category] LIKE '%Per Diem%'
                                )
                            )
                    )
            ''')

            columns = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                if row_dict.get('shift_date'):
                    row_dict['shift_date'] = row_dict['shift_date'].isoformat() if hasattr(row_dict['shift_date'], 'isoformat') else str(row_dict['shift_date'])
                shifts.append(row_dict)
        except Exception as e:
            print(f"Error loading VNDLY per diem shifts: {e}")

        conn.close()

        b4_assignments = len([r for r in assignments if r.get('source_system') == 'B4'])
        vndly_assignments = len([r for r in assignments if r.get('source_system') == 'VNDLY'])
        b4_shifts = len([r for r in shifts if r.get('source_system') == 'B4'])
        vndly_shifts = len([r for r in shifts if r.get('source_system') == 'VNDLY'])
        print(f"Per Diem: {len(assignments)} assignments (B4: {b4_assignments}, VNDLY: {vndly_assignments}), "
              f"{len(shifts)} shifts (B4: {b4_shifts}, VNDLY: {vndly_shifts})")

        return func.HttpResponse(
            json.dumps({
                'assignments': assignments,
                'shifts': shifts
            }, default=str),
            mimetype="application/json",
            status_code=200
        )

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return func.HttpResponse(
            json.dumps({'error': str(e), 'assignments': [], 'shifts': []}),
            mimetype="application/json",
            status_code=500
        )
