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
    openai_api_key: str = ""
    openai_model: str = "gpt-5"
    openai_base_url: str = "https://api.openai.com/v1"
    xai_api_key: str = ""
    xai_model: str = "grok-4"
    xai_base_url: str = "https://api.x.ai/v1"

    # Интеграции
    bitrix24_webhook_url: str = ""
    google_service_account_file: str = ""
    rnp_spreadsheet_ids: str = ""  # id таблиц через запятую
    yandex_oauth_token: str = ""
    yandex_metrika_counter_id: str = ""
    # Один OAuth-токен Яндекса на Метрику, Вебмастер, Диск (права выдаются при получении токена).
    telegram_bot_token: str = ""
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_ssl: bool = True
    ftp_host: str = ""
    ftp_port: int = 21
    ftp_user: str = ""
    ftp_password: str = ""
    ftp_tls: bool = False  # FTPS (явный TLS)
    ftp_dir: str = "/"
    diadoc_client_id: str = ""
    diadoc_login: str = ""
    diadoc_password: str = ""
    mirzap_mcp_url: str = ""
    mirzap_mcp_token: str = ""
    google_calendar_ids: str = ""  # id календарей через запятую
    yandex_calendar_login: str = ""
    yandex_calendar_password: str = ""  # пароль приложения CalDAV

    # Хранилище файлов (в Docker это отдельный том)
    files_dir: str = "./data/files"
    seed_project_context: bool = True  # класть в «Файлы» документы о системе (папка «Контекст проекта»)
    max_upload_mb: int = 100

    bitrix_sync_minutes: int = 15
    bitrix_watch_minutes: int = 30

    # Проверки интеграций
    scheduler_enabled: bool = True
    integration_check_minutes: int = 60
    onec_expected_interval_hours: int = 24


settings = Settings()
