from warden.local.db import Database, uid
from warden.local.settings import LocalSettings


def submit_external(config, prompt, origin="discord", scope="server", incident_id=None, forced=None):
    settings = LocalSettings.from_config(config)
    db = Database(settings.state / "warden.db")
    db.initialize()
    if incident_id:
        existing = db.one("SELECT * FROM runs WHERE incident_id=? AND status IN ('queued','running','waiting')", (incident_id,))
        if existing:
            return existing
    conversation = db.conversation(prompt[:80], scope)
    if forced:
        forced = {**forced, "id": uid()}
    return db.submit(conversation["id"], prompt, scope, origin, incident_id, forced)
