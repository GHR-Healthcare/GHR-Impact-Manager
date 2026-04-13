import azure.functions as func
import pyodbc
import os
import json


def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    Returns trend data: a rolling window of assignments from both B4Health and VNDLY.
    Covers 6 weeks back through present (and beyond via end dates) to support
    4-week lookback + current week + 4-week forward projection in the frontend.
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
        assignments = []

        # ============================================================
        # B4Health - Assignments in rolling window
        # ============================================================
        try:
            cursor.execute('''
                SELECT
                    'B4' AS source_system,
                    CONCAT(Last_Name, ', ', First_Name) AS worker_name,
                    Health_System AS system,
                    Facility AS facility,
                    Agency AS agency,
                    Care_Type AS specialty,
                    Program AS category,
                    Start_Date AS startDate,
                    End_Date AS endDate
                FROM dhc.B4HealthOrder
                WHERE Contract_Status = 'Closed And Awarded'
                    AND Start_Date IS NOT NULL
                    AND (End_Date IS NULL OR End_Date >= DATEADD(WEEK, -4, GETDATE()))
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
            print(f"Error loading B4 trend assignments: {e}")

        # ============================================================
        # VNDLY - Assignments in rolling window
        # ============================================================
        try:
            cursor.execute('''
                SELECT
                    'VNDLY' AS source_system,
                    CONCAT([Contractor First Name], ' ', [Contractor Last Name]) AS worker_name,
                    [Health System] AS system,
                    [Default Work Site Name] AS facility,
                    [Vendor Name] AS agency,
                    [Job Title] AS specialty,
                    [Labor Type] AS category,
                    [Start Date] AS startDate,
                    [End Date] AS endDate
                FROM dbo.STAGING_VNDLY_WORKORDERS
                WHERE [Current Status] = 'Active'
                    AND [Start Date] IS NOT NULL
                    AND ([End Date] IS NULL OR [End Date] >= DATEADD(WEEK, -4, GETDATE()))
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
            print(f"Error loading VNDLY trend assignments: {e}")

        conn.close()

        print(f"Returning {len(assignments)} trend assignments")

        return func.HttpResponse(
            json.dumps({'assignments': assignments}, default=str),
            mimetype="application/json",
            status_code=200
        )

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return func.HttpResponse(
            json.dumps({'error': str(e), 'assignments': []}),
            mimetype="application/json",
            status_code=500
        )
