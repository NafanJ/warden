"""Minimal Discord Gateway client — receives native slash-command interactions.

Slash commands aren't channel messages, so the REST poller never sees them;
Discord pushes them as *interactions*. We get them by dialing the gateway over
an outbound websocket (the bot connects to Discord, never the reverse), so this
keeps warden's no-inbound-port design — unlike an HTTP interactions endpoint.

This is a deliberately small client: identify, heartbeat, and dispatch
INTERACTION_CREATE. It reconnects on drop and otherwise ignores every other
gateway event. Owner commands run through the same `dispatch` as the typed-message
path. Run inside the poller process (see discord_poller.main).
"""
from __future__ import annotations

import asyncio
import json

from warden.backends import Backend
from warden.config import Config
from warden.notifier import Channel
from warden.notifier import discord as dc
from warden.notifier.components import handle_component
from warden.notifier.discord_commands import dispatch, parse_interaction
from warden.store import Store

GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
# Interactions are delivered regardless of gateway intents, so we identify with
# none — warden never reads message content or other privileged events here.
INTENTS = 0

OP_DISPATCH, OP_HEARTBEAT, OP_IDENTIFY = 0, 1, 2
OP_RECONNECT, OP_INVALID_SESSION, OP_HELLO, OP_HEARTBEAT_ACK = 7, 9, 10, 11


async def _heartbeat(ws, interval_ms: int, state: dict) -> None:
    while True:
        await asyncio.sleep(interval_ms / 1000)
        await ws.send(json.dumps({"op": OP_HEARTBEAT, "d": state.get("seq")}))


def _author_id(interaction: dict) -> str:
    member = interaction.get("member") or {}
    user = member.get("user") or interaction.get("user") or {}
    return str(user.get("id", ""))


async def _dispatch_interaction(interaction: dict, config: Config, backend: Backend,
                                store: Store, channel: Channel) -> None:
    itype = interaction.get("type")
    if itype == 2:        # APPLICATION_COMMAND — a /slash command
        await _handle_command(interaction, config, backend, store, channel)
    elif itype == 3:      # MESSAGE_COMPONENT — a button click
        await _handle_component(interaction, config, backend, store, channel)


async def _handle_command(interaction: dict, config: Config, backend: Backend,
                          store: Store, channel: Channel) -> None:
    parsed = parse_interaction(interaction)
    if not parsed:
        return
    name, arg, author_id = parsed
    interaction_id, token = interaction["id"], interaction["token"]

    # ACK within Discord's 3s window before doing any real work.
    try:
        dc.interaction_defer(interaction_id, token)
    except Exception as exc:
        print(f"gateway: defer failed for /{name}: {exc}")
        return

    if config.discord_owner_id and author_id != config.discord_owner_id:
        dc.interaction_respond(config, token, "⛔ warden only takes commands from its owner.")
        return

    # dispatch is sync and can block ~30s (diagnose); run it off the event loop
    # so heartbeats keep flowing and the connection stays alive.
    reply = await asyncio.to_thread(dispatch, name, arg, config, backend, store, channel)
    try:
        dc.interaction_respond(
            config, token,
            reply or f"🔍 on it — results are posting in <#{config.discord_channel_id}>.")
    except Exception as exc:
        print(f"gateway: could not post result for /{name}: {exc}")


async def _handle_component(interaction: dict, config: Config, backend: Backend,
                            store: Store, channel: Channel) -> None:
    custom_id = (interaction.get("data") or {}).get("custom_id") or ""
    interaction_id, token = interaction["id"], interaction["token"]

    # Owner check first (it's instant), so a stranger gets a private rejection
    # rather than us deferring and wiping the owner's alert message.
    if config.discord_owner_id and _author_id(interaction) != config.discord_owner_id:
        try:
            dc.interaction_reply_ephemeral(interaction_id, token,
                                           "⛔ These buttons are the owner's.")
        except Exception as exc:
            print(f"gateway: ephemeral reject failed: {exc}")
        return

    # DEFERRED_UPDATE so the eventual edit lands on the button's own message.
    try:
        dc.interaction_defer(interaction_id, token, dc.DEFERRED_UPDATE)
    except Exception as exc:
        print(f"gateway: defer failed for button {custom_id}: {exc}")
        return

    reply = await asyncio.to_thread(handle_component, custom_id,
                                    config, backend, store, channel)
    try:  # edit the alert to the outcome and strip the buttons so it can't re-fire
        dc.interaction_respond(config, token, reply, components=[])
    except Exception as exc:
        print(f"gateway: could not update message for button {custom_id}: {exc}")


async def _session(config: Config, backend: Backend, store: Store, channel: Channel) -> None:
    import websockets  # local import so the poller still runs if the dep is absent
    state: dict = {"seq": None}
    async with websockets.connect(GATEWAY_URL, max_size=None) as ws:
        hello = json.loads(await ws.recv())
        hb = asyncio.create_task(_heartbeat(ws, hello["d"]["heartbeat_interval"], state))
        await ws.send(json.dumps({"op": OP_IDENTIFY, "d": {
            "token": config.discord_bot_token,
            "intents": INTENTS,
            "properties": {"os": "linux", "browser": "warden", "device": "warden"},
        }}))
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("s") is not None:
                    state["seq"] = msg["s"]
                op = msg.get("op")
                if op == OP_HEARTBEAT:               # server asked for one now
                    await ws.send(json.dumps({"op": OP_HEARTBEAT, "d": state["seq"]}))
                elif op in (OP_RECONNECT, OP_INVALID_SESSION):
                    return                            # drop and reconnect fresh
                elif op == OP_DISPATCH and msg.get("t") == "INTERACTION_CREATE":
                    asyncio.create_task(
                        _dispatch_interaction(msg["d"], config, backend, store, channel))
        finally:
            hb.cancel()


async def run(config: Config, backend: Backend, store: Store, channel: Channel) -> None:
    """Stay connected to the gateway, reconnecting with backoff on any drop."""
    backoff = 1
    while True:
        try:
            await _session(config, backend, store, channel)
            backoff = 1  # a clean return (reconnect/invalid session) — retry promptly
        except Exception as exc:
            print(f"gateway: session ended ({exc}); reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
