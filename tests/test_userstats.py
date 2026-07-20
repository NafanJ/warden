"""The Plex `user-stats` command (everyone vs one user, Plex + Tautulli merge)."""
from datetime import datetime, timezone

from warden.userstats import bucket_history, handle_userstats, merge_users

NOW = datetime.now(timezone.utc).timestamp()
DAY = 86400


class FakeBackend:
    """Tom is in both sources; Sarah is an invitee Plex knows and Tautulli doesn't."""

    PLEX_USERS = [
        {"account_id": 1, "name": "Tom"},
        {"account_id": 2, "name": "Benn"},
        {"account_id": 3, "name": "Sarah"},
    ]
    PLEX_HISTORY = [
        {"account_id": 1, "viewed_at": NOW - 0.5 * DAY, "seconds": 3600, "title": "A"},
        {"account_id": 1, "viewed_at": NOW - 3 * DAY, "seconds": 1800, "title": "B"},
        {"account_id": 1, "viewed_at": NOW - 40 * DAY, "seconds": 1800, "title": "C"},
        {"account_id": 3, "viewed_at": NOW - 2 * DAY, "seconds": 7200, "title": "D"},
    ]
    TAUTULLI_USERS = [
        {"user_id": 1, "name": "Tom", "plays": 54, "duration_seconds": 70941, "last_seen": None},
        {"user_id": 2, "name": "Benn", "plays": 46, "duration_seconds": 123641, "last_seen": None},
    ]
    STATS = {2: [{"days": 1, "plays": 2, "seconds": 5400},
                 {"days": 0, "plays": 46, "seconds": 123641}]}

    def plex_users(self):
        return self.PLEX_USERS

    def plex_history(self, account_id=None):
        if account_id is None:
            return self.PLEX_HISTORY
        return [r for r in self.PLEX_HISTORY if r["account_id"] == account_id]

    def tautulli_users(self):
        return self.TAUTULLI_USERS

    def tautulli_user_stats(self, user_id):
        return self.STATS.get(user_id, [])


def test_userstats_all_lists_everyone():
    out = handle_userstats("", FakeBackend())
    assert "all users" in out and "Tom" in out and "Benn" in out
    assert "Sources: Plex (primary) + Tautulli" in out


def test_invited_user_only_plex_knows_appears():
    out = handle_userstats("", FakeBackend())
    assert "Sarah" in out
    assert "2.0h" in out  # 7200s of Plex history, invisible to Tautulli


def test_userstats_all_keyword():
    assert "all users" in handle_userstats("all", FakeBackend())


def test_userstats_single_user_detail_from_plex_history():
    out = handle_userstats("sarah", FakeBackend())
    assert "Plex — Sarah" in out
    assert "Last 24h" in out and "All time" in out


def test_detail_falls_back_to_tautulli_when_plex_has_no_history():
    # Benn is in both rosters but has no Plex history rows.
    out = handle_userstats("benn", FakeBackend())
    assert "Plex — Benn" in out
    assert "46 plays" in out  # came from Tautulli's own buckets


def test_userstats_unknown_user_lists_known():
    out = handle_userstats("nobody", FakeBackend())
    assert "No Plex user matching" in out and "Tom" in out


def test_bucket_history_windows():
    rows = bucket_history(FakeBackend.PLEX_HISTORY[:3], now=NOW)
    by_days = {r["days"]: r for r in rows}
    assert by_days[1]["plays"] == 1        # only the 0.5d-old play
    assert by_days[7]["plays"] == 2        # + the 3d-old one
    assert by_days[30]["plays"] == 2       # 40d-old one falls outside
    assert by_days[0]["plays"] == 3        # all time
    assert by_days[0]["seconds"] == 7200


def test_merge_prefers_larger_source_without_double_counting():
    # Tom: Plex sees 3 plays, Tautulli remembers 54 from before Plex aged them out.
    merged = merge_users(FakeBackend.PLEX_USERS, FakeBackend.PLEX_HISTORY,
                         FakeBackend.TAUTULLI_USERS)
    tom = next(u for u in merged if u["name"] == "Tom")
    assert tom["plays"] == 54              # not 57 — sources overlap
    assert tom["user_id"] == 1 and tom["account_id"] == 1


def test_duplicate_plex_accounts_collapse_to_one_person():
    # Re-inviting someone mints a second accountID under the same name, with
    # history split across both (the real server has two "Robby" accounts).
    users = [{"account_id": 10, "name": "Robby"}, {"account_id": 11, "name": "Robby"}]
    history = [
        {"account_id": 10, "viewed_at": NOW - DAY, "seconds": 100, "title": "A"},
        {"account_id": 11, "viewed_at": NOW - DAY, "seconds": 200, "title": "B"},
    ]
    merged = merge_users(users, history, [])
    assert len(merged) == 1
    assert merged[0]["plays"] == 2 and merged[0]["duration_seconds"] == 300
    assert merged[0]["account_ids"] == [10, 11]


def test_aliases_join_one_person_across_sources():
    # The owner is "nafan_j" in Plex but "Nathan" in Tautulli — nothing in the
    # data links them, so without an alias they'd show up as two people.
    plex = [{"account_id": 1, "name": "nafan_j"}]
    history = [{"account_id": 1, "viewed_at": NOW - DAY, "seconds": 3600, "title": "A"}]
    taut = [{"user_id": 9, "name": "Nathan", "plays": 128, "duration_seconds": 240840,
             "last_seen": None}]
    assert len(merge_users(plex, history, taut)) == 2  # unaliased: two rows
    merged = merge_users(plex, history, taut, {"nafan_j": "Nathan"})
    assert len(merged) == 1
    assert merged[0]["name"] == "Nathan" and merged[0]["plays"] == 128


def test_merge_keeps_tautulli_only_users():
    # A revoked invite: Tautulli still remembers them, Plex no longer lists them.
    merged = merge_users([], [], FakeBackend.TAUTULLI_USERS)
    assert {u["name"] for u in merged} == {"Tom", "Benn"}


def test_plex_outage_degrades_to_tautulli():
    class NoPlex(FakeBackend):
        def plex_users(self):
            raise RuntimeError("plex unreachable")

    out = handle_userstats("", NoPlex())
    assert "Tom" in out and "Benn" in out
