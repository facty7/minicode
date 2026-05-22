import json
import os
import sys
from pathlib import Path
import tempfile

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    data_dir = tempfile.mkdtemp(prefix="minicode-smoke-")
    os.environ["MINICODE_DATA_DIR"] = data_dir
    os.environ["MINICODE_BANNER"] = "0"

    from app import app

    with TestClient(app) as client:
        health = client.get("/health")
        rebuild = client.post("/api/index/rebuild")
        repo_map = client.get("/api/repo-map", params={"limit": 8})
        rag = client.get("/api/rag", params={"q": "MiniCodeAgent patch preview", "limit": 5})
        preview = client.post(
            "/api/tool",
            json={
                "name": "patch_preview",
                "args": {
                    "path": "README.md",
                    "old": "MiniCode",
                    "new": "MiniCode Agent",
                    "count": 1,
                },
                "confirm": False,
            },
        )
        blocked = client.post(
            "/api/tool",
            json={
                "name": "run",
                "args": {"command": "python -m py_compile app.py"},
                "confirm": False,
            },
        )
        validated = client.post(
            "/api/tool",
            json={
                "name": "run",
                "args": {"command": "python -m py_compile app.py"},
                "confirm": True,
            },
        )

    result = {
        "health": health.status_code,
        "index_chunks": rebuild.json().get("chunks_indexed"),
        "repo_map_files": len(repo_map.json().get("files", [])),
        "rag_hits": rag.json().get("count"),
        "patch_preview_has_diff": "--- a/README.md" in json.dumps(preview.json(), ensure_ascii=False),
        "run_blocked_without_confirm": blocked.status_code == 400,
        "run_confirmed_ok": validated.json().get("ok") is True,
        "data_dir": data_dir,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
