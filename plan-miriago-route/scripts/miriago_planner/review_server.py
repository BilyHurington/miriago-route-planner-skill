"""Local review backend: static preview files plus a temporary decision store.

Decisions (keep / delete per point) go to a SQLite file with a full change
history. The server never writes a proposal or package; ``apply-review`` turns
the stored decisions into a new draft proposal, which then needs explicit
approval at the curation gate.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
import zipfile
from collections.abc import Mapping
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

VALID_DECISIONS = frozenset({"keep", "delete"})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class ReviewStoreError(ValueError):
    """Raised for invalid decisions or an unreadable decision store."""


class DecisionStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._session():
            pass

    @contextmanager
    def _session(self) -> Any:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS decisions("
            "point_id TEXT PRIMARY KEY, decision TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS history("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, point_id TEXT NOT NULL,"
            " old TEXT, new TEXT, at TEXT NOT NULL)"
        )
        connection.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
        return connection

    def decisions(self) -> dict[str, dict[str, str]]:
        with self.lock, self._session() as connection:
            rows = connection.execute(
                "SELECT point_id, decision, updated_at FROM decisions"
            ).fetchall()
        return {point_id: {"decision": decision, "updatedAt": at} for point_id, decision, at in rows}

    def set_many(self, changes: Mapping[str, Any]) -> int:
        for point_id, decision in changes.items():
            if not isinstance(point_id, str) or not point_id:
                raise ReviewStoreError("Point IDs must be non-empty strings.")
            if decision is not None and decision not in VALID_DECISIONS:
                raise ReviewStoreError(f"Invalid decision for {point_id}: {decision!r}")
        changed = 0
        stamp = _now()
        with self.lock, self._session() as connection:
            for point_id, decision in changes.items():
                row = connection.execute(
                    "SELECT decision FROM decisions WHERE point_id=?", (point_id,)
                ).fetchone()
                old = row[0] if row else None
                if old == decision:
                    continue
                if decision is None:
                    connection.execute("DELETE FROM decisions WHERE point_id=?", (point_id,))
                else:
                    connection.execute(
                        "INSERT INTO decisions(point_id, decision, updated_at) VALUES(?,?,?) "
                        "ON CONFLICT(point_id) DO UPDATE SET decision=excluded.decision, "
                        "updated_at=excluded.updated_at",
                        (point_id, decision, stamp),
                    )
                connection.execute(
                    "INSERT INTO history(point_id, old, new, at) VALUES(?,?,?,?)",
                    (point_id, old, decision, stamp),
                )
                changed += 1
        return changed

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.lock, self._session() as connection:
            rows = connection.execute(
                "SELECT point_id, old, new, at FROM history ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 5_000)),),
            ).fetchall()
        return [dict(zip(("pointId", "old", "new", "at"), row)) for row in rows]

    def set_meta(self, key: str, value: str) -> None:
        with self.lock, self._session() as connection:
            connection.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def meta(self) -> dict[str, str]:
        with self.lock, self._session() as connection:
            return dict(connection.execute("SELECT key, value FROM meta").fetchall())


def load_decisions(path: str | Path) -> dict[str, str]:
    """Read decisions from a review SQLite store or an exported JSON file."""
    source = Path(path).expanduser()
    if not source.is_file():
        raise ReviewStoreError(f"Decision source does not exist: {source}")
    if source.suffix.casefold() in {".sqlite", ".sqlite3", ".db"}:
        return {key: value["decision"] for key, value in DecisionStore(source).decisions().items()}
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewStoreError(f"Cannot read decisions {source}: {exc}") from exc
    result: dict[str, str] = {}
    if isinstance(value.get("decisions"), Mapping):
        for point_id, item in value["decisions"].items():
            decision = item.get("decision") if isinstance(item, Mapping) else item
            if decision in VALID_DECISIONS:
                result[point_id] = decision
    for item in value.get("deleted", []) or []:
        point_id = item.get("id") if isinstance(item, Mapping) else item
        if isinstance(point_id, str):
            result[point_id] = "delete"
    for point_id in value.get("kept", []) or []:
        if isinstance(point_id, str):
            result.setdefault(point_id, "keep")
    return result


def make_handler(root: Path, store: DecisionStore, package_path: Path | None) -> type:
    package_zip = zipfile.ZipFile(package_path) if package_path else None
    asset_names = (
        {name for name in package_zip.namelist() if name.startswith("assets/")}
        if package_zip
        else set()
    )
    package_lock = threading.Lock()

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(root), **kwargs)

        def log_message(self, format: str, *args: Any) -> None:
            if self.path.startswith("/api/"):
                sys.stderr.write(
                    json.dumps({"event": "review_api", "method": self.command, "path": self.path}) + "\n"
                )

        def end_headers(self) -> None:
            if self.path.startswith("/api/") or self.path.endswith(".html"):
                self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def _json(self, code: int, value: Any) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> Any:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 8 * 1024 * 1024:
                raise ReviewStoreError("Request body is too large.")
            return json.loads(self.rfile.read(length) or b"{}")

        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path == "/api/ping":
                return self._json(200, {"ok": True, "packageAssets": bool(package_zip), **store.meta()})
            if url.path == "/api/decisions":
                decisions = store.decisions()
                counts = {
                    name: sum(1 for item in decisions.values() if item["decision"] == name)
                    for name in sorted(VALID_DECISIONS)
                }
                return self._json(200, {"decisions": decisions, "count": counts})
            if url.path == "/api/history":
                limit = int(parse_qs(url.query).get("limit", ["50"])[0])
                return self._json(200, {"history": store.history(limit)})
            if url.path.startswith("/package-asset/") and package_zip:
                name = unquote(url.path[len("/package-asset/") :])
                if name not in asset_names:
                    return self._json(404, {"error": "unknown asset"})
                with package_lock:
                    data = package_zip.read(name)
                self.send_response(200)
                self.send_header("Content-Type", _image_type(name))
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=3600")
                self.end_headers()
                self.wfile.write(data)
                return None
            return super().do_GET()

        def do_PUT(self) -> None:
            url = urlparse(self.path)
            if not url.path.startswith("/api/decisions/"):
                return self._json(404, {"error": "not found"})
            point_id = unquote(url.path[len("/api/decisions/") :])
            try:
                decision = self._body().get("decision")
                store.set_many({point_id: decision})
            except (ReviewStoreError, json.JSONDecodeError, AttributeError) as exc:
                return self._json(400, {"error": str(exc)})
            return self._json(200, {"pointId": point_id, "decision": decision})

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/api/decisions/bulk":
                return self._json(404, {"error": "not found"})
            try:
                items = self._body().get("decisions") or {}
                if not isinstance(items, Mapping):
                    raise ReviewStoreError("decisions must be an object.")
                changed = store.set_many(items)
            except (ReviewStoreError, json.JSONDecodeError, AttributeError) as exc:
                return self._json(400, {"error": str(exc)})
            return self._json(200, {"updated": changed})

    return Handler


def serve_review(
    preview: str | Path,
    *,
    db: str | Path,
    package: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8791,
    ready: Any = None,
) -> None:
    if host not in LOOPBACK_HOSTS:
        raise ReviewStoreError("The review server only binds to a loopback address.")
    page = Path(preview).expanduser().resolve()
    if not page.is_file():
        raise ReviewStoreError(f"Preview page does not exist: {page}")
    store = DecisionStore(db)
    package_path = Path(package).expanduser().resolve() if package else None
    if package_path:
        store.set_meta("package", str(package_path))
    server = ThreadingHTTPServer((host, port), make_handler(page.parent, store, package_path))
    url = f"http://{host}:{server.server_address[1]}/{page.name}"
    print(json.dumps({"status": "review_server_running", "url": url, "db": str(store.path)}, ensure_ascii=False), flush=True)
    if ready is not None:
        ready(server)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _image_type(name: str) -> str:
    lowered = name.casefold()
    if lowered.endswith(".png"):
        return "image/png"
    if lowered.endswith(".webp"):
        return "image/webp"
    if lowered.endswith(".gif"):
        return "image/gif"
    return "image/jpeg"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")
