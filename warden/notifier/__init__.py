"""Notification channels. WhatsApp is primary; LogChannel is the no-config
fallback and what tests use."""
from __future__ import annotations

from typing import Protocol

from warden.config import Config


class Channel(Protocol):
    def send(self, text: str) -> None: ...

    def send_approval(self, action_id: int, text: str) -> str | None:
        """Post a Tier 2 approval prompt. May return a transport reference (e.g.
        a Discord message id) the approval can later be matched back to; channels
        without a richer approval affordance just post the text and return None."""
        ...


def get_channel(config: Config) -> Channel:
    if config.notify_channel == "whatsapp":
        from warden.notifier.whatsapp import WhatsAppChannel
        return WhatsAppChannel(config)
    if config.notify_channel == "discord":
        from warden.notifier.discord import DiscordChannel
        return DiscordChannel(config)
    from warden.notifier.logchannel import LogChannel
    return LogChannel(config)
