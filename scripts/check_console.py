"""Check authenticated console reads and one knowledge-only worker round trip."""
import argparse
import json
import time
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://192.168.1.101:8486")
    parser.add_argument("--password-file", type=Path, default=Path(".local-state/owner-password.txt"))
    args = parser.parse_args()
    with httpx.Client(base_url=args.url, timeout=30, headers={"Origin": args.url}) as client:
        assert client.get("/healthz").json()["status"] == "ok"
        assert client.get("/api/settings").status_code == 401
        client.post("/api/session", json={"password": args.password_file.read_text().strip()}).raise_for_status()
        settings = client.get("/api/settings").json()
        assert settings["runtime"]["chat_ready"] and settings["runtime"]["embeddings_ready"]
        assert settings["worker"]
        assert client.get("/api/server").status_code == 200
        conversation = client.post("/api/conversations", json={"title": "Console acceptance check", "scope": "knowledge"}).json()
        run = client.post(f"/api/conversations/{conversation['id']}/messages", json={
            "content": "Reply with exactly: Local worker is ready.", "scope": "knowledge",
        }).json()
        started = time.monotonic()
        try:
            while time.monotonic() - started < 360:
                thread = client.get(f"/api/conversations/{conversation['id']}/messages").json()
                state = thread["runs"][-1]
                if state["status"] not in {"queued", "running", "waiting"}:
                    assert state["status"] == "completed", state
                    assert any(m["role"] == "assistant" and "Local worker is ready" in m["content"] for m in thread["messages"])
                    print(json.dumps({"url": args.url, "mode": settings["mode"], "status": state["status"],
                                      "seconds": round(time.monotonic() - started, 2)}))
                    break
                time.sleep(1)
            else:
                raise TimeoutError("Queued console check did not complete")
        finally:
            client.post(f"/api/runs/{run['id']}/cancel").raise_for_status()
            client.patch(f"/api/conversations/{conversation['id']}", json={"archived": True})
            client.delete("/api/session").raise_for_status()


if __name__ == "__main__":
    main()
