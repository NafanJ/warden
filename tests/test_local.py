import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pwdlib import PasswordHash

from warden.agent.coder import queue_full_task, start_full_task
from warden.config import Config
from warden.local.api import create_app
from warden.local.db import Database, uid
from warden.local.knowledge import Knowledge
from warden.local.models import LocalModel
from warden.local.settings import LocalSettings
from warden.local.worker import process_run


class Model:
    def __init__(self, replies=None):
        self.replies = list(replies or [{"role": "assistant", "content": "Evidence reviewed. [1]"}])
        self.requests = []

    def embed(self, texts):
        return [[1.0] + [0.0] * 767 for _ in texts]

    def status(self):
        return {"online": True, "models": [], "chat_ready": True, "embeddings_ready": True}

    def chat(self, messages, tools, emit, check):
        check()
        self.requests.append(messages)
        reply = self.replies.pop(0)
        emit("token", {"text": reply.get("content", "")})
        return reply


class Backend:
    def __init__(self):
        self.restarts = []

    def container_restart(self, name):
        self.restarts.append(name)
        return "restarted " + name

    def container_inspect(self, name):
        return {"name": name, "running": True, "status": "running", "health": "healthy"}


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.delenv("WARDEN_OLLAMA_URL", raising=False)
    monkeypatch.setenv("WARDEN_KNOWLEDGE_ROOTS", str(tmp_path / "library"))
    (tmp_path / "library").mkdir()
    config = Config(engine="langgraph", mode="active", state_dir=tmp_path, incidents_dir=tmp_path / "incidents")
    settings = LocalSettings.from_config(config)
    db = Database(tmp_path / "warden.db")
    db.initialize()
    model, backend = Model(), Backend()
    knowledge = Knowledge(db, settings, model)
    return config, settings, db, model, backend, knowledge


def run_with_restart(local):
    _, _, db, _, _, _ = local
    conversation = db.conversation()
    return db.submit(conversation["id"], "Restart filebrowser", forced={"id": uid(), "name": "container_restart", "args": {"name": "filebrowser", "reason": "Not responding"}})


def process(local, run):
    config, settings, db, model, backend, knowledge = local
    process_run(db, settings, config, backend, model, knowledge, run["id"])


def test_approval_survives_restart_and_verifies_once(local):
    _, settings, db, _, backend, _ = local
    run = run_with_restart(local)
    process(local, run)
    assert backend.restarts == []
    assert db.one("SELECT status FROM runs WHERE id=?", (run["id"],))["status"] == "waiting"
    action = db.one("SELECT * FROM actions")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: db.decide(action["id"], True), range(2)))
    db.recover()
    process(local, run)
    process(local, run)
    assert backend.restarts == ["filebrowser"]
    action = db.one("SELECT * FROM actions")
    assert action["status"] == "executed"
    assert json.loads(action["verified_json"])["observed"]["health"] == "healthy"
    assert (settings.state / "graph.db").exists()


@pytest.mark.parametrize("decision", [False, "expired", "cancelled", "uncertain"])
def test_unapproved_or_uncertain_actions_never_execute(local, decision):
    _, _, db, _, backend, _ = local
    run = run_with_restart(local)
    process(local, run)
    action = db.one("SELECT * FROM actions")
    if decision == "expired":
        old = (datetime.now(timezone.utc) - timedelta(hours=13)).isoformat()
        db.execute("UPDATE actions SET created_at=?", (old,))
        db.decide(action["id"], True)
    elif decision == "cancelled":
        db.cancel(run["id"])
        db.decide(action["id"], True)
    elif decision == "uncertain":
        db.decide(action["id"], True)
        db.execute("UPDATE actions SET status='executing'")
        db.recover()
    else:
        db.decide(action["id"], False)
    process(local, run)
    assert backend.restarts == []


def test_tampered_approval_is_refused(local):
    _, _, db, _, backend, _ = local
    run = run_with_restart(local)
    process(local, run)
    action = db.one("SELECT * FROM actions")
    db.decide(action["id"], True)
    db.execute("UPDATE actions SET args_json=?", ('{"name":"plex"}',))
    process(local, run)
    assert not backend.restarts
    assert db.one("SELECT status FROM runs")["status"] == "failed"


def test_knowledge_updates_removes_and_rejects_outside_roots(local):
    _, settings, db, _, _, knowledge = local
    path = settings.roots[0] / "runbook.md"
    path.write_text("Plex buffering: inspect transcoding and disk utilization.")
    source = knowledge.add_source("Runbooks", "folder", settings.roots[0])
    knowledge.index(source["id"])
    found = knowledge.search("Plex buffering")
    assert found and found[0]["name"] == "runbook.md"
    assert found[0]["page"] == 1
    path.write_text("Sonarr queue repairs require owner approval.")
    knowledge.index(source["id"])
    assert not knowledge.search("buffering", semantic=False)
    assert knowledge.search("Sonarr", semantic=False)
    with pytest.raises(ValueError):
        knowledge.add_source("Everything", "folder", Path("/"))
    knowledge.remove(source["id"])
    assert path.exists()
    assert not knowledge.search("Sonarr")
    assert db.all("SELECT * FROM chunks") == []


def test_credentials_and_symlinks_are_not_indexed(local):
    _, settings, _, _, _, knowledge = local
    root = settings.roots[0]
    (root / ".env.txt").write_text("secret-token")
    (root / "secrets.txt").write_text("top-level-secret")
    (root / "credentials").mkdir()
    (root / "credentials" / "keys.txt").write_text("another-secret")
    (root / "linked.txt").symlink_to(root / ".env.txt")
    source = knowledge.add_source("Selected", "folder", root)
    knowledge.index(source["id"])
    assert knowledge.search("secret", semantic=False) == []


def test_conversation_retains_history(local):
    _, _, db, model, _, _ = local
    c = db.conversation()
    first = db.submit(c["id"], "Remember this question", "knowledge")
    process(local, first)
    model.replies.append({"role": "assistant", "content": "Following up."})
    second = db.submit(c["id"], "Continue", "knowledge")
    process(local, second)
    assert any("Remember this question" in m["content"] for m in model.requests[-1])
    assert len(db.all("SELECT * FROM messages")) == 4


def test_single_active_run_per_conversation(local):
    _, _, db, _, _, _ = local
    c = db.conversation()
    db.submit(c["id"], "First")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        db.submit(c["id"], "Second")


def test_cloud_full_tasks_are_disabled(local):
    config, _, _, _, _, _ = local
    config.enable_full_tasks = True
    assert "disabled" in queue_full_task("task", config, None, None)
    assert "disabled" in start_full_task({}, config, None, None)


def test_only_loopback_models_are_allowed(local, monkeypatch):
    monkeypatch.setenv("WARDEN_OLLAMA_URL", "https://example.com")
    with pytest.raises(ValueError):
        LocalSettings.from_config(local[0])


def test_api_auth_csrf_upload_and_source_access(local):
    config, _, db, model, backend, _ = local
    db.execute("INSERT INTO local_settings VALUES('password_hash',?)", (PasswordHash.recommended().hash("test-password"),))
    client = TestClient(create_app(config, backend, model))
    assert client.get("/api/conversations").status_code == 401
    assert client.post("/api/session", json={"password": "test-password"}).status_code == 403
    client.headers["Origin"] = "http://testserver"
    assert client.post("/api/session", json={"password": "test-password"}).status_code == 200
    created = client.post("/api/conversations", json={}).json()
    assert client.post(f"/api/conversations/{created['id']}/messages", json={"content": "Hello", "scope": "knowledge"}).status_code == 200
    assert client.post(f"/api/conversations/{created['id']}/messages", json={"content": "Again"}).status_code == 409
    assert client.post("/api/sources/upload", files={"file": ("bad.exe", b"bytes")}).status_code == 400
    source = client.post("/api/sources/upload", files={"file": ("notes.md", b"A personal note")})
    assert source.status_code == 200
    assert client.post("/api/sources/folder", json={"path": "/"}).status_code == 400
    assert client.get("/api/documents/missing").status_code == 404
    assert client.patch("/api/settings/routines", json={"download_throttling": False}).status_code == 200
    assert client.get("/api/settings").json()["routines"]["download_throttling"] is False
    assert client.delete("/api/session").status_code == 200
    assert client.get("/api/runs").status_code == 401


def test_local_model_cancels_before_first_token(local, monkeypatch):
    started, cancelled = False, False

    async def waiting(request):
        nonlocal started, cancelled
        started = True
        try:
            await asyncio.sleep(60)
        finally:
            cancelled = True
        return httpx.Response(200)

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(waiting), **kw))

    def check():
        if started:
            raise InterruptedError("Cancelled before response headers")

    with pytest.raises(InterruptedError, match="before response headers"):
        LocalModel(local[1]).chat([], [], lambda *args: None, check)
    assert cancelled


def test_local_model_streams_text_tools_and_usage(local, monkeypatch):
    call = {"function": {"name": "get_containers", "arguments": {}}}
    lines = [{"message": {"content": "Inspecting ", "tool_calls": [call]}},
             {"message": {"content": "containers."}, "done": True, "eval_count": 3}]
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="\n".join(json.dumps(row) for row in lines)))
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=transport, **kw))
    events = []
    result = LocalModel(local[1]).chat([], [], lambda kind, data: events.append((kind, data)))
    assert result == {"role": "assistant", "content": "Inspecting containers.", "tool_calls": [call]}
    assert events[-1] == ("usage", {"eval_count": 3})
