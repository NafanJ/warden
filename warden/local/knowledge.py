from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

import sqlite_vec
from pypdf import PdfReader

from warden.local.db import Database, now, uid
from warden.local.models import LocalModel
from warden.local.settings import LocalSettings

ALLOWED = {".pdf", ".md", ".txt"}
EXCLUDED = {"node_modules", "state", ".git", ".venv", "__pycache__", "secrets", "credentials"}
MAX_BYTES = 25 * 1024 * 1024


class IndexPaused(Exception):
    """An interactive run takes precedence over background indexing."""


class Knowledge:
    def __init__(self, db: Database, settings: LocalSettings, model: LocalModel):
        self.db, self.settings, self.model = db, settings, model
        with db.connect() as c:
            self.vector(c)
            c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vectors USING vec0(embedding float[768])")

    @staticmethod
    def vector(c):
        c.enable_load_extension(True)
        sqlite_vec.load(c)
        c.enable_load_extension(False)

    def allowed_folder(self, path: Path) -> Path:
        resolved = path.expanduser().resolve(strict=True)
        if not resolved.is_dir() or not any(resolved == r or r in resolved.parents for r in self.settings.roots):
            raise ValueError("Folder must be inside a configured knowledge root")
        return resolved

    def add_source(self, name: str, kind: str, path: Path, collection="General") -> dict:
        if kind == "folder":
            path = self.allowed_folder(path)
        identity = uid()
        self.db.execute("INSERT INTO sources(id,name,kind,path,collection,created_at) VALUES(?,?,?,?,?,?)",
                        (identity, name[:160], kind, str(path.resolve()), collection[:80], now()))
        self.db.enqueue("index", identity)
        return self.db.one("SELECT * FROM sources WHERE id=?", (identity,))

    def _delete_document(self, c, identity):
        for row in c.execute("SELECT id FROM chunks WHERE document_id=?", (identity,)).fetchall():
            c.execute("DELETE FROM chunk_vectors WHERE rowid=?", (row["id"],))
            c.execute("DELETE FROM chunks_fts WHERE chunk_id=?", (row["id"],))
        c.execute("DELETE FROM chunks WHERE document_id=?", (identity,))
        c.execute("DELETE FROM documents WHERE id=?", (identity,))

    def remove(self, identity: str):
        with self.db.connect() as c:
            self.vector(c)
            for row in c.execute("SELECT id FROM documents WHERE source_id=?", (identity,)).fetchall():
                self._delete_document(c, row["id"])
            c.execute("DELETE FROM sources WHERE id=?", (identity,))
            c.execute("DELETE FROM jobs WHERE kind='index' AND target=? AND status='queued'", (identity,))

    def _files(self, source: dict) -> list[Path]:
        path = Path(source["path"])
        if source["kind"] == "upload":
            return [path] if path.is_file() else []
        if source["kind"] == "folder":
            self.allowed_folder(path)
        paths = []
        for directory, dirs, files in os.walk(path, followlinks=False):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in EXCLUDED and not (Path(directory) / d).is_symlink()]
            for name in files:
                item = Path(directory) / name
                if name.startswith(".") or item.is_symlink() or item.stem.lower() in {"secrets", "credentials", "passwords", "tokens", "keys"}:
                    continue
                if item.suffix.lower() in ALLOWED and item.stat().st_size <= MAX_BYTES:
                    paths.append(item)
                if len(paths) >= 5000:
                    raise ValueError("Source exceeds 5,000 documents; select a smaller folder")
        return paths

    @staticmethod
    def extract(path: Path):
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("Document exceeds 25 MB")
        if path.suffix.lower() == ".pdf":
            reader = PdfReader(path)
            if reader.is_encrypted:
                raise ValueError("Encrypted PDFs must be unlocked before upload")
            if len(reader.pages) > 1000:
                raise ValueError("PDF exceeds 1,000 pages")
            pages = [(i + 1, p.extract_text() or "") for i, p in enumerate(reader.pages)]
            if not any(text.strip() for _, text in pages):
                raise ValueError("This PDF requires OCR")
            return pages
        return [(1, path.read_text(encoding="utf-8"))]

    def index(self, identity: str, check=lambda: None):
        source = self.db.one("SELECT * FROM sources WHERE id=?", (identity,))
        if not source:
            return
        self.db.execute("UPDATE sources SET status='indexing',error=NULL WHERE id=?", (identity,))
        try:
            paths = self._files(source)
            seen, errors = set(), []
            for path in paths:
                check()
                if not self.db.one("SELECT id FROM sources WHERE id=?", (identity,)):
                    return
                seen.add(str(path))
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                old = self.db.one("SELECT * FROM documents WHERE source_id=? AND path=?", (identity, str(path)))
                if old and old["hash"] == digest and old["status"] == "ready":
                    continue
                doc_id = old["id"] if old else uid()
                try:
                    pages = self.extract(path)
                    parts = []
                    for page, content in pages:
                        words = content.split()
                        # Small passages remain below the embedding model's 2K-token window.
                        for start in range(0, len(words), 200):
                            text = " ".join(words[start:start + 240])[:1600]
                            if text.strip():
                                parts.append((page, text))
                    vectors = []
                    for start in range(0, len(parts), 8):
                        check()
                        vectors.extend(self.model.embed([f"title: {path.name} | text: {s[1]}" for s in parts[start:start + 8]]))
                    if any(len(v) != 768 for v in vectors):
                        raise ValueError("Embedding model must produce 768 dimensions")
                    with self.db.connect() as c:
                        self.vector(c)
                        c.execute("BEGIN IMMEDIATE")
                        if not c.execute("SELECT id FROM sources WHERE id=?", (identity,)).fetchone():
                            return
                        self._delete_document(c, doc_id)
                        c.execute("INSERT INTO documents VALUES(?,?,?,?,?,'ready',NULL,?,?)",
                                  (doc_id, identity, path.name, str(path), digest, len(pages), now()))
                        for (page, content), vector in zip(parts, vectors, strict=True):
                            row = c.execute("INSERT INTO chunks(document_id,page,content,embedding_model) VALUES(?,?,?,?)",
                                            (doc_id, page, content, self.settings.embedding_model)).lastrowid
                            c.execute("INSERT INTO chunks_fts(content,chunk_id) VALUES(?,?)", (content, row))
                            c.execute("INSERT INTO chunk_vectors(rowid,embedding) VALUES(?,?)", (row, sqlite_vec.serialize_float32(vector)))
                except IndexPaused:
                    raise
                except Exception as exc:
                    errors.append(f"{path.name}: {exc}")
                    with self.db.connect() as c:
                        self.vector(c)
                        c.execute("BEGIN IMMEDIATE")
                        if not c.execute("SELECT id FROM sources WHERE id=?", (identity,)).fetchone():
                            return
                        self._delete_document(c, doc_id)
                        c.execute("INSERT INTO documents VALUES(?,?,?,?,?,'error',?,0,?)",
                                  (doc_id, identity, path.name, str(path), digest, str(exc)[:500], now()))
            with self.db.connect() as c:
                self.vector(c)
                for row in c.execute("SELECT id,path FROM documents WHERE source_id=?", (identity,)).fetchall():
                    if row["path"] not in seen:
                        self._delete_document(c, row["id"])
            self.db.execute("UPDATE sources SET status=?,error=?,indexed_at=? WHERE id=?",
                            ("error" if errors else "ready", "\n".join(errors)[:2000] or None, now(), identity))
        except IndexPaused:
            self.db.execute("UPDATE sources SET status='queued' WHERE id=?", (identity,))
            raise
        except Exception as exc:
            self.db.execute("UPDATE sources SET status='error',error=? WHERE id=?", (str(exc)[:1000], identity))

    def search(self, query: str, semantic=True, collection: str | None = None) -> list[dict]:
        scores = {}
        words = re.findall(r"\w+", query)[:24]
        with self.db.connect() as c:
            self.vector(c)
            if words:
                rows = c.execute("SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT 30",
                                 (" OR ".join('"' + w + '"' for w in words),)).fetchall()
                for rank, row in enumerate(rows):
                    scores[int(row[0])] = 1 / (60 + rank)
            if semantic and c.execute("SELECT 1 FROM chunks LIMIT 1").fetchone():
                vector = self.model.embed([f"task: search result | query: {query[:1600]}"])[0]
                for rank, row in enumerate(c.execute("SELECT rowid FROM chunk_vectors WHERE embedding MATCH ? AND k=30 ORDER BY distance",
                                                    (sqlite_vec.serialize_float32(vector),))):
                    scores[row[0]] = scores.get(row[0], 0) + 1 / (60 + rank)
            results, seen = [], set()
            for chunk_id in sorted(scores, key=scores.get, reverse=True):
                row = c.execute("SELECT c.id,c.document_id,c.page,c.content,d.name,d.hash,s.collection,s.id AS source_id FROM chunks c JOIN documents d ON d.id=c.document_id JOIN sources s ON s.id=d.source_id WHERE c.id=? AND d.status='ready'",
                                (chunk_id,)).fetchone()
                if row and row["content"] not in seen and (not collection or row["collection"] == collection):
                    results.append(dict(row))
                    seen.add(row["content"])
                if len(results) == 6:
                    break
            return results
