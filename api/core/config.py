from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # blockchain indexer keys
    etherscan_api_key: str = ""
    trongrid_api_key: str = ""
    solscan_api_key: str = ""
    covalent_api_key: str = ""

    # infra
    postgres_dsn: str = "postgresql+asyncpg://vasp:vasp@localhost:5432/vasp"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "vasp-dev-password"
    neo4j_database: str = ""  # empty = server default; AuraDB needs "neo4j"
    redis_url: str = "redis://localhost:6379/0"

    # mock SAHYOG
    sahyog_mock_url: str = "http://localhost:8091"
    engine_webhook_secret: str = "dev-webhook-secret-change-me"

    # M7 persistence + intel
    store_backend: str = "auto"  # auto | postgres | memory
    queue_backend: str = "auto"  # auto | redis | memory
    sanctions_table_path: str = ""  # full OFAC XML path; "" = fixture sample

    # M10 watchlist: poll cadence in minutes (arq cron runs at these
    # minute marks each hour: 15 -> :00, :15, :30, :45)
    watch_poll_minutes: int = 15

    # M25 indexer cache
    indexer_cache_backend: str = "auto"  # auto | redis | memory | none
    indexer_cache_ttl: int = 3600
    indexer_cache_max_entries: int = 10000
    indexer_cache_prefix: str = "vasp:idx:v1"

    # M31 rate limiter: shared per-chain throttle, calls/second.
    # Conservative default keeps a margin under Etherscan's 3/sec cap.
    indexer_rate_limit_per_sec: float = 2.5

    # M12 RBAC: when true, X-API-Key is required on every protected
    # route. Default false keeps local/dev and existing tests working;
    # every action is still audit-logged under the system identity.
    auth_enforced: bool = False


settings = Settings()
