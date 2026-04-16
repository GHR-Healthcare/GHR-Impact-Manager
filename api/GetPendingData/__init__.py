import azure.functions as func
import pyodbc
import os
import json
from datetime import datetime, date


def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    Returns pending GHR submissions/offers from B4 and VNDLY.
    Only GHR agency submissions are included.
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
        submissions = []
        errors = []

        # ============================================================
        # B4 - GHR submissions that are still active (not cancelled)
        # A submission is cancelled if ANY of these dates are set:
        #   Agency_Retracted_Date, Hospital_Decline_Date, Agency_Decline_Date
        # Status derived from dates:
        #   Awarded: Date_Awarded IS NOT NULL
        #   Offer Pending: Offer_Date IS NOT NULL (but not awarded)
        #   Submitted: everything else
        # ============================================================
        try:
            cursor.execute('''
                SELECT
                    'B4' AS source_system,
                    Contract_Assignment_ID AS submission_id,
                    Health_System AS health_system,
                    Facility AS facility,
                    Unit AS unit_name,
                    Position_Type AS position_type,
                    Care_Type AS care_type,
                    Program_Name AS program,
                    Agency_Name AS agency,
                    Professional AS worker_name,
                    Submission_Date AS submission_date,
                    Offer_Date AS offer_date,
                    Date_Awarded AS date_awarded,
                    Agency_Retracted_Date AS agency_retracted_date,
                    Hospital_Decline_Date AS hospital_decline_date,
                    Hospital_Decline_Reason AS hospital_decline_reason,
                    Agency_Decline_Date AS agency_decline_date,
                    Offer_Decline_Reason AS offer_decline_reason,
                    RTO AS rto,
                    CASE
                        WHEN Agency_Retracted_Date IS NOT NULL THEN 'Retracted'
                        WHEN Hospital_Decline_Date IS NOT NULL THEN 'Hospital Declined'
                        WHEN Agency_Decline_Date IS NOT NULL THEN 'Agency Declined'
                        WHEN Date_Awarded IS NOT NULL THEN 'Awarded'
                        WHEN Offer_Date IS NOT NULL THEN 'Offer Pending'
                        ELSE 'Submitted'
                    END AS status
                    ,CASE WHEN Agency_Name LIKE '%GHR%' THEN 1 ELSE 0 END AS is_ghr
                FROM dhc.B4Health_Contract_Submissions
                WHERE Submission_Date >= DATEADD(MONTH, -6, GETDATE())
                    AND LTRIM(RTRIM(IsActive)) = 'Yes'
                ORDER BY Submission_Date DESC
            ''')

            columns = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                for key, val in row_dict.items():
                    if isinstance(val, (datetime, date)):
                        row_dict[key] = val.isoformat()
                submissions.append(row_dict)
        except Exception as e:
            errors.append(f"B4: {str(e)}")
            print(f"Error loading B4 pending submissions: {e}")

        # ============================================================
        # VNDLY - GHR submissions (non-terminal statuses)
        # Terminal: Job Closed, Rejected, Withdrawn, Offer Declined, Onboarded
        # ============================================================
        try:
            cursor.execute('''
                SELECT
                    'VNDLY' AS source_system,
                    [Job Application System Id] AS submission_id,
                    [Health System] AS health_system,
                    [JobSystemKey] AS job_id,
                    [Vendor Company Name] AS agency,
                    CONCAT([First Name], ' ', [Last Name]) AS worker_name,
                    [Full Name] AS full_name,
                    [Status] AS status,
                    [Application Date] AS submission_date,
                    [Offer Release Date] AS offer_date,
                    [Offer Accepted Date] AS offer_accepted_date,
                    [Pending Offer Release Date] AS pending_offer_date,
                    [Ready To Onboard Date] AS ready_to_onboard_date,
                    [Onboarded Date] AS onboarded_date,
                    [Client Interview Date] AS interview_date,
                    [Client Rejected Date] AS client_rejected_date,
                    [Vendor Offer Declined Date] AS vendor_declined_date,
                    [Vendor Withdrawn Date] AS vendor_withdrawn_date,
                    [Withdrawal Reason - Choice] AS withdrawal_reason,
                    [Rejected Reason - Choice] AS rejected_reason,
                    [RTO] AS rto,
                    [Candidate ID] AS candidate_id
                    ,CASE WHEN [Vendor Company Name] LIKE '%GHR%' THEN 1 ELSE 0 END AS is_ghr
                FROM dbo.STAGING_VNDLY_SUBMISSIONS
                WHERE 1=1
                    AND [Application Date] >= DATEADD(MONTH, -6, GETDATE())
                ORDER BY [Application Date] DESC
            ''')

            columns = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                for key, val in row_dict.items():
                    if isinstance(val, (datetime, date)):
                        row_dict[key] = val.isoformat()
                submissions.append(row_dict)
        except Exception as e:
            errors.append(f"VNDLY: {str(e)}")
            print(f"Error loading VNDLY pending submissions: {e}")

        cursor.close()
        conn.close()

        return func.HttpResponse(
            json.dumps({"submissions": submissions, "errors": errors}),
            mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"}
        )

    except Exception as e:
        return func.HttpResponse(
            json.dumps({"error": str(e)}),
            status_code=500,
            mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"}
        )
