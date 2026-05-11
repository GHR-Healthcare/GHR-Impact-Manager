import azure.functions as func
import pyodbc
import os
import json
import re
from datetime import datetime


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
                    CONCAT(Last_Name, ', ', First_Name) AS worker_name,
                    Health_System AS system,
                    Facility AS facility,
                    Agency AS agency,
                    Start_Date AS startDate,
                    End_Date AS endDate,
                    Account_Manager AS account_manager,
                    Hiring_Manager AS hiring_manager
                FROM dhc.B4HealthOrder
                WHERE Contract_Status = 'Closed And Awarded'
                    AND Program LIKE '%Per Diem%'
                    AND Start_Date IS NOT NULL
                    AND Health_System <> 'Sunrise Senior Living Management (California)'
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
        # Uses CTE to build Per Diem worker list from B4HealthOrder,
        # then joins to ESR to find shifts. This catches systems like
        # St Lukes where ESR Program isn't tagged as Per Diem.
        # ============================================================
        try:
            cursor.execute(f'''
                WITH PD_Workers AS (
                    SELECT DISTINCT
                        CONCAT(Last_Name, ', ', First_Name) AS employee_name,
                        Health_System,
                        Facility
                    FROM dhc.B4HealthOrder
                    WHERE Program LIKE '%Per Diem%'
                        AND Contract_Status = 'Closed And Awarded'
                        AND Last_Name IS NOT NULL
                        AND First_Name IS NOT NULL
                        AND Health_System <> 'Sunrise Senior Living Management (California)'
                )
                SELECT DISTINCT
                    'B4' AS source_system,
                    e.[Employee] AS worker_name,
                    e.[Work Date] AS shift_date,
                    e.[Health System] AS system,
                    e.[Agency Name] AS agency,
                    COALESCE(e.[Facility Name], w.Facility) AS facility
                FROM dhc.B4HealthESR e
                LEFT JOIN PD_Workers w
                    ON w.employee_name = e.[Employee]
                    AND w.Health_System = e.[Health System]
                WHERE e.[Health System] IS NOT NULL
                    AND e.[Health System] <> 'Sunrise Senior Living Management (California)'
                    AND e.[Work Date] >= {date_from}
                    AND e.[Work Date] < {date_to}
                    AND e.[Program] LIKE '%Per Diem%'
                    AND (e.[WC] IS NULL OR (
                        e.[WC] NOT LIKE '%cancel%'

                        AND e.[WC] NOT LIKE 'Expense%'
                        AND e.[WC] NOT IN ('Request Time Off', 'Unscheduled PTO', 'Sick Time', 'Call Out', 'Meeting', 'Education', 'Bonus')
                    ))
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
        # VNDLY - Per Diem Assignments (from Work Orders)
        # Include historical work orders so weeks in the past still report
        # Active Headcount. Frontend filters by date-range overlap per week.
        #
        # Facility override: when a worker has an active B4 per-diem
        # assignment with a more specific facility name (e.g. "Cape Regional
        # Medical Center"), prefer that over VNDLY's generic Default Work
        # Site Name (which can be a GL code like "Nursing Administration-01-
        # 6601-5400" that doesn't distinguish sub-facilities). Keeps Cape
        # Regional broken out on the Per Diem tab after the B4→VNDLY move.
        # ============================================================
        try:
            cursor.execute('''
                WITH B4PerDiemFacility AS (
                    SELECT DISTINCT
                        LOWER(LTRIM(RTRIM(CONCAT(First_Name, ' ', Last_Name)))) AS norm_worker,
                        Health_System,
                        Facility AS b4_facility
                    FROM dhc.B4HealthOrder
                    WHERE Program LIKE '%Per Diem%'
                        AND Contract_Status = 'Closed And Awarded'
                        AND Last_Name IS NOT NULL
                        AND First_Name IS NOT NULL
                        AND Facility IS NOT NULL
                )
                SELECT DISTINCT
                    'VNDLY' AS source_system,
                    CAST(wo.[Work Order Id] AS NVARCHAR(50)) AS contract_id,
                    CONCAT(wo.[Contractor First Name], ' ', wo.[Contractor Last Name]) AS worker_name,
                    wo.[Health System] AS system,
                    COALESCE(b4.b4_facility, wo.[Default Work Site Name]) AS facility,
                    wo.[Vendor Name] AS agency,
                    wo.[Start Date] AS startDate,
                    wo.[End Date] AS endDate,
                    NULL AS account_manager,
                    wo.[Resource Manager] AS hiring_manager
                FROM dbo.STAGING_VNDLY_WORKORDERS wo
                LEFT JOIN B4PerDiemFacility b4
                    ON b4.norm_worker = LOWER(LTRIM(RTRIM(CONCAT(wo.[Contractor First Name], ' ', wo.[Contractor Last Name]))))
                    AND b4.Health_System = wo.[Health System]
                WHERE wo.[Start Date] IS NOT NULL
                    AND wo.[Labor Type] LIKE '%Per Diem%'
                    AND (wo.[Current Status] IS NULL OR wo.[Current Status] NOT IN ('Cancelled', 'Rejected', 'Draft'))
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

        # ============================================================
        # VNDLY - Shifts Worked by Per Diem Workers
        # Uses STAGING_VNDLY_SPEND filtered by Labor Type directly.
        # Same B4PerDiemFacility override as the assignment query above —
        # ensures shifts roll up to the right sub-facility (Cape Regional
        # etc.) when VNDLY tags them with a generic Cooper Main GL code.
        # ============================================================
        try:
            cursor.execute(f'''
                WITH B4PerDiemFacility AS (
                    SELECT DISTINCT
                        LOWER(LTRIM(RTRIM(CONCAT(First_Name, ' ', Last_Name)))) AS norm_worker,
                        Health_System,
                        Facility AS b4_facility
                    FROM dhc.B4HealthOrder
                    WHERE Program LIKE '%Per Diem%'
                        AND Contract_Status = 'Closed And Awarded'
                        AND Last_Name IS NOT NULL
                        AND First_Name IS NOT NULL
                        AND Facility IS NOT NULL
                )
                SELECT
                    'VNDLY' AS source_system,
                    CONCAT(s.[Contractor First Name], ' ', s.[Contractor Last Name]) AS worker_name,
                    s.[Item Date] AS shift_date,
                    s.[Health System] AS system,
                    s.[Vendor Company Name] AS agency,
                    COALESCE(b4.b4_facility, wo.[Default Work Site Name]) AS facility
                FROM dbo.STAGING_VNDLY_SPEND s
                INNER JOIN (
                    SELECT DISTINCT
                        CONCAT([Contractor First Name], ' ', [Contractor Last Name]) AS worker_name,
                        [Health System],
                        [Default Work Site Name],
                        [Start Date] AS assignment_start,
                        [End Date] AS assignment_end
                    FROM dbo.STAGING_VNDLY_WORKORDERS
                    WHERE [Labor Type] LIKE '%Per Diem%'
                ) wo ON wo.worker_name = CONCAT(s.[Contractor First Name], ' ', s.[Contractor Last Name])
                    AND wo.[Health System] = s.[Health System]
                    AND s.[Item Date] >= wo.assignment_start
                    AND (wo.assignment_end IS NULL OR s.[Item Date] <= wo.assignment_end)
                LEFT JOIN B4PerDiemFacility b4
                    ON b4.norm_worker = LOWER(LTRIM(RTRIM(CONCAT(s.[Contractor First Name], ' ', s.[Contractor Last Name]))))
                    AND b4.Health_System = s.[Health System]
                WHERE s.[Health System] IS NOT NULL
                    AND s.[Item Date] >= {date_from}
                    AND s.[Item Date] < {date_to}
                    AND s.[Labor Type] LIKE '%Per Diem%'
            ''')

            columns = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                if row_dict.get('shift_date'):
                    row_dict['shift_date'] = row_dict['shift_date'].isoformat() if hasattr(row_dict['shift_date'], 'isoformat') else str(row_dict['shift_date'])
                shifts.append(row_dict)
        except Exception as e:
            print(f"Error loading VNDLY per diem shifts: {e}")

        # ============================================================
        # B4Health - Open (Unfilled) Orders (individual rows for modal drill-down)
        # ============================================================
        open_orders = []
        try:
            cursor.execute(f'''
                SELECT
                    'B4' AS source_system,
                    [Facility Name] AS facility_name,
                    NULL AS health_system,
                    [Position ID] AS position_id,
                    [Specialty Name] AS specialty,
                    [Number of Positions] AS total_positions,
                    NULL AS open_positions,
                    [Start Date] AS start_date,
                    [Hiring Manager] AS hiring_manager,
                    NULL AS job_status
                FROM dhc.B4HEALTHOPENORDER
                WHERE Program LIKE '%Per Diem%'
                    AND [Start Date] >= {date_from}
                    AND [Start Date] < {date_to}
            ''')
            columns = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                if row_dict.get('start_date'):
                    row_dict['start_date'] = row_dict['start_date'].isoformat() if hasattr(row_dict['start_date'], 'isoformat') else str(row_dict['start_date'])
                open_orders.append(row_dict)
        except Exception as e:
            print(f"Error loading B4 open orders: {e}")

        # ============================================================
        # VNDLY - Jobs (Total Positions = Open + Filled, individual rows)
        # ============================================================
        try:
            cursor.execute(f'''
                SELECT
                    'VNDLY' AS source_system,
                    [Work Site (Job)] AS facility_name,
                    [Health System] AS health_system,
                    [JobSystemKey] AS position_id,
                    [Job Title] AS specialty,
                    [Job Quantity] AS total_positions,
                    [Open Positions] AS open_positions,
                    [Start Date] AS start_date,
                    [Resource Manager (Job)] AS hiring_manager,
                    [Job Status] AS job_status
                FROM dbo.STAGING_VNDLY_JOBS
                WHERE [Job Category] LIKE '%Per Diem%'
                    AND ([Start Date] IS NULL OR [Start Date] < {date_to})
                    AND ([End Date] IS NULL OR [End Date] >= {date_from})
            ''')
            columns = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                if row_dict.get('start_date'):
                    row_dict['start_date'] = row_dict['start_date'].isoformat() if hasattr(row_dict['start_date'], 'isoformat') else str(row_dict['start_date'])
                open_orders.append(row_dict)
        except Exception as e:
            print(f"Error loading VNDLY open orders: {e}")

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
                'shifts': shifts,
                'openOrders': open_orders
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
