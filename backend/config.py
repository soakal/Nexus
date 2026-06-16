
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
    chat_history_limit: int = 20
    agent_write_enabled: bool = True  # Tier 2.4 hard master switch: executor write tools on/off
    whisper_api: bool = False
    whisper_model: str = "base"
    pr_stale_hours: int = 48
    action_confirm_ttl_seconds: int = 3600
    goal_ttl_seconds: int = 86400          # pending-proposal TTL (24 h)
    goal_debounce_seconds: int = 3600      # cooldown before same fingerprint re-proposed
    goal_backoff_base_seconds: int = 300   # failure backoff base (seconds)
    goal_max_attempts: int = 5             # max retries before goal stays failed
    nexus_port: int = 3000
    backend_port: int = 8000
    weather_lat: float = 42.33
    weather_lon: float = -83.04
    debug: bool = False

    # Cost governor / kill switch (Tier 1.5) — seed defaults for the SystemState
    # row; .env-overridable. The live values are read from SystemState at runtime.
    daily_budget_usd: float = 25.0
    per_task_budget_usd: float = 5.0
    autonomy_enabled_default: bool = True

    # Spend-metering price verification flag (Tier 3 observability).
    # Set to True in .env after confirming _PRICE_PER_MTOK in router.py against
    # live Anthropic billing. Until True, a startup WARNING fires each boot.
    # Set True 2026-06-16 after verifying _PRICE_PER_MTOK against Anthropic's
    # official pricing page (Opus 4.8 $5/$25, Sonnet 4.6 $3/$15, Haiku 4.5 $1/$5).
    prices_verified: bool = True

    # Tier 3 — suggest-only autonomous goal proposer.
    proposer_enabled: bool = True
    proposer_interval_hours: int = 6
    proposer_max_per_tick: int = 3

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

    def validate(self) -> None:
        """Fail fast on misconfiguration at startup, before the scheduler/agents run.

        Raises ValueError for a malformed briefing_time/briefing_timezone and
        RuntimeError listing every missing required secret (all collected, not just
        the first). Only the two secrets that core function depends on are required;
        every integration already degrades gracefully when its own secret is absent.
        """
        # briefing_time — must satisfy scheduler.py's `hour, minute = briefing_time.split(":")`.
        parts = self.briefing_time.split(":")
        if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
            raise ValueError(
                f"Invalid briefing_time {self.briefing_time!r}; expected HH:MM 24h"
            )
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(
                f"Invalid briefing_time {self.briefing_time!r}; hour must be 0-23, minute 0-59"
            )

        # briefing_timezone — CronTrigger(timezone=...) would otherwise fail at job time.
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(self.briefing_timezone)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(
                f"Invalid briefing_timezone {self.briefing_timezone!r}: {e}"
            )

        # Required secrets — ANTHROPIC_API_KEY (every agent call bills it) and
        # NEXUS_API_KEY (auth for all /api/*). Others are optional/feature-degraded.
        required = {
            "ANTHROPIC_API_KEY": "anthropic_api_key",
            "NEXUS_API_KEY": "nexus_api_key",
        }
        missing: list[str] = []
        for name, prop in required.items():
            try:
                value = getattr(self, prop)
            except Exception:
                value = None
            if not value:
                missing.append(name)
        if missing:
            raise RuntimeError(f"Missing required secrets: {', '.join(missing)}")

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
