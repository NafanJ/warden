"""Incident report writing with secret redaction.

Reports are committed to a public repo, so everything passes through redact()
before touching disk: configured secrets, LAN IPs, and anything that looks
like an API key or bearer token.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from warden.config import Config

LAN_IP = re.compile(r"\b(?:192\.168|10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b")
KEY_LIKE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|[a-f0-9]{32}|[A-Za-z0-9+/=]{40,})\b")
CATEGORIES = {"container_down", "disk_pressure", "disk_unavailable", "stalled_download",
              "tunnel_down", "arr_queue_error", "oom", "other"}


def redact(text: str, secrets: list[str]) -> str:
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = LAN_IP.sub("[LAN-IP]", text)
    text = KEY_LIKE.sub("[REDACTED-KEY]", text)
    return text


def slugify(title: str, max_len: int = 50) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:max_len] or "incident"


def write_incident_report(config: Config, incident_id: int, title: str,
                          category: str, status: str, markdown: str) -> Path:
    config.incidents_dir.mkdir(parents=True, exist_ok=True)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = config.incidents_dir / f"{date}-{incident_id:04d}-{slugify(title)}.md"
    header = (
        f"# {title}\n\n"
        f"- **Incident:** #{incident_id}\n"
        f"- **Category:** {category}\n"
        f"- **Status:** {status}\n"
        f"- **Mode:** {config.mode}\n"
        f"- **Written:** {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n\n"
    )
    path.write_text(redact(header + markdown.strip() + "\n", config.secrets()))
    return path
