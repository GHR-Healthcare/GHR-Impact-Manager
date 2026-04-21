import azure.functions as func
import pyodbc
import os
import json
from datetime import datetime, timedelta

def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    Returns stats data for:
    - onAssignment: Currently active work orders/assignments
    - upcoming: New starts in the near future
    
    Combines data from both B4Health and VNDLY systems.
    """
    try:
        conn = pyodbc.connect(
            f"DRIVER={{ODBC Driver 17 for SQL Server}};"
            f"SERVER={os.environ['DB_HOST']};"
            f"DATABASE={os.environ['POSITIONS_DB']};"
            f"UID={os.environ['DB_USER']};"
            f"PWD={os.environ['DB_PASSWORD']};"
            f"TrustServerCertificate=yes"
        )
        
        cursor = conn.cursor()
        on_assignment = []
        upcoming = []
        
        # ============================================================
        # B4Health - Active Assignments
        # ============================================================
        try:
            cursor.execute('''
                SELECT
                    'B4' AS source_system,
                    Contract_ID AS position_id,
                    CONCAT(First_Name, ' ', Last_Name) AS candidate_name,
                    Agency AS agency,
                    Facility AS facility,
                    Health_System AS system,
                    Care_Type AS specialty,
                    Start_Date AS startDate,
                    End_Date AS endDate,
                    Contract_Status AS status
                FROM dhc.B4HealthOrder
                WHERE Contract_Status = 'Closed And Awarded'
                    AND Start_Date IS NOT NULL
                    AND Start_Date <= GETDATE()
                    AND (End_Date IS NULL OR End_Date >= GETDATE())
                    AND Health_System NOT LIKE '%Richmond University%'
                    AND Health_System NOT LIKE '%Redeemer%'
                    AND Health_System <> 'Sunrise Senior Living Management (California)'
            ''')

            columns = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                # Convert dates to ISO format
                if row_dict.get('startDate'):
                    row_dict['startDate'] = row_dict['startDate'].isoformat() if hasattr(row_dict['startDate'], 'isoformat') else str(row_dict['startDate'])
                if row_dict.get('endDate'):
                    row_dict['endDate'] = row_dict['endDate'].isoformat() if hasattr(row_dict['endDate'], 'isoformat') else str(row_dict['endDate'])
                on_assignment.append(row_dict)
        except Exception as e:
            print(f"Error loading B4 active assignments: {e}")
        
        # ============================================================
        # B4Health - Upcoming Starts
        # ============================================================
        try:
            cursor.execute('''
                SELECT
                    'B4' AS source_system,
                    Contract_ID AS position_id,
                    CONCAT(First_Name, ' ', Last_Name) AS candidate_name,
                    Agency AS agency,
                    Facility AS facility,
                    Health_System AS system,
                    Care_Type AS specialty,
                    Start_Date AS startDate,
                    End_Date AS endDate,
                    Contract_Status AS status
                FROM dhc.B4HealthOrder
                WHERE Contract_Status = 'Closed And Awarded'
                    AND Start_Date IS NOT NULL
                    AND Start_Date > GETDATE()
                    AND Start_Date <= DATEADD(day, 30, GETDATE())
                    AND Health_System NOT LIKE '%Richmond University%'
                    AND Health_System NOT LIKE '%Redeemer%'
                    AND Health_System <> 'Sunrise Senior Living Management (California)'
            ''')
            
            columns = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                if row_dict.get('startDate'):
                    row_dict['startDate'] = row_dict['startDate'].isoformat() if hasattr(row_dict['startDate'], 'isoformat') else str(row_dict['startDate'])
                if row_dict.get('endDate'):
                    row_dict['endDate'] = row_dict['endDate'].isoformat() if hasattr(row_dict['endDate'], 'isoformat') else str(row_dict['endDate'])
                upcoming.append(row_dict)
        except Exception as e:
            print(f"Error loading B4 upcoming: {e}")
        
        # ============================================================
        # VNDLY - Active Assignments (from Work Orders)
        # ============================================================
        try:
            cursor.execute('''
                SELECT
                    'VNDLY' AS source_system,
                    CAST([Work Order Id] AS NVARCHAR(50)) AS position_id,
                    CONCAT([Contractor First Name], ' ', [Contractor Last Name]) AS candidate_name,
                    [Vendor Name] AS agency,
                    [Default Work Site Name] AS facility,
                    [Health System] AS system,
                    [Job Title] AS specialty,
                    [Start Date] AS startDate,
                    [End Date] AS endDate,
                    [Current Status] AS status
                FROM dbo.STAGING_VNDLY_WORKORDERS
                WHERE [Current Status] = 'Active'
                    AND [Start Date] IS NOT NULL
                    AND [Start Date] <= GETDATE()
                    AND ([End Date] IS NULL OR [End Date] >= GETDATE())
            ''')
            
            columns = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                if row_dict.get('startDate'):
                    row_dict['startDate'] = row_dict['startDate'].isoformat() if hasattr(row_dict['startDate'], 'isoformat') else str(row_dict['startDate'])
                if row_dict.get('endDate'):
                    row_dict['endDate'] = row_dict['endDate'].isoformat() if hasattr(row_dict['endDate'], 'isoformat') else str(row_dict['endDate'])
                on_assignment.append(row_dict)
        except Exception as e:
            print(f"Error loading VNDLY active assignments: {e}")
        
        # ============================================================
        # VNDLY - Upcoming Starts (confirmed but not yet started)
        # ============================================================
        try:
            cursor.execute('''
                SELECT
                    'VNDLY' AS source_system,
                    CAST([Work Order Id] AS NVARCHAR(50)) AS position_id,
                    CONCAT([Contractor First Name], ' ', [Contractor Last Name]) AS candidate_name,
                    [Vendor Name] AS agency,
                    [Default Work Site Name] AS facility,
                    [Health System] AS system,
                    [Job Title] AS specialty,
                    [Start Date] AS startDate,
                    [End Date] AS endDate,
                    [Current Status] AS status
                FROM dbo.STAGING_VNDLY_WORKORDERS
                WHERE [Current Status] IN ('Verification In Progress', 'Ready to Onboard', 'Offer Released')
                    AND [Start Date] IS NOT NULL
                    AND [Start Date] > GETDATE()
                    AND [Start Date] <= DATEADD(day, 30, GETDATE())
            ''')
            
            columns = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                if row_dict.get('startDate'):
                    row_dict['startDate'] = row_dict['startDate'].isoformat() if hasattr(row_dict['startDate'], 'isoformat') else str(row_dict['startDate'])
                if row_dict.get('endDate'):
                    row_dict['endDate'] = row_dict['endDate'].isoformat() if hasattr(row_dict['endDate'], 'isoformat') else str(row_dict['endDate'])
                upcoming.append(row_dict)
        except Exception as e:
            print(f"Error loading VNDLY upcoming: {e}")
        
        conn.close()
        
        b4_active = len([r for r in on_assignment if r.get('source_system') == 'B4'])
        vndly_active = len([r for r in on_assignment if r.get('source_system') == 'VNDLY'])
        print(f"Returning {len(on_assignment)} active (B4: {b4_active}, VNDLY: {vndly_active}), {len(upcoming)} upcoming")
        
        return func.HttpResponse(
            json.dumps({
                'onAssignment': on_assignment,
                'upcoming': upcoming
            }, default=str),
            mimetype="application/json",
            status_code=200
        )
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return func.HttpResponse(
            json.dumps({'error': str(e), 'onAssignment': [], 'upcoming': []}),
            mimetype="application/json",
            status_code=500
        )