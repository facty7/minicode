from __future__ import annotations

import asyncio
import datetime as dt
import dataclasses
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import textwrap
import sys
import uuid
from collections import Counter
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DATA_DIR = Path(os.getenv("MINICODE_DATA_DIR", str(ROOT / "data"))).resolve()
WORKSPACE_ROOT = Path(os.getenv("MINICODE_WORKSPACE", str(ROOT))).resolve()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "").strip()
MODEL_NAME = os.getenv("MINICODE_MODEL", "gpt-4.1-mini").strip()
ALLOW_SHELL = os.getenv("MINICODE_ALLOW_SHELL", "0") == "1"
MAX_SEARCH_RESULTS = int(os.getenv("MINICODE_MAX_SEARCH_RESULTS", "8"))
MAX_CONTEXT_CHARS = int(os.getenv("MINICODE_MAX_CONTEXT_CHARS", "14000"))
MAX_FILE_BYTES = int(os.getenv("MINICODE_MAX_FILE_BYTES", "800000"))
CODE_CHUNK_LINES = int(os.getenv("MINICODE_CODE_CHUNK_LINES", "80"))
CODE_CHUNK_OVERLAP = int(os.getenv("MINICODE_CODE_CHUNK_OVERLAP", "12"))
SHOW_BANNER = os.getenv("MINICODE_BANNER", "1") != "0"

TEXT_EXTENSIONS = {
    ".py",
    ".md",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".js",
    ".ts",
    ".html",
    ".css",
    ".toml",
    ".env",
    ".sh",
    ".bat",
    ".ps1",
    ".csv",
    ".ini",
}

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    "data",
}

RISKY_COMMAND_HINTS = [
    r"\brm\b",
    r"\bdel\b",
    r"\brmdir\b",
    r"\bRemove-Item\b",
    r"\bformat\b",
    r"\bshutdown\b",
    r"\bgit\s+push\b",
    r"\bInvoke-WebRequest\b",
    r"\bcurl\b.*https?://",
    r"\bwget\b.*https?://",
]

BANNER = r"""
MiniCode
 /\_/\
( o.o )  终端伙伴已上线
 > ^ <   仓库索引已就绪

  写入和命令执行会先确认
"""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def shorten(text: str, limit: int = 220) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def safe_relpath(raw: str | Path) -> Path:
    candidate = (WORKSPACE_ROOT / Path(raw)).resolve()
    if not candidate.is_relative_to(WORKSPACE_ROOT):
        raise HTTPException(status_code=400, detail="Path escapes workspace root")
    return candidate


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def is_text_file(path: Path) -> bool:
    if path.suffix.lower() in TEXT_EXTENSIONS:
        return True
    return path.name in {"Dockerfile", "README", "README.md", ".gitignore"}


def parse_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]{2,}|\d+", text) if len(t) > 1]


def line_snippets(text: str, start_line: int, tokens: list[str], limit: int = 3) -> list[str]:
    snippets: list[str] = []
    token_set = set(tokens)
    for offset, line in enumerate(text.splitlines(), start=0):
        low = line.lower()
        if any(token in low for token in token_set):
            snippets.append(f"{start_line + offset}: {line.strip()}")
            if len(snippets) >= limit:
                break
    if not snippets:
        first = text.splitlines()[: min(3, len(text.splitlines()))]
        snippets = [f"{start_line + i}: {line.strip()}" for i, line in enumerate(first)]
    return snippets


def extract_json_block(text: str) -> dict[str, Any] | None:
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception:
        return None


def format_block(title: str, body: str) -> str:
    return f"{title}\n{body.strip()}"


def make_unified_diff(path: str, old: str, new: str, context: int = 3) -> str:
    import difflib

    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=context,
        )
    )


@dataclass
class ToolAction:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    risk: str = "safe"
    reason: str = ""


@dataclass
class ToolResult:
    tool: str
    ok: bool
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    requires_confirmation: bool = False


class DB:
    def __init__(self, path: Path):
        self.path = path
        ensure_parent(self.path)
        self._init()

    def conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self.conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    summary TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    meta_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    content TEXT NOT NULL,
                    score REAL NOT NULL DEFAULT 0.5,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tool_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    args_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    ok INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cache (
                    cache_key TEXT PRIMARY KEY,
                    prompt_json TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS code_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL,
                    chunk_no INTEGER NOT NULL,
                    start_line INTEGER NOT NULL,
                    end_line INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    token_json TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(path, chunk_no)
                );

                CREATE INDEX IF NOT EXISTS idx_code_chunks_path ON code_chunks(path);

                CREATE TABLE IF NOT EXISTS role_traces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def create_session(self, title: str = "New session") -> str:
        session_id = str(uuid.uuid4())
        now = utc_now()
        with self.conn() as conn:
            conn.execute(
                "INSERT INTO sessions(id, title, summary, created_at, updated_at) VALUES (?, ?, '', ?, ?)",
                (session_id, title, now, now),
            )
        return session_id

    def ensure_session(self, session_id: str | None, title_hint: str = "New session") -> str:
        if session_id:
            with self.conn() as conn:
                row = conn.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
                if row:
                    return session_id
        return self.create_session(title_hint)

    def update_session(self, session_id: str, *, title: str | None = None, summary: str | None = None) -> None:
        fields = []
        values: list[Any] = []
        if title is not None:
            fields.append("title = ?")
            values.append(title)
        if summary is not None:
            fields.append("summary = ?")
            values.append(summary)
        fields.append("updated_at = ?")
        values.append(utc_now())
        values.append(session_id)
        with self.conn() as conn:
            conn.execute(f"UPDATE sessions SET {', '.join(fields)} WHERE id = ?", values)

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self.conn() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if not row:
                return None
            return dict(row)

    def list_sessions(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.conn() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def add_message(self, session_id: str, role: str, content: str, meta: dict[str, Any] | None = None) -> None:
        with self.conn() as conn:
            conn.execute(
                "INSERT INTO messages(session_id, role, content, meta_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, role, content, json.dumps(meta or {}, ensure_ascii=False), utc_now()),
            )
        self.update_session(session_id)

    def list_messages(self, session_id: str, limit: int = 30) -> list[dict[str, Any]]:
        with self.conn() as conn:
            rows = conn.execute(
                "SELECT role, content, meta_json, created_at FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
            items = [dict(r) for r in rows][::-1]
            for item in items:
                try:
                    item["meta"] = json.loads(item.pop("meta_json") or "{}")
                except Exception:
                    item["meta"] = {}
            return items

    def add_memory(self, session_id: str, category: str, content: str, score: float = 0.6) -> None:
        with self.conn() as conn:
            conn.execute(
                "INSERT INTO memories(session_id, category, content, score, created_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, category, content, score, utc_now()),
            )

    def list_memories(self, session_id: str | None = None, limit: int = 40) -> list[dict[str, Any]]:
        with self.conn() as conn:
            if session_id:
                rows = conn.execute(
                    "SELECT * FROM memories WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                    (session_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM memories ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]

    def search_memories(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        tokens = [t for t in re.findall(r"[\w\u4e00-\u9fff]+", query.lower()) if len(t) > 1]
        all_items = self.list_memories(limit=200)
        scored: list[tuple[float, dict[str, Any]]] = []
        for item in all_items:
            content = (item["content"] or "").lower()
            score = 0.0
            for token in tokens:
                if token in content:
                    score += 1.0
            if score:
                item = dict(item)
                item["match_score"] = score
                scored.append((score, item))
        scored.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
        return [item for _, item in scored[:limit]]

    def add_tool_run(self, session_id: str, tool_name: str, args: dict[str, Any], result: dict[str, Any], ok: bool) -> None:
        with self.conn() as conn:
            conn.execute(
                "INSERT INTO tool_runs(session_id, tool_name, args_json, result_json, ok, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, tool_name, json.dumps(args, ensure_ascii=False), json.dumps(result, ensure_ascii=False), int(ok), utc_now()),
            )

    def list_tool_runs(self, session_id: str, limit: int = 60) -> list[dict[str, Any]]:
        with self.conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tool_runs WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
            items = [dict(r) for r in rows]
            for item in items:
                try:
                    item["args"] = json.loads(item.pop("args_json") or "{}")
                except Exception:
                    item["args"] = {}
                try:
                    item["result"] = json.loads(item.pop("result_json") or "{}")
                except Exception:
                    item["result"] = {}
                item["ok"] = bool(item["ok"])
            return items

    def add_role_trace(self, session_id: str, role: str, status: str, detail: dict[str, Any]) -> None:
        with self.conn() as conn:
            conn.execute(
                "INSERT INTO role_traces(session_id, role, status, detail_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, role, status, json.dumps(detail, ensure_ascii=False), utc_now()),
            )

    def list_role_traces(self, session_id: str, limit: int = 80) -> list[dict[str, Any]]:
        with self.conn() as conn:
            rows = conn.execute(
                "SELECT * FROM role_traces WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
            items = [dict(r) for r in rows][::-1]
            for item in items:
                try:
                    item["detail"] = json.loads(item.pop("detail_json") or "{}")
                except Exception:
                    item["detail"] = {}
            return items

    def replace_code_index(self, chunks: list[dict[str, Any]]) -> None:
        now = utc_now()
        with self.conn() as conn:
            conn.execute("DELETE FROM code_chunks")
            conn.executemany(
                """
                INSERT INTO code_chunks(path, chunk_no, start_line, end_line, text, token_json, fingerprint, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item["path"],
                        item["chunk_no"],
                        item["start_line"],
                        item["end_line"],
                        item["text"],
                        json.dumps(item["tokens"], ensure_ascii=False),
                        item["fingerprint"],
                        now,
                    )
                    for item in chunks
                ],
            )

    def code_index_stats(self) -> dict[str, Any]:
        with self.conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS chunk_count, COUNT(DISTINCT path) AS file_count, MAX(updated_at) AS updated_at FROM code_chunks"
            ).fetchone()
            return dict(row) if row else {"chunk_count": 0, "file_count": 0, "updated_at": None}

    def search_code_index(self, query: str, limit: int = 6) -> dict[str, Any]:
        query_tokens = tokenize(query)
        if not query_tokens:
            return {"query": query, "count": 0, "items": [], "context": ""}
        phrase = query.lower().strip()
        scored: list[tuple[float, dict[str, Any]]] = []
        with self.conn() as conn:
            rows = conn.execute("SELECT * FROM code_chunks").fetchall()
        for row in rows:
            item = dict(row)
            text = item["text"]
            path = item["path"]
            low_text = text.lower()
            low_path = path.lower()
            try:
                tokens = json.loads(item["token_json"] or "[]")
            except Exception:
                tokens = tokenize(text)
            counts = Counter(tokens)
            token_score = sum(counts[token] for token in query_tokens)
            path_score = sum(3 for token in query_tokens if token in low_path)
            phrase_score = 4 if phrase and phrase in low_text else 0
            score = token_score + path_score + phrase_score
            if score <= 0:
                continue
            scored.append(
                (
                    float(score),
                    {
                        "path": path,
                        "score": float(score),
                        "start_line": item["start_line"],
                        "end_line": item["end_line"],
                        "snippets": line_snippets(text, int(item["start_line"]), query_tokens),
                        "excerpt": shorten(text, 1200),
                    },
                )
            )
        scored.sort(key=lambda pair: (-pair[0], pair[1]["path"], pair[1]["start_line"]))
        items = [item for _, item in scored[:limit]]
        context_lines: list[str] = []
        for item in items[:5]:
            context_lines.append(f"[{item['path']}:{item['start_line']}-{item['end_line']}]")
            context_lines.extend(item["snippets"])
        return {"query": query, "count": len(scored), "items": items, "context": "\n".join(context_lines)}

    def repo_map(self, limit: int = 18) -> dict[str, Any]:
        with self.conn() as conn:
            rows = conn.execute(
                """
                SELECT path,
                       COUNT(*) AS chunk_count,
                       MIN(start_line) AS first_line,
                       MAX(end_line) AS last_line,
                       MAX(updated_at) AS updated_at
                FROM code_chunks
                GROUP BY path
                ORDER BY chunk_count DESC, path ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        files = [dict(row) for row in rows]
        return {
            "files": files,
            "summary": f"{len(files)} indexed files shown",
            "stats": self.code_index_stats(),
        }

    def get_cache(self, key: str) -> dict[str, Any] | None:
        with self.conn() as conn:
            row = conn.execute("SELECT * FROM cache WHERE cache_key = ?", (key,)).fetchone()
            if not row:
                return None
            return dict(row)

    def set_cache(self, key: str, prompt: dict[str, Any], response: dict[str, Any]) -> None:
        with self.conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache(cache_key, prompt_json, response_json, created_at) VALUES (?, ?, ?, ?)",
                (key, json.dumps(prompt, ensure_ascii=False), json.dumps(response, ensure_ascii=False), utc_now()),
            )


class WorkspaceTools:
    def __init__(self, root: Path):
        self.root = root

    def _resolve(self, raw: str | Path) -> Path:
        path = (self.root / Path(raw)).resolve()
        if not path.is_relative_to(self.root):
            raise HTTPException(status_code=400, detail="Path escapes workspace root")
        return path

    def _iter_files(self) -> Iterable[Path]:
        for path in self.root.rglob("*"):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.is_file():
                yield path

    def tree(self, raw: str = ".", max_depth: int = 2, max_entries: int = 120) -> dict[str, Any]:
        base = self._resolve(raw)
        if not base.exists():
            raise HTTPException(status_code=404, detail="Path not found")
        lines: list[str] = []
        count = 0

        def walk(path: Path, prefix: str, depth: int) -> None:
            nonlocal count
            if count >= max_entries:
                return
            children = sorted(
                [p for p in path.iterdir() if not p.name.startswith(".")],
                key=lambda p: (p.is_file(), p.name.lower()),
            )
            for index, child in enumerate(children):
                if count >= max_entries:
                    return
                branch = "└─ " if index == len(children) - 1 else "├─ "
                lines.append(f"{prefix}{branch}{child.name}")
                count += 1
                if child.is_dir() and depth < max_depth:
                    extension = "   " if index == len(children) - 1 else "│  "
                    walk(child, prefix + extension, depth + 1)

        lines.append(base.name if base != self.root else base.name or str(base))
        if base.is_dir():
            walk(base, "", 0)
        return {"root": str(base), "text": "\n".join(lines), "count": count}

    def search(self, query: str, limit: int = MAX_SEARCH_RESULTS) -> dict[str, Any]:
        tokens = [t.lower() for t in re.findall(r"[\w\u4e00-\u9fff]+", query) if len(t) > 1]
        if not tokens:
            tokens = [query.lower().strip()]
        results: list[dict[str, Any]] = []
        for path in self._iter_files():
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            if not is_text_file(path):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            score = 0
            snippets: list[str] = []
            for line_no, line in enumerate(text.splitlines(), start=1):
                low = line.lower()
                hit_count = sum(low.count(token) for token in tokens if token in low)
                if hit_count:
                    score += hit_count
                    if len(snippets) < 3:
                        snippets.append(f"{line_no}: {line.strip()}")
            if score:
                results.append(
                    {
                        "path": str(path.relative_to(self.root)),
                        "score": score,
                        "snippets": snippets,
                    }
                )
        results.sort(key=lambda item: (-item["score"], item["path"]))
        return {"query": query, "count": len(results), "items": results[:limit]}

    def read(self, raw: str, start: int = 1, end: int = 120) -> dict[str, Any]:
        path = self._resolve(raw)
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Cannot read file: {exc}") from exc
        lines = text.splitlines()
        start = max(1, int(start))
        end = max(start, int(end))
        end = min(end, len(lines))
        excerpt = [f"{i + 1}: {lines[i]}" for i in range(start - 1, end)]
        return {
            "path": str(path.relative_to(self.root)),
            "start": start,
            "end": end,
            "line_count": len(lines),
            "text": "\n".join(excerpt),
        }

    def write(self, raw: str, content: str, mode: str = "overwrite") -> dict[str, Any]:
        path = self._resolve(raw)
        ensure_parent(path)
        if mode == "append" and path.exists():
            with path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
        else:
            path.write_text(content, encoding="utf-8", newline="\n")
        return {"path": str(path.relative_to(self.root)), "bytes": len(content.encode("utf-8"))}

    def replace(self, raw: str, old: str, new: str, count: int = 1) -> dict[str, Any]:
        path = self._resolve(raw)
        if not path.exists():
            raise HTTPException(status_code=404, detail="File not found")
        text = path.read_text(encoding="utf-8", errors="ignore")
        occurrences = text.count(old)
        if not occurrences:
            return {"path": str(path.relative_to(self.root)), "changed": False, "occurrences": 0}
        updated = text.replace(old, new, count if count > 0 else occurrences)
        path.write_text(updated, encoding="utf-8", newline="\n")
        return {
            "path": str(path.relative_to(self.root)),
            "changed": True,
            "occurrences": occurrences,
            "bytes": len(updated.encode("utf-8")),
        }

    def preview_replace(self, raw: str, old: str, new: str, count: int = 1) -> dict[str, Any]:
        if not old:
            raise HTTPException(status_code=400, detail="old text is required for patch preview")
        path = self._resolve(raw)
        if not path.exists():
            raise HTTPException(status_code=404, detail="File not found")
        text = path.read_text(encoding="utf-8", errors="ignore")
        occurrences = text.count(old)
        rel = str(path.relative_to(self.root))
        if not occurrences:
            return {"path": rel, "changed": False, "occurrences": 0, "diff": ""}
        updated = text.replace(old, new, count if count > 0 else occurrences)
        return {
            "path": rel,
            "changed": text != updated,
            "occurrences": occurrences,
            "diff": make_unified_diff(rel, text, updated),
        }

    def apply_patch(self, raw: str, old: str, new: str, count: int = 1) -> dict[str, Any]:
        preview = self.preview_replace(raw, old, new, count)
        if not preview.get("changed"):
            return preview
        result = self.replace(raw, old, new, count)
        result["diff"] = preview.get("diff", "")
        return result

    def append_text(self, raw: str, content: str) -> dict[str, Any]:
        return self.write(raw, content, mode="append")

    def delete(self, raw: str) -> dict[str, Any]:
        path = self._resolve(raw)
        if not path.exists():
            raise HTTPException(status_code=404, detail="File not found")
        if path.is_dir():
            raise HTTPException(status_code=400, detail="Refuse to delete directories from the agent")
        path.unlink()
        return {"path": str(path.relative_to(self.root)), "deleted": True}

    def _command_is_risky(self, command: str) -> tuple[bool, str]:
        for pattern in RISKY_COMMAND_HINTS:
            if re.search(pattern, command, re.I):
                return True, pattern
        if any(token in command for token in ["&&", "||", "|", ">", "<", ";"]):
            return True, "shell-meta"
        return False, ""

    def run(self, command: str, cwd: str | None = None, confirm: bool = False, timeout: int = 120) -> dict[str, Any]:
        risky, reason = self._command_is_risky(command)
        if risky and not confirm:
            return {
                "ok": False,
                "requires_confirmation": True,
                "reason": reason,
                "summary": "Command flagged as risky. Re-run with confirm=true.",
            }
        if not confirm and not ALLOW_SHELL:
            return {
                "ok": False,
                "requires_confirmation": True,
                "summary": "Shell execution is disabled by default. Set confirm=true in the UI to continue.",
            }
        workdir = self.root if not cwd else self._resolve(cwd)
        proc = subprocess.run(
            command,
            cwd=str(workdir),
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        text = out if out else err
        text = textwrap.shorten(text, width=7000, placeholder=" …")
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "cwd": str(workdir.relative_to(self.root)) if workdir != self.root else ".",
            "stdout": out,
            "stderr": err,
            "summary": text or "(no output)",
        }

    def validate(self, paths: list[str] | str | None = None) -> dict[str, Any]:
        raw_paths = [paths] if isinstance(paths, str) else list(paths or [])
        if not raw_paths:
            raw_paths = [str(path.relative_to(self.root)) for path in self._iter_files() if path.suffix.lower() in {".py", ".json"}]
        checks: list[dict[str, Any]] = []
        for raw in raw_paths[:20]:
            path = self._resolve(raw)
            rel = str(path.relative_to(self.root))
            if not path.exists() or not path.is_file():
                checks.append({"path": rel, "ok": False, "kind": "exists", "summary": "file not found"})
                continue
            suffix = path.suffix.lower()
            if suffix == ".py":
                proc = subprocess.run(
                    [sys.executable, "-m", "py_compile", str(path)],
                    cwd=str(self.root),
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                checks.append(
                    {
                        "path": rel,
                        "ok": proc.returncode == 0,
                        "kind": "py_compile",
                        "summary": (proc.stderr or proc.stdout or "syntax ok").strip(),
                    }
                )
            elif suffix == ".json":
                try:
                    json.loads(path.read_text(encoding="utf-8"))
                    checks.append({"path": rel, "ok": True, "kind": "json", "summary": "json ok"})
                except Exception as exc:
                    checks.append({"path": rel, "ok": False, "kind": "json", "summary": str(exc)})
            elif is_text_file(path):
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                    ok = "\x00" not in text
                    checks.append({"path": rel, "ok": ok, "kind": "text", "summary": "text readable" if ok else "contains null bytes"})
                except Exception as exc:
                    checks.append({"path": rel, "ok": False, "kind": "text", "summary": str(exc)})
            else:
                checks.append({"path": rel, "ok": True, "kind": "skip", "summary": "no validator for this file type"})
        ok = all(item["ok"] for item in checks)
        return {
            "ok": ok,
            "count": len(checks),
            "checks": checks,
            "summary": f"{sum(1 for item in checks if item['ok'])}/{len(checks)} validation checks passed",
        }


class CodeIndexer:
    def __init__(self, db: DB, tools: WorkspaceTools):
        self.db = db
        self.tools = tools

    def _chunk_file(self, path: Path) -> list[dict[str, Any]]:
        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        if not lines:
            return []
        rel = str(path.relative_to(self.tools.root))
        step = max(1, CODE_CHUNK_LINES - CODE_CHUNK_OVERLAP)
        chunks: list[dict[str, Any]] = []
        chunk_no = 0
        for start in range(0, len(lines), step):
            end = min(start + CODE_CHUNK_LINES, len(lines))
            body = "\n".join(lines[start:end])
            if not body.strip():
                continue
            fingerprint = hashlib.sha256(f"{rel}:{start}:{body}".encode("utf-8")).hexdigest()
            chunks.append(
                {
                    "path": rel,
                    "chunk_no": chunk_no,
                    "start_line": start + 1,
                    "end_line": end,
                    "text": body,
                    "tokens": tokenize(rel + "\n" + body),
                    "fingerprint": fingerprint,
                }
            )
            chunk_no += 1
            if end >= len(lines):
                break
        return chunks

    def rebuild(self) -> dict[str, Any]:
        chunks: list[dict[str, Any]] = []
        files = 0
        for path in self.tools._iter_files():
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            if not is_text_file(path):
                continue
            try:
                file_chunks = self._chunk_file(path)
            except Exception:
                continue
            if file_chunks:
                files += 1
                chunks.extend(file_chunks)
        self.db.replace_code_index(chunks)
        stats = self.db.code_index_stats()
        return {"ok": True, "files_indexed": files, "chunks_indexed": len(chunks), "stats": stats}

    def stats(self) -> dict[str, Any]:
        return self.db.code_index_stats()

    def search(self, query: str, limit: int = 6, auto_rebuild: bool = True) -> dict[str, Any]:
        stats = self.stats()
        if auto_rebuild and int(stats.get("chunk_count") or 0) == 0:
            self.rebuild()
        result = self.db.search_code_index(query, limit=limit)
        result["stats"] = self.stats()
        return result

    def repo_map(self, limit: int = 18) -> dict[str, Any]:
        stats = self.stats()
        if int(stats.get("chunk_count") or 0) == 0:
            self.rebuild()
        return self.db.repo_map(limit=limit)


class LLMBridge:
    def __init__(self, db: DB):
        self.db = db
        self.enabled = bool(OPENAI_API_KEY)
        self.model = MODEL_NAME
        self.base_url = OPENAI_BASE_URL or None
        self._client = None

    def _cache_key(self, payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def json_completion(self, system: str, user: str, cache_tag: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        key = self._cache_key({"cache_tag": cache_tag, "system": system, "user": user, "payload": payload})
        cached = self.db.get_cache(key)
        if cached:
            try:
                return json.loads(cached["response_json"])
            except Exception:
                pass
        try:
            from openai import AsyncOpenAI

            if self._client is None:
                self._client = AsyncOpenAI(api_key=OPENAI_API_KEY, base_url=self.base_url)
            resp = await self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.2,
            )
            text = resp.choices[0].message.content or ""
            data = extract_json_block(text)
            if data is not None:
                self.db.set_cache(key, {"system": system, "user": user, "payload": payload}, data)
                return data
        except Exception:
            return None
        return None


def build_summary(messages: list[dict[str, Any]], memories: list[dict[str, Any]]) -> str:
    if not messages:
        return ""
    recent = messages[-6:]
    lines = []
    if memories:
        lines.append("记忆: " + "; ".join(shorten(m["content"], 80) for m in memories[:3]))
    for message in recent:
        role = "U" if message["role"] == "user" else "A"
        lines.append(f"{role}: {shorten(message['content'], 100)}")
    return shorten("\n".join(lines), 1200)


def extract_paths(text: str) -> list[str]:
    candidates = re.findall(r"(?:[A-Za-z]:\\\\[^\\\s`\"']+|(?:\.{1,2}[\\/])?[^\\\s`\"']+\.[A-Za-z0-9]{1,8})", text)
    unique: list[str] = []
    for item in candidates:
        normalized = item.strip().strip(".,;:()[]{}")
        if normalized and normalized not in unique:
            unique.append(normalized)
    return unique


def classify_intent(message: str) -> str:
    msg = message.lower()
    if any(key in message for key in ["记住", "memory", "偏好", "约束", "保存为记忆"]):
        return "memory"
    if any(key in message for key in ["入口", "启动方式", "怎么启动", "如何启动", "项目结构", "主要模块"]):
        return "rag"
    if any(
        key in message
        for key in ["代码理解", "源码", "调用链", "执行流程", "函数", "类", "在哪里", "在哪", "代码", "分析", "解释", "定位", "MiniCode", "Agent"]
    ) or any(key in msg for key in ["symbol", "reference", "call graph"]):
        return "rag"
    if any(key in message for key in ["搜索", "查找", "定位", "find", "search", "grep"]):
        return "search"
    if any(key in message for key in ["查看", "打开", "读取", "read", "inspect", "show"]):
        return "read"
    if any(key in message for key in ["运行", "执行", "测试", "pytest", "lint", "format"]):
        return "run"
    if any(key in message for key in ["tree", "目录", "结构", "workspace", "文件树"]):
        return "tree"
    if any(key in message for key in ["修改", "修复", "新增", "添加", "重构", "实现", "patch", "edit", "改"]):
        return "edit"
    if "总结" in message or "压缩" in message or "汇总" in message:
        return "summarize"
    return "chat"


def plan_for_intent(intent: str) -> list[str]:
    plans = {
        "rag": ["检索代码索引", "拼接候选上下文", "给出可追溯代码依据"],
        "search": ["定位候选文件", "读取关键片段", "整理最相关结果"],
        "read": ["找到目标文件", "提取上下文", "给出要点"],
        "run": ["识别命令", "做安全检查", "执行并反馈"],
        "tree": ["展示目录结构", "聚焦关键文件", "说明下一步"],
        "edit": ["定位修改目标", "形成改动方案", "必要时执行写入", "建议运行校验"],
        "memory": ["提取可复用信息", "写入记忆", "回显结果"],
        "summarize": ["压缩历史上下文", "保留关键决策", "输出摘要"],
        "chat": ["理解意图", "给出建议", "必要时继续拆分"],
    }
    return plans[intent]


def extract_command(message: str) -> str:
    fenced = re.findall(r"```(?:bash|sh|powershell|cmd)?\s*(.*?)```", message, re.S)
    if fenced:
        return fenced[0].strip()
    match = re.search(r"(?:运行|执行|run|exec|command)[:：]?\s*(.+)", message, re.I)
    if match:
        return match.group(1).strip()
    return message.strip()


def detect_patch_request(message: str) -> dict[str, str] | None:
    patterns = [
        r"(?:预览|preview|生成diff|diff)\s+(.+?)\s+(?:把|将)\s+(.+?)\s+(?:改成|替换为|to)\s+(.+)$",
        r"(?:patch|修改|替换)\s+(.+?)\s+(.+?)\s*(?:=>|->|为|to)\s*(.+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, message, re.I)
        if not match:
            continue
        path, old, new = [part.strip().strip("'\"") for part in match.groups()]
        if path and old and new:
            return {"path": path, "old": old, "new": new}
    return None


def infer_memory_notes(message: str, reply: str) -> list[dict[str, Any]]:
    notes = []
    if any(key in message for key in ["以后", "默认", "偏好", "记住", "请始终", "不要"]):
        notes.append(
            {
                "category": "preference",
                "content": shorten(message, 220),
                "score": 0.75,
            }
        )
    if any(key in reply for key in ["已定位", "已找到", "已记住", "已执行"]):
        notes.append(
            {
                "category": "decision",
                "content": shorten(reply, 220),
                "score": 0.55,
            }
        )
    return notes


class MiniCodeAgent:
    def __init__(self, db: DB, tools: WorkspaceTools, indexer: CodeIndexer, llm: LLMBridge):
        self.db = db
        self.tools = tools
        self.indexer = indexer
        self.llm = llm

    def _tool_action(self, name: str, args: dict[str, Any], risk: str = "safe", reason: str = "") -> ToolAction:
        return ToolAction(tool=name, args=args, risk=risk, reason=reason)

    def _heuristic_actions(self, intent: str, message: str, confirm_risky: bool) -> list[ToolAction]:
        if intent == "rag":
            return [self._tool_action("rag_search", {"query": message, "limit": 6})]
        if intent == "search":
            return [
                self._tool_action("repo_map", {"limit": 12}),
                self._tool_action("rag_search", {"query": message, "limit": 6}),
                self._tool_action("search", {"query": message, "limit": MAX_SEARCH_RESULTS}),
            ]
        if intent == "read":
            paths = extract_paths(message)
            if paths:
                return [self._tool_action("read", {"path": paths[0], "start": 1, "end": 160})]
            return [
                self._tool_action("search", {"query": message, "limit": 5}),
            ]
        if intent == "tree":
            paths = extract_paths(message)
            return [
                self._tool_action("tree", {"path": paths[0] if paths else ".", "max_depth": 2}),
                self._tool_action("repo_map", {"limit": 12}),
            ]
        if intent == "run":
            return [self._tool_action("run", {"command": extract_command(message), "confirm": confirm_risky}, risk="risky")]
        if intent == "memory":
            return [self._tool_action("remember", {"category": "note", "content": message, "score": 0.7})]
        if intent == "edit":
            patch = detect_patch_request(message)
            if patch:
                tool_name = "apply_patch" if confirm_risky else "patch_preview"
                return [self._tool_action(tool_name, patch, risk="risky" if tool_name == "apply_patch" else "safe")]
            paths = extract_paths(message)
            if paths:
                return [
                    self._tool_action("read", {"path": paths[0], "start": 1, "end": 160}),
                    self._tool_action("validate", {"paths": [paths[0]]}),
                ]
            return [
                self._tool_action("repo_map", {"limit": 12}),
                self._tool_action("search", {"query": message, "limit": 5}),
            ]
        if intent == "summarize":
            return []
        return []

    async def _llm_request(self, session: dict[str, Any], message: str, context: str, memories: list[dict[str, Any]], intent: str) -> dict[str, Any] | None:
        if not self.llm.enabled:
            return None
        system = textwrap.dedent(
            """
            你是 MiniCode，一个面向代码仓库的智能体。
            你要遵守安全规则：高风险 shell、文件写入、删除、外部网络请求都要标记为 risky，并解释原因。
            你需要输出 JSON，字段如下：
            {
              "reply": "给用户的简短回复",
              "plan": ["步骤1", "步骤2"],
              "actions": [
                {"tool": "rag_search|repo_map|search|read|tree|patch_preview|apply_patch|write|replace|append|delete|run|validate|remember", "args": {...}, "risk": "safe|risky", "reason": "..."}
              ],
              "memory": [
                {"category": "preference|decision|task|note", "content": "...", "score": 0.5}
              ],
              "summary": "对当前会话的压缩摘要",
              "needs_confirmation": false
            }
            只输出 JSON，不要解释。
            """
        ).strip()
        user = json.dumps(
            {
                "session": session,
                "intent": intent,
                "message": message,
                "context": context[:MAX_CONTEXT_CHARS],
                "memories": memories[:8],
                "workspace_root": str(WORKSPACE_ROOT),
                "allowed_tools": ["rag_search", "repo_map", "search", "read", "tree", "patch_preview", "apply_patch", "write", "replace", "append", "delete", "run", "validate", "remember"],
            },
            ensure_ascii=False,
            indent=2,
        )
        data = await self.llm.json_completion(system, user, cache_tag="minicode-agent", payload={"intent": intent})
        return data if isinstance(data, dict) else None

    def _format_tool_result(self, result: dict[str, Any]) -> str:
        if not result:
            return ""
        if result.get("tool") == "rag_search":
            items = result.get("data", {}).get("items", [])
            if not items:
                return "代码索引里没有找到直接命中的上下文。"
            lines = []
            for item in items[:4]:
                lines.append(f"- {item['path']}:{item['start_line']}-{item['end_line']} (score {item['score']}): {shorten(' | '.join(item.get('snippets', [])), 180)}")
            return "\n".join(lines)
        if result.get("tool") == "repo_map":
            files = result.get("data", {}).get("files", [])
            if not files:
                return "代码索引尚未生成。"
            return "\n".join(
                f"- {item['path']} ({item['chunk_count']} chunks, lines {item['first_line']}-{item['last_line']})"
                for item in files[:8]
            )
        if result.get("tool") in {"patch_preview", "apply_patch"}:
            diff = result.get("data", {}).get("diff", "")
            return diff[:1200] if diff else result.get("summary", "")
        if result.get("tool") == "search":
            items = result.get("data", {}).get("items", [])
            if not items:
                return "没有搜到直接命中的文件。"
            lines = []
            for item in items[:4]:
                lines.append(f"- {item['path']} (score {item['score']}): {shorten(' | '.join(item.get('snippets', [])), 140)}")
            return "\n".join(lines)
        if result.get("tool") in {"read", "tree"}:
            return result.get("summary") or result.get("data", {}).get("text", "")
        if result.get("tool") == "run":
            data = result.get("data", {})
            parts = [data.get("summary", "")]
            if data.get("returncode") is not None:
                parts.append(f"returncode={data['returncode']}")
            return " | ".join(p for p in parts if p)
        if result.get("tool") == "validate":
            checks = result.get("data", {}).get("checks", [])
            return "\n".join(f"- {item['path']} [{item['kind']}]: {item['summary']}" for item in checks[:8])
        if result.get("tool") == "rebuild_index":
            data = result.get("data", {})
            return f"索引文件 {data.get('files_indexed', 0)} 个，chunk {data.get('chunks_indexed', 0)} 个"
        return result.get("summary", "")

    def _execute_action(self, session_id: str, action: ToolAction, confirm_risky: bool) -> ToolResult:
        tool = action.tool
        args = dict(action.args or {})
        if tool == "rag_search":
            data = self.indexer.search(args.get("query", ""), limit=parse_int(args.get("limit"), 6))
            summary = f"代码索引命中 {data['count']} 个 chunk"
            ok = True
        elif tool == "repo_map":
            data = self.indexer.repo_map(limit=parse_int(args.get("limit"), 18))
            summary = f"Repo map 展示 {len(data.get('files', []))} 个索引文件"
            ok = True
        elif tool == "rebuild_index":
            data = self.indexer.rebuild()
            summary = f"重建代码索引: {data['files_indexed']} 个文件 / {data['chunks_indexed']} 个 chunk"
            ok = True
        elif tool == "search":
            data = self.tools.search(args.get("query", ""), limit=parse_int(args.get("limit"), MAX_SEARCH_RESULTS))
            summary = f"找到 {data['count']} 个候选文件"
            ok = True
        elif tool == "read":
            data = self.tools.read(args.get("path", ""), start=parse_int(args.get("start"), 1), end=parse_int(args.get("end"), 120))
            summary = f"读取 {data['path']} 的 {data['start']}-{data['end']} 行"
            ok = True
        elif tool == "tree":
            data = self.tools.tree(args.get("path", "."), max_depth=parse_int(args.get("max_depth"), 2))
            summary = f"展开目录 {data['root']}"
            ok = True
        elif tool == "run":
            args["confirm"] = bool(args.get("confirm") or confirm_risky)
            data = self.tools.run(args.get("command", ""), cwd=args.get("cwd"), confirm=bool(args.get("confirm")), timeout=parse_int(args.get("timeout"), 120))
            ok = bool(data.get("ok"))
            summary = data.get("summary", "命令执行完成")
            if data.get("requires_confirmation"):
                return ToolResult(tool=tool, ok=False, summary=summary, data=data, requires_confirmation=True)
        elif tool == "write":
            if not confirm_risky:
                return ToolResult(tool=tool, ok=False, summary="写入需要确认后再执行", data=args, requires_confirmation=True)
            data = self.tools.write(args.get("path", ""), args.get("content", ""), mode=args.get("mode", "overwrite"))
            summary = f"写入 {data['path']}"
            ok = True
        elif tool == "replace":
            if not confirm_risky:
                return ToolResult(tool=tool, ok=False, summary="替换需要确认后再执行", data=args, requires_confirmation=True)
            data = self.tools.replace(args.get("path", ""), args.get("old", ""), args.get("new", ""), count=parse_int(args.get("count"), 1))
            summary = f"替换 {data['path']}"
            ok = bool(data.get("changed"))
        elif tool == "patch_preview":
            data = self.tools.preview_replace(args.get("path", ""), args.get("old", ""), args.get("new", ""), count=parse_int(args.get("count"), 1))
            summary = f"生成 {data['path']} 的 diff 预览" if data.get("changed") else f"{data.get('path', '')} 未产生 diff"
            ok = bool(data.get("changed"))
        elif tool == "apply_patch":
            if not confirm_risky:
                preview = self.tools.preview_replace(args.get("path", ""), args.get("old", ""), args.get("new", ""), count=parse_int(args.get("count"), 1))
                return ToolResult(tool=tool, ok=False, summary="应用 patch 需要确认后再执行", data=preview, requires_confirmation=True)
            data = self.tools.apply_patch(args.get("path", ""), args.get("old", ""), args.get("new", ""), count=parse_int(args.get("count"), 1))
            summary = f"应用 patch 到 {data.get('path', '')}"
            ok = bool(data.get("changed"))
        elif tool == "append":
            if not confirm_risky:
                return ToolResult(tool=tool, ok=False, summary="追加需要确认后再执行", data=args, requires_confirmation=True)
            data = self.tools.append_text(args.get("path", ""), args.get("content", ""))
            summary = f"追加写入 {data['path']}"
            ok = True
        elif tool == "delete":
            if not confirm_risky:
                return ToolResult(tool=tool, ok=False, summary="删除需要确认后再执行", data=args, requires_confirmation=True)
            data = self.tools.delete(args.get("path", ""))
            summary = f"删除 {data['path']}"
            ok = True
        elif tool == "validate":
            data = self.tools.validate(args.get("paths"))
            summary = data.get("summary", "validation finished")
            ok = bool(data.get("ok"))
        elif tool == "remember":
            content = args.get("content", "")
            category = args.get("category", "note")
            score = float(args.get("score", 0.6))
            self.db.add_memory(session_id, category, content, score)
            data = {"category": category, "content": content, "score": score}
            summary = f"已记住一条 {category} 记忆"
            ok = True
        else:
            data = {"error": f"unknown tool {tool}"}
            summary = f"未知工具: {tool}"
            ok = False
        self.db.add_tool_run(session_id, tool, args, data, ok)
        return ToolResult(tool=tool, ok=ok, summary=summary, data=data)

    async def chat(self, session_id: str | None, message: str, confirm_risky: bool = False) -> dict[str, Any]:
        clean_message = message.strip()
        title_hint = shorten(clean_message, 24) or "MiniCode session"
        session_id = self.db.ensure_session(session_id, title_hint=title_hint)
        session = self.db.get_session(session_id) or {"id": session_id, "title": title_hint, "summary": ""}

        if session.get("title") in {"New session", "MiniCode session"} and clean_message:
            self.db.update_session(session_id, title=title_hint)
            session = self.db.get_session(session_id) or session

        self.db.add_message(session_id, "user", clean_message, meta={"confirm_risky": confirm_risky})

        recent_messages = self.db.list_messages(session_id, limit=18)
        relevant_memories = self.db.search_memories(clean_message, limit=6)
        context = build_summary(recent_messages, relevant_memories)
        intent = classify_intent(clean_message)

        llm_payload = await self._llm_request(session, clean_message, context, relevant_memories, intent)

        plan = plan_for_intent(intent)
        actions: list[ToolAction] = self._heuristic_actions(intent, clean_message, confirm_risky)
        reply = ""
        memory_candidates: list[dict[str, Any]] = []
        needs_confirmation = False

        if isinstance(llm_payload, dict):
            reply = str(llm_payload.get("reply") or "").strip()
            plan = [str(p).strip() for p in llm_payload.get("plan", []) if str(p).strip()] or plan
            raw_actions = llm_payload.get("actions", [])
            if isinstance(raw_actions, list) and raw_actions:
                actions = [
                    ToolAction(
                        tool=str(item.get("tool", "")).strip(),
                        args=dict(item.get("args") or {}),
                        risk=str(item.get("risk", "safe")),
                        reason=str(item.get("reason", "")),
                    )
                    for item in raw_actions
                    if str(item.get("tool", "")).strip()
                ]
            raw_memory = llm_payload.get("memory", [])
            if isinstance(raw_memory, list):
                for item in raw_memory:
                    if isinstance(item, dict) and item.get("content"):
                        memory_candidates.append(
                            {
                                "category": str(item.get("category", "note")),
                                "content": str(item["content"]),
                                "score": float(item.get("score", 0.6)),
                            }
                        )
            if llm_payload.get("needs_confirmation"):
                needs_confirmation = True

        role_trace: list[dict[str, Any]] = []

        def record_role(role: str, status: str, detail: dict[str, Any]) -> None:
            payload = {"role": role, "status": status, "detail": detail}
            role_trace.append(payload)
            self.db.add_role_trace(session_id, role, status, detail)

        record_role(
            "Planner",
            "ok",
            {
                "intent": intent,
                "plan": plan,
                "actions": [asdict(action) for action in actions],
                "memories_considered": len(relevant_memories),
            },
        )
        risky_actions = [asdict(action) for action in actions if action.risk == "risky" or action.tool in {"write", "replace", "apply_patch", "append", "delete", "run"}]
        record_role(
            "RiskChecker",
            "needs_confirmation" if risky_actions and not confirm_risky else "ok",
            {
                "confirm_risky": confirm_risky,
                "risky_actions": risky_actions,
            },
        )

        tool_results: list[ToolResult] = []
        changed_paths: list[str] = []
        for action in actions:
            if action.risk == "risky" and not confirm_risky:
                tool_results.append(
                    ToolResult(
                        tool=action.tool,
                        ok=False,
                        summary=f"{action.tool} 需要确认后再执行",
                        data={"action": asdict(action)},
                        requires_confirmation=True,
                    )
                )
                needs_confirmation = True
                continue
            result = self._execute_action(session_id, action, confirm_risky=confirm_risky)
            if result.requires_confirmation:
                needs_confirmation = True
            tool_results.append(result)
            if result.ok and result.tool in {"write", "replace", "apply_patch", "append"}:
                path = str(result.data.get("path") or "").strip()
                if path and path not in changed_paths:
                    changed_paths.append(path)

        record_role(
            "Executor",
            "ok" if any(result.ok for result in tool_results) else "idle",
            {
                "tool_count": len(tool_results),
                "tool_summaries": [result.summary for result in tool_results[:6]],
            },
        )

        if changed_paths:
            validation_result = self._execute_action(
                session_id,
                ToolAction(tool="validate", args={"paths": changed_paths}, reason="post-edit validation"),
                confirm_risky=True,
            )
            tool_results.append(validation_result)
            index_refresh = self.indexer.rebuild()
            tool_results.append(
                ToolResult(
                    tool="rebuild_index",
                    ok=bool(index_refresh.get("ok")),
                    summary=f"重建代码索引: {index_refresh.get('files_indexed', 0)} 个文件 / {index_refresh.get('chunks_indexed', 0)} 个 chunk",
                    data=index_refresh,
                )
            )
            record_role(
                "Reviewer",
                "ok" if validation_result.ok else "failed",
                {
                    "changed_paths": changed_paths,
                    "validation": validation_result.data,
                    "index_refresh": index_refresh,
                },
            )
        else:
            record_role(
                "Reviewer",
                "skipped",
                {
                    "reason": "no modified files",
                    "changed_paths": changed_paths,
                },
            )

        if not reply:
            if intent == "rag":
                reply = "我先从代码索引里检索相关上下文。"
            elif intent == "search":
                reply = "我先把相关文件找出来了。"
            elif intent == "read":
                reply = "我已经帮你读出目标片段。"
            elif intent == "edit":
                reply = "我先定位修改范围，确认后可以直接落盘。"
            elif intent == "run":
                reply = "我会先执行这个命令并把结果回传。"
            elif intent == "memory":
                reply = "我已经把这条偏好记下来了。"
            elif intent == "tree":
                reply = "这是当前工作区结构。"
            else:
                reply = "我已经根据当前任务拆了步骤。"

        if not memory_candidates:
            memory_candidates = infer_memory_notes(clean_message, reply)
        for note in memory_candidates:
            self.db.add_memory(
                session_id,
                note["category"],
                note["content"],
                float(note.get("score", 0.6)),
            )

        summary = build_summary(self.db.list_messages(session_id, limit=14), self.db.list_memories(session_id, limit=6))
        self.db.update_session(session_id, summary=summary)

        lines = [reply]
        if plan:
            lines.append("计划:")
            lines.extend(f"{idx}. {item}" for idx, item in enumerate(plan, start=1))
        if tool_results:
            lines.append("工具结果:")
            for result in tool_results:
                lines.append(f"- {result.summary}")
                snippet = self._format_tool_result({"tool": result.tool, "summary": result.summary, "data": result.data})
                if snippet and snippet != result.summary:
                    lines.append(textwrap.indent(shorten(snippet, 900), "  "))
        if needs_confirmation:
            lines.append("有高风险步骤，确认后再继续。")
        if memory_candidates:
            lines.append("记忆:")
            lines.extend(f"- {shorten(item['content'], 140)}" for item in memory_candidates[:3])

        final_reply = "\n".join(lines)
        self.db.add_message(
            session_id,
            "assistant",
            final_reply,
            meta={
                "intent": intent,
                "needs_confirmation": needs_confirmation,
                "tool_count": len(tool_results),
                "role_trace_count": len(role_trace),
            },
        )

        return {
            "session_id": session_id,
            "title": self.db.get_session(session_id)["title"],
            "intent": intent,
            "reply": final_reply,
            "plan": plan,
            "needs_confirmation": needs_confirmation,
            "tools": [asdict(item) for item in tool_results],
            "role_trace": role_trace,
            "memories": self.db.list_memories(session_id, limit=8),
            "summary": summary,
            "workspace_root": str(WORKSPACE_ROOT),
        }

    def direct_tool(self, name: str, args: dict[str, Any], session_id: str | None = None, confirm: bool = False) -> dict[str, Any]:
        session_id = self.db.ensure_session(session_id, title_hint="工具调用")
        action = ToolAction(tool=name, args=args, risk="risky" if name in {"write", "replace", "apply_patch", "append", "delete", "run"} else "safe")
        result = self._execute_action(session_id, action, confirm_risky=confirm)
        payload = {"session_id": session_id, **asdict(result)}
        self.db.add_role_trace(
            session_id,
            "Executor",
            "ok" if result.ok else "failed",
            {"tool": name, "args": args, "summary": result.summary},
        )
        if result.ok and result.tool in {"write", "replace", "apply_patch", "append"}:
            path = str(result.data.get("path") or "").strip()
            if path:
                validation = self.tools.validate([path])
                index_refresh = self.indexer.rebuild()
                payload["validation"] = validation
                payload["index_refresh"] = index_refresh
                self.db.add_role_trace(
                    session_id,
                    "Reviewer",
                    "ok" if validation.get("ok") else "failed",
                    {"changed_paths": [path], "validation": validation, "index_refresh": index_refresh},
                )
        elif result.ok and result.tool in {"rag_search", "repo_map", "patch_preview", "search", "read", "tree", "validate", "rebuild_index", "remember"}:
            self.db.add_role_trace(
                session_id,
                "Reviewer",
                "skipped",
                {"reason": "direct tool call", "tool": name},
            )
        return payload


DB_PATH = DATA_DIR / "minicode.sqlite3"
db = DB(DB_PATH)
tools = WorkspaceTools(WORKSPACE_ROOT)
indexer = CodeIndexer(db, tools)
llm = LLMBridge(db)
brain = MiniCodeAgent(db, tools, indexer, llm)

if SHOW_BANNER:
    print(BANNER)
    print(f"  工作区: {WORKSPACE_ROOT}")
    print(f"  数据:   {DATA_DIR}")
    print(f"  模型:   {'enabled' if llm.enabled else 'offline heuristics'}")
    print(f"  命令:   {'enabled' if ALLOW_SHELL else 'confirm-only'}")
    print("")

if int(indexer.stats().get("chunk_count") or 0) == 0:
    try:
        indexer.rebuild()
    except Exception:
        pass


class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str = Field(..., min_length=1)
    confirm_risky: bool = False


class ToolRequest(BaseModel):
    name: str = Field(..., min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None
    confirm: bool = False


class PatchRequest(BaseModel):
    path: str = Field(..., min_length=1)
    old: str = Field(..., min_length=1)
    new: str
    count: int = 1
    session_id: str | None = None
    confirm: bool = False

    def tool_args(self) -> dict[str, Any]:
        return {"path": self.path, "old": self.old, "new": self.new, "count": self.count}


class SessionCreateRequest(BaseModel):
    title: str | None = None


app = FastAPI(title="MiniCode", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(Exception)
async def unhandled_exception_handler(_: Any, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": str(exc)})


@app.get("/")
async def root():
    index = STATIC_DIR / "index.html"
    if not index.exists():
        raise HTTPException(status_code=500, detail="Frontend is missing")
    return FileResponse(index)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "workspace_root": str(WORKSPACE_ROOT),
        "model_enabled": llm.enabled,
        "allow_shell": ALLOW_SHELL,
    }


@app.get("/api/bootstrap")
async def bootstrap():
    session = db.list_sessions(limit=20)
    memory = db.list_memories(limit=20)
    tree = tools.tree(".", max_depth=2, max_entries=140)
    return {
        "workspace_root": str(WORKSPACE_ROOT),
        "sessions": session,
        "memories": memory,
        "tree": tree,
        "code_index": indexer.stats(),
        "model_enabled": llm.enabled,
        "allow_shell": ALLOW_SHELL,
    }


@app.get("/api/sessions")
async def list_sessions():
    return {"items": db.list_sessions(limit=40)}


@app.post("/api/sessions")
async def create_session(body: SessionCreateRequest):
    title = body.title or "New session"
    session_id = db.create_session(title=title)
    return {"session_id": session_id, "title": title}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "session": session,
        "messages": db.list_messages(session_id, limit=200),
        "memories": db.list_memories(session_id, limit=60),
        "tool_runs": db.list_tool_runs(session_id, limit=60),
        "role_traces": db.list_role_traces(session_id, limit=80),
    }


@app.post("/api/chat")
async def chat(body: ChatRequest):
    return await brain.chat(body.session_id, body.message, confirm_risky=body.confirm_risky)


@app.post("/api/tool")
async def direct_tool(body: ToolRequest):
    if body.name in {"write", "replace", "apply_patch", "append", "delete", "run"} and not body.confirm:
        raise HTTPException(status_code=400, detail="This tool needs confirm=true")
    return brain.direct_tool(body.name, body.args, session_id=body.session_id, confirm=body.confirm)


@app.get("/api/tree")
async def api_tree(path: str = ".", depth: int = 2):
    return tools.tree(path, max_depth=depth)


@app.get("/api/search")
async def api_search(q: str, limit: int = MAX_SEARCH_RESULTS):
    return tools.search(q, limit=limit)


@app.get("/api/rag")
async def api_rag(q: str, limit: int = 6):
    return indexer.search(q, limit=limit)


@app.get("/api/repo-map")
async def api_repo_map(limit: int = 18):
    return indexer.repo_map(limit=limit)


@app.post("/api/patch/preview")
async def api_patch_preview(body: PatchRequest):
    return brain.direct_tool("patch_preview", body.tool_args(), session_id=body.session_id, confirm=False)


@app.post("/api/patch/apply")
async def api_patch_apply(body: PatchRequest):
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Patch apply needs confirm=true")
    return brain.direct_tool("apply_patch", body.tool_args(), session_id=body.session_id, confirm=True)


@app.post("/api/index/rebuild")
async def api_rebuild_index():
    return indexer.rebuild()


@app.get("/api/read")
async def api_read(path: str, start: int = 1, end: int = 120):
    return tools.read(path, start=start, end=end)


@app.get("/api/memories")
async def api_memories(session_id: str | None = None, query: str | None = None):
    if query:
        return {"items": db.search_memories(query, limit=20)}
    return {"items": db.list_memories(session_id=session_id, limit=60)}


@app.post("/api/memories")
async def add_memory(session_id: str | None = None, category: str = "note", content: str = "", score: float = 0.6):
    sid = db.ensure_session(session_id, title_hint="Memory")
    db.add_memory(sid, category, content, score)
    return {"session_id": sid, "category": category, "content": content, "score": score}
