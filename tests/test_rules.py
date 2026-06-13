import json
from pathlib import Path

from warden.config import Config
from warden.sentinel.rules import evaluate

FIXTURES = Path(__file__).parent.parent / "evals" / "fixtures"


def cfg(**kwargs) -> Config:
    return Config(disk_threshold_pct=92, stall_threshold_hours=4,
                  stall_min_age_hours=1, **kwargs)


def snapshot_of(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())["snapshot"]


def test_container_down_detected():
    anomalies = evaluate(snapshot_of("container-down-filebrowser"), cfg())
    keys = [a.key for a in anomalies]
    assert "container_down:filebrowser" in keys


def test_ignored_containers_skipped():
    anomalies = evaluate(snapshot_of("container-down-filebrowser"),
                         cfg(ignored_containers=["filebrowser"]))
    assert not [a for a in anomalies if a.category == "container_down"]


def test_disk_pressure_detected():
    anomalies = evaluate(snapshot_of("disk-pressure"), cfg())
    disk = [a for a in anomalies if a.category == "disk_pressure"]
    assert len(disk) == 1
    assert disk[0].key == "disk_pressure:/mnt/Modi"


def test_stall_detected_but_not_healthy_torrent():
    anomalies = evaluate(snapshot_of("stalled-torrent"), cfg())
    stalls = [a for a in anomalies if a.category == "stalled_download"]
    assert len(stalls) == 1
    assert "Some.Show" in stalls[0].summary


def test_stall_detected_with_peers_but_idle():
    snap = snapshot_of("stalled-torrent")
    for t in snap["torrents"]:
        if t["name"].startswith("Healthy"):
            t["inactive_hours"] = 6.0  # has peers, but no movement past the threshold
    stalls = [a for a in evaluate(snap, cfg()) if a.category == "stalled_download"]
    assert len(stalls) == 2  # the 0-peer one AND the idle-with-peers one
    assert any("peers but no movement" in a.summary for a in stalls)


def test_idle_with_peers_below_threshold_not_flagged():
    snap = snapshot_of("stalled-torrent")
    for t in snap["torrents"]:
        if t["name"].startswith("Healthy"):
            t["inactive_hours"] = 1.0  # idle, but under the 4h threshold
    stalls = [a for a in evaluate(snap, cfg()) if a.category == "stalled_download"]
    assert len(stalls) == 1  # only the genuinely stalled 0-peer torrent


def test_fresh_torrent_not_flagged():
    snap = snapshot_of("stalled-torrent")
    for t in snap["torrents"]:
        t["age_hours"] = 0.2  # younger than min age
    anomalies = evaluate(snap, cfg())
    assert not [a for a in anomalies if a.category == "stalled_download"]


def test_mount_dropped_detected():
    snap = {"mount_health": [{"path": "/mnt/Modi", "mounted": False,
                              "accessible": False, "read_only": False, "error": "gone"}]}
    anomalies = evaluate(snap, cfg())
    m = [a for a in anomalies if a.category == "disk_unavailable"]
    assert len(m) == 1 and "NOT MOUNTED" in m[0].summary


def test_mount_readonly_detected():
    snap = {"mount_health": [{"path": "/mnt/Modi", "mounted": True,
                              "accessible": True, "read_only": True, "error": None}]}
    assert [a for a in evaluate(snap, cfg()) if a.category == "disk_unavailable"
            and "READ-ONLY" in a.summary]


def test_healthy_mount_not_flagged():
    snap = {"mount_health": [{"path": "/", "mounted": True, "accessible": True,
                              "read_only": False, "error": None}]}
    assert not [a for a in evaluate(snap, cfg()) if a.category == "disk_unavailable"]


def test_tunnel_down_detected():
    snap = {"url_checks": [{"url": "https://plex.example.com", "ok": False, "error": "timeout"}]}
    anomalies = evaluate(snap, cfg())
    assert [a for a in anomalies if a.category == "tunnel_down"]


def test_green_snapshot_no_anomalies():
    snap = {
        "docker_ps": [{"name": "plex", "state": "running", "status": "Up 3 days"}],
        "disk_usage": [{"path": "/", "used_pct": 11.0, "free_gb": 400, "total_gb": 500, "used_gb": 50}],
        "torrents": [],
        "arr_queue": {"sonarr": [], "radarr": []},
        "url_checks": [{"url": "https://plex.example.com", "ok": True, "status": 200}],
    }
    assert evaluate(snap, cfg()) == []
