"""Capability-scoped, read-only loopback server for timed multi-stem playback."""

from __future__ import annotations

import importlib.resources
import json
import mimetypes
import re
import secrets
import threading
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO

from .paths import project_artifact
from .state import load_manifest
from .types import ProjectManifest

_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


@dataclass(frozen=True)
class ServedFile:
    path: Path
    content_type: str


def _content_type(path: Path) -> str:
    guessed, _encoding = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _payload(manifest: ProjectManifest, token: str) -> dict[str, object]:
    return {
        "schema": "kilix.playalong.web/v1",
        "project": {
            "id": manifest["id"],
            "title": manifest["title"],
            "artist": manifest["artist"],
            "duration": manifest["source"].get("duration", 0),
        },
        "tracks": [
            {
                "id": track["id"],
                "label": track["label"],
                "kind": track["kind"],
                "defaultMuted": track["default_muted"],
                "url": f"/{token}/media/{track['id']}",
            }
            for track in manifest["tracks"]
        ],
        "lyricsUrl": f"/{token}/api/lyrics",
        "tabUrl": f"/{token}/api/tab",
        "printUrl": f"/{token}/export/print",
        "asciiTabUrl": f"/{token}/export/tab.txt",
        "midiUrl": f"/{token}/export/guitar.mid",
    }


def _handler(
    project_dir: Path,
    manifest: ProjectManifest,
    token: str,
    allowed_hosts: set[str],
) -> type[BaseHTTPRequestHandler]:
    track_files = {
        track["id"]: ServedFile(
            project_artifact(project_dir, track["path"]),
            _content_type(Path(track["path"])),
        )
        for track in manifest["tracks"]
    }
    lyrics = manifest["lyrics"] or {}
    tablature = manifest["tablature"] or {}

    def optional_artifact(section: dict[str, object], name: str) -> Path:
        value = section.get(name)
        if isinstance(value, str) and value:
            return project_artifact(project_dir, value)
        return project_dir / ".unavailable-artifact"

    exports = {
        "print": ServedFile(project_dir / "exports" / "playalong.html", "text/html; charset=utf-8"),
        "tab.txt": ServedFile(
            optional_artifact(tablature, "ascii_path"),
            "text/plain; charset=utf-8",
        ),
        "guitar.mid": ServedFile(optional_artifact(tablature, "midi_path"), "audio/midi"),
    }
    api_files = {
        "lyrics": ServedFile(optional_artifact(lyrics, "path"), "application/json"),
        "tab": ServedFile(optional_artifact(tablature, "path"), "application/json"),
    }
    web_root = importlib.resources.files("kilix_playalong").joinpath("web")

    class Handler(BaseHTTPRequestHandler):
        server_version = "KilixPlayalong/0.1"

        def log_message(self, _format: str, *_arguments: object) -> None:
            return

        def _security_headers(self, content_security_policy: str) -> None:
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header(
                "Permissions-Policy",
                "accelerometer=(), camera=(), geolocation=(), microphone=()",
            )
            self.send_header("Content-Security-Policy", content_security_policy)

        def _headers(self, status: HTTPStatus, content_type: str, length: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self._security_headers(
                "default-src 'self'; base-uri 'none'; form-action 'none'; "
                "frame-ancestors 'none'; script-src 'self'; style-src 'self'; "
                "media-src 'self'; connect-src 'self'; img-src 'self' data:; "
                "object-src 'none'",
            )
            self.end_headers()

        def _reject(self, status: HTTPStatus = HTTPStatus.NOT_FOUND) -> None:
            body = f"{status.value} {status.phrase}\n".encode("ascii")
            self._headers(status, "text/plain; charset=utf-8", len(body))
            if self.command != "HEAD":
                self.wfile.write(body)

        def _authorized(self) -> bool:
            host = self.headers.get("Host", "").lower()
            return host in allowed_hosts and self.path.startswith(f"/{token}/")

        def _bytes(self, body: bytes, content_type: str) -> None:
            self._headers(HTTPStatus.OK, content_type, len(body))
            if self.command != "HEAD":
                self.wfile.write(body)

        def _serve_file(self, item: ServedFile, *, ranges: bool = False) -> None:
            try:
                size = item.path.stat().st_size
            except OSError:
                self._reject()
                return
            start, end = 0, size - 1
            status = HTTPStatus.OK
            range_header = self.headers.get("Range") if ranges else None
            if range_header:
                match = _RANGE.fullmatch(range_header.strip())
                if match is None:
                    self._reject(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    return
                left, right = match.groups()
                if not left and not right:
                    self._reject(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    return
                if left:
                    start = int(left)
                    end = min(size - 1, int(right)) if right else size - 1
                else:
                    suffix = min(size, int(right))
                    start = size - suffix
                if start > end or start >= size:
                    self._reject(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    return
                status = HTTPStatus.PARTIAL_CONTENT
            length = max(0, end - start + 1)
            self.send_response(status)
            self.send_header("Content-Type", item.content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if item.content_type.startswith("text/html"):
                policy = (
                    "default-src 'none'; base-uri 'none'; form-action 'none'; "
                    "frame-ancestors 'none'; style-src 'unsafe-inline'; "
                    "script-src 'unsafe-inline'"
                )
            else:
                policy = "default-src 'none'; base-uri 'none'; frame-ancestors 'none'"
            self._security_headers(policy)
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            with item.path.open("rb") as stream:
                stream.seek(start)
                self._copy(stream, length)

        def _copy(self, source: BinaryIO, remaining: int) -> None:
            while remaining:
                block = source.read(min(64 * 1024, remaining))
                if not block:
                    return
                self.wfile.write(block)
                remaining -= len(block)

        def _route(self) -> None:
            if not self._authorized():
                self._reject()
                return
            route = self.path.split("?", 1)[0][len(token) + 2 :]
            if route in {"", "/"}:
                body = web_root.joinpath("index.html").read_bytes()
                self._bytes(body, "text/html; charset=utf-8")
            elif route == "static/app.js":
                self._bytes(
                    web_root.joinpath("app.js").read_bytes(),
                    "text/javascript; charset=utf-8",
                )
            elif route == "static/styles.css":
                self._bytes(web_root.joinpath("styles.css").read_bytes(), "text/css; charset=utf-8")
            elif route == "api/project":
                body = json.dumps(_payload(manifest, token)).encode("utf-8")
                self._bytes(body, "application/json")
            elif route.startswith("api/") and route[4:] in api_files:
                self._serve_file(api_files[route[4:]])
            elif route.startswith("media/") and route[6:] in track_files:
                self._serve_file(track_files[route[6:]], ranges=True)
            elif route.startswith("export/") and route[7:] in exports:
                self._serve_file(exports[route[7:]])
            else:
                self._reject()

        def do_GET(self) -> None:
            self._route()

        def do_HEAD(self) -> None:
            self._route()

        def do_POST(self) -> None:
            self._reject(HTTPStatus.METHOD_NOT_ALLOWED)

    return Handler


class PlayalongServer:
    def __init__(self, project_dir: Path, *, port: int = 0):
        self.project_dir = project_dir
        self.manifest = load_manifest(project_dir)
        self.token = secrets.token_urlsafe(24)
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"} if port else set()
        handler = _handler(project_dir, self.manifest, self.token, allowed_hosts)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        actual_port = self.httpd.server_address[1]
        if port == 0:
            allowed_hosts.update({f"127.0.0.1:{actual_port}", f"localhost:{actual_port}"})
        self.url = f"http://127.0.0.1:{actual_port}/{self.token}/"
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._thread is not None:
            self.httpd.shutdown()
            self._thread.join(timeout=5)
            self._thread = None
        self.httpd.server_close()

    def serve(self, *, open_browser: bool = True) -> None:
        if open_browser:
            webbrowser.open(self.url)
        try:
            self.httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.httpd.server_close()

    def __enter__(self) -> PlayalongServer:
        self.start()
        return self

    def __exit__(self, *_arguments: object) -> None:
        self.close()
