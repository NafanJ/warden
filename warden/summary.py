"""End-of-day digest across all services, posted once a day (21:00 via a timer).

Deterministic and free by default — a single live snapshot plus store queries.
Only spends on a short gpt-4o-mini narrative when something actually happened.

Run:  python -m warden.summary
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from warden.backends import Backend
from warden.backends.live import LiveBackend
from warden.config import Config, load_config
from warden.notifier import Channel, get_channel
from warden.sentinel.collectors import collect_snapshot
from warden.sentinel.rules import evaluate
from warden.store import Store


def gather(config: Config, backend: Backend, store: Store, hours: int = 24) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=hours)).isoformat()
    snapshot = collect_snapshot(backend, config)
    anomalies = evaluate(snapshot, config)

    containers = snapshot.get("docker_ps") or []
    up = sum(1 for c in containers if (c.get("state") or "").lower() == "running")

    def q(sql: str, *a) -> list[dict]:
        return [dict(r) for r in store.conn.execute(sql, a).fetchall()]

    incidents = q("SELECT * FROM incidents WHERE opened_at >= ? ORDER BY id", since)
    actions = q("SELECT * FROM actions WHERE created_at >= ? ORDER BY id", since)
    tier1 = q("SELECT * FROM audit WHERE ts >= ? AND decision='allowed' AND tier >= 1", since)

    return {
        "date": now.astimezone().strftime("%a %d %b"),
        "containers_up": up,
        "containers_total": len(containers),
        "disk": snapshot.get("disk_usage") or [],
        "torrents": len(snapshot.get("torrents") or []),
        "stalled": sum(1 for a in anomalies if a.category == "stalled_download"),
        "incidents": incidents,
        "actions": actions,
        "tier1": tier1,
        "cost": round(sum((i.get("cost_usd") or 0) for i in incidents), 4),
        "unresolved": store.unresolved_incidents(),
        "collector_errors": snapshot.get("collector_errors") or {},
    }


def is_notable(g: dict[str, Any]) -> bool:
    """Did anything happen worth narrating? (vs. a fully quiet day)."""
    return bool(g["incidents"] or g["tier1"] or g["actions"])


def format_summary(g: dict[str, Any]) -> str:
    disk = "  ·  ".join(
        f"{d['path']} {d['used_pct']}%" + (" ⚠️" if d.get("used_pct", 0) >= 92 else "")
        for d in g["disk"]
    ) or "n/a"
    lines = [
        f"📊 **warden daily — {g['date']}**",
        f"Containers:  {g['containers_up']}/{g['containers_total']} up",
        f"Disk:        {disk}",
        f"Downloads:   {g['torrents']} torrent(s), {g['stalled']} stalled",
        "",
        f"Today:       {len(g['incidents'])} incident(s)"
        + (f" — {', '.join(sorted({i['category'] for i in g['incidents']}))}" if g["incidents"] else ""),
        f"Actions:     {len(g['tier1'])} auto-fix(es) · {len(g['actions'])} approval(s) requested",
        f"Agent cost:  ${g['cost']:.2f}",
    ]
    needs = [f"  • {i['summary']}" for i in g["unresolved"][:6]]
    if g["collector_errors"]:
        needs.append(f"  • monitoring gap: {', '.join(g['collector_errors'])} unreachable")
    if needs:
        lines += ["", "⚠️ **Needs you:**", *needs]
    return "\n".join(lines)


def narrate(g: dict[str, Any], config: Config) -> str | None:
    """A short, plain-language recap — only called on notable days."""
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
    text = format_summary(g)
    if is_notable(g):
        note = narrate(g, config)
        if note:
            text += "\n\n" + note
    get_channel(config).send(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
