"""Warden's owner commands, defined once and shared by both Discord paths.

`COMMANDS` is the Discord slash-command schema — registered with the API so the
`/` picker shows each command with its (optional) parameter hints. `dispatch`
runs a command by name, used identically by the message poller (typed `status`)
and the gateway (native `/status`), so the two can never drift.

STRING is Discord's option type 3.
"""
from __future__ import annotations

import asyncio

from warden.agent.runner import run_diagnose
from warden.backends import Backend
from warden.config import Config
from warden.notifier import Channel
from warden.store import Store
from warden.summary import format_status, gather
from warden.userstats import handle_userstats

_STRING = 3

COMMANDS: list[dict] = [
    {
        "name": "status",
        "description": "Live health: containers, disk, downloads, Plex, and what's flagged right now.",
    },
    {
        "name": "diagnose",
        "description": "Investigate a problem in plain language; warden reads the host and reports back (~30s).",
        "options": [
            {"name": "question", "description": "What to look into, e.g. why is plex buffering",
             "type": _STRING, "required": True},
        ],
    },
    {
        "name": "user-stats",
        "description": "Plex watch stats from Tautulli, optionally focused on one user.",
        "options": [
            {"name": "user", "description": "Plex username to focus on (optional)",
             "type": _STRING, "required": False},
        ],
    },
]

# Names accepted on the typed message path (incl. legacy aliases) -> canonical.
ALIASES = {
    "status": "status", "!status": "status",
    "diagnose": "diagnose", "!diagnose": "diagnose",
    "user-stats": "user-stats", "userstats": "user-stats", "!user-stats": "user-stats",
}


def parse_interaction(interaction: dict) -> tuple[str, str, str] | None:
    """Pull (command_name, arg, author_id) out of a slash-command interaction.

    Warden's commands take at most one string option, so the options list
    collapses to a single `arg` string. Returns None for anything that isn't an
    application-command interaction (type 2)."""
    if interaction.get("type") != 2:
        return None
    data = interaction.get("data") or {}
    name = data.get("name") or ""
    arg = ""
    for opt in data.get("options") or []:
        if opt.get("value") is not None:
            arg = str(opt["value"])
            break
    # guild interactions carry the invoker under member.user; DMs under user
    member = interaction.get("member") or {}
    user = member.get("user") or interaction.get("user") or {}
    return name, arg, str(user.get("id", ""))


def dispatch(name: str, arg: str, config: Config, backend: Backend,
             store: Store, channel: Channel) -> str | None:
    """Run a warden command. Returns text to show the owner, or None when the
    command streams its own output to the channel (diagnose). Never raises —
    failures come back as a message string."""
    name = ALIASES.get(name, name)
    arg = (arg or "").strip()
    try:
        if name == "status":
            return format_status(gather(config, backend, store))
        if name == "user-stats":
            return handle_userstats(arg, backend)
        if name == "diagnose":
            if not arg:
                return ("Usage: `/diagnose question:<what to look into>` — "
                        "e.g. `why is plex buffering`")
            channel.send(f"🔍 investigating: _{arg}_ … (~30s)")
            asyncio.run(run_diagnose(arg, config, backend, store, channel))
            return None  # run_diagnose posts its findings to the channel itself
    except Exception as exc:
        return f"warden: `{name}` failed — {exc}"
    return None


def main() -> int:
    """Register (overwrite) warden's slash commands, then exit. Run once after
    changing COMMANDS; the poller also re-registers on every start."""
    from warden.config import load_config
    from warden.notifier import discord as dc
    config = load_config()
    if not config.discord_bot_token:
        print("DISCORD_BOT_TOKEN not set; nothing to register.")
        return 1
    scope = dc.register_commands(config, COMMANDS)
    print(f"registered {len(COMMANDS)} slash command(s) to {scope}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
