"""Read-only local inference smoke test. Does not execute model-proposed tools."""
import json
import time

import httpx

from warden.config import load_config
from warden.local.models import LocalModel
from warden.local.settings import LocalSettings


def main():
    settings = LocalSettings.from_config(load_config())
    model = LocalModel(settings)
    results = {"runtime": model.status(), "measurements": []}
    started = time.monotonic()
    vectors = model.embed(["task: search result | query: Plex buffering", "title: Runbook | text: Inspect Plex transcoding and disk usage."])
    results["embeddings"] = {"seconds": round(time.monotonic() - started, 2), "dimensions": [len(v) for v in vectors]}
    cases = [
        ("cold_chat", [{"role": "user", "content": "Say 'Warden is ready.' and nothing else."}], []),
        ("warm_chat", [{"role": "user", "content": "In one sentence, what does a server monitoring tool do?"}], []),
        ("tool_selection", [{"role": "system", "content": "Use the provided tool to inspect the server before answering."}, {"role": "user", "content": "List my running containers."}],
         [{"type": "function", "function": {"name": "get_containers", "description": "List live Docker containers", "parameters": {"type": "object", "properties": {}}}}]),
    ]
    for name, messages, tools in cases:
        start = time.monotonic()
        response = httpx.post(settings.ollama_url + "/api/chat", timeout=300,
                              json={"model": settings.chat_model, "messages": messages, "tools": tools,
                                    "stream": False, "think": False,
                                    "options": {"num_thread": 3, "num_ctx": 8192, "num_predict": 100, "temperature": 0}})
        response.raise_for_status()
        data = response.json()
        measurement = {"name": name, "seconds": round(time.monotonic() - start, 2),
                       "message": data["message"], "eval_count": data.get("eval_count"),
                       "eval_duration": data.get("eval_duration"), "load_duration": data.get("load_duration")}
        results["measurements"].append(measurement)
        print(json.dumps(measurement), flush=True)
    target = settings.state / "benchmark.json"
    target.write_text(json.dumps(results, indent=2))
    assert results["measurements"][-1]["message"].get("tool_calls"), "Model did not select the supplied tool"
    print(f"Benchmark saved to {target}")


if __name__ == "__main__":
    main()
