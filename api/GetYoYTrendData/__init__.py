import azure.functions as func
import pyodbc
import os
import json


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
                    SELECT
                        'B4' AS src,
                        CONCAT(Last_Name, ', ', First_Name) AS worker,
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

                    UNION ALL

                    SELECT
                        'VNDLY' AS src,
                        CONCAT([Contractor First Name], ' ', [Contractor Last Name]) AS worker,
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
                        WHEN a.agency LIKE '%GHR%' OR a.agency LIKE '%Planet Healthcare%'
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
                        WHEN a.agency LIKE '%GHR%' OR a.agency LIKE '%Planet Healthcare%'
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
