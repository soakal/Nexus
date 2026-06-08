
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Non-secret config from .env
    hass_host: str = "http://localhost:8123"
    unifi_host: str = "https://192.168.1.1"
    unifi_username: str = ""
    unraid_host: str = "192.168.1.1"
    obsidian_host: str = "http://localhost:27123"
    channels_host: str = "http://localhost:8089"
    adguard_host: str = "http://localhost:3000"
    adguard_user: str = "admin"
    hermes_host: str = "http://localhost:9000"
    github_username: str = ""
    briefing_time: str = "07:00"
    briefing_timezone: str = "America/Detroit"
    memo_watch_folder: str = "./watched_memos"
    whisper_api: bool = False
    whisper_model: str = "base"
    pr_stale_hours: int = 48
    nexus_port: int = 3000
    backend_port: int = 8000
    weather_lat: float = 42.33
    weather_lon: float = -83.04
    debug: bool = False

    # Secret properties via vault (lazy)
    @property
    def anthropic_api_key(self) -> str:
        from backend.secrets.manager import get_secret
        return get_secret("ANTHROPIC_API_KEY")

    @property
    def hass_token(self) -> str:
        from backend.secrets.manager import get_secret
        return get_secret("HASS_TOKEN")

    @property
    def unifi_password(self) -> str:
        from backend.secrets.manager import get_secret
        return get_secret("UNIFI_PASSWORD")

    @property
    def unraid_api_key(self) -> str:
        from backend.secrets.manager import get_secret
        return get_secret("UNRAID_API_KEY")

    @property
    def obsidian_token(self) -> str:
        from backend.secrets.manager import get_secret
        return get_secret("OBSIDIAN_TOKEN")

    @property
    def github_token(self) -> str:
        from backend.secrets.manager import get_secret
        return get_secret("GITHUB_TOKEN")

    @property
    def openweather_api_key(self) -> str:
        from backend.secrets.manager import get_secret
        return get_secret("OPENWEATHER_API_KEY")

    @property
    def openrouter_api_key(self) -> str:
        from backend.secrets.manager import get_secret
        return get_secret("OPENROUTER_API_KEY")

    @property
    def hermes_webhook_secret(self) -> str:
        from backend.secrets.manager import get_secret
        return get_secret("HERMES_WEBHOOK_SECRET")

    @property
    def nexus_api_key(self) -> str:
        from backend.secrets.manager import get_secret
        return get_secret("NEXUS_API_KEY")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


_settings_instance: Settings | None = None

def get_settings() -> Settings:
    global _settings_instance
    if _settings_instance is None:
        _settings_instance = Settings()
    return _settings_instance
