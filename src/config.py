from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    github_webhook_secret: str = "changeme"
    github_token: str
    target_repos: str = ""             # comma-separated owner/name
    trigger_label: str = "swarm-fix"

    telegram_bot_token: str
    telegram_chat_id: str

    proposer_model: str = "ollama/qwen2.5-coder:14b"
    breaker_model: str = "ollama/qwen3:14b"
    arbitrator_model: str = "ollama/qwen3:14b"
    embed_model: str = "ollama/nomic-embed-text"
    embed_dim: int = 768
    ollama_api_base: str = "http://localhost:11434"

    database_url: str = "postgresql+asyncpg://swarm:swarm@localhost:5432/swarm"
    redis_url: str = "redis://localhost:6379/0"
    repos_dir: str = "./repos"

    graph_recursion_limit: int = 40  # hub topology: ~11 supersteps/run + ~7 per revise round
    max_revision_rounds: int = 2
    token_bucket_per_run: int = 250_000
    daily_spend_cap_usd: float = 1.50

    sandbox_image: str = "swarm-sandbox"
    sandbox_timeout_s: int = 180
    sandbox_memory_mb: int = 1024

    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = ""

    @property
    def repos(self) -> list[str]:
        return [r.strip() for r in self.target_repos.split(",") if r.strip()]


settings = Settings()
