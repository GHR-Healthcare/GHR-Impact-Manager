import azure.functions as func
import pyodbc
import os
import json
from shared_code.auth import require_allowed_domain
from shared_code.data_source import is_non_msp, get_bullhorn_conn, get_symplr_conn
from shared_code.bullhorn_systems import (
    build_system_case_expr,
    build_scope_filter,
)
from shared_code.symplr_systems import (
    build_system_case_expr as symplr_system_case_expr,
    build_scope_filter as symplr_scope_filter,
    build_division_case_expr as symplr_division_case_expr,
)


# Non-MSP open job order statuses. Discovery turned up only two — there's
# no submission funnel so anything else (Filled, Placed, Closed, Cancelled,
# Archive) is terminal.
BULLHORN_OPEN_JO_STATUSES = ('Accepting Candidates', 'On Hold')


def _bullhorn_positions_data():
    """Returns open Bullhorn job orders. Raises on error."""
    conn = get_bullhorn_conn()
    cursor = conn.cursor()
    system_case = build_system_case_expr('jo.clientCorporationID')
    scope_filter = build_scope_filter('jo.clientCorporationID')
    status_list = ', '.join("'" + s + "'" for s in BULLHORN_OPEN_JO_STATUSES)

    cursor.execute(f'''
        SELECT
            'Bullhorn' AS source_system,
            CAST(jo.jobOrderID AS NVARCHAR(50)) AS position_id,
            ISNULL(jo.employmentType, 'Unknown') AS program,
            cc.name AS facility,
            jo.title AS specialty,
            CAST(jo.dateAdded AS DATE) AS date_added,
            NULL AS unit,
            NULL AS cost_center,
            TRY_CAST(jo.clientBillRate AS DECIMAL(10,2)) AS bill_rate,
            0 AS bill_rate_estimated,
            TRY_CAST(jo.hoursPerWeek AS DECIMAL(10,2)) AS shift_hours,
            NULL AS shift_time,
            LTRIM(RTRIM(ISNULL(u.firstName, '') + ' ' + ISNULL(u.lastName, ''))) AS hiring_manager,
            0 AS num_submissions,
            jo.numOpenings AS num_positions,
            NULL AS requisition_reason,
            NULL AS shift_diff,
            NULL AS min_hours,
            CAST(jo.startDate AS DATE) AS open_start_date,
            jo.employmentType AS time_type,
            NULL AS start_time,
            NULL AS end_time,
            jo.status AS status,
            ({system_case}) AS health_system,
            jo.customText1 AS profession,
            jo.customText2 AS subspecialty,
            jo.customTextBlock1 AS division,
            NULL AS region
        FROM dbo.View_JobOrder jo
        LEFT JOIN dbo.View_ClientCorporation cc ON jo.clientCorporationID = cc.clientCorporationID
        LEFT JOIN dbo.View_CorporateUser u ON jo.ownerID = u.corporateUserID
        WHERE jo.isDeleted = 0
            AND jo.status IN ({status_list})
            AND {scope_filter}
        ORDER BY jo.dateAdded DESC
    ''')
    columns = [column[0] for column in cursor.description]
    rows = []
    for row in cursor.fetchall():
        row_dict = dict(zip(columns, row))
        if row_dict.get('date_added'):
            row_dict['date_added'] = row_dict['date_added'].isoformat() if hasattr(row_dict['date_added'], 'isoformat') else str(row_dict['date_added'])
        if row_dict.get('open_start_date'):
            row_dict['open_start_date'] = row_dict['open_start_date'].isoformat() if hasattr(row_dict['open_start_date'], 'isoformat') else str(row_dict['open_start_date'])
        row_dict['ghrSubs'] = 0
        row_dict['avSubs'] = 0
        row_dict['ghrDeclines'] = 0
        row_dict['avDeclines'] = 0
        row_dict['candidates'] = []
        rows.append(row_dict)
    conn.close()
    return rows


def _symplr_positions_data():
    """Returns open Symplr positions. Two sources are unioned:
      - lt_order rows with status='open' (long-term open requisitions)
      - Orderless 'open' orders (lt_orderid IN (0, NULL)) aggregated by
        (customer, specialty, nursetype) so a batch of identical unfilled
        shifts collapses into one position row with num_positions = shift count.
    """
    conn = get_symplr_conn()
    if conn is None:
        return []
    cursor = conn.cursor()
    sys_case = symplr_system_case_expr('lt.clientid')
    scope = symplr_scope_filter('lt.clientid')
    division_case = symplr_division_case_expr('lt.clientid')
    sys_case_orders = symplr_system_case_expr('o.customerid')
    scope_orders = symplr_scope_filter('o.customerid')
    division_case_orders = symplr_division_case_expr('o.customerid')

    def _serialize(row_dict):
        if row_dict.get('date_added'):
            row_dict['date_added'] = row_dict['date_added'].isoformat() if hasattr(row_dict['date_added'], 'isoformat') else str(row_dict['date_added'])
        if row_dict.get('open_start_date'):
            row_dict['open_start_date'] = row_dict['open_start_date'].isoformat() if hasattr(row_dict['open_start_date'], 'isoformat') else str(row_dict['open_start_date'])
        row_dict['ghrSubs'] = 0
        row_dict['avSubs'] = 0
        row_dict['ghrDeclines'] = 0
        row_dict['avDeclines'] = 0
        row_dict['candidates'] = []
        return row_dict

    # Each of the three Symplr positions queries runs in its own try block so
    # one bad query doesn't take down the others. Without this, a SQL error in
    # query 3 kills the whole _symplr_positions_data() call via the outer
    # _non_msp_positions try/except, and the Symplr book vanishes from the UI.
    rows = []

    # ---- 1. lt_order with status='open' (long-term open requisitions) ----
    try:
        cursor.execute(f'''
            SELECT
                'Symplr' AS source_system,
                CAST(lt.lt_orderid AS NVARCHAR(50)) AS position_id,
                ISNULL(lt.nursetype, 'Unknown') AS program,
                pc.clientname AS facility,
                LTRIM(RTRIM(ISNULL(lt.nursetype, '') + ' — ' + ISNULL(lt.specialty, ''))) AS specialty,
                CAST(lt.date_entered AS DATE) AS date_added,
                NULL AS unit,
                ISNULL(lt.costCenterNumber, '') AS cost_center,
                NULL AS bill_rate,
                1 AS bill_rate_estimated,
                TRY_CAST(lt.HoursPerWeek AS DECIMAL(10,2)) AS shift_hours,
                NULL AS shift_time,
                NULL AS hiring_manager,
                0 AS num_submissions,
                1 AS num_positions,
                NULL AS requisition_reason,
                NULL AS shift_diff,
                NULL AS min_hours,
                CAST(lt.date_start AS DATE) AS open_start_date,
                'Long-Term' AS time_type,
                NULL AS start_time,
                NULL AS end_time,
                lt.status AS status,
                ({sys_case}) AS health_system,
                lt.specialty AS profession,
                NULL AS subspecialty,
                ({division_case}) AS division,
                pc.state AS region
            FROM dbo.lt_order lt
            LEFT JOIN dbo.profile_client pc ON lt.clientid = pc.recordid
            WHERE lt.status = 'open'
                AND {scope}
            ORDER BY lt.date_entered DESC
        ''')
        columns = [column[0] for column in cursor.description]
        rows.extend(_serialize(dict(zip(columns, row))) for row in cursor.fetchall())
    except Exception as e:
        print(f"Symplr positions: lt_order open query failed: {e}")
        import traceback; traceback.print_exc()

    # ---- 2. Orderless 'open' orders (lt_orderid IN (0, NULL)) ----
    # Aggregate by (customer, specialty, nursetype) so identical unfilled
    # shifts collapse to one row with num_positions = shift count.
    try:
        cursor.execute(f'''
            SELECT
                'Symplr' AS source_system,
                CAST(MAX(o.orderid) AS NVARCHAR(50)) AS position_id,
                ISNULL(MAX(o.nursetype), 'Unknown') AS program,
                MAX(pc.clientname) AS facility,
                LTRIM(RTRIM(ISNULL(MAX(o.nursetype), '') + ' — ' + ISNULL(MAX(o.specialty), ''))) AS specialty,
                CAST(MIN(o.datetimecreated) AS DATE) AS date_added,
                NULL AS unit,
                ISNULL(MAX(o.costCenterNumber), '') AS cost_center,
                NULL AS bill_rate,
                1 AS bill_rate_estimated,
                NULL AS shift_hours,
                NULL AS shift_time,
                NULL AS hiring_manager,
                0 AS num_submissions,
                COUNT(*) AS num_positions,
                NULL AS requisition_reason,
                NULL AS shift_diff,
                NULL AS min_hours,
                CAST(MIN(o.jobdatestart) AS DATE) AS open_start_date,
                'Per Shift' AS time_type,
                NULL AS start_time,
                NULL AS end_time,
                'open' AS status,
                ({sys_case_orders}) AS health_system,
                MAX(o.specialty) AS profession,
                NULL AS subspecialty,
                ({division_case_orders}) AS division,
                MAX(pc.state) AS region
            FROM dbo.orders o
            LEFT JOIN dbo.profile_client pc ON o.customerid = pc.recordid
            WHERE o.status = 'open'
                AND (o.lt_orderid IS NULL OR o.lt_orderid = 0)
                AND o.jobdatestart >= GETDATE()
                AND {scope_orders}
            GROUP BY o.customerid, o.specialty, o.nursetype
        ''')
        columns = [column[0] for column in cursor.description]
        rows.extend(_serialize(dict(zip(columns, row))) for row in cursor.fetchall())
    except Exception as e:
        print(f"Symplr positions: orderless open query failed: {e}")
        import traceback; traceback.print_exc()

    # ---- 3. Uncovered shifts under non-open lt_orders ----
    # One row per parent lt_orderid with num_positions = unfilled shifts.
    # Skip parents whose lt_order.status='open' (those are already in query 1).
    # GROUP BY includes lt.clientid because the system_case expression
    # references it and SQL Server requires GROUP BY membership for
    # non-aggregated columns.
    try:
        cursor.execute(f'''
            SELECT
                'Symplr' AS source_system,
                CAST(o.lt_orderid AS NVARCHAR(50)) AS position_id,
                ISNULL(MAX(lt.nursetype), MAX(o.nursetype)) AS program,
                MAX(pc.clientname) AS facility,
                LTRIM(RTRIM(
                    ISNULL(MAX(lt.nursetype), MAX(o.nursetype)) + ' — ' +
                    ISNULL(MAX(lt.specialty), MAX(o.specialty))
                )) AS specialty,
                CAST(MIN(o.datetimecreated) AS DATE) AS date_added,
                NULL AS unit,
                ISNULL(MAX(lt.costCenterNumber), MAX(o.costCenterNumber)) AS cost_center,
                NULL AS bill_rate,
                1 AS bill_rate_estimated,
                NULL AS shift_hours,
                NULL AS shift_time,
                NULL AS hiring_manager,
                0 AS num_submissions,
                COUNT(*) AS num_positions,
                NULL AS requisition_reason,
                NULL AS shift_diff,
                NULL AS min_hours,
                CAST(MIN(o.jobdatestart) AS DATE) AS open_start_date,
                'Uncovered Shifts' AS time_type,
                NULL AS start_time,
                NULL AS end_time,
                'open' AS status,
                ({sys_case}) AS health_system,
                MAX(lt.specialty) AS profession,
                NULL AS subspecialty,
                ({division_case}) AS division,
                MAX(pc.state) AS region
            FROM dbo.orders o
            INNER JOIN dbo.lt_order lt ON o.lt_orderid = lt.lt_orderid
            LEFT JOIN dbo.profile_client pc ON lt.clientid = pc.recordid
            WHERE o.status = 'open'
                AND o.lt_orderid IS NOT NULL AND o.lt_orderid <> 0
                AND lt.status <> 'open'
                AND (o.filledby IS NULL OR o.filledby = 0)
                AND o.jobdatestart >= GETDATE()
                AND {scope}
            GROUP BY o.lt_orderid, lt.clientid
        ''')
        columns = [column[0] for column in cursor.description]
        rows.extend(_serialize(dict(zip(columns, row))) for row in cursor.fetchall())
    except Exception as e:
        print(f"Symplr positions: uncovered-shifts query failed: {e}")
        import traceback; traceback.print_exc()

    conn.close()
    print(f"Symplr positions: returning {len(rows)} rows")
    return rows


def _non_msp_positions(req: func.HttpRequest) -> func.HttpResponse:
    positions = []
    errors = []
    try:
        positions.extend(_bullhorn_positions_data())
    except Exception as e:
        print(f"Bullhorn positions error: {e}")
        import traceback; traceback.print_exc()
        errors.append(f"bullhorn: {e}")
    try:
        positions.extend(_symplr_positions_data())
    except Exception as e:
        print(f"Symplr positions error: {e}")
        import traceback; traceback.print_exc()
        errors.append(f"symplr: {e}")
    print(f"non-MSP positions: {len(positions)} rows (errors: {errors or 'none'})")
    return func.HttpResponse(
        json.dumps(positions, default=str),
        mimetype="application/json",
        status_code=200,
    )


def main(req: func.HttpRequest) -> func.HttpResponse:
    auth_error = require_allowed_domain(req)
    if auth_error:
        return auth_error

    if is_non_msp():
        return _non_msp_positions(req)

    try:
        # Connect to positions database
        conn = pyodbc.connect(
            f"DRIVER={{ODBC Driver 17 for SQL Server}};"
            f"SERVER={os.environ['DB_HOST']};"
            f"DATABASE={os.environ['POSITIONS_DB']};"
            f"UID={os.environ['DB_USER']};"
            f"PWD={os.environ['DB_PASSWORD']};"
            f"TrustServerCertificate=yes"
        )
        
        cursor = conn.cursor()
        positions = []
        b4_position_ids = []
        vndly_position_ids = []
        
        # ============================================================
        # PART 1: Get B4Health Open Positions
        # ============================================================
        cursor.execute('''
            SELECT 
                'B4' AS source_system,
                RTRIM(LTRIM(o.[Position ID])) AS position_id,
                o.[Program] AS program,
                o.[Facility Name] AS facility,
                o.[Specialty Name] AS specialty,
                o.[Date Added] AS date_added,
                o.[Unit Name] AS unit,
                o.[Cost Center] AS cost_center,
                o.[Bill Rate] AS bill_rate,
                o.[Shift Hours] AS shift_hours,
                o.[Shift Time] AS shift_time,
                o.[Hiring Manager] AS hiring_manager,
                o.[# of Submissions] AS num_submissions,
                o.[Number of Positions] AS num_positions,
                o.[Requisition_Reason] AS requisition_reason,
                o.[Shift Diff] AS shift_diff,
                o.[Min Hours] AS min_hours,
                o.[Start Date] AS open_start_date,
                COALESCE(b.Time_Type, CAST(o.[Shift Hours] AS NVARCHAR(50))) AS time_type,
                b.Start_Time AS start_time,
                b.End_Time AS end_time,
                b.Contract_Status AS status,
                b.Health_System AS health_system
            FROM dhc.B4HEALTHOPENORDER o
            LEFT JOIN dhc.B4HealthOrder b 
                ON RTRIM(LTRIM(o.[Position ID])) = RTRIM(LTRIM(b.Contract_ID))
            ORDER BY o.[Date Added] DESC
        ''')
        
        columns = [column[0] for column in cursor.description]
        
        for row in cursor.fetchall():
            row_dict = dict(zip(columns, row))
            
            # Convert dates to ISO format
            if row_dict.get('date_added'):
                row_dict['date_added'] = row_dict['date_added'].isoformat() if hasattr(row_dict['date_added'], 'isoformat') else str(row_dict['date_added'])
            
            # Convert time fields to strings
            if row_dict.get('start_time'):
                row_dict['start_time'] = str(row_dict['start_time'])
            if row_dict.get('end_time'):
                row_dict['end_time'] = str(row_dict['end_time'])
            
            # Initialize submission counts
            row_dict['ghrSubs'] = 0
            row_dict['avSubs'] = 0
            row_dict['ghrDeclines'] = 0
            row_dict['avDeclines'] = 0
            row_dict['candidates'] = []
            
            positions.append(row_dict)
            if row_dict.get('position_id'):
                b4_position_ids.append(row_dict['position_id'])
        
        # ============================================================
        # PART 2: Get VNDLY Open Positions
        # ============================================================
        cursor.execute('''
            SELECT 
                'VNDLY' AS source_system,
                [JobSystemKey] AS position_id,
                [Job Category] AS program,
                COALESCE([Facility], [Health System]) AS facility,
                [Job Title] AS specialty,
                [Job Approval Date] AS date_added,
                [Organization Unit (Job)] AS unit,
                [Charge Code - Cost Center] AS cost_center,
                COALESCE([Bill Rate], [Suggested Bill Rate], [Max Bill Rate]) AS bill_rate,
                CASE 
                    WHEN [Bill Rate] IS NOT NULL THEN 0
                    ELSE 1
                END AS bill_rate_estimated,
                [Standard Hours Per Week] AS shift_hours,
                [Shift Time Type] AS shift_time,
                [Resource Manager (Job)] AS hiring_manager,
                [Interviews Performed (for this job)] AS num_submissions,
                [Open Positions] AS num_positions,
                [Reason For Hire] AS requisition_reason,
                NULL AS shift_diff,
                NULL AS min_hours,
                [Start Date] AS open_start_date,
                [Job Type] AS time_type,
                NULL AS start_time,
                NULL AS end_time,
                [Job Status] AS status,
                [Health System] AS health_system
            FROM dbo.STAGING_VNDLY_JOBS
            WHERE [Job Status] = 'Active'
            ORDER BY [Job Approval Date] DESC
        ''')
        
        columns = [column[0] for column in cursor.description]
        
        for row in cursor.fetchall():
            row_dict = dict(zip(columns, row))
            
            # Convert dates to ISO format
            if row_dict.get('date_added'):
                row_dict['date_added'] = row_dict['date_added'].isoformat() if hasattr(row_dict['date_added'], 'isoformat') else str(row_dict['date_added'])
            
            # Initialize submission counts
            row_dict['ghrSubs'] = 0
            row_dict['avSubs'] = 0
            row_dict['ghrDeclines'] = 0
            row_dict['avDeclines'] = 0
            row_dict['candidates'] = []
            
            positions.append(row_dict)
            if row_dict.get('position_id'):
                vndly_position_ids.append(row_dict['position_id'])
        
        # Create lookup dict for positions
        pos_lookup = {p['position_id']: p for p in positions}
        
        # ============================================================
        # PART 3: Get B4Health Submissions
        # ============================================================
        if b4_position_ids:
            placeholders = ','.join(['?' for _ in b4_position_ids])
            cursor.execute(f'''
                SELECT 
                    RTRIM(LTRIM(Contract_Assignment_ID)) AS Contract_Assignment_ID,
                    Agency_Name,
                    Professional,
                    Submission_Date,
                    Agency_Retracted_Date,
                    Hospital_Decline_Date,
                    Hospital_Decline_Reason,
                    Offer_Date,
                    Agency_Decline_Date,
                    Offer_Decline_Reason,
                    Date_Awarded,
                    RTO,
                    IsActive
                FROM dhc.B4Health_Contract_Submissions
                WHERE RTRIM(LTRIM(Contract_Assignment_ID)) IN ({placeholders})
            ''', b4_position_ids)
            
            sub_columns = [column[0] for column in cursor.description]
            
            for row in cursor.fetchall():
                sub = dict(zip(sub_columns, row))
                pos_id = sub.get('Contract_Assignment_ID')
                
                if pos_id and pos_id in pos_lookup:
                    position = pos_lookup[pos_id]
                    
                    # Determine if declined
                    is_declined = bool(
                        sub.get('Hospital_Decline_Date') or 
                        sub.get('Agency_Decline_Date') or 
                        sub.get('Agency_Retracted_Date')
                    )
                    
                    # Determine decline reason
                    decline_reason = None
                    if sub.get('Hospital_Decline_Date'):
                        decline_reason = sub.get('Hospital_Decline_Reason') or 'Hospital Declined'
                    elif sub.get('Agency_Decline_Date'):
                        decline_reason = sub.get('Offer_Decline_Reason') or 'Agency Declined'
                    elif sub.get('Agency_Retracted_Date'):
                        decline_reason = 'Agency Retracted'
                    
                    # Determine if GHR (GHR or Planet Healthcare, not The Planet Group)
                    agency = str(sub.get('Agency_Name') or '').lower()
                    is_ghr = 'ghr' in agency or 'planet healthcare' in agency
                    
                    # Build candidate object
                    candidate = {
                        'name': sub.get('Professional') or 'Unknown',
                        'agency': sub.get('Agency_Name') or 'Unknown',
                        'submitDate': sub.get('Submission_Date').isoformat() if sub.get('Submission_Date') and hasattr(sub.get('Submission_Date'), 'isoformat') else None,
                        'offerDate': sub.get('Offer_Date').isoformat() if sub.get('Offer_Date') and hasattr(sub.get('Offer_Date'), 'isoformat') else None,
                        'awardedDate': sub.get('Date_Awarded').isoformat() if sub.get('Date_Awarded') and hasattr(sub.get('Date_Awarded'), 'isoformat') else None,
                        'rto': sub.get('RTO'),
                        'isDeclined': is_declined,
                        'declineReason': decline_reason,
                        'hospDeclineDate': sub.get('Hospital_Decline_Date').isoformat() if sub.get('Hospital_Decline_Date') and hasattr(sub.get('Hospital_Decline_Date'), 'isoformat') else None,
                        'agencyDeclineDate': sub.get('Agency_Decline_Date').isoformat() if sub.get('Agency_Decline_Date') and hasattr(sub.get('Agency_Decline_Date'), 'isoformat') else None,
                        'agencyRetractedDate': sub.get('Agency_Retracted_Date').isoformat() if sub.get('Agency_Retracted_Date') and hasattr(sub.get('Agency_Retracted_Date'), 'isoformat') else None,
                        'isGHR': is_ghr,
                        'isActive': sub.get('IsActive') or False
                    }
                    
                    position['candidates'].append(candidate)
                    
                    # Update counts
                    if is_declined:
                        if is_ghr:
                            position['ghrDeclines'] += 1
                        else:
                            position['avDeclines'] += 1
                    else:
                        if is_ghr:
                            position['ghrSubs'] += 1
                        else:
                            position['avSubs'] += 1
        
        # ============================================================
        # PART 4: Get VNDLY Submissions
        # ============================================================
        if vndly_position_ids:
            placeholders = ','.join(['?' for _ in vndly_position_ids])
            cursor.execute(f'''
                SELECT 
                    [JobSystemKey] AS job_id,
                    [Full Name] AS candidate_name,
                    [Vendor Company Name] AS agency,
                    [Application Date] AS submission_date,
                    [Status] AS status,
                    [Client Interview Date] AS interview_date,
                    [Client Rejected Date] AS client_rejected_date,
                    [Rejected Reason - Choice] AS reject_reason_choice,
                    [Rejected Reason - Text] AS reject_reason_text,
                    [Vendor Offer Declined Date] AS vendor_declined_date,
                    [Vendor Withdrawn Date] AS vendor_withdrawn_date,
                    [Withdrawal Reason - Choice] AS withdrawal_reason_choice,
                    [Withdrawal Reason - Text] AS withdrawal_reason_text,
                    [Offer Release Date] AS offer_date,
                    [Offer Accepted Date] AS offer_accepted_date,
                    [Onboarded Date] AS onboarded_date,
                    [Ready To Onboard Date] AS rto_date,
                    [Candidate ID] AS candidate_id
                FROM dbo.STAGING_VNDLY_SUBMISSIONS
                WHERE [JobSystemKey] IN ({placeholders})
            ''', vndly_position_ids)
            
            sub_columns = [column[0] for column in cursor.description]
            
            for row in cursor.fetchall():
                sub = dict(zip(sub_columns, row))
                pos_id = sub.get('job_id')
                
                if pos_id and pos_id in pos_lookup:
                    position = pos_lookup[pos_id]
                    
                    status = str(sub.get('status') or '').lower()
                    
                    # Determine if declined/withdrawn based on dates OR status
                    is_declined = bool(
                        sub.get('client_rejected_date') or 
                        sub.get('vendor_declined_date') or 
                        sub.get('vendor_withdrawn_date') or
                        status in ('rejected', 'offer declined', 'job closed')
                    )
                    
                    # Determine decline reason
                    decline_reason = None
                    if sub.get('client_rejected_date') or status == 'rejected':
                        decline_reason = sub.get('reject_reason_choice') or sub.get('reject_reason_text') or 'Client Rejected'
                    elif sub.get('vendor_declined_date') or status == 'offer declined':
                        decline_reason = 'Vendor Declined Offer'
                    elif sub.get('vendor_withdrawn_date'):
                        decline_reason = sub.get('withdrawal_reason_choice') or sub.get('withdrawal_reason_text') or 'Vendor Withdrawn'
                    elif status == 'job closed':
                        decline_reason = 'Job Closed'
                    
                    # Determine if GHR (GHR or Planet Healthcare, not The Planet Group)
                    agency = str(sub.get('agency') or '').lower()
                    is_ghr = 'ghr' in agency or 'planet healthcare' in agency
                    
                    # Build candidate object
                    candidate = {
                        'name': sub.get('candidate_name') or 'Unknown',
                        'agency': sub.get('agency') or 'Unknown',
                        'submitDate': sub.get('submission_date').isoformat() if sub.get('submission_date') and hasattr(sub.get('submission_date'), 'isoformat') else None,
                        'offerDate': sub.get('offer_date').isoformat() if sub.get('offer_date') and hasattr(sub.get('offer_date'), 'isoformat') else None,
                        'awardedDate': sub.get('offer_accepted_date').isoformat() if sub.get('offer_accepted_date') and hasattr(sub.get('offer_accepted_date'), 'isoformat') else None,
                        'rto': sub.get('rto_date').isoformat() if sub.get('rto_date') and hasattr(sub.get('rto_date'), 'isoformat') else None,
                        'isDeclined': is_declined,
                        'declineReason': decline_reason,
                        'hospDeclineDate': sub.get('client_rejected_date').isoformat() if sub.get('client_rejected_date') and hasattr(sub.get('client_rejected_date'), 'isoformat') else None,
                        'agencyDeclineDate': sub.get('vendor_declined_date').isoformat() if sub.get('vendor_declined_date') and hasattr(sub.get('vendor_declined_date'), 'isoformat') else None,
                        'agencyRetractedDate': sub.get('vendor_withdrawn_date').isoformat() if sub.get('vendor_withdrawn_date') and hasattr(sub.get('vendor_withdrawn_date'), 'isoformat') else None,
                        'interviewDate': sub.get('interview_date').isoformat() if sub.get('interview_date') and hasattr(sub.get('interview_date'), 'isoformat') else None,
                        'isGHR': is_ghr,
                        'isActive': sub.get('status') == 'Active' if sub.get('status') else False,
                        'status': sub.get('status')
                    }
                    
                    position['candidates'].append(candidate)
                    
                    # Update counts
                    if is_declined:
                        if is_ghr:
                            position['ghrDeclines'] += 1
                        else:
                            position['avDeclines'] += 1
                    else:
                        if is_ghr:
                            position['ghrSubs'] += 1
                        else:
                            position['avSubs'] += 1
        
        conn.close()
        
        b4_count = len([p for p in positions if p.get('source_system') == 'B4'])
        vndly_count = len([p for p in positions if p.get('source_system') == 'VNDLY'])
        print(f"Returning {len(positions)} positions (B4: {b4_count}, VNDLY: {vndly_count})")
        
        return func.HttpResponse(
            json.dumps(positions, default=str),
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