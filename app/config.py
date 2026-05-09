from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    twilio_account_sid: str = "AC6cd9ff155c1a096a29bd744216bd0710"
    twilio_auth_token: str = "84281657938650887a2f521636492204"
    twilio_whatsapp_from: str = "+14155238886"  # whatsapp:+14155238886

    # MD WhatsApp number (E.164 inside whatsapp: prefix for replies)
    md_whatsapp_to: str = "+917538866308"

    # GCP project that owns the BigQuery dataset (same as in your service account JSON)
    gcp_project_id: str = "alubee-prod"

    # Dataset.table under gcp_project_id (project prefix optional)
    bq_employees_table: str = "alubee_production_marts.employee_master"

    # Path to service account JSON; repo root if relative. Skipped if
    # GOOGLE_APPLICATION_CREDENTIALS is already set in the environment.
    google_application_credentials: str = "bq_service_acc.json"

    twilio_validate_webhook: bool = True

    public_base_url: str = ""
