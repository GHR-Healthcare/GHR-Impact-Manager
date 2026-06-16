import azure.functions as func
import pyodbc
import os
import json
from shared_code.auth import require_allowed_domain
from shared_code.data_source import is_non_msp
from shared_code.bullhorn_systems import (
    build_system_case_expr,
    build_scope_filter,
)


# Same status set GetTrendData uses — includes Completed/Termination so
# prior-year weeks reflect placements that actually ran back then.
BULLHORN_YOY_STATUSES = (
    'Approved', 'Pending Start', 'Cleared', 'Onboarding', 'Started',
    'Completed', 'Termination',
)


def _bullhorn_yoy(req: func.HttpRequest) -> func.HttpResponse:
    """
    Pre-aggregated weekly headcount for the non-MSP (Bullhorn) instance,
    going back 60 weeks. Used to overlay prior-year lines on the trend chart.

    Output shape matches MSP — array of { week_start, system, category,
    facility, vendor_type, headcount }. vendor_type is always 'GHR' for
    non-MSP since there's no affiliate split.
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
        status_list = ', '.join("'" + s + "'" for s in BULLHORN_YOY_STATUSES)

        rows = []
        try:
            cursor.execute(f'''
                ;WITH Weeks AS (
                    SELECT TOP 60
                        DATEADD(WEEK, 1 - ROW_NUMBER() OVER (ORDER BY (SELECT NULL)),
                                DATEADD(DAY, 1 - DATEPART(WEEKDAY, CAST(GETDATE() AS DATE)),
                                        CAST(GETDATE() AS DATE))
                        ) AS week_start
                    FROM sys.all_objects
                ),
                Placements AS (
                    SELECT
                        p.placementID,
                        LOWER(LTRIM(RTRIM(ISNULL(c.firstName,'') + ' ' + ISNULL(c.lastName,'')))) AS worker,
                        ({system_case}) AS system,
                        cc.name AS facility,
                        ISNULL(p.employmentType, 'Unknown') AS category,
                        CAST(p.dateBegin AS DATE) AS sd,
                        CAST(p.dateEnd AS DATE) AS ed
                    FROM dbo.View_Placement p
                    LEFT JOIN dbo.View_Candidate c ON p.candidateID = c.candidateID
                    LEFT JOIN dbo.View_ClientCorporation cc ON p.clientCorporationID = cc.clientCorporationID
                    WHERE p.isDeleted = 0
                        AND p.status IN ({status_list})
                        AND p.dateBegin IS NOT NULL
                        AND p.dateBegin >= DATEADD(WEEK, -62, GETDATE())
                        AND {scope_filter}
                )
                SELECT
                    CONVERT(VARCHAR(10), w.week_start, 23) AS week_start,
                    p.system,
                    p.category,
                    p.facility,
                    'GHR' AS vendor_type,
                    COUNT(DISTINCT p.worker) AS headcount
                FROM Weeks w
                INNER JOIN Placements p
                    ON p.sd <= DATEADD(DAY, 6, w.week_start)
                    AND (p.ed IS NULL OR p.ed >= w.week_start)
                GROUP BY w.week_start, p.system, p.category, p.facility
                ORDER BY w.week_start, p.system
            ''')
            columns = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                row_dict['headcount'] = int(row_dict.get('headcount') or 0)
                rows.append(row_dict)
        except Exception as e:
            print(f"Error loading Bullhorn YoY trend data: {e}")
            import traceback; traceback.print_exc()

        conn.close()
        print(f"Returning {len(rows)} Bullhorn YoY trend aggregate rows")

        return func.HttpResponse(
            json.dumps({'rows': rows}, default=str),
            mimetype="application/json",
            status_code=200,
            headers={"Access-Control-Allow-Origin": "*"}
        )
    except Exception as e:
        print(f"Bullhorn YoY error: {e}")
        import traceback; traceback.print_exc()
        return func.HttpResponse(
            json.dumps({'error': str(e), 'rows': []}),
            mimetype="application/json",
            status_code=500,
        )


def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    Returns pre-aggregated weekly headcount data going back 60 weeks,
    used to overlay prior-year trend lines on the trend chart.

    Output: array of { week_start, system, category, facility, vendor_type, source, headcount }
    where vendor_type is 'GHR' or 'Affiliate'.

    Transitioned systems (RUMC, Redeemer, Cooper) are a special case — B4 data
    is used for the historical period, VNDLY data is used for the current period.
    Since workers are counted DISTINCT across sources per week, any overlap during
    transition is deduped naturally.
    """
    auth_error = require_allowed_domain(req)
    if auth_error:
        return auth_error

    if is_non_msp():
        return _bullhorn_yoy(req)

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
        rows = []

        try:
            cursor.execute('''
                ;WITH Weeks AS (
                    SELECT TOP 60
                        DATEADD(WEEK, 1 - ROW_NUMBER() OVER (ORDER BY (SELECT NULL)),
                                DATEADD(DAY, 1 - DATEPART(WEEKDAY, CAST(GETDATE() AS DATE)),
                                        CAST(GETDATE() AS DATE))
                        ) AS week_start
                    FROM sys.all_objects
                ),
                Assignments AS (
                    -- Worker name is normalized to lower-case "first last" so
                    -- the same person counts once across B4 and VNDLY for
                    -- transitioned systems. B4 stores "Last, First" — we
                    -- already build it with CONCAT here, so we just produce
                    -- "first last" directly from the source columns.
                    SELECT
                        'B4' AS src,
                        LOWER(LTRIM(RTRIM(CONCAT(First_Name, ' ', Last_Name)))) AS worker,
                        Health_System AS system,
                        Facility AS facility,
                        Program AS category,
                        Agency AS agency,
                        Start_Date AS sd,
                        End_Date AS ed
                    FROM dhc.B4HealthOrder
                    WHERE Contract_Status = 'Closed And Awarded'
                        AND Start_Date IS NOT NULL
                        AND Start_Date >= DATEADD(WEEK, -62, GETDATE())
                        AND Health_System <> 'Sunrise Senior Living Management (California)'

                    UNION ALL

                    SELECT
                        'VNDLY' AS src,
                        LOWER(LTRIM(RTRIM(CONCAT([Contractor First Name], ' ', [Contractor Last Name])))) AS worker,
                        [Health System] AS system,
                        [Default Work Site Name] AS facility,
                        [Labor Type] AS category,
                        [Vendor Name] AS agency,
                        [Start Date] AS sd,
                        [End Date] AS ed
                    FROM dbo.STAGING_VNDLY_WORKORDERS
                    WHERE [Current Status] = 'Active'
                        AND [Start Date] IS NOT NULL
                        AND [Start Date] >= DATEADD(WEEK, -62, GETDATE())
                )
                SELECT
                    CONVERT(VARCHAR(10), w.week_start, 23) AS week_start,
                    a.system,
                    a.category,
                    a.facility,
                    CASE
                        WHEN (a.src = 'B4'    AND a.agency LIKE 'GHR%')
                          OR (a.src = 'VNDLY' AND a.agency LIKE '%GHR%')
                          OR a.agency LIKE '%Planet Healthcare%'
                        THEN 'GHR' ELSE 'Affiliate'
                    END AS vendor_type,
                    COUNT(DISTINCT a.worker) AS headcount
                FROM Weeks w
                INNER JOIN Assignments a
                    ON a.sd <= DATEADD(DAY, 6, w.week_start)
                    AND (a.ed IS NULL OR a.ed >= w.week_start)
                GROUP BY
                    w.week_start,
                    a.system,
                    a.category,
                    a.facility,
                    CASE
                        WHEN (a.src = 'B4'    AND a.agency LIKE 'GHR%')
                          OR (a.src = 'VNDLY' AND a.agency LIKE '%GHR%')
                          OR a.agency LIKE '%Planet Healthcare%'
                        THEN 'GHR' ELSE 'Affiliate'
                    END
                ORDER BY w.week_start, a.system
            ''')

            columns = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                row_dict['headcount'] = int(row_dict.get('headcount') or 0)
                rows.append(row_dict)
        except Exception as e:
            print(f"Error loading YoY trend data: {e}")
            import traceback
            traceback.print_exc()

        conn.close()
        print(f"Returning {len(rows)} YoY trend aggregate rows")

        return func.HttpResponse(
            json.dumps({'rows': rows}, default=str),
            mimetype="application/json",
            status_code=200,
            headers={"Access-Control-Allow-Origin": "*"}
        )

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return func.HttpResponse(
            json.dumps({'error': str(e), 'rows': []}),
            mimetype="application/json",
            status_code=500
        )
