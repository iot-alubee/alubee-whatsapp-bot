from google.cloud import bigquery

from app.config import Settings


def normalize_mobile_digits(raw_from: str) -> str:
    """Twilio From is like 'whatsapp:+9198xxxx'. Keep digits only."""
    digits = "".join(c for c in raw_from if c.isdigit())
    return digits


def fetch_employee(settings: Settings, normalized_mobile: str) -> dict | None:
    """
    Expect a BigQuery employee_master table with columns:
      id (STRING),
      employee (STRING display name),
      department (STRING),
      mobile (STRING — matched after stripping non-digits)

    Override query shape by changing this function / table name via env.
    """
    if not settings.gcp_project_id or not settings.bq_employees_table:
        return None

    client = bigquery.Client(project=settings.gcp_project_id)

    sql = f"""
    SELECT
      id,
      employee,
      department,
      REGEXP_REPLACE(CAST(mobile AS STRING), r'[^0-9]', '') AS mobile_digits
    FROM `{settings.bq_employees_table}`
    WHERE REGEXP_REPLACE(CAST(mobile AS STRING), r'[^0-9]', '') = @mobile
    LIMIT 1
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("mobile", "STRING", normalized_mobile),
        ]
    )

    rows = list(client.query(sql, job_config=job_config).result())
    if not rows:
        # Fallback: match last 10 digits (common for stored local numbers)
        if len(normalized_mobile) > 10:
            tail = normalized_mobile[-10:]
            sql_tail = f"""
            SELECT
              id,
              employee,
              department,
              REGEXP_REPLACE(CAST(mobile AS STRING), r'[^0-9]', '') AS mobile_digits
            FROM `{settings.bq_employees_table}`
            WHERE ENDS_WITH(REGEXP_REPLACE(CAST(mobile AS STRING), r'[^0-9]', ''), @tail)
            LIMIT 1
            """
            job_config_tail = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("tail", "STRING", tail),
                ]
            )
            rows = list(client.query(sql_tail, job_config=job_config_tail).result())

    if not rows:
        return None

    row = rows[0]
    return {
        "id": row["id"],
        "employee_name": row["employee"],
        "department": row["department"],
        "mobile_digits": row["mobile_digits"],
    }
