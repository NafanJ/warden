from __future__ import annotations

from datetime import datetime, timezone

from warden.config import Config


class LogChannel:
    """Writes notifications to state/notifications.log — used until WhatsApp
    is configured, and by tests/evals."""

    def __init__(self, config: Config):
        self.path = config.state_dir / "notifications.log"
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.path.open("a") as f:
            f.write(f"--- {stamp} ---\n{text}\n")
