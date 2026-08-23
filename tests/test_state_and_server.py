from __future__ import annotations

import ast
import contextlib
import inspect
import json
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from kilix_playalong import server as server_module
from kilix_playalong.errors import CorruptProjectError
from kilix_playalong.paths import project_artifact
from kilix_playalong.server import PlayalongServer
from kilix_playalong.state import load_manifest, new_manifest, save_manifest
from kilix_playalong.util import private_write


def _ready_project(root: Path) -> Path:
    project = root / "song-server123"
    project.mkdir(mode=0o700, parents=True)
    manifest = new_manifest(
        "song-server123",
        url_sha256="0" * 64,
        rights_statement="confirmed for a synthetic fixture",
    )
    manifest["title"] = "Server <Test>"
    manifest["artist"] = "Fixture Artist"
    manifest["source"]["duration"] = 4.0

    stem = project / "stems" / "vocals.wav"
    private_write(stem, b"0123456789")
    manifest["tracks"] = [
        {
            "id": "vocals",
            "label": "Vocals",
            "kind": "vocals",
            "path": "stems/vocals.wav",
            "sha256": "0" * 64,
            "size": 10,
            "default_muted": False,
        }
    ]
    private_write(
        project / "lyrics" / "lyrics.json",
        json.dumps({"schema": "kilix.playalong.lyrics/v1", "cues": []}).encode(),
    )
    private_write(
        project / "tab" / "guitar-tab.json",
        json.dumps(
            {
                "schema": "kilix.playalong.tab/v1",
                "tuning": {"midi": [40, 45, 50, 55, 59, 64], "labels": []},
                "events": [],
            }
        ).encode(),
    )
    private_write(project / "exports" / "playalong.html", b"<!doctype html><title>print</title>")
    private_write(project / "exports" / "guitar-tab.txt", b"tab")
    private_write(project / "midi" / "guitar.mid", b"midi")
    manifest["lyrics"] = {
        "path": "lyrics/lyrics.json",
        "source": "fixture",
        "language": "en",
        "visible": True,
    }
    manifest["tablature"] = {
        "path": "tab/guitar-tab.json",
        "ascii_path": "exports/guitar-tab.txt",
        "midi_path": "midi/guitar.mid",
        "visible": True,
        "tuning": [40, 45, 50, 55, 59, 64],
        "max_fret": 20,
    }
    save_manifest(project, manifest)
    return project


def test_state_round_trip_and_corruption_detection(tmp_path: Path) -> None:
    project = _ready_project(tmp_path)
    assert load_manifest(project)["title"] == "Server <Test>"
    state = project / "project.state"
    payload = bytearray(state.read_bytes())
    payload[-1] ^= 0xFF
    state.write_bytes(payload)
    with pytest.raises(CorruptProjectError, match="integrity"):
        load_manifest(project)


def test_project_artifacts_cannot_escape_private_project(tmp_path: Path) -> None:
    project = tmp_path / "song-contained"
    project.mkdir()
    assert project_artifact(project, "stems/guitar.wav") == project / "stems" / "guitar.wav"
    with pytest.raises(CorruptProjectError, match="escapes"):
        project_artifact(project, "../../outside")
    with pytest.raises(CorruptProjectError, match="absolute"):
        project_artifact(project, "/etc/passwd")


def test_loopback_server_capability_routes_and_ranges(tmp_path: Path) -> None:
    project = _ready_project(tmp_path)
    with PlayalongServer(project) as server:
        with urllib.request.urlopen(server.url, timeout=3) as response:
            page = response.read().decode()
            assert response.headers["Content-Security-Policy"]
            assert "Kilix Playalong" in page

        with urllib.request.urlopen(server.url + "api/project", timeout=3) as response:
            payload = json.load(response)
            assert payload["schema"] == "kilix.playalong.web/v1"
            assert payload["project"]["title"] == "Server <Test>"
            assert payload["tracks"][0]["url"].startswith(f"/{server.token}/")

        with urllib.request.urlopen(server.url + "export/print", timeout=3) as response:
            policy = response.headers["Content-Security-Policy"]
            assert "default-src 'none'" in policy
            assert "style-src 'unsafe-inline'" in policy
            assert response.headers["Cross-Origin-Resource-Policy"] == "same-origin"

        request = urllib.request.Request(
            server.url + "media/vocals",
            headers={"Range": "bytes=2-5"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            assert response.status == 206
            assert response.headers["Content-Range"] == "bytes 2-5/10"
            assert response.read() == b"2345"

        wrong = server.url.replace(server.token, "wrong-capability")
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(wrong, timeout=3)
        assert raised.value.code == 404

        post = urllib.request.Request(server.url + "api/project", method="POST")
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(post, timeout=3)
        assert raised.value.code == 405


ABORTED_REQUESTS = 12
LARGE_MEDIA_BYTES = 8 * 1024 * 1024


class _Probe:
    """Capture the exceptions socketserver would have dumped to stderr."""

    def __init__(self, server: PlayalongServer, monkeypatch: pytest.MonkeyPatch) -> None:
        self.errors: list[BaseException | None] = []
        self._finished = threading.Semaphore(0)
        shutdown_request = server.httpd.shutdown_request

        def record(_request: object, _client_address: object) -> None:
            self.errors.append(sys.exc_info()[1])

        def finish(request: socket.socket | tuple[bytes, socket.socket]) -> None:
            try:
                shutdown_request(request)
            finally:
                self._finished.release()

        monkeypatch.setattr(server.httpd, "handle_error", record)
        monkeypatch.setattr(server.httpd, "shutdown_request", finish)

    def wait(self, requests: int, timeout: float = 20.0) -> None:
        for _ in range(requests):
            assert self._finished.acquire(timeout=timeout), "server request never finished"


def _port(server: PlayalongServer) -> int:
    return int(server.httpd.server_address[1])


def _raw_request(port: int, method: str, path: str, *headers: str) -> bytes:
    lines = [f"{method} {path} HTTP/1.1", f"Host: 127.0.0.1:{port}", *headers, "Connection: close"]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("iso-8859-1")


def _raw_reply(port: int, request: bytes) -> bytes:
    """Send one raw request and read every byte back; the server always closes."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    received = b""
    try:
        sock.sendall(request)
        while True:
            block = sock.recv(65536)
            if not block:
                break
            received += block
    finally:
        sock.close()
    return received


def _exchange(port: int, request: bytes) -> tuple[str, dict[str, str], bytes]:
    head, _, body = _raw_reply(port, request).partition(b"\r\n\r\n")
    status_line, _, block = head.partition(b"\r\n")
    headers = {}
    for field in block.split(b"\r\n"):
        name, colon, value = field.partition(b":")
        if colon:
            headers[name.decode("iso-8859-1").lower()] = value.strip().decode("iso-8859-1")
    return status_line.decode("iso-8859-1"), headers, body


def _status_line(port: int, request: bytes) -> str:
    return _exchange(port, request)[0]


def _abort_mid_response(port: int, request: bytes) -> None:
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        sock.sendall(request)
        sock.recv(1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    finally:
        sock.close()


def _ranged(server: PlayalongServer, header: str, *, method: str = "GET") -> urllib.request.Request:
    return urllib.request.Request(
        server.url + "media/vocals",
        headers={"Range": header},
        method=method,
    )


def test_aborted_client_connections_are_not_reported_as_server_faults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _ready_project(tmp_path)
    private_write(project / "stems" / "vocals.wav", bytes(LARGE_MEDIA_BYTES))
    with PlayalongServer(project) as server:
        probe = _Probe(server, monkeypatch)
        port = _port(server)
        request = _raw_request(port, "GET", f"/{server.token}/media/vocals", "Range: bytes=0-")
        for _ in range(ABORTED_REQUESTS):
            _abort_mid_response(port, request)
        probe.wait(ABORTED_REQUESTS)
        assert probe.errors == []
        with urllib.request.urlopen(server.url + "api/project", timeout=5) as response:
            assert response.status == 200


def test_genuine_handler_faults_still_reach_the_server_error_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _ready_project(tmp_path)

    def explode(_manifest: object, _token: str) -> dict[str, object]:
        raise ValueError("synthetic handler fault")

    monkeypatch.setattr(server_module, "_payload", explode)
    with PlayalongServer(project) as server:
        probe = _Probe(server, monkeypatch)
        with contextlib.suppress(OSError):
            urllib.request.urlopen(server.url + "api/project", timeout=5).close()
        probe.wait(1)
        assert [type(error) for error in probe.errors] == [ValueError]


def test_oversized_range_digits_are_rejected_without_faulting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _ready_project(tmp_path)
    with PlayalongServer(project) as server:
        probe = _Probe(server, monkeypatch)
        oversized = f"bytes={'9' * 4301}-"
        for method in ("GET", "HEAD"):
            with pytest.raises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(_ranged(server, oversized, method=method), timeout=5)
            assert raised.value.code == 416
            raised.value.close()
        probe.wait(2)
        assert probe.errors == []


def test_range_variants_keep_their_documented_behaviour(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _ready_project(tmp_path)
    with PlayalongServer(project) as server:
        probe = _Probe(server, monkeypatch)
        satisfiable = (
            ("bytes=0-", "bytes 0-9/10", b"0123456789"),
            ("bytes=8-99", "bytes 8-9/10", b"89"),
            ("bytes=9-9", "bytes 9-9/10", b"9"),
            ("bytes=-3", "bytes 7-9/10", b"789"),
            ("bytes=-99", "bytes 0-9/10", b"0123456789"),
        )
        for header, content_range, body in satisfiable:
            with urllib.request.urlopen(_ranged(server, header), timeout=5) as response:
                assert response.status == 206
                assert response.headers["Content-Range"] == content_range
                assert response.read() == body
        unsatisfiable = (
            "bytes=-",
            "bytes=-0",
            "bytes=10-",
            "bytes=5-2",
            "bytes=abc",
            "bytes=-1-5",
            "bytes=0-1, 4-5",
            f"bytes={'9' * 20}-",
            "items=0-1",
        )
        for header in unsatisfiable:
            with pytest.raises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(_ranged(server, header), timeout=5)
            assert raised.value.code == 416, header
            raised.value.close()
        probe.wait(len(satisfiable) + len(unsatisfiable))
        assert probe.errors == []


def test_server_header_hides_the_interpreter_version(tmp_path: Path) -> None:
    project = _ready_project(tmp_path)
    with PlayalongServer(project) as server:
        with urllib.request.urlopen(server.url, timeout=5) as response:
            assert response.headers["Server"] == "KilixPlayalong/0.1"
        wrong = server.url.replace(server.token, "wrong-capability")
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(wrong, timeout=5)
        assert raised.value.headers["Server"] == "KilixPlayalong/0.1"
        raised.value.close()


def test_non_ascii_request_paths_are_rejected_without_faulting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _ready_project(tmp_path)
    # These reach _authorized as latin-1 text and fall through to compare_digest;
    # the UnicodeEncodeError guard beside it is unreachable over HTTP by
    # construction, so what is pinned here is the 404 and the absent fault.
    with PlayalongServer(project) as server:
        probe = _Probe(server, monkeypatch)
        port = _port(server)
        paths = (
            "/\xff\xfe\x80/media/vocals",
            f"/{server.token[:-1]}\xff/media/vocals",
            "/\xe9",
        )
        for path in paths:
            assert _status_line(port, _raw_request(port, "GET", path)).split()[1] == "404"
        probe.wait(len(paths))
        assert probe.errors == []


def test_crafted_paths_cannot_escape_the_capability_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _ready_project(tmp_path)
    private_write(tmp_path / "outside.txt", b"secret")
    with PlayalongServer(project) as server:
        probe = _Probe(server, monkeypatch)
        port = _port(server)
        unauthorized = (
            f"?/{server.token}/api/project",
            f"/{server.token}api/project",
            f"/{server.token[:-1]}/api/project",
            f"/x/{server.token}/api/project",
            f"http://127.0.0.1:{port}/{server.token}/api/project",
            "//wrong-capability/api/project",
            "////api/project",
        )
        contained = (
            "media/../../../etc/passwd",
            "media/vocals/../../../outside.txt",
            "export/../../outside.txt",
            "export/../project.state",
            "api/../../../outside.txt",
            "static/../../server.py",
            "../outside.txt",
        )
        for path in unauthorized:
            assert _status_line(port, _raw_request(port, "GET", path)).split()[1] == "404"
        for method in ("GET", "HEAD"):
            for route in contained:
                path = f"/{server.token}/{route}"
                assert _status_line(port, _raw_request(port, method, path)).split()[1] == "404"
        # CPython collapses a leading "//" before parse_request returns, so the capability
        # check and the route slice always agree on one canonical path (CPython gh-87389).
        normalized = f"//{server.token}/api/project"
        assert _status_line(port, _raw_request(port, "GET", normalized)).split()[1] == "200"
        probe.wait(len(unauthorized) + 2 * len(contained) + 1)
        assert probe.errors == []


SECURITY_HEADERS = (
    "cache-control",
    "referrer-policy",
    "x-content-type-options",
    "x-frame-options",
    "cross-origin-resource-policy",
    "permissions-policy",
    "content-security-policy",
)
STRICT_POLICY = "default-src 'none'; base-uri 'none'; frame-ancestors 'none'"
# A request line one byte over the stdlib's 65536 limit, sent whole and with no
# trailing bytes, so the 414 reply is not racing unread input on the close.
OVERLONG_REQUEST_LINE = b"GET /" + b"a" * 65532


def test_pre_auth_error_replies_carry_the_security_headers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """send_error() never reaches _route, so its replies must be armoured too."""
    project = _ready_project(tmp_path)
    with PlayalongServer(project) as server:
        probe = _Probe(server, monkeypatch)
        port = _port(server)
        host = f"Host: 127.0.0.1:{port}"
        exchanges = (
            # unsupported method, rejected after the request line parses
            ("501", _raw_request(port, "PUT", "/x")),
            # bad request syntax, rejected inside parse_request
            ("400", f"GET / x HTTP/1.1\r\n{host}\r\n\r\n".encode("iso-8859-1")),
            # request line over the stdlib limit, rejected before parse_request
            ("414", OVERLONG_REQUEST_LINE),
        )
        for expected, request in exchanges:
            status, headers, body = _exchange(port, request)
            assert status.split()[1] == expected, status
            missing = [name for name in SECURITY_HEADERS if name not in headers]
            assert missing == [], (expected, missing)
            assert headers["content-security-policy"] == STRICT_POLICY, expected
            assert headers["cache-control"] == "no-store", expected
            assert headers["x-frame-options"] == "DENY", expected
            assert headers["server"] == "KilixPlayalong/0.1", expected
            # Not an unauthenticated text/html page, and neither the reason phrase
            # nor the body echoes the request back.
            assert headers["content-type"] == "text/plain; charset=utf-8", expected
            assert b"<" not in body, body
            assert "PUT" not in status and b"PUT" not in body, (status, body)
            assert "aaa" not in status and b"aaa" not in body, (status, body)
        probe.wait(len(exchanges))
        assert probe.errors == []


def test_authorized_replies_keep_their_own_policies(tmp_path: Path) -> None:
    """The default strict policy must not have flattened the per-route ones."""
    project = _ready_project(tmp_path)
    with PlayalongServer(project) as server:
        with urllib.request.urlopen(server.url, timeout=5) as response:
            assert "script-src 'self'" in response.headers["Content-Security-Policy"]
        with urllib.request.urlopen(server.url + "media/vocals", timeout=5) as response:
            assert response.headers["Content-Security-Policy"] == STRICT_POLICY
            assert response.headers["Accept-Ranges"] == "bytes"
        with urllib.request.urlopen(server.url + "export/print", timeout=5) as response:
            assert "style-src 'unsafe-inline'" in response.headers["Content-Security-Policy"]
        port = _port(server)
        _, headers, _ = _exchange(port, _raw_request(port, "GET", "/wrong-capability/"))
        assert [name for name in SECURITY_HEADERS if name not in headers] == []


OUTBOUND_IO_NAMES = frozenset(
    {
        "asyncio",
        "connect",
        "create_connection",
        "ftplib",
        "http",
        "httpx",
        "Popen",
        "popen",
        "requests",
        "smtplib",
        "socket",
        "socketserver",
        "spawn",
        "subprocess",
        "system",
        "urllib",
        "webbrowser",
    }
)


def test_request_handling_opens_no_outbound_connections() -> None:
    """Pin the invariant that makes the ConnectionError catch in handle_one_request sound.

    That catch is scoped by exception *type*, not by which socket raised, so it is
    only correct while nothing under a request can raise a ConnectionError of its
    own -- i.e. while the request path opens no outbound socket, subprocess pipe or
    HTTP client. Comments cannot enforce that; this can. Two limits are worth being
    exact about. The scope is the module's own request-path code -- _handler plus the
    module-level _payload that every /api/project request calls -- and not the stdlib
    file I/O below it. The method is a denylist: names are read from the parsed tree,
    so prose in the module's comments does not trip it, and a way to reach the network
    under none of these names would not either.
    """
    request_path = (server_module._handler, server_module._payload)
    tree = ast.parse("".join(inspect.getsource(function) for function in request_path))
    referenced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.add(node.attr)
        elif isinstance(node, ast.Import):
            referenced.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            referenced.update((node.module or "").split(".")[:1])
            referenced.update(alias.name for alias in node.names)
    assert not OUTBOUND_IO_NAMES & referenced, sorted(OUTBOUND_IO_NAMES & referenced)


def test_http_0_9_degenerate_replies_carry_no_markup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reply classes end_headers() cannot arm, pinned as harmless.

    CPython holds request_version at its HTTP/0.9 default until a 3-word request line
    parses, and emits the error body alone whenever it never gets there. Three ways in,
    not one: a version it rejects, a request line with too few words to carry one, and
    a 2-word line whose method is not GET. No status line, no headers, nothing for
    end_headers() to attach to. Bounded rather than closed: no browser speaks HTTP/0.9,
    and the body is plain text that echoes nothing, so a client that content-sniffed the
    bare bytes would find no markup to run. The fourth way in -- a 2-word GET, which
    parses and is routed -- is not degenerate and is pinned separately by
    test_http_0_9_replies_ship_bare_bodies_bounded_by_the_token.
    """
    project = _ready_project(tmp_path)
    with PlayalongServer(project) as server:
        probe = _Probe(server, monkeypatch)
        port = _port(server)
        host = f"Host: 127.0.0.1:{port}"
        degenerate = (
            (b"505 HTTP Version Not Supported\n", f"GET / HTTP/9.9\r\n{host}\r\n\r\n"),
            (b"400 Bad Request\n", f"GET /wrong HTTP/x.y\r\n{host}\r\n\r\n"),
            # fewer than 2 words: never reaches the version branch at all
            (b"400 Bad Request\n", f"GET\r\n{host}\r\n\r\n"),
            # 2 words, and HTTP/0.9 has no method but GET
            (b"400 Bad Request\n", f"HEAD /wrong\r\n{host}\r\n\r\n"),
        )
        for expected, request in degenerate:
            # Not an HTTP message at all: the whole reply is the body.
            reply = _raw_reply(port, request.encode("iso-8859-1"))
            assert reply == expected, reply
            assert b"<" not in reply and b"wrong" not in reply, reply
        probe.wait(len(degenerate))
        assert probe.errors == []


def _old_style_request(port: int, path: str) -> bytes:
    """A 2-word request line: CPython reads this as HTTP/0.9 and routes it."""
    return f"GET {path}\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode("iso-8859-1")


def test_http_0_9_replies_ship_bare_bodies_bounded_by_the_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gap end_headers() documents: a 2-word request line is routed, and ships bare.

    send_response, send_header and end_headers all no-op while request_version is
    HTTP/0.9, so the reply is its body and nothing else -- no status line, no
    Content-Type, no CSP, no nosniff. Byte-identical before this release's changes;
    what stands between an HTTP/0.9 client and any of it is the capability token, and
    no browser emits such a request line. Pinned so the claim in end_headers() stays
    honest: if these replies ever start carrying headers, that comment is what to fix.
    """
    project = _ready_project(tmp_path)
    with PlayalongServer(project) as server:
        probe = _Probe(server, monkeypatch)
        port = _port(server)
        for route, opening in (("", b"<!doctype html>"), ("api/project", b'{"schema"')):
            reply = _raw_reply(port, _old_style_request(port, f"/{server.token}/{route}"))
            assert reply.startswith(opening), reply[:60]
            assert not reply.startswith(b"HTTP/"), reply[:60]
            assert [name for name in SECURITY_HEADERS if name.encode() in reply.lower()] == []
        # Without the token the same request line yields one plain-text line, no markup.
        rejected = _raw_reply(port, _old_style_request(port, "/wrong-capability/"))
        assert rejected == b"404 Not Found\n", rejected
        probe.wait(3)
        assert probe.errors == []


STALLED_CONNECTIONS = 8
TEST_READ_TIMEOUT = 0.5
PAUSED_STREAM_SECONDS = 1.5


def test_a_stalled_client_cannot_pin_a_handler_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unfinished header block must not hold a handler thread open indefinitely.

    The request line and header block are read before any route runs, so this costs an
    attacker no capability token: with timeout unset, every connection that stalled
    mid-request held its thread until the client gave up first.
    """
    project = _ready_project(tmp_path)
    handler_class = server_module._handler(project, load_manifest(project), "token", set())
    assert handler_class.timeout == server_module._HEADER_READ_TIMEOUT
    assert 0 < server_module._HEADER_READ_TIMEOUT <= 60
    with PlayalongServer(project) as server:
        probe = _Probe(server, monkeypatch)
        monkeypatch.setattr(server.httpd.RequestHandlerClass, "timeout", TEST_READ_TIMEOUT)
        port = _port(server)
        baseline = threading.active_count()
        stalled = [
            socket.create_connection(("127.0.0.1", port), timeout=10)
            for _ in range(STALLED_CONNECTIONS)
        ]
        try:
            for sock in stalled:
                sock.sendall(b"GET /x HTTP/1.1\r\n")  # no terminating header block
            for sock in stalled:
                try:
                    assert sock.recv(64) == b"", "a stalled client got a reply, not a close"
                except TimeoutError:
                    pytest.fail("the server held a stalled connection past its read timeout")
        finally:
            for sock in stalled:
                sock.close()
        probe.wait(STALLED_CONNECTIONS)
        assert probe.errors == []
        deadline = time.monotonic() + 10
        while threading.active_count() > baseline and time.monotonic() < deadline:
            time.sleep(0.05)
        assert threading.active_count() <= baseline, "handler threads outlived their clients"
        with urllib.request.urlopen(server.url + "api/project", timeout=5) as response:
            assert response.status == 200


def test_a_paused_media_stream_outlives_the_read_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read timeout must not follow the reply into the body write.

    A browser that pauses an audio element stops reading mid-stream, and the socket
    stops draining. Bounding the whole connection instead of the read phase would cut
    that off at whatever the buffers held -- silently, because the stdlib catches the
    socket timeout itself and logs it through the muted log_error, so no fault surfaces
    and the client just sees a short file.
    """
    project = _ready_project(tmp_path)
    private_write(project / "stems" / "vocals.wav", bytes(LARGE_MEDIA_BYTES))
    with PlayalongServer(project) as server:
        probe = _Probe(server, monkeypatch)
        monkeypatch.setattr(server.httpd.RequestHandlerClass, "timeout", TEST_READ_TIMEOUT)
        port = _port(server)
        sock = socket.create_connection(("127.0.0.1", port), timeout=20)
        received = b""
        try:
            sock.sendall(_raw_request(port, "GET", f"/{server.token}/media/vocals"))
            received += sock.recv(65536)
            time.sleep(PAUSED_STREAM_SECONDS)  # the stream stalls past the read timeout
            while True:
                block = sock.recv(65536)
                if not block:
                    break
                received += block
        finally:
            sock.close()
        head, _, body = received.partition(b"\r\n\r\n")
        assert head.split(b"\r\n")[0].split()[1] == b"200", head[:80]
        assert len(body) == LARGE_MEDIA_BYTES, len(body)
        probe.wait(1)
        assert probe.errors == []
