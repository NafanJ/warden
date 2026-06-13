"""The torrent reaper: completed downloads get removed from Transmission
(data kept), but never one an *arr is still importing, and never in dry-run.
"""
from warden.backends.replay import ReplayBackend
from warden.sentinel.reaper import reap_completed_torrents


def _torrent(tid: int, name: str, done: float, h: str) -> dict:
    return {"id": tid, "name": name, "percentDone": done, "hashString": h,
            "peersConnected": 20, "status": 6}


def _backend(torrents, arr_queue=None):
    return ReplayBackend({"torrents": torrents, "arr_queue": arr_queue or {}})


def test_completed_untracked_torrent_is_removed_keeping_data(config, store, channel):
    backend = _backend([_torrent(1, "Done.Movie", 1.0, "ABC")])
    removed = reap_completed_torrents(config, backend, store, channel)
    assert removed == ["Done.Movie"]
    assert backend.actions_taken == [
        {"action": "remove_torrents", "ids": [1], "delete_data": False}
    ]


def test_incomplete_torrent_is_left_alone(config, store, channel):
    backend = _backend([_torrent(1, "Half.Movie", 0.5, "ABC")])
    assert reap_completed_torrents(config, backend, store, channel) == []
    assert backend.actions_taken == []


def test_completed_torrent_still_importing_is_kept(config, store, channel):
    # Radarr is mid-import (download_id == the torrent hash) — don't yank it.
    backend = _backend(
        [_torrent(1, "Importing.Movie", 1.0, "abc")],
        arr_queue={"radarr": [{"download_id": "ABC", "status": "completed"}]},
    )
    assert reap_completed_torrents(config, backend, store, channel) == []
    assert backend.actions_taken == []


def test_dry_run_removes_nothing(config, store, channel):
    config.mode = "dry-run"
    backend = _backend([_torrent(1, "Done.Movie", 1.0, "ABC")])
    assert reap_completed_torrents(config, backend, store, channel) == []
    assert backend.actions_taken == []


def test_disabled_via_config(config, store, channel):
    config.reap_completed = False
    backend = _backend([_torrent(1, "Done.Movie", 1.0, "ABC")])
    assert reap_completed_torrents(config, backend, store, channel) == []
    assert backend.actions_taken == []


def test_arr_unreachable_skips_reaping(config, store, channel):
    class FlakyArr(ReplayBackend):
        def arr_queue(self, app):
            raise RuntimeError("radarr unreachable")

    backend = FlakyArr({"torrents": [_torrent(1, "Done.Movie", 1.0, "ABC")]})
    # can't confirm it isn't mid-import, so be conservative and remove nothing
    assert reap_completed_torrents(config, backend, store, channel) == []
    assert backend.actions_taken == []


def test_removal_is_audited_as_tier1_autofix(config, store, channel):
    backend = _backend([_torrent(1, "Done.Movie", 1.0, "ABC")])
    reap_completed_torrents(config, backend, store, channel)
    rows = store.conn.execute(
        "SELECT tier, decision FROM audit WHERE tool='remove_torrents'").fetchall()
    assert [tuple(r) for r in rows] == [(1, "allowed")]
