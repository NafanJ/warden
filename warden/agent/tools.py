"""Warden's custom in-process MCP tools.

Every tool wraps a Backend method — the agent never gets Bash or raw file
access. Built as a factory so live and replay backends are interchangeable.
"""
from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from warden.agent import report as report_mod
from warden.backends import Backend
from warden.config import Config
from warden.store import Store


def _text(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, str):
        payload = json.dumps(payload, indent=2, default=str)
    return {"content": [{"type": "text", "text": payload[:40000]}]}


def _error(exc: Exception) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"ERROR: {exc}"}], "is_error": True}


def build_warden_server(backend: Backend, config: Config, store: Store,
                        incident_id: int | None, run_result: dict[str, Any]):
    """run_result is mutated by write_report so the runner can read the outcome."""

    @tool("get_containers", "List all Docker containers with state and status.", {})
    async def get_containers(args):
        try:
            return _text(backend.docker_ps())
        except Exception as exc:
            return _error(exc)

    @tool("container_logs", "Tail recent logs from a container.",
          {"name": str, "lines": int})
    async def container_logs(args):
        try:
            return _text(backend.container_logs(args["name"], int(args.get("lines", 100))))
        except Exception as exc:
            return _error(exc)

    @tool("container_inspect",
          "Inspect a container: state, exit code, OOM flag, restart count/policy, memory limit.",
          {"name": str})
    async def container_inspect(args):
        try:
            return _text(backend.container_inspect(args["name"]))
        except Exception as exc:
            return _error(exc)

    @tool("container_restart", "Restart a container (Tier 1, reversible).", {"name": str})
    async def container_restart(args):
        try:
            return _text(backend.container_restart(args["name"]))
        except Exception as exc:
            return _error(exc)

    @tool("docker_prune",
          "Reclaim disk by removing dangling Docker images and build cache (Tier 1, "
          "safe — only unreferenced layers, nothing a container uses).", {})
    async def docker_prune(args):
        try:
            return _text(backend.docker_prune())
        except Exception as exc:
            return _error(exc)

    @tool("disk_usage", "Disk usage for the monitored mount points.", {})
    async def disk_usage(args):
        try:
            return _text(backend.disk_usage(config.disk_paths))
        except Exception as exc:
            return _error(exc)

    @tool("du_summary", "Directory size breakdown (du) for a path.",
          {"path": str, "depth": int})
    async def du_summary(args):
        try:
            return _text(backend.du_summary(args["path"], int(args.get("depth", 1))))
        except Exception as exc:
            return _error(exc)

    @tool("memory", "Host memory and swap state.", {})
    async def memory(args):
        try:
            return _text(backend.memory())
        except Exception as exc:
            return _error(exc)

    @tool("list_torrents",
          "List torrents in Transmission with progress, peers, and inactivity.", {})
    async def list_torrents(args):
        try:
            return _text(backend.torrents())
        except Exception as exc:
            return _error(exc)

    @tool("remove_torrents",
          "Remove torrents from Transmission by id (Tier 1 without data; "
          "delete_data=true is destructive and needs approval).",
          {"ids": list, "delete_data": bool})
    async def remove_torrents(args):
        try:
            return _text(backend.remove_torrents(list(args["ids"]), bool(args.get("delete_data", False))))
        except Exception as exc:
            return _error(exc)

    @tool("arr_queue", "Read the Sonarr or Radarr download queue.", {"app": str})
    async def arr_queue(args):
        try:
            return _text(backend.arr_queue(args["app"]))
        except Exception as exc:
            return _error(exc)

    @tool("arr_blocklist_research",
          "Blocklist queue items in Sonarr/Radarr and remove from the download client; "
          "the *arr app re-searches automatically (Tier 1).",
          {"app": str, "queue_ids": list})
    async def arr_blocklist_research(args):
        try:
            return _text(backend.arr_blocklist_and_research(args["app"], list(args["queue_ids"])))
        except Exception as exc:
            return _error(exc)

    @tool("tautulli_activity",
          "Current Plex activity from Tautulli: active stream count, transcodes, "
          "bandwidth, and who is watching what. Check before restarting Plex.", {})
    async def tautulli_activity(args):
        try:
            return _text(backend.tautulli_activity())
        except Exception as exc:
            return _error(exc)

    @tool("check_urls", "Check reachability of the public service URLs.", {})
    async def check_urls(args):
        try:
            return _text(backend.check_urls(config.public_urls))
        except Exception as exc:
            return _error(exc)

    @tool("list_dir", "List a directory with per-entry sizes (recursive for dirs).",
          {"path": str})
    async def list_dir(args):
        try:
            return _text(backend.list_dir(args["path"]))
        except Exception as exc:
            return _error(exc)

    @tool("delete_paths",
          "Delete files or directories (Tier 2 — destructive, requires owner approval; "
          "only allowed under the downloads tree).",
          {"paths": list, "reason": str})
    async def delete_paths(args):
        try:
            return _text(backend.delete_paths(list(args["paths"])))
        except Exception as exc:
            return _error(exc)

    @tool("write_report",
          "Write the final incident report. Call exactly once, at the end. "
          f"category must be one of: {sorted(report_mod.CATEGORIES)}. "
          "status: resolved | escalated | monitoring. markdown: sections "
          "'## Observed', '## Diagnosis', '## Actions taken', '## Outcome' "
          "(and '## Proposed actions' when anything is pending or dry-run).",
          {"title": str, "category": str, "status": str, "markdown": str})
    async def write_report(args):
        try:
            category = args["category"] if args["category"] in report_mod.CATEGORIES else "other"
            path = report_mod.write_incident_report(
                config, incident_id or 0, args["title"], category, args["status"], args["markdown"],
            )
            run_result.update({
                "category": category,
                "status": args["status"],
                "title": args["title"],
                "report_path": str(path),
            })
            if incident_id:
                store.set_report_path(incident_id, str(path))
            return _text(f"report written to {path}")
        except Exception as exc:
            return _error(exc)

    tools = [
        get_containers, container_logs, container_inspect, container_restart,
        docker_prune, disk_usage, du_summary, memory, list_torrents, remove_torrents,
        arr_queue, arr_blocklist_research, tautulli_activity, check_urls, list_dir,
        delete_paths, write_report,
    ]
    return create_sdk_mcp_server(name="warden", version="0.1.0", tools=tools)
