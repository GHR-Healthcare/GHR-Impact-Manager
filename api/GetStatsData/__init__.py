import azure.functions as func
import pyodbc
import os
import json
from datetime import datetime, timedelta
from shared_code.auth import require_allowed_domain
from shared_code.data_source import is_non_msp
from shared_code.bullhorn_systems import (
    build_system_case_expr,
    build_scope_filter,
)


# Statuses where the placement is awarded and actively in play. Per spec §5
# there is no pre-active funnel for non-MSP — future-dated 'Approved' /
# 'Pending Start' / etc. are the pipeline. Completed/Termination are excluded
# here because Stats is a snapshot of currently-in-play work, not history.
BULLHORN_ACTIVE_SNAPSHOT_STATUSES = (
    'Approved', 'Pending Start', 'Cleared', 'Onboarding', 'Started',
)


def _bullhorn_stats(req: func.HttpRequest) -> func.HttpResponse:
    """
    Stats data for the non-MSP (Bullhorn) instance.

    Shape mirrors the MSP response: { onAssignment[], upcoming[] }. The Stats
    tab itself was removed from the UI, but the Trend tab's cascading filter
    logic reads this data, so we still need to return the right shape.
    """
    try:
        conn = pyodbc.connect(
            f"DRIVER={{ODBC Driver 17 for SQL Server}};"
            f"SERVER={os.environ['BULLHORN_HOST']};"
            f"DATABASE={os.environ['BULLHORN_DB']};"
            f"UID={os.environ['BULLHORN_USER']};"
            f"PWD={os.environ['BULLHORN_PASSWORD']};"
            f"TrustServerCertificate=yes;"
            f"Encrypt=yes"
        )
        cursor = conn.cursor()

        system_case = build_system_case_expr('p.clientCorporationID')
        scope_filter = build_scope_filter('p.clientCorporationID')
        status_list = ', '.join("'" + s + "'" for s in BULLHORN_ACTIVE_SNAPSHOT_STATUSES)

        on_assignment = []
        upcoming = []

        # ============================================================
        # Bullhorn — currently active (started + not yet ended)
        # ============================================================
        try:
            cursor.execute(f'''
                SELECT
                    'Bullhorn' AS source_system,
                    CAST(p.placementID AS NVARCHAR(50)) AS position_id,
                    LTRIM(RTRIM(ISNULL(c.firstName, '') + ' ' + ISNULL(c.lastName, ''))) AS candidate_name,
                    'GHR' AS agency,
                    cc.name AS facility,
                    ({system_case}) AS system,
                    p.customText1 AS specialty,
                    CAST(p.dateBegin AS DATE) AS startDate,
                    CAST(p.dateEnd AS DATE) AS endDate,
                    p.status AS status
                FROM dbo.View_Placement p
                LEFT JOIN dbo.View_Candidate c ON p.candidateID = c.candidateID
                LEFT JOIN dbo.View_ClientCorporation cc ON p.clientCorporationID = cc.clientCorporationID
                WHERE p.isDeleted = 0
                    AND p.status IN ({status_list})
                    AND p.dateBegin IS NOT NULL
                    AND p.dateBegin <= GETDATE()
                    AND (p.dateEnd IS NULL OR p.dateEnd >= GETDATE())
                    AND {scope_filter}
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
            print(f"Error loading Bullhorn active assignments: {e}")
            import traceback; traceback.print_exc()

        # ============================================================
        # Bullhorn — upcoming starts within the next 30 days
        # ============================================================
        try:
            cursor.execute(f'''
                SELECT
                    'Bullhorn' AS source_system,
                    CAST(p.placementID AS NVARCHAR(50)) AS position_id,
                    LTRIM(RTRIM(ISNULL(c.firstName, '') + ' ' + ISNULL(c.lastName, ''))) AS candidate_name,
                    'GHR' AS agency,
                    cc.name AS facility,
                    ({system_case}) AS system,
                    p.customText1 AS specialty,
                    CAST(p.dateBegin AS DATE) AS startDate,
                    CAST(p.dateEnd AS DATE) AS endDate,
                    p.status AS status
                FROM dbo.View_Placement p
                LEFT JOIN dbo.View_Candidate c ON p.candidateID = c.candidateID
                LEFT JOIN dbo.View_ClientCorporation cc ON p.clientCorporationID = cc.clientCorporationID
                WHERE p.isDeleted = 0
                    AND p.status IN ({status_list})
                    AND p.dateBegin IS NOT NULL
                    AND p.dateBegin > GETDATE()
                    AND p.dateBegin <= DATEADD(DAY, 30, GETDATE())
                    AND {scope_filter}
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
            print(f"Error loading Bullhorn upcoming starts: {e}")
            import traceback; traceback.print_exc()

        conn.close()
        print(f"Returning Bullhorn stats: {len(on_assignment)} active, {len(upcoming)} upcoming")

        return func.HttpResponse(
            json.dumps({'onAssignment': on_assignment, 'upcoming': upcoming}, default=str),
            mimetype="application/json",
            status_code=200,
        )
    except Exception as e:
        print(f"Bullhorn stats error: {e}")
        import traceback; traceback.print_exc()
        return func.HttpResponse(
            json.dumps({'error': str(e), 'onAssignment': [], 'upcoming': []}),
            mimetype="application/json",
            status_code=500,
        )


def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    Returns stats data for:
    - onAssignment: Currently active work orders/assignments
    - upcoming: New starts in the near future
    
    Combines data from both B4Health and VNDLY systems.
    """
    auth_error = require_allowed_domain(req)
    if auth_error:
        return auth_error

    if is_non_msp():
        return _bullhorn_stats(req)

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