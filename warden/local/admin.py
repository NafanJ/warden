"""Initialize owner access and export the public API schema."""
import argparse
import json
import secrets
from pathlib import Path

from pwdlib import PasswordHash

from warden.config import load_config
from warden.local.db import Database
from warden.local.settings import LocalSettings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init", "schema"])
    parser.add_argument("--output")
    args = parser.parse_args()
    config = load_config()
    settings = LocalSettings.from_config(config)
    settings.state.mkdir(parents=True, exist_ok=True)
    db = Database(settings.state / "warden.db")
    db.initialize()
    if args.command == "schema":
        from warden.local.api import create_app
        Path(args.output or "web/openapi.json").write_text(json.dumps(create_app(config).openapi(), indent=2))
        return
    if not db.one("SELECT 1 FROM local_settings WHERE key='password_hash'"):
        password = secrets.token_urlsafe(18)
        db.execute("INSERT INTO local_settings VALUES('password_hash',?)", (PasswordHash.recommended().hash(password),))
        target = settings.state / "owner-password.txt"
        target.touch(mode=0o600)
        target.write_text(password + "\n")
        target.chmod(0o600)
        print(f"Owner password created in {target}")
    else:
        print("Owner password is already configured")
    (settings.state / "knowledge").mkdir(exist_ok=True)
    config.incidents_dir.mkdir(parents=True, exist_ok=True)
    if not db.one("SELECT 1 FROM sources WHERE kind='reports'"):
        from warden.local.knowledge import Knowledge
        from warden.local.models import LocalModel
        Knowledge(db, settings, LocalModel(settings)).add_source("Warden incident reports", "reports", config.incidents_dir, "Server")


if __name__ == "__main__":
    main()
