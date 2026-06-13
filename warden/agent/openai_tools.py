"""OpenAI function-calling schemas for warden's 15 tools, plus a dispatcher.

Same tools, same Backend, same behaviour as the Claude SDK server in tools.py —
only the wire format differs. Execution is gated by tiers.decide_tool *before*
the dispatcher is ever called, so this module just maps a tool name to its
Backend method.
"""
from __future__ import annotations

import json
from typing import Any

from warden.agent import report as report_mod
from warden.backends import Backend
from warden.config import Config
from warden.store import Store


def _fn(name: str, description: str, properties: dict | None = None,
        required: list[str] | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": required or [],
                "additionalProperties": False,
            },
        },
    }


TOOL_SCHEMAS: list[dict] = [
    _fn("get_containers", "List all Docker containers with state and status."),
    _fn("container_logs", "Tail recent logs from a container.",
        {"name": {"type": "string"}, "lines": {"type": "integer", "default": 100}}, ["name"]),
    _fn("container_inspect",
        "Inspect a container: state, exit code, OOM flag, restart count/policy, memory limit.",
        {"name": {"type": "string"}}, ["name"]),
    _fn("container_restart", "Restart a container (Tier 1, reversible).",
        {"name": {"type": "string"}}, ["name"]),
    _fn("disk_usage", "Disk usage for the monitored mount points."),
    _fn("du_summary", "Directory size breakdown (du) for a path.",
        {"path": {"type": "string"}, "depth": {"type": "integer", "default": 1}}, ["path"]),
    _fn("memory", "Host memory and swap state."),
    _fn("list_torrents", "List torrents in Transmission with progress, peers, and inactivity."),
    _fn("remove_torrents",
        "Remove torrents from Transmission by id (Tier 1 without data; "
        "delete_data=true is destructive and needs approval).",
        {"ids": {"type": "array", "items": {"type": "integer"}},
         "delete_data": {"type": "boolean", "default": False}}, ["ids"]),
    _fn("arr_queue", "Read the Sonarr or Radarr download queue.",
        {"app": {"type": "string", "enum": ["sonarr", "radarr"]}}, ["app"]),
    _fn("arr_blocklist_research",
        "Blocklist queue items in Sonarr/Radarr and remove from the download client; "
        "the *arr app re-searches automatically (Tier 1).",
        {"app": {"type": "string", "enum": ["sonarr", "radarr"]},
         "queue_ids": {"type": "array", "items": {"type": "integer"}}}, ["app", "queue_ids"]),
    _fn("check_urls", "Check reachability of the public service URLs."),
    _fn("list_dir", "List a directory with per-entry sizes (recursive for dirs).",
        {"path": {"type": "string"}}, ["path"]),
    _fn("delete_paths",
        "Delete files or directories (Tier 2 — destructive, requires owner approval; "
        "only allowed under the downloads tree).",
        {"paths": {"type": "array", "items": {"type": "string"}}, "reason": {"type": "string"}},
        ["paths", "reason"]),
    _fn("write_report",
        "Write the final incident report. Call exactly once, at the end. markdown must "
        "have sections '## Observed', '## Diagnosis', '## Actions taken', '## Outcome' "
        "(and '## Proposed actions' when anything is pending or dry-run).",
        {"title": {"type": "string"},
         "category": {"type": "string", "enum": sorted(report_mod.CATEGORIES)},
         "status": {"type": "string", "enum": ["resolved", "escalated", "monitoring"]},
         "markdown": {"type": "string"}},
        ["title", "category", "status", "markdown"]),
]


def _stringify(payload: Any) -> str:
    if not isinstance(payload, str):
        payload = json.dumps(payload, indent=2, default=str)
    return payload[:40000]


def execute_tool(name: str, args: dict[str, Any], *, backend: Backend, config: Config,
                 store: Store, incident_id: int | None, run_result: dict[str, Any]) -> str:
    """Run a tool that has already passed the tier gate. Returns the tool-message
    content string (errors are returned as text so the model can react)."""
    try:
        if name == "get_containers":
            return _stringify(backend.docker_ps())
        if name == "container_logs":
            return _stringify(backend.container_logs(args["name"], int(args.get("lines", 100))))
        if name == "container_inspect":
            return _stringify(backend.container_inspect(args["name"]))
        if name == "container_restart":
            return _stringify(backend.container_restart(args["name"]))
        if name == "disk_usage":
            return _stringify(backend.disk_usage(config.disk_paths))
        if name == "du_summary":
            return _stringify(backend.du_summary(args["path"], int(args.get("depth", 1))))
        if name == "memory":
            return _stringify(backend.memory())
        if name == "list_torrents":
            return _stringify(backend.torrents())
        if name == "remove_torrents":
            return _stringify(backend.remove_torrents(list(args["ids"]), bool(args.get("delete_data", False))))
        if name == "arr_queue":
            return _stringify(backend.arr_queue(args["app"]))
        if name == "arr_blocklist_research":
            return _stringify(backend.arr_blocklist_and_research(args["app"], list(args["queue_ids"])))
        if name == "check_urls":
            return _stringify(backend.check_urls(config.public_urls))
        if name == "list_dir":
            return _stringify(backend.list_dir(args["path"]))
        if name == "delete_paths":
            return _stringify(backend.delete_paths(list(args["paths"])))
        if name == "write_report":
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
            return _stringify(f"report written to {path}")
        return f"ERROR: unknown tool {name}"
    except Exception as exc:
        return f"ERROR: {exc}"
