
import logging

from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    # Non-secret config from .env
    hass_host: str = "http://localhost:8123"
    unifi_host: str = "https://192.168.1.1"
    unifi_username: str = ""
    unraid_host: str = "192.168.1.1"
    obsidian_vault_path: str = "C:\\Users\\Brian\\iCloudDrive\\iCloud~md~obsidian"
    brain_mcp_url: str = "http://localhost:8765"
    brain_mcp_token: str = ""  # set in .env if mcp_write_token is configured
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
    goal_debounce_seconds: int = 21600     # cooldown before same fingerprint re-proposed (6h)
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
    # Narrow auto-approve: auto-runs ONLY low-risk reversible autonomous goals;
    # everything else (medium/high risk, irreversible, human-proposed) still needs human approval.
    auto_approve_low_risk: bool = True

    # Phone notification settings (via Hermes->Telegram).
    phone_notifications_enabled: bool = True   # gate for all notify_phone calls
    autonomy_digest_enabled: bool = True        # send a daily autonomy summary
    autonomy_digest_time: str = "20:00"         # 24h HH:MM for the daily digest job

    # Orchestrator model tiers (per role) — .env-overridable so you can trade cost
    # vs quality without code changes. Defaults are the "balanced/cheaper" profile:
    # Sonnet plans + executes (good results, ~half Opus cost), Haiku verifies (a
    # criteria yes/no check it handles well at ~1/5 the Opus rate). To restore max
    # quality set the planner/verifier back to "claude-opus-4-8" in .env. Any valid
    # Anthropic model id works (billed to ANTHROPIC_API_KEY).
    orchestrator_planner_model: str = "claude-sonnet-4-6"
    orchestrator_executor_model: str = "claude-sonnet-4-6"
    orchestrator_verifier_model: str = "claude-haiku-4-5-20251001"

    # Deep-link base URL appended to every phone alert so Brian can tap straight
    # to the Safety page. Set to "" to disable. .env-overridable.
    # Uses the Tailscale MagicDNS name (not the LAN IP 192.168.1.119) so taps from
    # phone notifications work from anywhere on the tailnet, not just at home.
    app_base_url: str = "http://win11-vm-proxmox:3000"

    # Weekly spend reconciliation report (surfaced for manual comparison vs Anthropic billing).
    spend_report_enabled: bool = True
    spend_report_day: str = "mon"   # APScheduler day_of_week value for the weekly cron
    spend_report_time: str = "08:00"  # 24h HH:MM

    # Local backup settings — db + secrets copied to backups/<timestamp>/ daily.
    # backups/ is gitignored; secrets NEVER leave the local machine via this path.
    backup_enabled: bool = True
    backup_dir: str = "backups"
    backup_retention_days: int = 7
    backup_time: str = "03:30"  # 24h HH:MM for the daily backup job

    # Per-verb throttle + circuit breaker on broker writes (Tier 3 guardrails).
    # Applied ONLY to agent/autonomous ALLOWED dispatches; user actions are never throttled.
    verb_throttle_max: int = 5           # max dispatches per kind in the window
    verb_throttle_window_s: int = 300    # rolling window in seconds (5 min)
    breaker_failure_threshold: int = 3   # consecutive failures in window to trip the breaker
    breaker_cooldown_s: int = 900        # seconds a tripped kind stays forbidden (15 min)

    # Recurring-goal scheduler tick (Tier 3 council w33gixx93).
    # goal_recurrence_enabled=True: scheduler runs tick_recurring_goals every 30 min.
    # Disable in .env to turn off re-dispatch without touching the kill switch.
    goal_recurrence_enabled: bool = True

    # Success-criteria evaluation: when True and a goal has a success_criteria,
    # a Haiku check runs after a task succeeds to decide if the criterion was
    # actually met. False ignores criteria and marks the goal completed mechanically.
    success_criteria_eval_enabled: bool = True

    # /api/trigger HMAC signing (Tier 1.6 autonomy ingress hardening).
    # trigger_hmac_required=False: backward-compatible — Bearer-only callers still work.
    # trigger_hmac_required=True: every call must carry a valid X-Timestamp / X-Signature.
    # trigger_hmac_window_s: replay window in seconds (default 5 minutes).
    trigger_hmac_required: bool = False
    trigger_hmac_window_s: int = 300

    # CORS allowlist — localhost + RFC1918 private LAN + Tailscale (CGNAT 100.64.0.0/10
    # = 100.64-127.x.x, and *.ts.net MagicDNS) so remote access over Tailscale works;
    # public origins stay blocked. Any port. Override in .env to add a hostname.
    cors_allow_origin_regex: str = r"^https?://(localhost|127\.0\.0\.1|0\.0\.0\.0|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}|([\w-]+\.)+ts\.net)(:\d+)?$"

    # Live hung-step watchdog — reaps orphaned 'running' TaskSteps whose worker is
    # gone and whose heartbeat is stale, resetting them to 'pending' and re-enqueueing
    # the owning Task so work resumes without waiting for a reboot.
    step_watchdog_enabled: bool = True
    step_hung_timeout_s: int = 600  # seconds before a running step with no live worker is reaped

    # Scheduler stall watchdog + Hermes dead-letter alert (Tier 3 blind-spot removal).
    # watchdog_enabled: master gate for both checks (scheduler stall + dead-letter).
    # scheduler_stall_grace_s: a scheduler job overdue by more than this is flagged stalled.
    # dead_letter_attempts: PendingDelivery rows at/above this attempt count are dead-lettered.
    # watchdog_alert_cooldown_s: minimum seconds between repeat phone alerts for the same condition.
    watchdog_enabled: bool = True
    scheduler_stall_grace_s: int = 600
    dead_letter_attempts: int = 5
    watchdog_alert_cooldown_s: int = 3600

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

        # Non-fatal warning: notifications will 401 silently if the secret is absent.
        if self.phone_notifications_enabled:
            try:
                secret = self.hermes_webhook_secret
                if not secret:
                    raise ValueError("empty")
            except Exception:
                logger.error(
                    "phone_notifications_enabled=True but HERMES_WEBHOOK_SECRET is missing "
                    "from the vault. ALL phone notifications will 401 and silently queue. "
                    "Add it with: python tools/manage_vault.py set HERMES_WEBHOOK_SECRET"
                )

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
