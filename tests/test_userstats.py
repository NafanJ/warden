"""The Plex `user-stats` command (everyone vs one user)."""
from warden.userstats import handle_userstats


class FakeTautulli:
    USERS = [
        {"user_id": 1, "name": "Tom", "plays": 54, "duration_seconds": 70941, "last_seen": None},
        {"user_id": 2, "name": "Benn", "plays": 46, "duration_seconds": 123641, "last_seen": None},
    ]
    STATS = {1: [{"days": 1, "plays": 2, "seconds": 5400},
                 {"days": 7, "plays": 8, "seconds": 21600},
                 {"days": 0, "plays": 54, "seconds": 70941}]}

    def tautulli_users(self):
        return self.USERS

    def tautulli_user_stats(self, user_id):
        return self.STATS.get(user_id, [])


def test_userstats_all_lists_everyone():
    out = handle_userstats("", FakeTautulli())
    assert "all users" in out and "Tom" in out and "Benn" in out
    assert "54 plays" in out


def test_userstats_all_keyword():
    assert "all users" in handle_userstats("all", FakeTautulli())


def test_userstats_single_user_detail():
    out = handle_userstats("tom", FakeTautulli())  # case-insensitive substring
    assert "Plex — Tom" in out
    assert "Last 24h" in out and "All time" in out


def test_userstats_unknown_user_lists_known():
    out = handle_userstats("nobody", FakeTautulli())
    assert "No Plex user matching" in out and "Tom" in out
