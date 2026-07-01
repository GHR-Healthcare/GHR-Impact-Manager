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


# Same status set GetTrendData uses — includes Completed/Termination so
# prior-year weeks reflect placements that actually ran back then.
BULLHORN_YOY_STATUSES = (
    'Approved', 'Pending Start', 'Cleared', 'Onboarding', 'Started',
    'Completed', 'Termination',
)


def _bullhorn_yoy_data():
    """Returns YoY weekly headcount rows from Bullhorn. Raises on error."""
    conn = get_bullhorn_conn()
    cursor = conn.cursor()
    system_case = build_system_case_expr('p.clientCorporationID')
    scope_filter = build_scope_filter('p.clientCorporationID')
    status_list = ', '.join("'" + s + "'" for s in BULLHORN_YOY_STATUSES)

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
                -- Division lives on the client (see GetTrendData note).
                ISNULL(cc.customTextBlock1, 'Unknown') AS division,
                CAST(NULL AS NVARCHAR(50)) AS region,
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
            p.system, p.category, p.facility, p.division, p.region,
            'GHR' AS vendor_type,
            COUNT(DISTINCT p.worker) AS headcount
        FROM Weeks w
        INNER JOIN Placements p
            ON p.sd <= DATEADD(DAY, 6, w.week_start)
            AND (p.ed IS NULL OR p.ed >= w.week_start)
        GROUP BY w.week_start, p.system, p.category, p.facility, p.division, p.region
        ORDER BY w.week_start, p.system
    ''')
    columns = [column[0] for column in cursor.description]
    rows = []
    for row in cursor.fetchall():
        row_dict = dict(zip(columns, row))
        row_dict['headcount'] = int(row_dict.get('headcount') or 0)
        rows.append(row_dict)
    conn.close()
    return rows


def _symplr_yoy_data():
    """Returns YoY weekly headcount rows from Symplr. Raises on error.

    Placements CTE unions two sources: lt_order multi-week placements and
    orderless filled orders (lt_orderid IN (0, NULL)) aggregated by
    worker+client.
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
                lt.lt_orderid,
                LOWER(LTRIM(RTRIM(ISNULL(pt.firstname,'') + ' ' + ISNULL(pt.lastname,'')))) AS worker,
                ({sys_case}) AS system,
                pc.clientname AS facility,
                ISNULL(lt.nursetype, 'Unknown') AS category,
                ISNULL(({division_case}), 'Unknown') AS division,
                pc.state AS region,
                CAST(lt.date_start AS DATE) AS sd,
                CAST(lt.date_end AS DATE) AS ed
            FROM dbo.lt_order lt
            LEFT JOIN dbo.profile_client pc ON lt.clientid = pc.recordid
            LEFT JOIN dbo.profile_temp pt ON lt.tempid = pt.recordid
            WHERE lt.status = 'filled'
                AND lt.date_start IS NOT NULL
                AND lt.date_start >= DATEADD(WEEK, -62, GETDATE())
                AND {scope}

            UNION ALL

            SELECT
                NULL AS lt_orderid,
                LOWER(LTRIM(RTRIM(ISNULL(MAX(pt.firstname),'') + ' ' + ISNULL(MAX(pt.lastname),'')))) AS worker,
                ({sys_case_orders}) AS system,
                MAX(pc.clientname) AS facility,
                ISNULL(MAX(o.nursetype), 'Unknown') AS category,
                ISNULL(({division_case_orders}), 'Unknown') AS division,
                MAX(pc.state) AS region,
                CAST(MIN(o.jobdatestart) AS DATE) AS sd,
                CAST(MAX(o.jobdateend)   AS DATE) AS ed
            FROM dbo.orders o
            LEFT JOIN dbo.profile_client pc ON o.customerid = pc.recordid
            LEFT JOIN dbo.profile_temp   pt ON o.filledby   = pt.recordid
            WHERE o.status = 'filled'
                AND (o.lt_orderid IS NULL OR o.lt_orderid = 0)
                AND o.filledby IS NOT NULL AND o.filledby > 0
                AND o.jobdatestart IS NOT NULL
                AND o.jobdatestart >= DATEADD(WEEK, -62, GETDATE())
                AND {scope_orders}
            GROUP BY o.customerid, o.filledby
        )
        SELECT
            CONVERT(VARCHAR(10), w.week_start, 23) AS week_start,
            p.system, p.category, p.facility, p.division, p.region,
            'GHR' AS vendor_type,
            COUNT(DISTINCT p.worker) AS headcount
        FROM Weeks w
        INNER JOIN Placements p
            ON p.sd <= DATEADD(DAY, 6, w.week_start)
            AND (p.ed IS NULL OR p.ed >= w.week_start)
        GROUP BY w.week_start, p.system, p.category, p.facility, p.division, p.region
        ORDER BY w.week_start, p.system
    ''')
    columns = [column[0] for column in cursor.description]
    rows = []
    for row in cursor.fetchall():
        row_dict = dict(zip(columns, row))
        row_dict['headcount'] = int(row_dict.get('headcount') or 0)
        rows.append(row_dict)
    conn.close()
    return rows


def _non_msp_yoy(req: func.HttpRequest) -> func.HttpResponse:
    rows = []
    errors = []
    try:
        rows.extend(_bullhorn_yoy_data())
    except Exception as e:
        print(f"Bullhorn YoY error: {e}")
        import traceback; traceback.print_exc()
        errors.append(f"bullhorn: {e}")
    try:
        rows.extend(_symplr_yoy_data())
    except Exception as e:
        print(f"Symplr YoY error: {e}")
        import traceback; traceback.print_exc()
        errors.append(f"symplr: {e}")
    print(f"non-MSP YoY: {len(rows)} aggregate rows (errors: {errors or 'none'})")
    return func.HttpResponse(
        json.dumps({'rows': rows}, default=str),
        mimetype="application/json",
        status_code=200,
        headers={"Access-Control-Allow-Origin": "*"}
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
        return _non_msp_yoy(req)

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
