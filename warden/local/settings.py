from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from warden.config import Config


@dataclass
class LocalSettings:
    state: Path
    ollama_url: str = "http://127.0.0.1:11434"
    chat_model: str = "qwen3.5:4b-q4_K_M"
    embedding_model: str = "embeddinggemma:300m-qat-q8_0"
    roots: tuple[Path, ...] = ()
    max_calls: int = 8
    run_timeout: int = 300
    session_hours: int = 24

    @classmethod
    def from_config(cls, config: Config) -> LocalSettings:
        url = os.getenv("WARDEN_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
        if urlparse(url).hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Local AI requires a loopback Ollama address")
        state = config.state_dir.resolve()
        roots = tuple(Path(p).expanduser().resolve() for p in
                      os.getenv("WARDEN_KNOWLEDGE_ROOTS", str(state / "knowledge")).split(os.pathsep) if p)
        return cls(state, url, os.getenv("WARDEN_CHAT_MODEL", cls.chat_model),
                   os.getenv("WARDEN_EMBED_MODEL", cls.embedding_model), roots)
