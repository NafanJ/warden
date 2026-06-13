"""Notification channels. WhatsApp is primary; LogChannel is the no-config
fallback and what tests use."""
from __future__ import annotations

from typing import Protocol

from warden.config import Config


class Channel(Protocol):
    def send(self, text: str) -> None: ...


def get_channel(config: Config) -> Channel:
    if config.notify_channel == "whatsapp":
        from warden.notifier.whatsapp import WhatsAppChannel
        return WhatsAppChannel(config)
    if config.notify_channel == "discord":
        from warden.notifier.discord import DiscordChannel
        return DiscordChannel(config)
    from warden.notifier.logchannel import LogChannel
    return LogChannel(config)
