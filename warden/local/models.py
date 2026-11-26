from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx

from warden.local.settings import LocalSettings


class LocalModel:
    def __init__(self, settings: LocalSettings):
        self.settings = settings

    def status(self) -> dict:
        try:
            response = httpx.get(f"{self.settings.ollama_url}/api/tags", timeout=3)
            response.raise_for_status()
            names = [m["name"] for m in response.json().get("models", [])]
            return {"online": True, "models": names, "chat_ready": self.settings.chat_model in names,
                    "embeddings_ready": self.settings.embedding_model in names}
        except (httpx.HTTPError, ValueError) as exc:
            return {"online": False, "models": [], "chat_ready": False,
                    "embeddings_ready": False, "error": str(exc)}

    def embed(self, texts: list[str]) -> list[list[float]]:
        response = httpx.post(f"{self.settings.ollama_url}/api/embed", timeout=180,
                              json={"model": self.settings.embedding_model, "input": texts,
                                    "truncate": False, "keep_alive": "5m", "options": {"num_thread": 3}})
        response.raise_for_status()
        return response.json()["embeddings"]

    def chat(self, messages: list[dict], tools: list[dict], emit: Callable,
             check: Callable = lambda: None) -> dict:
        payload = {"model": self.settings.chat_model, "messages": messages, "tools": tools,
                   "think": False, "stream": True, "keep_alive": "5m",
                   "options": {"num_ctx": 8192, "num_batch": 256, "num_thread": 3, "num_predict": 1024, "temperature": 0.2}}

        async def receive():
            content, calls = "", []
            async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=5)) as client:
                async with client.stream("POST", f"{self.settings.ollama_url}/api/chat", json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        check()
                        if not line:
                            continue
                        item = json.loads(line)
                        if item.get("error"):
                            raise RuntimeError(item["error"])
                        message = item.get("message", {})
                        token = message.get("content", "")
                        if token:
                            content += token
                            emit("token", {"text": token})
                        calls.extend(message.get("tool_calls", []))
                        if item.get("done"):
                            emit("usage", {k: item[k] for k in ("total_duration", "prompt_eval_count", "eval_count", "eval_duration") if k in item})
            return {"role": "assistant", "content": content, **({"tool_calls": calls} if calls else {})}

        async def watched():
            # CPU prompt evaluation can take minutes before headers or tokens arrive.
            task = asyncio.create_task(receive())
            try:
                while not task.done():
                    check()
                    await asyncio.wait({task}, timeout=0.25)
                return await task
            finally:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

        return asyncio.run(watched())
