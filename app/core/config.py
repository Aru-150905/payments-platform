from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Every knob the app needs, read from environment variables.

    Config lives here and nowhere else so that the same image can run on your
    laptop and on AWS with only env vars changing. This is the single most
    boring rule in system design and the one people break first.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "payments-platform"
    env: str = "local"

    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/payments"
    redis_url: str = "redis://localhost:6379/0"

    kafka_bootstrap_servers: str = "localhost:29092"
    kafka_client_id: str = "payments-api"
    kafka_consumer_group: str = "payments-workers"


settings = Settings()
