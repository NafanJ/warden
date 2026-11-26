"""Deploy the tested local console while retaining Warden's existing state."""
import json
import os
import pwd
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values, set_key

ROOT = Path(__file__).resolve().parents[1]
DEST = Path("/opt/warden")
UNITS = ["warden-ui.service", "warden-worker.service"]


def run(*args):
    subprocess.run(args, check=True)


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run with sudo and the project's Python environment")
    if not (ROOT / "web/dist/index.html").is_file():
        raise SystemExit("Build web/ before deployment")
    values = dotenv_values(DEST / ".env")
    state = Path(values.get("WARDEN_STATE_DIR") or "state")
    state = state if state.is_absolute() else DEST / state
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = DEST.parent / f"warden-backup-{stamp}"
    backup.mkdir(mode=0o700)
    shutil.copy2(DEST / ".env", backup / "environment")
    if (state / "warden.db").exists():
        with sqlite3.connect(state / "warden.db") as source:
            unfinished = source.execute("SELECT count(*) FROM actions WHERE status IN ('pending','approved','executing')").fetchone()[0]
            if unfinished:
                raise SystemExit(f"Resolve {unfinished} existing approvals before cutover; deployment has not changed running services")
            with sqlite3.connect(backup / "warden.db") as target:
                source.backup(target)
    # Keep the previous code for rollback without copying models, credentials or state.
    run("rsync", "-a", "--exclude=.venv", "--exclude=state", "--exclude=.env", "--exclude=.git", str(DEST) + "/", str(backup / "code") + "/")
    run("systemctl", "stop", "warden-sentinel.timer", "warden-summary.timer")
    for _ in range(90):
        active = subprocess.run(["systemctl", "is-active", "warden-sentinel.service", "warden-summary.service"], capture_output=True, text=True).stdout
        if not any(line in {"active", "activating"} for line in active.splitlines()):
            break
        time.sleep(2)
    else:
        run("systemctl", "start", "warden-sentinel.timer", "warden-summary.timer")
        raise SystemExit("A legacy job is still running; cutover postponed")
    for unit in ["warden-discord.service", "warden-webhook.service", *UNITS]:
        subprocess.run(["systemctl", "stop", unit], check=False)
    # Recheck after stopping every writer, then refresh the consistent state backup.
    if (state / "warden.db").exists():
        with sqlite3.connect(state / "warden.db") as source:
            unfinished = source.execute("SELECT count(*) FROM actions WHERE status IN ('pending','approved','executing')").fetchone()[0]
            if unfinished:
                run("systemctl", "start", "warden-discord.service", "warden-webhook.service", "warden-sentinel.timer", "warden-summary.timer")
                raise SystemExit(f"Resolve {unfinished} approvals created during cutover; legacy services restored")
            with sqlite3.connect(backup / "warden.db") as target:
                source.backup(target)
    try:
        run("rsync", "-a", "--exclude=.git", "--exclude=.venv", "--exclude=.env", "--exclude=.local.env",
            "--exclude=.local-state", "--exclude=state", "--exclude=node_modules", "--exclude=test-results", "--exclude=playwright-report",
            str(ROOT) + "/", str(DEST) + "/")
        owner = pwd.getpwnam("nathan")
        run("chown", "-R", "nathan:nathan", str(DEST))
        run("sudo", "-u", "nathan", "-H", "/home/nathan/.local/bin/uv", "pip", "sync", "--python", str(DEST / ".venv/bin/python"), str(DEST / "requirements.lock"))
        run("sudo", "-u", "nathan", "-H", "/home/nathan/.local/bin/uv", "pip", "install", "--python", str(DEST / ".venv/bin/python"), "--no-deps", "-e", str(DEST))
        for key, value in {"WARDEN_ENGINE": "langgraph", "LLM_PROVIDER": "ollama", "ENABLE_FULL_TASKS": "false",
                           "WARDEN_CHAT_MODEL": "qwen3.5:4b-q4_K_M", "WARDEN_EMBED_MODEL": "embeddinggemma:300m-qat-q8_0",
                           "LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false"}.items():
            set_key(DEST / ".env", key, value)
        os.chown(DEST / ".env", owner.pw_uid, owner.pw_gid)
        os.chmod(DEST / ".env", 0o600)
        for unit in UNITS:
            shutil.copy2(DEST / "deploy" / unit, Path("/etc/systemd/system") / unit)
        subprocess.run(["sudo", "-u", "nathan", str(DEST / ".venv/bin/python"), "-m", "warden.local.admin", "init"], cwd=DEST, check=True)
        run("systemctl", "daemon-reload")
        run("systemctl", "enable", "--now", *UNITS)
        run("systemctl", "start", "warden-webhook.service", "warden-discord.service", "warden-sentinel.timer", "warden-summary.timer")
    except BaseException:
        print(f"Cutover failed. Backup is at {backup}; cloud AI will not be re-enabled automatically.")
        raise
    print(json.dumps({"url": "http://192.168.1.101:8485", "backup": str(backup), "password_file": str(state / "owner-password.txt")}))


if __name__ == "__main__":
    main()
