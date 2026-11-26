from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pwdlib import PasswordHash
from pydantic import BaseModel, Field

from warden.agent.report import redact
from warden.backends.live import LiveBackend
from warden.config import Config, load_config
from warden.local.db import Database, now, uid
from warden.local.knowledge import ALLOWED, MAX_BYTES, Knowledge
from warden.local.models import LocalModel
from warden.local.settings import LocalSettings
from warden.sentinel.collectors import collect_snapshot


class Conversation(BaseModel):
    id: str
    title: str
    scope: str
    archived: int
    created_at: str
    updated_at: str


class Run(BaseModel):
    id: str
    conversation_id: str
    status: str
    prompt: str
    scope: str
    origin: str
    incident_id: int | None = None
    error: str | None = None
    created_at: str
    updated_at: str


class RunEvent(BaseModel):
    id: int
    run_id: str
    kind: str
    data_json: str
    created_at: str


class Approval(BaseModel):
    id: int
    tool: str
    args_json: str
    status: str
    description: str | None = None
    run_id: str | None = None
    result: str | None = None
    verified_json: str | None = None
    created_at: str


class Source(BaseModel):
    id: str
    name: str
    kind: str
    collection: str
    status: str
    error: str | None = None
    indexed_at: str | None = None


class Citation(BaseModel):
    id: int
    document_id: str
    page: int
    content: str
    name: str
    hash: str
    collection: str
    source_id: str


class ServerSnapshot(BaseModel):
    collected_at: str
    snapshot: dict


class Login(BaseModel):
    password: str = Field(min_length=1, max_length=512)


class NewConversation(BaseModel):
    title: str = Field(default="New conversation", min_length=1, max_length=120)
    scope: Literal["auto", "knowledge", "server"] = "auto"


class EditConversation(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    archived: bool | None = None


class MessageInput(BaseModel):
    content: str = Field(min_length=1, max_length=12000)
    scope: Literal["auto", "knowledge", "server"] = "auto"


class Decision(BaseModel):
    approved: bool


class Folder(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    collection: str = Field(default="General", min_length=1, max_length=80)


class Routines(BaseModel):
    download_throttling: bool | None = None
    completed_torrent_cleanup: bool | None = None


def create_app(config: Config | None = None, backend=None, model=None) -> FastAPI:
    config = config or load_config()
    settings = LocalSettings.from_config(config)
    settings.state.mkdir(parents=True, exist_ok=True)
    db = Database(settings.state / "warden.db")
    db.initialize()
    backend = backend or LiveBackend(config)
    model = model or LocalModel(settings)
    knowledge = Knowledge(db, settings, model)
    app = FastAPI(title="Warden Local", version="1.0.0", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db, app.state.settings = db, settings

    def authorized(request: Request):
        token = request.cookies.get("warden_session", "")
        hashed = hashlib.sha256(token.encode()).hexdigest()
        if not token or not db.one("SELECT 1 FROM sessions WHERE token_hash=? AND expires_at>?", (hashed, now())):
            raise HTTPException(401, "Sign in to Warden")

    @app.middleware("http")
    async def security(request, call_next):
        if request.url.path.startswith("/api") and request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if not origin or urlparse(origin).netloc != request.headers.get("host"):
                return Response("Invalid request origin", status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.post("/api/session")
    def login(body: Login, request: Request, response: Response):
        address = request.client.host if request.client else "unknown"
        attempt = db.one("SELECT * FROM login_attempts WHERE address=?", (address,))
        if attempt and attempt["reset_at"] > now() and attempt["failures"] >= 5:
            raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")
        stored = db.one("SELECT value FROM local_settings WHERE key='password_hash'")
        valid = bool(stored and PasswordHash.recommended().verify(body.password, stored["value"]))
        if not valid:
            failures = attempt["failures"] + 1 if attempt and attempt["reset_at"] > now() else 1
            db.execute("INSERT OR REPLACE INTO login_attempts VALUES(?,?,?)",
                       (address, failures, (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()))
            raise HTTPException(401, "Incorrect password" if stored else "Owner password has not been initialized")
        db.execute("DELETE FROM login_attempts WHERE address=?", (address,))
        token = secrets.token_urlsafe(32)
        expiry = (datetime.now(timezone.utc) + timedelta(hours=settings.session_hours)).isoformat()
        db.execute("DELETE FROM sessions WHERE expires_at<?", (now(),))
        db.execute("INSERT INTO sessions VALUES(?,?)", (hashlib.sha256(token.encode()).hexdigest(), expiry))
        response.set_cookie("warden_session", token, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", max_age=settings.session_hours * 3600)
        return {"authenticated": True}

    @app.get("/api/session", dependencies=[Depends(authorized)])
    def session():
        return {"authenticated": True, "owner": "Nathan"}

    @app.delete("/api/session", dependencies=[Depends(authorized)])
    def logout(request: Request, response: Response):
        token = request.cookies.get("warden_session", "")
        db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
        response.delete_cookie("warden_session")
        return {"authenticated": False}

    @app.get("/api/openapi.json", dependencies=[Depends(authorized)])
    def schema():
        return app.openapi()

    @app.get("/api/conversations", response_model=list[Conversation], dependencies=[Depends(authorized)])
    def conversations(q: str = ""):
        return db.all("SELECT * FROM conversations WHERE archived=0 AND title LIKE ? ORDER BY updated_at DESC", (f"%{q[:200]}%",))

    @app.post("/api/conversations", response_model=Conversation, dependencies=[Depends(authorized)])
    def new_conversation(body: NewConversation):
        return db.conversation(body.title, body.scope)

    @app.patch("/api/conversations/{identity}", dependencies=[Depends(authorized)])
    def edit_conversation(identity: str, body: EditConversation):
        if body.archived and db.one("SELECT 1 FROM runs WHERE conversation_id=? AND status IN ('queued','running','waiting')", (identity,)):
            raise HTTPException(409, "Stop the active investigation before archiving")
        for field, value in body.model_dump(exclude_none=True).items():
            db.execute(f"UPDATE conversations SET {field}=? WHERE id=?", (value, identity))
        return {"ok": True}

    @app.get("/api/conversations/{identity}/messages", dependencies=[Depends(authorized)])
    def messages(identity: str):
        return {"messages": db.all("SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at", (identity,)),
                "runs": db.all("SELECT * FROM runs WHERE conversation_id=? ORDER BY created_at", (identity,))}

    @app.post("/api/conversations/{identity}/messages", response_model=Run, dependencies=[Depends(authorized)])
    def send_message(identity: str, body: MessageInput):
        try:
            return db.submit(identity, body.content.strip(), body.scope)
        except sqlite3.IntegrityError:
            raise HTTPException(409, "This conversation already has an active investigation")
        except ValueError as exc:
            raise HTTPException(404, str(exc))

    @app.get("/api/runs", response_model=list[Run], dependencies=[Depends(authorized)])
    def runs():
        return db.all("SELECT * FROM runs ORDER BY created_at DESC LIMIT 100")

    @app.get("/api/runs/{identity}/events", response_model=list[RunEvent], dependencies=[Depends(authorized)])
    def events(identity: str):
        return db.all("SELECT * FROM run_events WHERE run_id=? ORDER BY id", (identity,))

    @app.get("/api/runs/{identity}/stream", dependencies=[Depends(authorized)])
    async def stream(identity: str, request: Request):
        if not db.one("SELECT 1 FROM runs WHERE id=?", (identity,)):
            raise HTTPException(404, "Run not found")
        try:
            cursor = int(request.headers.get("last-event-id", "0"))
        except ValueError:
            cursor = 0

        async def generate():
            nonlocal cursor
            while not await request.is_disconnected():
                for row in db.all("SELECT * FROM run_events WHERE run_id=? AND id>? ORDER BY id LIMIT 200", (identity, cursor)):
                    cursor = row["id"]
                    yield f"id: {cursor}\ndata: {json.dumps(row)}\n\n"
                status = db.one("SELECT status FROM runs WHERE id=?", (identity,))
                yield f"event: status\ndata: {json.dumps(status)}\n\n"
                if status and status["status"] in {"completed", "failed", "cancelled"}:
                    break
                await asyncio.sleep(0.5)
        return StreamingResponse(generate(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.post("/api/runs/{identity}/cancel", dependencies=[Depends(authorized)])
    def cancel(identity: str):
        db.cancel(identity)
        return {"ok": True}

    @app.get("/api/approvals", response_model=list[Approval], dependencies=[Depends(authorized)])
    def approvals():
        return db.all("SELECT * FROM actions ORDER BY id DESC LIMIT 100")

    @app.post("/api/approvals/{identity}/decision", response_model=Approval, dependencies=[Depends(authorized)])
    def decide(identity: int, body: Decision):
        try:
            return db.decide(identity, body.approved)
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @app.get("/api/sources", response_model=list[Source], dependencies=[Depends(authorized)])
    def sources():
        return db.all("SELECT * FROM sources ORDER BY created_at DESC")

    @app.post("/api/sources/folder", response_model=Source, dependencies=[Depends(authorized)])
    def add_folder(body: Folder):
        try:
            return knowledge.add_source(Path(body.path).name, "folder", Path(body.path), body.collection)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/sources/upload", response_model=Source, dependencies=[Depends(authorized)])
    async def upload(file: UploadFile, collection: str = "General"):
        filename = Path(file.filename or "document").name
        if Path(filename).suffix.lower() not in ALLOWED:
            raise HTTPException(400, "Choose a PDF, Markdown, or text file")
        contents = await file.read(MAX_BYTES + 1)
        if len(contents) > MAX_BYTES:
            raise HTTPException(413, "Document exceeds 25 MB")
        folder = settings.state / "uploads" / uid()
        folder.mkdir(parents=True, mode=0o700)
        path = folder / filename
        path.write_bytes(contents)
        path.chmod(0o600)
        return knowledge.add_source(filename, "upload", path, collection)

    @app.post("/api/sources/{identity}/reindex", dependencies=[Depends(authorized)])
    def reindex(identity: str):
        if not db.one("SELECT 1 FROM sources WHERE id=?", (identity,)):
            raise HTTPException(404, "Source not found")
        db.execute("UPDATE sources SET status='queued',error=NULL WHERE id=?", (identity,))
        db.enqueue("index", identity)
        return {"ok": True}

    @app.delete("/api/sources/{identity}", dependencies=[Depends(authorized)])
    def remove(identity: str):
        knowledge.remove(identity)
        return {"ok": True}

    @app.get("/api/documents", dependencies=[Depends(authorized)])
    def documents(q: str = "", collection: str = ""):
        return db.all("SELECT d.*,s.collection FROM documents d JOIN sources s ON s.id=d.source_id WHERE d.name LIKE ? AND (?='' OR s.collection=?) ORDER BY d.updated_at DESC LIMIT 500",
                      (f"%{q[:200]}%", collection, collection))

    @app.get("/api/knowledge/search", response_model=list[Citation], dependencies=[Depends(authorized)])
    def search(q: str, collection: str | None = None):
        # Interactive browsing stays available while the model is busy or offline.
        return knowledge.search(q, semantic=False, collection=collection)

    @app.get("/api/documents/{identity}", dependencies=[Depends(authorized)])
    def document(identity: str):
        row = db.one("SELECT d.*,s.collection FROM documents d JOIN sources s ON s.id=d.source_id WHERE d.id=?", (identity,))
        if not row:
            raise HTTPException(404, "This source is no longer available")
        return {"document": row, "chunks": db.all("SELECT id,page,content FROM chunks WHERE document_id=? ORDER BY id", (identity,))}

    @app.get("/api/server", response_model=ServerSnapshot, dependencies=[Depends(authorized)])
    def server():
        snapshot = collect_snapshot(backend, config)
        if config.tautulli_api_key:
            try:
                snapshot["plex"] = backend.tautulli_activity()
            except Exception as exc:
                snapshot.setdefault("collector_errors", {})["plex"] = str(exc)
        return {"collected_at": now(), "snapshot": json.loads(redact(json.dumps(snapshot, default=str), config.secrets()))}

    @app.get("/api/server/containers/{name}", dependencies=[Depends(authorized)])
    def container(name: str):
        if name.startswith("-") or "/" in name:
            raise HTTPException(400, "Invalid container name")
        return {"inspect": backend.container_inspect(name), "logs": redact(backend.container_logs(name, 100), config.secrets())}

    @app.get("/api/incidents", dependencies=[Depends(authorized)])
    def incidents():
        return db.all("SELECT * FROM incidents ORDER BY id DESC LIMIT 100")

    @app.get("/api/incidents/{identity}", dependencies=[Depends(authorized)])
    def incident(identity: int):
        row = db.one("SELECT * FROM incidents WHERE id=?", (identity,))
        if not row:
            raise HTTPException(404, "Incident not found")
        path = Path(row["report_path"]).resolve() if row["report_path"] else None
        roots = [config.incidents_dir.resolve(), settings.state]
        report = path.read_text() if path and path.is_file() and any(root in path.parents for root in roots) else None
        return {"incident": row, "report": report}

    @app.get("/api/settings", dependencies=[Depends(authorized)])
    def runtime():
        saved = db.one("SELECT value FROM local_settings WHERE key='routines'")
        routines = {"download_throttling": config.plex_throttle_downloads, "completed_torrent_cleanup": config.reap_completed}
        if saved:
            routines.update(json.loads(saved["value"]))
        return {"model": settings.chat_model, "embedding_model": settings.embedding_model,
                "runtime": model.status(), "knowledge_roots": [str(p) for p in settings.roots],
                "local_only": True, "action_policy": "approval", "mode": config.mode,
                "routines": routines,
                "worker": db.one("SELECT value FROM local_settings WHERE key='worker_heartbeat'")}

    @app.patch("/api/settings/routines", dependencies=[Depends(authorized)])
    def set_routines(body: Routines):
        with db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT value FROM local_settings WHERE key='routines'").fetchone()
            routines = json.loads(row[0]) if row else {}
            routines.update(body.model_dump(exclude_none=True))
            c.execute("INSERT OR REPLACE INTO local_settings VALUES('routines',?)", (json.dumps(routines),))
        return routines

    # Source and installed layouts both keep web alongside the warden package.
    dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        if path.startswith("api/") or not (dist / "index.html").exists():
            raise HTTPException(404, "Build the Warden frontend first")
        return FileResponse(dist / "index.html")

    return app
