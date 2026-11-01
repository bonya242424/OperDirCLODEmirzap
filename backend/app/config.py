from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # dev | prod. В prod система не стартует с ненадёжным SECRET_KEY.
    environment: str = "dev"
    database_url: str = "sqlite:///./dev.db"
    secret_key: str = "change-me"
    access_token_minutes: int = 60 * 12

    # Первый владелец создаётся при старте, если в базе ещё нет пользователей.
    admin_email: str = ""
    admin_password: str = ""

    # Модель ИИ. Слой заменяемый: см. app/llm.
    anthropic_api_key: str = ""
    llm_model: str = "claude-sonnet-5-5"
    llm_base_url: str = ""

    # Интеграции
    bitrix24_webhook_url: str = ""
    google_service_account_file: str = ""
    rnp_spreadsheet_ids: str = ""  # id таблиц через запятую
    yandex_oauth_token: str = ""
    yandex_metrika_counter_id: str = ""

    # Проверки интеграций
    scheduler_enabled: bool = True
    integration_check_minutes: int = 60
    onec_expected_interval_hours: int = 24


settings = Settings()
