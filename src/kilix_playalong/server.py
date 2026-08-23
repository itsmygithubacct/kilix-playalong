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

# 19 digits is the widest byte-position any file can have; the bound keeps an
# attacker's digits out of int(). RFC 7233 does permit leading zeros, so a
# byte-position padded past 19 digits gets a 416 rather than a 206 -- no client
# emits one, and 416 is what this module already returns for any Range it
# cannot parse. Every int() below is inside this bound; there is no other.
_RANGE = re.compile(r"^bytes=(\d{0,19})-(\d{0,19})$")
# The policy every reply gets unless the route widens it, including the stdlib's
# own send_error pages, which are generated before any route runs.
_STRICT_POLICY = "default-src 'none'; base-uri 'none'; frame-ancestors 'none'"
# BaseHTTPRequestHandler leaves timeout unset, so a client that connects and then
# stalls holds a handler thread for as long as it likes -- and the request line and
# header block are read before any route runs, so it need not hold the capability
# token to do it. This bounds that read phase only: parse_request() lifts it as soon
# as the header block is in, because a whole-connection timeout would silently truncate a
# browser that pauses a media stream mid-file. The only writes still inside the
# bound are the stdlib's own one-line errors for a request line rejected before
# parse_request (414), never a served file. Loopback keeps the reach local either way.
_HEADER_READ_TIMEOUT = 10.0


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
    # ASCII by construction (secrets.token_urlsafe). A non-ASCII token now fails
    # here instead of building a prefix that could never match.
    capability_prefix = f"/{token}/".encode("ascii")

    class Handler(BaseHTTPRequestHandler):
        server_version = "KilixPlayalong/0.1"
        # send_error() replies are unauthenticated, so they are made to look like
        # _reject()'s: text/plain, "<code> <phrase>". Interpolating %(message)s is
        # only safe because send_error() below forces it to the status code's own
        # phrase -- the two must change together, and
        # test_pre_auth_error_replies_carry_the_security_headers fails if they do not.
        error_content_type = "text/plain; charset=utf-8"
        error_message_format = "%(code)d %(message)s\n"
        # The CSP for the reply being built. end_headers() reads it, so a reply that
        # never reaches a route -- every send_error page -- still gets the strict one.
        response_policy = _STRICT_POLICY

        # socketserver's setup() applies this to the connection before the request
        # line is read; parse_request() lifts it once the header block is in.
        timeout = _HEADER_READ_TIMEOUT

        def log_message(self, _format: str, *_arguments: object) -> None:
            return

        def version_string(self) -> str:
            return self.server_version

        def parse_request(self) -> bool:
            parsed = super().parse_request()
            # The read phase the timeout bounds is over, whether the request parsed
            # or was rejected. A body write must not inherit it: the stdlib swallows
            # a socket timeout, so a paused stream would truncate with no trace.
            self.connection.settimeout(None)
            return parsed

        def send_error(
            self,
            code: int,
            message: str | None = None,
            explain: str | None = None,
        ) -> None:
            # The stdlib passes the offending method or request line as `message`,
            # which lands in the reason phrase and the body. Drop it for the status
            # code's own phrase: an unauthenticated reply echoes nothing.
            super().send_error(code, None, explain)

        # A client that seeks or closes a tab resets the connection mid-write. That is
        # routine, not a fault, and letting it reach handle_error buries real tracebacks.
        # The catch is scoped by exception TYPE, not by which socket raised it, so it is
        # sound only while nothing under a request can raise a ConnectionError of its
        # own: no code on this request path -- this handler or _payload -- opens an
        # outbound connection, subprocess pipe or HTTP client, so every ConnectionError
        # here comes from this client connection. That invariant is not left to the
        # reader: test_request_handling_opens_no_outbound_connections fails if a denied
        # name appears in the parsed tree of _handler or of _payload, which together
        # are this module's whole request path. Below them run only stdlib calls that
        # touch the filesystem or nothing at all (json, pathlib, importlib.resources,
        # re, secrets); the check does not read those, and what bounds them is that
        # none of them opens a socket, not a test.
        def handle_one_request(self) -> None:
            # protocol_version is HTTP/1.0, so close_connection is always True and one
            # instance serves exactly one reply. The reset is what keeps that true of
            # the policy if a later change ever lets a connection carry a second.
            self.response_policy = type(self).response_policy
            try:
                super().handle_one_request()
            except ConnectionError:
                self.close_connection = True

        def end_headers(self) -> None:
            # The seam every reply with a status line passes through -- hand-written
            # ones and the stdlib's send_error pages alike. It is a no-op for an
            # HTTP/0.9 reply, where CPython emits the body alone: a 2-word request
            # line is routed normally and ships its bytes bare, and so does an error
            # for a request line whose version never parsed. (The 414 for an over-long
            # line is not in that class: handle_one_request assigns request_version
            # itself, so that reply is armoured like the rest.) That surface is
            # bounded, not closed -- without the capability token it yields only a
            # one-line plain-text error, and no browser emits an HTTP/0.9 request
            # line. Pinned by test_http_0_9_replies_ship_bare_bodies_bounded_by_the_token.
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header(
                "Permissions-Policy",
                "accelerometer=(), camera=(), geolocation=(), microphone=()",
            )
            self.send_header("Content-Security-Policy", self.response_policy)
            super().end_headers()

        def _headers(self, status: HTTPStatus, content_type: str, length: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.response_policy = (
                "default-src 'self'; base-uri 'none'; form-action 'none'; "
                "frame-ancestors 'none'; script-src 'self'; style-src 'self'; "
                "media-src 'self'; connect-src 'self'; img-src 'self' data:; "
                "object-src 'none'"
            )
            self.end_headers()

        def _reject(self, status: HTTPStatus = HTTPStatus.NOT_FOUND) -> None:
            body = f"{status.value} {status.phrase}\n".encode("ascii")
            self._headers(status, "text/plain; charset=utf-8", len(body))
            if self.command != "HEAD":
                self.wfile.write(body)

        def _authorized(self) -> bool:
            host = self.headers.get("Host", "").lower()
            if host not in allowed_hosts:
                return False
            # parse_request builds self.path with str(raw_requestline, "iso-8859-1"),
            # so every code point is <= U+00FF and this encode cannot raise today; the
            # guard is kept because its failure mode is closed (deny), not open.
            try:
                candidate = self.path[: len(capability_prefix)].encode("iso-8859-1")
            except UnicodeEncodeError:
                return False
            return secrets.compare_digest(candidate, capability_prefix)

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
                self.response_policy = (
                    "default-src 'none'; base-uri 'none'; form-action 'none'; "
                    "frame-ancestors 'none'; style-src 'unsafe-inline'; "
                    "script-src 'unsafe-inline'"
                )
            else:
                self.response_policy = _STRICT_POLICY
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
