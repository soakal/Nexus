
import logging

from pydantic import field_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    # Non-secret config from .env
    hass_host: str = "http://localhost:8123"
    unifi_host: str = "https://192.168.1.1"
    unifi_username: str = ""
    unraid_host: str = "192.168.1.1"
    # Native SSH path for docker prune (Phase 7d) -- deliberately separate from
    # unraid_host, which feeds the GraphQL API URL, not SSH. Empty default means
    # _ssh_prune_sync() raises "UNRAID_SSH_HOST not configured" before any network
    # attempt -- this is shipped-but-not-yet-live until the SSH credential is
    # installed on Unraid (a separate, human-gated step; see CLAUDE.md Phase 7d).
    unraid_ssh_host: str = ""
    unraid_ssh_user: str = "root"
    unraid_ssh_port: int = 22
    unraid_ssh_prune_timeout_s: int = 60
    # Native SSH path for restarting the LXC's NEXUS remotely via Telegram
    # (2026-08-15, `/restart lxc`) -- same forced-command pattern as the
    # Unraid docker-prune path above. Deliberately SSH, not an authenticated
    # HTTP call to the LXC's own :8000 -- the restart channel must not
    # depend on the process being restarted; sshd is independent of NEXUS.
    # Empty default means _ssh_restart_sync() raises before any network
    # attempt -- shipped-but-not-yet-live until the SSH credential is
    # installed on the LXC (a separate, human-gated step; see CLAUDE.md).
    lxc_ssh_host: str = ""
    lxc_ssh_user: str = "root"
    lxc_ssh_port: int = 22
    lxc_ssh_restart_timeout_s: int = 120
    proxmox_host: str = "https://192.168.1.60:8006"
    obsidian_vault_path: str = "C:\\Users\\Brian\\iCloudDrive\\iCloud~md~obsidian"
    brain_mcp_url: str = "http://localhost:8765"

    # Infisical secret store (see backend/secrets/manager.py). Empty defaults —
    # "auto" mode (default secrets_backend) falls back to the legacy Fernet
    # vault until all four of these are set in the gitignored .env.
    infisical_url: str = ""
    infisical_client_id: str = ""
    infisical_client_secret: str = ""
    infisical_project_id: str = ""
    infisical_env: str = "prod"
    infisical_cache_ttl_s: int = 300
    secrets_backend: str = "auto"  # "auto" | "infisical" | "vault"
    mail_autodraft_enabled: bool = True
    mail_autodraft_interval_minutes: int = 30
    mail_autotrash_enabled: bool = True
    channels_host: str = "http://localhost:8089"
    adguard_host: str = "http://localhost:3000"
    adguard_user: str = "admin"
    github_username: str = ""
    briefing_time: str = "07:00"
    briefing_timezone: str = "America/Detroit"
    memo_watch_folder: str = "./watched_memos"
    chat_history_limit: int = 20
    agent_write_enabled: bool = True  # Tier 2.4 hard master switch: executor write tools on/off
    whisper_api: bool = False
    whisper_model: str = "base"
    pr_stale_hours: int = 48
    action_confirm_ttl_seconds: int = 86400
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

    # Gates ONLY the daily morning_briefing scheduler-job registration -- no
    # other briefing read/write path. Default True; set False on whichever
    # instance is NOT the current owner (mirrors linux-lxc's identical field,
    # added there first -- Track B cutover, 2026-08-15, see CLAUDE.md).
    morning_briefing_enabled: bool = True

    # Phone notification settings (via Telegram).
    phone_notifications_enabled: bool = True   # gate for all notify_phone calls
    autonomy_digest_enabled: bool = True        # send a daily autonomy summary
    autonomy_digest_time: str = "20:00"         # 24h HH:MM for the daily digest job
    # Kinds to silence — informational ones already covered by the daily digest.
    # Override in .env as comma-separated: PHONE_SUPPRESSED_KINDS=auto_approved,throttled
    phone_suppressed_kinds: set[str] = {"auto_approved", "throttled", "scheduler_stall"}

    @field_validator("phone_suppressed_kinds", mode="before")
    @classmethod
    def _parse_suppressed_kinds(cls, v):
        if isinstance(v, str):
            return {k.strip() for k in v.split(",") if k.strip()}
        return v

    # Action-judge gate (Tier 3 second-opinion check before dispatch).
    # action_judge_mode: "off" (bypass entirely), "shadow" (log verdict, always
    # allow), "enforce" (log verdict, can block). action_judge_exempt_kinds are
    # never sent to the judge (fast-path allow). Override in .env as
    # comma-separated: ACTION_JUDGE_EXEMPT_KINDS=send_notification,other_kind
    action_judge_exempt_kinds: set[str] = {"send_notification"}
    action_judge_model: str = "claude-haiku-4-5-20251001"
    action_judge_timeout_s: int = 20
    action_judge_mode: str = "shadow"

    @field_validator("action_judge_exempt_kinds", mode="before")
    @classmethod
    def _parse_judge_exempt_kinds(cls, v):
        if isinstance(v, str):
            return {k.strip() for k in v.split(",") if k.strip()}
        return v

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
    app_base_url: str = "http://win11-vm-proxmox.tailfa52c.ts.net:3000"

    # Weekly spend reconciliation report (surfaced for manual comparison vs Anthropic billing).
    spend_report_enabled: bool = True
    spend_report_day: str = "mon"   # APScheduler day_of_week value for the weekly cron
    spend_report_time: str = "08:00"  # 24h HH:MM

    # Weekly Facts -> Brain digest (backend/agents/facts_digest.py). Runs 30 min
    # before brain_organizer's 02:00 nightly fold so the same night's digest
    # note is picked up into Brain/wiki/.
    # ENABLED 2026-07-29: the pre-fix duplicate-subject noise ("Charlie"/
    # "Charlee", multiple spellings of "Unraid"/"UniFi"/"Calendar event"/
    # "On-call schedule") was cleaned up via backend/agents/facts_cleanup.py
    # (see tests/test_facts_cleanup.py) -- 12 subject renames + 9 predicate-
    # level merges applied against the live nexus.db (backed up first to
    # nexus.db.bak-2026-07-29), 125 -> 116 active facts, 33 distinct active
    # subjects (was 40), verified idempotent (a second run is a no-op) and
    # non-destructive (total row count unchanged -- SUPERSEDE, never DELETE).
    # The still-live extraction-prompt fix (facts.py's canonical-key fallback
    # in _db_upsert_fact) keeps new inserts from re-accumulating this same
    # noise going forward.
    facts_digest_enabled: bool = True
    facts_digest_day: str = "sun"      # APScheduler day_of_week value
    facts_digest_time: str = "01:30"   # 24h HH:MM

    # Gates ONLY the nightly 02:00 brain_organizer scheduler-job registration
    # in setup_scheduler() -- NOT main.py's :8765 Brain MCP server spawn and
    # NOT POST /api/brain-organizer/run (both share the same on-disk venv but
    # have their own independent existence checks). Default True so a fresh
    # checkout / the LXC instance registers the job normally. Set to False on
    # the Windows instance's own .env as of 2026-08-14: the LXC now owns
    # nightly Brain digestion (both instances had a working venv and would
    # otherwise both run the job against their own Syncthing-synced vault
    # copy every night -- a duplicate-digestion race, not data loss, but one
    # that produces divergent wiki content needing manual reconciliation).
    # Named narrowly (not a bare brain_organizer_enabled) on purpose -- a
    # broader name would invite gating the MCP spawn with it too, which was
    # caught and reverted before deploying (see CLAUDE.md's dated entry).
    brain_organizer_nightly_enabled: bool = True

    # Gates ONLY the weekly Sunday 02:30 wiki_fragmentation_report scheduler
    # job registration -- not wiki_ingest.py's module import (still needed by
    # this function) and not anything else. Same reasoning and same night as
    # brain_organizer_nightly_enabled above: pairs with it as one 02:00->02:30
    # pipeline, one owner. Default True; set False on the Windows instance's
    # own .env as of 2026-08-15 since the LXC owns this too.
    wiki_fragmentation_report_enabled: bool = True

    # Local backup settings — db + secrets copied to backups/<timestamp>/ daily.
    # backups/ is gitignored; secrets NEVER leave the local machine via this path.
    backup_enabled: bool = True
    backup_dir: str = "backups"
    backup_retention_days: int = 7
    backup_time: str = "03:30"  # 24h HH:MM for the daily backup job

    # Retention for high-frequency sample tables. uptimesample is written every
    # 2m and its window must stay above the widest UI query or a glance would
    # silently see a shorter history than expected. trendsnapshot is no longer
    # written at all (Trends feature removed 2026-07-07) -- its retention job
    # just drains the table to empty over this window, then no-ops forever.
    # Set to 0 to disable pruning for that table.
    uptime_retention_days: int = 35
    trend_retention_days: int = 100

    # When True, a completed goal's outcome also runs Haiku fact-extraction
    # (source='task'). Default ON (2026-07-07): best-effort, never blocks
    # completion (test_goal_outcomes.py::test_distill_never_blocks_completion),
    # and turns completed-goal outcomes into searchable facts feeding the
    # proposer's fact-triggered goals. Set False to disable.
    goal_outcome_distill_llm: bool = True

    # Extra HTTP uptime targets — non-integration services watched by the
    # 2-min uptime job. Comma- or newline-separated "name|url|expect_status"
    # entries (expect_status optional, default 200), e.g.
    # UPTIME_HTTP_TARGETS="glp|http://192.168.1.50:8765|200,openwebui|http://192.168.1.56:3000"
    uptime_http_targets: str = ""

    # Unraid vault backup — encrypted vault + key copied to a UNC share daily and
    # on every secret save. Leave blank to disable.
    unraid_backup_path: str = r"\\192.168.1.50\Computer Backup\Nexus_backup"
    # Default OFF: the default SMB destination isn't reliably Windows-ACL-hardenable
    # from this host, and nexus.vault ciphertext alone is still a useful backup
    # without shipping the decryption key alongside it. Opt back in only if the
    # backup destination is independently secured (restricted share ACL, etc).
    unraid_backup_include_key: bool = False   # back up .vault.key alongside nexus.vault
    # SMB credentials for the backup share — stored in vault as UNRAID_BACKUP_USER / UNRAID_BACKUP_PASSWORD
    # (vault-backed @property methods below; leave vault keys absent if guest/pre-mapped)

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

    # CORS allowlist — localhost + RFC1918 private LAN + Tailscale (CGNAT 100.64.0.0/10
    # = 100.64-127.x.x, and *.ts.net MagicDNS) so remote access over Tailscale works;
    # public origins stay blocked. Any port. Override in .env to add a hostname.
    cors_allow_origin_regex: str = r"^https?://(localhost|127\.0\.0\.1|0\.0\.0\.0|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}|([\w-]+\.)+ts\.net)(:\d+)?$"

    # Live hung-step watchdog — reaps orphaned 'running' TaskSteps whose worker is
    # gone and whose heartbeat is stale, resetting them to 'pending' and re-enqueueing
    # the owning Task so work resumes without waiting for a reboot.
    step_watchdog_enabled: bool = True
    step_hung_timeout_s: int = 600  # seconds before a running step with no live worker is reaped

    # Scheduler stall watchdog + dead-letter alert (Tier 3 blind-spot removal).
    # watchdog_enabled: master gate for both checks (scheduler stall + dead-letter).
    # scheduler_stall_grace_s: a scheduler job overdue by more than this is flagged stalled.
    # dead_letter_attempts: PendingDelivery rows at/above this attempt count are dead-lettered.
    # watchdog_alert_cooldown_s: minimum seconds between repeat phone alerts for the same condition.
    watchdog_enabled: bool = True
    scheduler_stall_grace_s: int = 600
    dead_letter_attempts: int = 8
    watchdog_alert_cooldown_s: int = 3600

    # Deploy-drift check — pages when the repo HEAD has moved but the running
    # process was booted from an older commit (stale process after a git pull
    # without a restart). Runs inside the 5-min watchdog job, so it shares
    # watchdog_enabled's gate. Reuses watchdog_alert_cooldown_s for repeat alerts.
    deploy_drift_check_enabled: bool = True

    # Budget early-warning — a single phone alert per local day when spend
    # crosses budget_warn_pct of the daily cap. Runs inside the same 5-min
    # watchdog job (see run_watchdog), so it shares watchdog_enabled's gate.
    budget_warn_enabled: bool = True
    budget_warn_pct: float = 0.80

    # 401-burst watchdog — pages once when one client floods failed API-key auths
    # (a stale NEXUS_API_KEY cached in a browser tab's localStorage). Runs inside
    # the same 5-min watchdog job, so it shares watchdog_enabled's gate.
    #
    # Numbers are calibrated against the real 2026-07-25 incident: 133 x 401 from
    # one LAN device over 139 minutes (~0.96/min — a BACKGROUNDED tab, since Chrome
    # throttles background timers to ~1 fire/min; the affected pages normally poll
    # every 10-15s, so a FOREGROUND stale tab is ~6-10/min).
    #
    # threshold=25 / window=30min:
    #   * At the observed background rate it trips ~25 min into a storm and pages
    #     on the next 5-min tick — vs. the ~90 min it went unnoticed.
    #   * At the foreground rate it trips in ~4 min.
    #   * It is comfortably ABOVE any legitimate cause: a mistyped key in Settings,
    #     or the gap during a key rotation, is noticed and corrected in well under
    #     a minute, i.e. <=10 failures even at the fastest 10s poll.
    #   Do NOT lower the threshold below ~15 without also shortening the window —
    #   the empirical background rate is ~1/min, so a 15-min window can physically
    #   never hold more than ~15 events from a throttled tab.
    #
    # quiet == window, deliberately: "no failures for a full window" is the same
    # statement as "the counter has fully drained", so one number governs both.
    auth_burst_enabled: bool = True
    auth_burst_threshold: int = 25
    auth_burst_window_minutes: int = 30

    # Integration contract canary — asserts each integration's CACHED fetch() still
    # has the shape its real consumers index into (backend/safety/contracts.py).
    # Runs inside the same 5-min watchdog job, so it shares watchdog_enabled's gate.
    #
    # Deliberately reads the CACHED fetch(), not fetch.__wrapped__: the cached value
    # IS what briefing/tools/chat read, so validating it validates reality; and a
    # fresh call would re-trigger real side effects (homeassistant.fetch can POST
    # reload_config_entry; unifi.fetch writes KnownDevice rows + does a full login).
    #
    # consecutive_ticks=3: the watchdog fires every 5 min and these fetch() caches
    # hold for 30-60s, so 3 ticks are 3 genuinely independent observations spanning
    # 15 min — no two can share a cache window. One tick can be a transient; three
    # spanning 15 min is a persistent shape change. 15 min is also comfortably
    # inside the lead time before the 07:00 briefing, which is the single biggest
    # consumer of these fields and the thing a silent blank would turn into a lie.
    # An integration that RAISES is not a breach (that's an outage, already covered
    # by the 2-min uptime job) — only a successful-but-wrong-shaped return counts.
    #
    # Reuses watchdog_alert_cooldown_s (3600) for the repeat-alert cooldown — no new
    # knob, same one the stall/dead-letter checks use.
    contract_canary_enabled: bool = True
    contract_canary_consecutive_ticks: int = 3

    # Council-loop post-mortem (backend/agents/council_postmortem.py). Triggered by
    # Council-loop's own run-loop.ps1 at driver exit via POST /api/trigger
    # {"task_name": "council_postmortem"} — NOT scheduled here, because /goal
    # TRUNCATES .council/state/history.jsonl, so a poller that misses the window
    # loses that session permanently.
    #
    # Model: Haiku. Council-loop currently assigns Arbiter=opus, Engineer=sonnet,
    # Security=sonnet, Realist=opus (.council/config.local.json overrides the
    # tracked config's realist=sonnet), so Haiku is the only role-free model —
    # genuine independence for the single extraction call. # VERIFY: re-check if
    # Council-loop's model assignment changes. The real independence, though, comes
    # from checks 1-3 being pure git/AST with no model in the loop at all.
    #
    # run_tests defaults False: executing a foreign repo's configured test_commands
    # inside the NEXUS process is a much bigger trust step than reading its git
    # history, and ProcessForge's runner (pip-audit + pytest) takes minutes and
    # reaches the network. Opt in deliberately.
    council_postmortem_enabled: bool = True
    council_loop_path: str = r"C:\Users\Brian\Documents\Council-loop"
    council_postmortem_model: str = "claude-haiku-4-5-20251001"
    council_postmortem_run_tests: bool = False
    # Bounds the placeholder-code scan (2 git subprocesses per changed .py
    # file, each with its own 30s timeout) — ProcessForge's largest real
    # council cycle range touched ~12 files, so 200 is a generous ceiling,
    # not a real limit on ordinary sessions.
    council_postmortem_max_files: int = 200

    # NEXUS-native Telegram bot. telegram_poll_timeout_s is Telegram's long-poll
    # wait, capped at 50 by their API. calendar_days_ahead matches gcal.py's
    # existing 7-day window.
    telegram_poll_enabled: bool = True
    telegram_poll_timeout_s: int = 25
    calendar_days_ahead: int = 7
    # Phase 2a — text commands/chat. A message older than this (Telegram
    # replays up to 24h of un-acked updates after a NEXUS restart) is dropped
    # rather than re-executed as a command. Buttons are never age-filtered —
    # their own idempotency already covers replay.
    telegram_command_max_age_s: int = 300

    # Homelab watcher (backend/agents/homelab_watch.py) — 60s edge-alert loop.
    # Interval is hardcoded at 60s in scheduler.py (matches retry_deliveries/
    # record_uptime); only thresholds are tunable here. Explicitly NOT built:
    # doorbell/camera (declined by Brian) and NEXUS's own liveness check (a
    # process can't monitor its own death — needs external monitoring).
    homelab_watch_enabled: bool = True
    homelab_disk_temp_warn_c: int = 45
    homelab_garage_entity_id: str = "cover.garage_door_garage_door"
    homelab_garage_open_minutes: int = 30
    # B10: opt-in "all clear" notice once a homelab_watch alert that actually
    # paged (not one suppressed by calibration/dedup) clears. Default False —
    # this is a new, previously-nonexistent notification; Brian opts in
    # explicitly rather than getting a second page per incident unasked.
    homelab_recovery_notify_enabled: bool = False

    # Monthly watch for whether Anthropic has shipped a public API-credit-
    # balance endpoint yet (backend/agents/anthropic_balance_watch.py) — no
    # such API exists today (confirmed 2026-08-05, see that module's
    # docstring). Read-only, no LLM, notifies only on a genuine change from
    # last month's persisted signal.
    anthropic_balance_watch_enabled: bool = True

    # Outcome Tracker (docs/outcome-tracker-spec.md), rollout step 1 — the
    # write/dedup foundation in backend/agents/outcomes.py. Master switch
    # first: outcome_flags_enabled=False makes record_flag() a no-op and every
    # read path degrade to (none)/[] (the documented §7.7 rollback). Sweep is
    # its own sub-flag, matching budget_warn_enabled's precedent (§3.5) so a
    # missed sweep from watchdog_enabled=False doesn't couple two unrelated
    # durability guarantees. Cooldown/retention/briefing-max are plain tunables.
    outcome_flags_enabled: bool = True
    outcome_flag_sweep_enabled: bool = True
    outcome_flag_false_positive_cooldown_days: int = 30
    outcome_flag_retention_days: int = 180
    outcome_flag_briefing_max: int = 10

    # Calibration Loop (docs/calibration-loop-spec.md), rollout step 1 — the
    # schema/config foundation only; nothing computes and nothing suppresses
    # yet. Two master switches on purpose: calibration_enabled ships the
    # measurement (harmless), calibration_suppression_enabled is THE behavior
    # change and stays off until Brian has read real numbers from the soak.
    calibration_enabled: bool = True              # compute hints + /calibration; harmless
    calibration_suppression_enabled: bool = False # THE behavior change — off for the soak
    calibration_window_days: int = 30
    calibration_min_verdicts: int = 5
    calibration_fp_threshold: float = 0.60
    calibration_clear_threshold: float = 0.40     # hysteresis floor
    calibration_hint_max_days: int = 30           # mandatory re-probation
    calibration_override_days: int = 90           # how long Brian's un-suppress is sticky
    calibration_suppress_high_severity: bool = False  # THE guardrail — do not flip lightly

    # A proactive daily homelab-status digest (Proxmox/Unraid/UniFi/AdGuard/
    # Channels DVR/HA/sports). Scheduled 5 minutes after briefing_time, not
    # independently configurable.
    homelab_digest_enabled: bool = True

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
    def unraid_ssh_private_key(self) -> str:
        """OpenSSH-format private key (full armor + trailing newline) for the
        native docker-prune SSH path (Phase 7d). Loud (KeyError propagates) --
        the caller in backend/integrations/unraid.py wraps this in its own
        try/except and re-raises RuntimeError("UNRAID_SSH_PRIVATE_KEY not
        configured"), mirroring unraid_api_key's precedent."""
        from backend.secrets.manager import get_secret
        return get_secret("UNRAID_SSH_PRIVATE_KEY")

    @property
    def lxc_ssh_private_key(self) -> str:
        """OpenSSH-format private key (full armor + trailing newline) for the
        native NEXUS-restart SSH path to the LXC (2026-08-15). Loud (KeyError
        propagates) -- backend/integrations/lxc_host.py wraps this in its own
        try/except and re-raises RuntimeError("LXC_SSH_PRIVATE_KEY not
        configured"), mirroring unraid_ssh_private_key's precedent."""
        from backend.secrets.manager import get_secret
        return get_secret("LXC_SSH_PRIVATE_KEY")

    @property
    def proxmox_token(self) -> str:
        # Full PVE header string: "PVEAPIToken=user@realm!tokenid=uuid".
        # Sent verbatim as the Authorization header value.
        from backend.secrets.manager import get_secret
        try:
            return get_secret("PROXMOX_TOKEN")
        except KeyError:
            return ""

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
    def nexus_api_key(self) -> str:
        from backend.secrets.manager import get_secret
        return get_secret("NEXUS_API_KEY")

    @property
    def unraid_backup_user(self) -> str:
        from backend.secrets.manager import get_secret
        try:
            return get_secret("UNRAID_BACKUP_USER")
        except KeyError:
            return ""

    @property
    def unraid_backup_password(self) -> str:
        from backend.secrets.manager import get_secret
        try:
            return get_secret("UNRAID_BACKUP_PASSWORD")
        except KeyError:
            return ""

    @property
    def brain_mcp_write_token(self) -> str:
        """Optional write token the Brain MCP server requires from NON-loopback callers."""
        from backend.secrets.manager import get_secret
        try:
            return get_secret("BRAIN_MCP_WRITE_TOKEN") or ""
        except KeyError:
            return ""

    @property
    def brain_mcp_token(self) -> str:
        """Token NEXUS sends as Authorization: Bearer when calling the Brain MCP
        server. Unified with brain_mcp_write_token — same handshake, one value:
        the server side (what it enforces) is canonical, so this just delegates
        to it. A machine-env BRAIN_MCP_WRITE_TOKEN still overrides via
        manager.get_secret's os.environ fallback if ever needed."""
        return self.brain_mcp_write_token

    @property
    def protonmail_mcp_url(self) -> str:
        """Operational config promoted to a secret 2026-07-24: a real tailnet
        IP, worth protecting even though the MCP server itself has no auth
        token. A missing key raises KeyError — every consumer already
        degrades loudly on failure (health_check -> False/OFFLINE, agent
        tools -> "unavailable" string, API routes -> 502) rather than
        silently limping along on an empty URL."""
        from backend.secrets.manager import get_secret
        return get_secret("PROTONMAIL_MCP_URL")

    @property
    def protonmail_account(self) -> str:
        """See protonmail_mcp_url — same reasoning, real personal account name."""
        from backend.secrets.manager import get_secret
        return get_secret("PROTONMAIL_ACCOUNT")

    @property
    def telegram_bot_token(self) -> str:
        """Loud (KeyError) — same reasoning as protonmail_mcp_url. notify() catches
        it, logs ERROR, and does NOT queue (a missing token never fixes itself on
        retry — same posture as a 401 from Telegram)."""
        from backend.secrets.manager import get_secret
        return get_secret("TELEGRAM_BOT_TOKEN")

    @property
    def telegram_chat_id(self) -> str:
        """Loud. Kept as str — Telegram's API accepts chat_id as a string verbatim,
        so no int() cast is needed and a malformed value fails at the API, not at
        import time."""
        from backend.secrets.manager import get_secret
        return get_secret("TELEGRAM_CHAT_ID")

    @property
    def google_calendar_ical_url(self) -> str:
        """Quiet ("" on KeyError) — calendar.fetch() raises RuntimeError when BOTH
        feeds are empty, so the loud failure happens once, in one place."""
        from backend.secrets.manager import get_secret
        try:
            return get_secret("GOOGLE_CALENDAR_ICAL_URL")
        except KeyError:
            return ""

    @property
    def apple_calendar_ical_url(self) -> str:
        """Quiet — genuinely optional second feed (gcal.py already treats it so)."""
        from backend.secrets.manager import get_secret
        try:
            return get_secret("APPLE_CALENDAR_ICAL_URL")
        except KeyError:
            return ""

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

        # action_judge_mode — must be one of the three modes the judge gate understands.
        valid_judge_modes = {"off", "shadow", "enforce"}
        if self.action_judge_mode not in valid_judge_modes:
            raise ValueError(
                f"Invalid action_judge_mode {self.action_judge_mode!r}; "
                f"expected one of {sorted(valid_judge_modes)}"
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

        # Non-fatal warning: notifications will fail silently if either secret is absent.
        if self.phone_notifications_enabled:
            missing_telegram = []
            for name, prop in (("TELEGRAM_BOT_TOKEN", "telegram_bot_token"), ("TELEGRAM_CHAT_ID", "telegram_chat_id")):
                try:
                    if not getattr(self, prop):
                        missing_telegram.append(name)
                except Exception:
                    missing_telegram.append(name)
            if missing_telegram:
                logger.error(
                    f"phone_notifications_enabled=True but {', '.join(missing_telegram)} "
                    "missing from the vault. ALL phone notifications will fail. Add via the "
                    "Settings page."
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
