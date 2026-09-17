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

    # M5: a single shared secret, not per-caller credentials — see ADR 0007
    # Decision 4 for what that tradeoff costs and why it's the right size for
    # this project. Checked with hmac.compare_digest, never `==` (timing).
    api_key: str = "dev-local-key"

    # M5: sliding-window rate limit, enforced in Redis — see ADR 0007
    # Decision 1. Applies per caller (the API key), to every request that
    # passes auth.
    rate_limit_window_ms: int = 60_000  # 1 minute
    rate_limit_max_requests: int = 60

    # M5: circuit breaker around the relay's Kafka producer — see ADR 0007
    # Decision 2. Guesses, not measurements (no production traffic to tune
    # against yet), which is exactly why they're config and not constants.
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_recovery_timeout_s: float = 30.0

    # M5: where each process's own Prometheus scrape endpoint listens. The
    # API exposes /metrics on its own FastAPI port; the relay and consumer
    # are standalone processes (not part of the FastAPI app) and need a
    # dedicated port each — see ADR 0007 Decision 3's consequences.
    relay_metrics_port: int = 9101
    consumer_metrics_port: int = 9102


settings = Settings()
