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
    redis_url: str = "redis://localhost:6379/0"

    # mock SAHYOG
    sahyog_mock_url: str = "http://localhost:8091"
    engine_webhook_secret: str = "dev-webhook-secret-change-me"

    # M7 persistence + intel
    store_backend: str = "auto"  # auto | postgres | memory
    queue_backend: str = "auto"  # auto | redis | memory
    sanctions_table_path: str = ""  # full OFAC XML path; "" = fixture sample


settings = Settings()
