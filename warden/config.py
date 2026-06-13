"""Central configuration, loaded from environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Config:
    mode: str = "dry-run"  # detect | dry-run | active
    model: str = "claude-sonnet-4-6"
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

    disk_paths: list[str] = field(default_factory=lambda: ["/", "/mnt/Modi"])
    disk_threshold_pct: int = 92
    public_urls: list[str] = field(default_factory=list)
    stall_threshold_hours: float = 4.0
    stall_min_age_hours: float = 1.0
    ignored_containers: list[str] = field(default_factory=list)

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
            self.sonarr_api_key,
            self.radarr_api_key,
            self.transmission_pass,
            self.wa_token,
            self.wa_app_secret,
            self.wa_verify_token,
            self.discord_bot_token,
        ]
        return [s for s in candidates if s]


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def load_config(env_file: str | Path | None = None) -> Config:
    load_dotenv(env_file or Path(__file__).resolve().parent.parent / ".env")
    e = os.environ.get
    return Config(
        mode=e("WARDEN_MODE", "dry-run"),
        model=e("WARDEN_MODEL", "claude-sonnet-4-6"),
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
        disk_paths=_csv(e("DISK_PATHS", "/,/mnt/Modi")),
        disk_threshold_pct=int(e("DISK_THRESHOLD_PCT", "92")),
        public_urls=_csv(e("PUBLIC_URLS", "")),
        stall_threshold_hours=float(e("STALL_THRESHOLD_HOURS", "4")),
        stall_min_age_hours=float(e("STALL_MIN_AGE_HOURS", "1")),
        ignored_containers=_csv(e("IGNORED_CONTAINERS", "")),
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
