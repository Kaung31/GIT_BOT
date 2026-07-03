from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    slack_bot_token: str
    slack_app_token: str
    slack_signing_secret: str = ""

    llm_model: str = "ollama/qwen3:14b"
    embed_model: str = "ollama/nomic-embed-text"
    embed_dim: int = 768
    ollama_api_base: str = "http://localhost:11434"

    database_url: str = "postgresql+asyncpg://slackagent:slackagent@localhost:5432/slackagent"
    redis_url: str = "redis://localhost:6379/0"

    jira_base_url: str = ""       # https://yoursite.atlassian.net
    jira_email: str = ""
    jira_api_token: str = ""
    jira_project_key: str = ""

    notion_api_token: str = ""
    notion_api_version: str = "2025-09-03"
    notion_data_source_id: str = ""
    notion_title_property: str = "Name"

    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = ""

    graph_recursion_limit: int = 12
    token_bucket_per_user: int = 50_000
    token_bucket_per_channel: int = 200_000

    standup_channel: str = ""
    standup_cron: str = "0 9 * * mon-fri"
    standup_collect_minutes: int = 60


settings = Settings()
