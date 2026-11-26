"""End-of-day digest across all services, posted once a day (21:00 via a timer).

Deterministic and free by default — a single live snapshot plus store queries.
Current alerts come from live checks; historical records are labelled separately.

Run:  python -m warden.summary
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from warden.backends import Backend
from warden.backends.live import LiveBackend
from warden.config import Config, load_config
from warden.notifier import Channel, get_channel
from warden.sentinel.collectors import collect_snapshot
from warden.sentinel.rules import evaluate, watched
from warden.store import Store, APPROVAL_TTL_HOURS


def _human_size(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _is_stalled(t: dict[str, Any], config: Config) -> bool:
    """Same definition the sentinel's stall rule uses: an incomplete torrent old
    enough to judge, with no piece activity for the threshold window."""
    inactive = t.get("inactive_hours")
    return ((t.get("percentDone") or 0) < 1.0
            and (t.get("age_hours") or 0) >= config.stall_min_age_hours
            and inactive is not None and inactive >= config.stall_threshold_hours)


def categorize_downloads(torrents: list[dict[str, Any]], config: Config) -> dict[str, list[dict]]:
    """Split torrents into todo / downloading / seeding / stalled, each a list of
    {name, size, pct, idle} for rendering."""
    buckets: dict[str, list[dict]] = {"downloading": [], "stalled": [], "seeding": [], "todo": []}
    for t in torrents:
        done = (t.get("percentDone") or 0) >= 1.0
        if not done and _is_stalled(t, config):
            key = "stalled"
        elif done:
            key = "seeding"
        elif t.get("status") == 4:  # Transmission status 4 = downloading
            key = "downloading"
        else:  # queued / paused / checking — not yet actively downloading or done
            key = "todo"
        buckets[key].append({
            "name": t.get("name") or "?",
            "size": t.get("totalSize") or 0,
            "pct": round((t.get("percentDone") or 0) * 100),
            "idle": t.get("inactive_hours"),
        })
    return buckets


# label + per-item suffix renderer for each bucket, in display order
_BUCKETS = [
    ("downloading", "⬇️ downloading", lambda i: f" · {i['pct']}%"),
    ("stalled", "⚠️ stalled", lambda i: f" · {i['pct']}%" + (f" · {i['idle']}h idle" if i.get("idle") is not None else "")),
    ("seeding", "🌱 seeding", lambda i: ""),
    ("todo", "⏳ todo", lambda i: ""),
]


def _downloads_lines(g: dict[str, Any]) -> list[str]:
    buckets = (g.get("downloads") or {}).get("buckets") or {}
    total = sum(len(v) for v in buckets.values())
    total_size = sum(i["size"] for v in buckets.values() for i in v)
    head = f"Downloads:   {total} torrent(s)" + (f" · {_human_size(total_size)}" if total else "")
    lines = [head]
    for key, label, suffix in _BUCKETS:
        items = buckets.get(key) or []
        if not items:
            continue
        bsize = _human_size(sum(i["size"] for i in items))
        lines.append(f"  {label} ({len(items)} · {bsize})")
        for i in items[:6]:
            name = i["name"] if len(i["name"]) <= 48 else i["name"][:47] + "…"
            lines.append(f"     • {name} — {_human_size(i['size'])}{suffix(i)}")
        if len(items) > 6:
            lines.append(f"     …and {len(items) - 6} more")
    return lines


def gather(config: Config, backend: Backend, store: Store, hours: int = 24) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=hours)).isoformat()
    snapshot = collect_snapshot(backend, config)
    anomalies = evaluate(snapshot, config)

    # Counts run over the watched population only: ignored names and finished
    # one-shot jobs (e.g. affine_migration_job, Exited(0) by design) are out, so
    # a healthy host reads "N/N up", never "N/N+1".
    containers = watched(snapshot.get("docker_ps") or [], config)
    up = sum(1 for c in containers if (c.get("state") or "").lower() == "running")
    down = [c.get("name") for c in containers
            if (c.get("state") or "").lower() != "running"]

    plex = {"stream_count": 0, "transcode_count": 0, "bandwidth_mbps": 0.0, "sessions": []}
    if config.tautulli_api_key:
        try:
            plex = backend.tautulli_activity()
        except Exception:
            pass

    def q(sql: str, *a) -> list[dict]:
        return [dict(r) for r in store.conn.execute(sql, a).fetchall()]

    incidents = q("SELECT * FROM incidents WHERE opened_at >= ? AND category != 'query' "
                  "ORDER BY id", since)
    actions = q("SELECT * FROM actions WHERE created_at >= ? OR decided_at >= ? ORDER BY id", since, since)
    tier1 = q("SELECT * FROM audit WHERE ts >= ? AND decision='allowed' AND tier >= 1", since)

    return {
        # at-a-glance live state (used by both views)
        "date": now.astimezone().strftime("%a %d %b"),
        "now": now.astimezone().strftime("%d %b %H:%M"),
        "containers_up": up,
        "containers_total": len(containers),
        "containers_down": [d for d in down if d],
        "disk": snapshot.get("disk_usage") or [],
        "disk_threshold": config.disk_threshold_pct,
        "torrents": len(snapshot.get("torrents") or []),
        "downloads": {"buckets": categorize_downloads(snapshot.get("torrents") or [], config)},
        "stalled": sum(1 for a in anomalies if a.category == "stalled_download"),
        "collector_errors": snapshot.get("collector_errors") or {},
        "tautulli": bool(config.tautulli_api_key),
        "plex": plex,
        # real-time (for `status`): what the sentinel would flag right now + waiting on you
        "anomalies": [{"category": a.category, "summary": a.summary} for a in anomalies],
        "pending": [dict(r) for r in
                    store.conn.execute("SELECT * FROM actions WHERE status='pending' AND created_at >= ? ORDER BY id",
                                       ((now - timedelta(hours=APPROVAL_TTL_HOURS)).isoformat(),))],
        # last-24h rollup (for the daily summary)
        "incidents": incidents,
        "actions": actions,
        "tier1": tier1,
        "cost": round(sum((i.get("cost_usd") or 0) for i in incidents), 4),
        "unresolved": store.unresolved_incidents(),
    }


def _action_label(row: dict[str, Any]) -> str:
    """A short, owner-readable phrase for one Tier-1 auto-fix (an audit row)."""
    tool = row.get("tool") or "action"
    try:
        args = json.loads(row.get("args_json") or "{}")
    except (ValueError, TypeError):
        args = {}
    if tool == "container_restart":
        return f"restarted {args.get('name', '?')}"
    if tool == "docker_prune":
        return "pruned Docker images / build cache"
    if tool == "remove_torrents":
        return "cleared a completed torrent"
    if tool == "arr_blocklist_research":
        return f"blocklisted + re-searched in {args.get('app', '*arr')}"
    if tool == "delete_paths":
        return f"deleted {len(args.get('paths') or [])} path(s)"
    detail = ", ".join(f"{k}={v}" for k, v in args.items())
    return f"{tool}({detail})" if detail else tool


def _autofix_lines(g: dict[str, Any]) -> list[str]:
    """Bulleted recap of the Tier-1 fixes warden made on its own, identical
    fixes collapsed into a single ×N bullet."""
    counts = Counter(_action_label(a) for a in g["tier1"])
    return [f"  • {label}" + (f" ×{n}" if n > 1 else "") for label, n in counts.items()]


def _disk_line(g: dict[str, Any]) -> str:
    warn_at = g.get("disk_threshold", 92)
    return "  ·  ".join(
        f"{d['path']} {d['used_pct']}%" + (" ⚠️" if d.get("used_pct", 0) >= warn_at else "")
        for d in g["disk"]
    ) or "n/a"


def format_status(g: dict[str, Any]) -> str:
    """Real-time health: current state + what the sentinel would flag *now* +
    anything waiting on the owner. (The daily rollup lives in format_summary.)"""
    down = g["containers_down"]
    lines = [
        f"📡 **warden status — {g['now']}**",
        f"Containers:  {g['containers_up']}/{g['containers_total']} up"
        + (f"  ⚠️ down: {', '.join(down)}" if down else ""),
        f"Disk:        {_disk_line(g)}",
    ]
    lines += _downloads_lines(g)
    if g.get("tautulli"):
        p = g["plex"]
        head = f"{p['stream_count']} stream(s)"
        if p["transcode_count"]:
            head += f", {p['transcode_count']} transcoding"
        if p["stream_count"]:
            head += f" · {p['bandwidth_mbps']} Mbps"
        lines.append(f"Plex:        {head}")
        for s in p["sessions"][:4]:
            tag = " (paused)" if s.get("state") == "paused" else ""
            lines.append(f"  ▶ {s.get('user')}: {s.get('title')}{tag}")
    lines.append("")
    if g["anomalies"]:
        lines.append(f"**Active issues ({len(g['anomalies'])}):**")
        lines += [f"  ⚠️ {a['summary']}" for a in g["anomalies"][:8]]
    else:
        lines.append("**Active issues:** none — all clear ✅")
    if g["pending"]:
        lines.append(f"**Awaiting your ✅ ({len(g['pending'])}):**")
        lines += [f"  • #{a['id']} {a['description']}" for a in g["pending"][:4]]
    if g["collector_errors"]:
        lines.append(f"⚠️ monitoring gap: {', '.join(g['collector_errors'])} unreachable")
    return "\n".join(lines)


def is_notable(g: dict[str, Any]) -> bool:
    """Did anything happen worth narrating? (vs. a fully quiet day)."""
    return bool(g["incidents"] or g["tier1"] or g["actions"])


def _daily_attention(g: dict[str, Any]) -> list[str]:
    # Incident records are history, not evidence of a condition still existing.
    lines = list(dict.fromkeys(a['summary'] for a in g.get('anomalies', [])))
    for action in g.get('pending', []):
        lines.append(f"Approval #{action['id']}: {action.get('description') or action['tool']}")
    if g.get('collector_errors'):
        lines.append('Could not check: ' + ', '.join(g['collector_errors']) + '. Health is incomplete.')
    return lines


def _daily_headline(g: dict[str, Any]) -> str:
    errors = g.get('collector_errors') or {}
    total, up = g['containers_total'], g['containers_up']
    if 'docker_ps' in errors or not total:
        state = 'Container status could not be confirmed.'
    elif up == total:
        state = f'All {total} containers are running.'
    else:
        state = f'{up} of {total} containers are running.'
    issues, pending = len(g.get('anomalies', [])), len(g.get('pending', []))
    if issues or pending:
        parts = []
        if issues:
            parts.append(f"{issues} current alert{'s' if issues != 1 else ''}")
        if pending:
            parts.append(f"{pending} approval{'s' if pending != 1 else ''} waiting")
        return state + ' ' + '; '.join(parts) + '.'
    if errors or not total:
        return state + ' Some health checks are unavailable; no approvals waiting.'
    return state + ' No current alerts or approvals waiting.'


def _daily_activity(g: dict[str, Any]) -> list[str]:
    lines = []
    # An "allowed" audit entry is written BEFORE execution. Never call it a fix.
    n = len(g.get('tier1', []))
    if n:
        lines.append(f"{n} automatic action{'s' if n != 1 else ''} authorised; outcomes not recorded in the audit log.")
    for action in g.get('actions', []):
        status = action.get('status')
        if status not in {'executed', 'failed', 'denied', 'expired', 'approved'}:
            continue
        label = {'executed': 'Recorded complete', 'failed': 'Failed', 'denied': 'Declined',
                 'expired': 'Expired', 'approved': 'Approved; completion not recorded'}[status]
        detail = action.get('description') or action.get('tool', 'action')
        lines.append(f"{label}: #{action['id']} {detail}")
    n = len(g.get('incidents', []))
    if n:
        lines.append(f"{n} new incident record{'s' if n != 1 else ''}; current conditions are shown above.")
    return lines


def _daily_sections(g: dict[str, Any]) -> list[tuple[str, list[str]]]:
    sections = []
    attention = _daily_attention(g)
    if attention:
        sections.append(('Needs attention', attention))
    activity = _daily_activity(g)
    if activity:
        sections.append(('Last 24 hours · recorded activity', activity))
    storage = []
    for d in sorted(g.get('disk', []), key=lambda d: d.get('used_pct', 0), reverse=True):
        label = 'System drive' if d['path'] == '/' else f"Media drive ({d['path']})" if d['path'] == '/mnt/Modi' else d['path']
        free = f"{d['free_gb']:.0f} GB free · " if d.get('free_gb') is not None else ''
        storage.append(f"{label}: {free}{d['used_pct']:.1f}% used")
    if storage:
        sections.append(('Storage', storage))
    # Routine seeding is not daily news. Stalls already appear in live alerts.
    buckets = g.get('downloads', {}).get('buckets', {})
    downloads = []
    for key, label in [('downloading', 'Downloading'), ('todo', 'Queued / paused / checking')]:
        for item in buckets.get(key, []):
            downloads.append(f"{label}: {item['name']} · {item['pct']}%")
    if downloads:
        sections.append(('Downloads in progress', downloads))
    if g.get('cost', 0) > 0:
        sections.append(('Recorded incident cost', [f"${g['cost']:.4f}"]))
    return sections


def _daily_field(lines: list[str]) -> str:
    # Keep complete bullets within Discord's field cap, reserving an overflow note.
    kept = []
    for line in lines[:6]:
        bullet = '• ' + (line if len(line) <= 260 else line[:257] + '…')
        if len('\n'.join(kept + [bullet])) > 950:
            break
        kept.append(bullet)
    if len(lines) > len(kept):
        kept.append(f"…and {len(lines) - len(kept)} more. Use /admin-status for live detail.")
    return '\n'.join(kept)


def format_summary(g: dict[str, Any], label: str = 'daily') -> str:
    lines = [f"**Warden {label} · {g['date']}**", _daily_headline(g)]
    for title, items in _daily_sections(g):
        lines.extend(['', f'**{title}**', _daily_field(items)])
    return '\n'.join(lines)


_GREEN, _AMBER, _RED = 0x3E8E5F, 0xC9A227, 0xB3452E


def build_embed(g: dict[str, Any], label: str = 'daily',
                note: str | None = None) -> dict[str, Any]:
    # Ignore legacy narrative: it could turn historical incidents into live claims.
    categories = {a['category'] for a in g.get('anomalies', [])}
    severe = bool(g.get('containers_down')) or bool(categories & {'container_down', 'disk_pressure', 'disk_unavailable', 'tunnel_down'})
    needs = bool(_daily_attention(g)) or not g['containers_total']
    return {
        'title': f"Warden {label} · {g['date']}",
        'description': _daily_headline(g),
        'color': _RED if severe else _AMBER if needs else _GREEN,
        'fields': [{'name': title, 'value': _daily_field(items), 'inline': False}
                   for title, items in _daily_sections(g)],
        'footer': {'text': f"Health checked {g['now']} · activity covers the last 24 hours"},
    }


def narrate(g: dict[str, Any], config: Config) -> str | None:
    """A short, plain-language recap — only called on notable days."""
    if config.engine == "langgraph":
        from warden.local.service import submit_external
        facts = {k: g[k] for k in ("incidents", "tier1", "actions", "unresolved")}
        submit_external(config, "Recap today's server activity in 2-3 sentences using only these facts:\n"
                        + json.dumps(facts, default=str)[:6000], origin="summary", scope="knowledge")
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=config.openai_api_key)
        facts = {k: g[k] for k in ("incidents", "tier1", "actions", "unresolved")}
        resp = client.chat.completions.create(
            model=config.openai_model, temperature=0.3, max_tokens=180,
            messages=[
                {"role": "system", "content":
                    "You are warden, an ops agent for a home media server. In 2-3 calm, "
                    "specific plain sentences, recap today's activity for the owner. No headers."},
                {"role": "user", "content": str(facts)[:6000]},
            ],
        )
        return (resp.choices[0].message.content or "").strip() or None
    except Exception:
        return None  # narrative is best-effort; the deterministic digest still posts


def main() -> int:
    config = load_config()
    backend = LiveBackend(config)
    store = Store(config.state_dir / "warden.db")
    g = gather(config, backend, store)
    channel = get_channel(config)
    if hasattr(channel, "send_embed"):  # Discord gets the rich digest
        channel.send_embed(build_embed(g))
    else:  # WhatsApp / log fall back to the plain-text rendering
        text = format_summary(g)
        channel.send(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
