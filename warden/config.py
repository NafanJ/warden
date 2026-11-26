"""Central configuration, loaded from environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Config:
    engine: str = "legacy"  # legacy | langgraph
    mode: str = "dry-run"  # detect | dry-run | active
    llm_provider: str = "openai"  # openai | claude
    model: str = "claude-sonnet-4-6"  # used when llm_provider == claude
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"  # used when llm_provider == openai
    max_budget_usd: float = 1.0

    state_dir: Path = Path("state")
    # Reports live *under* state_dir so they sit with the incident trigger JSONs +
    # DB and survive a deploy (the deploy rsync --delete excludes state/; a sibling
    # incidents/ dir would get wiped on every update).
    incidents_dir: Path = Path("state/incidents")

    sonarr_url: str = "http://localhost:8989"
    sonarr_api_key: str = ""
    radarr_url: str = "http://localhost:7878"
    radarr_api_key: str = ""
    transmission_url: str = "http://localhost:9091/transmission/rpc"
    transmission_user: str = ""
    transmission_pass: str = ""
    tautulli_url: str = "http://localhost:8181"
    tautulli_api_key: str = ""

    # Plex API — the primary source for `user-stats`. Libraries are shared by
    # invite, and Tautulli only knows users it has watched stream, so invitees
    # are missing or stale there. Plex knows the whole roster and holds the
    # server-side history. Token: open any library item → Get Info → View XML,
    # and copy the X-Plex-Token query param off the URL.
    plex_url: str = "http://localhost:32400"
    plex_token: str = ""
    # The same person can carry different names in each source (a Plex login vs
    # a Tautulli friendly name) or hold two Plex accounts after a re-invite.
    # Nothing in the data joins them, so map the variants onto one display name:
    # "plexlogin=Display, oldname=Display".
    plex_user_aliases: dict[str, str] = field(default_factory=dict)

    # Plex webhook → throttle Transmission while anyone is streaming.
    plex_throttle_downloads: bool = True
    plex_webhook_token: str = ""  # optional shared secret in the webhook URL (?token=)
    # containers serving live Plex streams — a restart while people are watching
    # is escalated to owner approval instead of done autonomously.
    stream_containers: list[str] = field(default_factory=lambda: ["plex"])

    disk_paths: list[str] = field(default_factory=lambda: ["/", "/mnt/Modi"])
    disk_threshold_pct: int = 92
    # Don't re-raise the same anomaly within this window if the last one was left
    # unresolved (escalated) — prevents re-investigating a persistent condition
    # (e.g. a full disk warden can't fix) every cycle.
    incident_cooldown_hours: float = 6.0
    # Per-incident-key overrides of the cooldown above, for known self-healing
    # conditions that recur on a fixed schedule (e.g. immich's nightly SIGSEGV
    # crash loop). A window wider than the recurrence interval turns nightly
    # re-paging into a periodic reminder. Keyed by anomaly key, e.g.
    # "container_down:immich-server".
    incident_cooldown_overrides: dict[str, float] = field(default_factory=dict)
    public_urls: list[str] = field(default_factory=list)
    stall_threshold_hours: float = 4.0
    stall_min_age_hours: float = 1.0
    ignored_containers: list[str] = field(default_factory=list)
    # The owner doesn't seed: once a torrent is 100% done and no *arr app is
    # still importing it, warden removes it from Transmission (keeping the data).
    reap_completed: bool = True

    # The only directory trees warden is ever allowed to delete within. Enforced
    # both at the permission gate (so out-of-bounds deletes are never queued) and
    # in the backend (defense in depth).
    delete_roots: list[str] = field(default_factory=lambda: ["/mnt/Modi/Kodi/downloads/"])

    notify_channel: str = "log"
    wa_token: str = ""
    wa_phone_number_id: str = ""
    wa_to: str = ""
    wa_verify_token: str = ""
    wa_app_secret: str = ""

    discord_bot_token: str = ""
    discord_channel_id: str = ""
    discord_member_role_id: str = ""
    discord_requests_url: str = "https://requests.nathanjoel.com"
    discord_owner_id: str = ""
    discord_guild_id: str = ""   # register slash commands here (instant); global if unset (~1h)

    # "@warden !full <task>" — a full Claude Code session on the host (shell +
    # file access, outside the 18-tool sandbox). Requires explicit owner approval
    # per task AND this flag: enabling means an approved Discord message can run
    # arbitrary commands on the host. Off by default.
    enable_full_tasks: bool = False
    full_task_cwd: str = ""            # working dir for full sessions ("" = $HOME)
    full_task_budget_usd: float = 2.0  # spend cap per full task

    def secrets(self) -> list[str]:
        """Every value that must never appear in a report or notification."""
        candidates = [
            os.environ.get("ANTHROPIC_API_KEY", ""),
            self.openai_api_key,
            self.sonarr_api_key,
            self.radarr_api_key,
            self.transmission_pass,
            self.wa_token,
            self.wa_app_secret,
            self.wa_verify_token,
            self.discord_bot_token,
            self.tautulli_api_key,
            self.plex_token,
            self.plex_webhook_token,
        ]
        return [s for s in candidates if s]


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _kv_aliases(value: str) -> dict[str, str]:
    """Parse 'plexname=Display, other=Display' into {lowercased alias: display}."""
    out: dict[str, str] = {}
    for pair in _csv(value):
        if "=" not in pair:
            continue
        alias, display = pair.split("=", 1)
        if alias.strip() and display.strip():
            out[alias.strip().lower()] = display.strip()
    return out


def _kv_hours(value: str) -> dict[str, float]:
    """Parse 'key=hours, key2=hours2' into {key: hours}. Anomaly keys contain a
    ':' (e.g. 'container_down:immich-server'), so split on the *last* '='."""
    out: dict[str, float] = {}
    for pair in _csv(value):
        if "=" not in pair:
            continue
        k, h = pair.rsplit("=", 1)
        try:
            out[k.strip()] = float(h)
        except ValueError:
            continue
    return out


def deletable(path: str, roots: list[str]) -> bool:
    """True only if `path` is strictly inside one of the allowed root trees — a
    descendant, never a root directory itself. So warden can clean files within
    the downloads tree but can never be asked to wipe a whole root."""
    try:
        rp = str(Path(path).resolve())
    except OSError:
        return False
    for root in roots:
        rr = str(Path(root).resolve())
        if rp.startswith(rr + "/"):  # strict descendant (excludes rr itself)
            return True
    return False


def load_config(env_file: str | Path | None = None) -> Config:
    load_dotenv(env_file or Path(__file__).resolve().parent.parent / ".env")
    e = os.environ.get
    state_dir = Path(e("WARDEN_STATE_DIR", "state"))
    config = Config(
        engine=e("WARDEN_ENGINE", "legacy"),
        mode=e("WARDEN_MODE", "dry-run"),
        llm_provider=e("LLM_PROVIDER", "openai"),
        model=e("WARDEN_MODEL", "claude-sonnet-4-6"),
        openai_api_key=e("OPENAI_API_KEY", ""),
        openai_model=e("OPENAI_MODEL", "gpt-4o-mini"),
        max_budget_usd=float(e("WARDEN_MAX_BUDGET_USD", "1.0")),
        state_dir=state_dir,
        incidents_dir=Path(e("WARDEN_INCIDENTS_DIR", str(state_dir / "incidents"))),
        sonarr_url=e("SONARR_URL", "http://localhost:8989"),
        sonarr_api_key=e("SONARR_API_KEY", ""),
        radarr_url=e("RADARR_URL", "http://localhost:7878"),
        radarr_api_key=e("RADARR_API_KEY", ""),
        transmission_url=e("TRANSMISSION_URL", "http://localhost:9091/transmission/rpc"),
        transmission_user=e("TRANSMISSION_USER", ""),
        transmission_pass=e("TRANSMISSION_PASS", ""),
        tautulli_url=e("TAUTULLI_URL", "http://localhost:8181"),
        tautulli_api_key=e("TAUTULLI_API_KEY", ""),
        plex_url=e("PLEX_URL", "http://localhost:32400"),
        plex_token=e("PLEX_TOKEN", ""),
        plex_user_aliases=_kv_aliases(e("PLEX_USER_ALIASES", "")),
        plex_throttle_downloads=e("PLEX_THROTTLE_DOWNLOADS", "true").lower() in ("1", "true", "yes", "on"),
        plex_webhook_token=e("PLEX_WEBHOOK_TOKEN", ""),
        stream_containers=_csv(e("STREAM_CONTAINERS", "plex")),
        disk_paths=_csv(e("DISK_PATHS", "/,/mnt/Modi")),
        disk_threshold_pct=int(e("DISK_THRESHOLD_PCT", "92")),
        incident_cooldown_hours=float(e("INCIDENT_COOLDOWN_HOURS", "6")),
        incident_cooldown_overrides=_kv_hours(e("INCIDENT_COOLDOWN_OVERRIDES", "")),
        public_urls=_csv(e("PUBLIC_URLS", "")),
        stall_threshold_hours=float(e("STALL_THRESHOLD_HOURS", "4")),
        stall_min_age_hours=float(e("STALL_MIN_AGE_HOURS", "1")),
        ignored_containers=_csv(e("IGNORED_CONTAINERS", "")),
        reap_completed=e("REAP_COMPLETED_TORRENTS", "true").lower() in ("1", "true", "yes", "on"),
        delete_roots=_csv(e("DELETE_ROOTS", "/mnt/Modi/Kodi/downloads/")),
        notify_channel=e("NOTIFY_CHANNEL", "log"),
        wa_token=e("WA_TOKEN", ""),
        wa_phone_number_id=e("WA_PHONE_NUMBER_ID", ""),
        wa_to=e("WA_TO", ""),
        wa_verify_token=e("WA_VERIFY_TOKEN", ""),
        wa_app_secret=e("WA_APP_SECRET", ""),
        discord_bot_token=e("DISCORD_BOT_TOKEN", ""),
        discord_channel_id=e("DISCORD_CHANNEL_ID", ""),
        discord_member_role_id=e("DISCORD_MEMBER_ROLE_ID", ""),
        discord_requests_url=e("DISCORD_REQUESTS_URL", "https://requests.nathanjoel.com"),
        discord_owner_id=e("DISCORD_OWNER_ID", ""),
        discord_guild_id=e("DISCORD_GUILD_ID", ""),
        enable_full_tasks=e("ENABLE_FULL_TASKS", "false").lower() in ("1", "true", "yes", "on"),
        full_task_cwd=e("FULL_TASK_CWD", ""),
        full_task_budget_usd=float(e("FULL_TASK_BUDGET_USD", "2.0")),
    )
    if config.engine == "langgraph":
        import json
        import sqlite3
        db_path = config.state_dir / "warden.db"
        if db_path.exists():
            with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as conn:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='local_settings'").fetchone():
                    row = conn.execute("SELECT value FROM local_settings WHERE key='routines'").fetchone()
                    if row:
                        routines = json.loads(row[0])
                        config.plex_throttle_downloads = routines.get("download_throttling", config.plex_throttle_downloads)
                        config.reap_completed = routines.get("completed_torrent_cleanup", config.reap_completed)
    return config
