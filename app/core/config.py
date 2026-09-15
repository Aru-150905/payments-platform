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

    # Its own group, distinct from any other consumer this system ever adds.
    # Consumer-group offsets are tracked per (group, topic, partition), so a
    # rebuild that resets THIS group's position can never disturb another
    # group's — but only if no other consumer ever shares this group id.
    read_model_consumer_group: str = "payments-read-model-v1"

    # The topic's own retention, made explicit (see `make topics`) rather
    # than left at the broker's 168h default. processed_events retention
    # must be >= this, with margin — see app/services/retention.py for why.
    kafka_topic_retention_hours: int = 168  # 7 days
    processed_events_retention_hours: int = 192  # 8 days: retention + 24h margin


settings = Settings()
