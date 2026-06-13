"""Central configuration, loaded from environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Config:
    mode: str = "dry-run"  # detect | dry-run | active
    llm_provider: str = "openai"  # openai | claude
    model: str = "claude-sonnet-4-6"  # used when llm_provider == claude
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"  # used when llm_provider == openai
    max_budget_usd: float = 1.0

    state_dir: Path = Path("state")
    incidents_dir: Path = Path("incidents")

    sonarr_url: str = "http://localhost:8989"
    sonarr_api_key: str = ""
    radarr_url: str = "http://localhost:7878"
    radarr_api_key: str = ""
    transmission_url: str = "http://localhost:9091/transmission/rpc"
    transmission_user: str = ""
    transmission_pass: str = ""
    tautulli_url: str = "http://localhost:8181"
    tautulli_api_key: str = ""
    # containers serving live Plex streams — a restart while people are watching
    # is escalated to owner approval instead of done autonomously.
    stream_containers: list[str] = field(default_factory=lambda: ["plex"])

    disk_paths: list[str] = field(default_factory=lambda: ["/", "/mnt/Modi"])
    disk_threshold_pct: int = 92
    # Don't re-raise the same anomaly within this window if the last one was left
    # unresolved (escalated) — prevents re-investigating a persistent condition
    # (e.g. a full disk warden can't fix) every cycle.
    incident_cooldown_hours: float = 6.0
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
    discord_owner_id: str = ""

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
        ]
        return [s for s in candidates if s]


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


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
    return Config(
        mode=e("WARDEN_MODE", "dry-run"),
        llm_provider=e("LLM_PROVIDER", "openai"),
        model=e("WARDEN_MODEL", "claude-sonnet-4-6"),
        openai_api_key=e("OPENAI_API_KEY", ""),
        openai_model=e("OPENAI_MODEL", "gpt-4o-mini"),
        max_budget_usd=float(e("WARDEN_MAX_BUDGET_USD", "1.0")),
        state_dir=Path(e("WARDEN_STATE_DIR", "state")),
        incidents_dir=Path(e("WARDEN_INCIDENTS_DIR", "incidents")),
        sonarr_url=e("SONARR_URL", "http://localhost:8989"),
        sonarr_api_key=e("SONARR_API_KEY", ""),
        radarr_url=e("RADARR_URL", "http://localhost:7878"),
        radarr_api_key=e("RADARR_API_KEY", ""),
        transmission_url=e("TRANSMISSION_URL", "http://localhost:9091/transmission/rpc"),
        transmission_user=e("TRANSMISSION_USER", ""),
        transmission_pass=e("TRANSMISSION_PASS", ""),
        tautulli_url=e("TAUTULLI_URL", "http://localhost:8181"),
        tautulli_api_key=e("TAUTULLI_API_KEY", ""),
        stream_containers=_csv(e("STREAM_CONTAINERS", "plex")),
        disk_paths=_csv(e("DISK_PATHS", "/,/mnt/Modi")),
        disk_threshold_pct=int(e("DISK_THRESHOLD_PCT", "92")),
        incident_cooldown_hours=float(e("INCIDENT_COOLDOWN_HOURS", "6")),
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
        discord_owner_id=e("DISCORD_OWNER_ID", ""),
    )
