"""The LRCLIB provider: what it asks, what it accepts, and how it fails.

No test here reaches lrclib.net. The matching tests never touch HTTP at all, and the
transport tests run the real urllib client against a server on 127.0.0.1 that this
file starts, with the conftest's recorder stepped aside for exactly those tests.
"""

from __future__ import annotations

import dataclasses
import json
import socket
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from kilix_playalong import __version__
from kilix_playalong.errors import ProviderFailedError, ProviderUnavailableError
from kilix_playalong.providers import lrclib
from kilix_playalong.providers.lrclib import Entry

# --------------------------------------------------------------------------- #
# Entries
# --------------------------------------------------------------------------- #

_SYNCED = "[00:12.34] Two folks in Nashville\n[00:15.00] I will look to mine\n"


def _entry(
    identifier: int,
    *,
    track: str = "You Look to Yours",
    artist: str = "Colter Wall",
    duration: float = 272.0,
    synced: str | None = _SYNCED,
    plain: str | None = "Two folks in Nashville\nI will look to mine\n",
    instrumental: bool = False,
) -> Entry:
    return Entry(
        id=identifier,
        track=track,
        artist=artist,
        album="",
        duration=duration,
        instrumental=instrumental,
        plain=plain,
        synced=synced,
    )


def _choose(entries: list[Entry], **overrides: Any) -> Entry | None:
    arguments: dict[str, Any] = {
        "title": "Colter Wall - You Look to Yours (Audio)",
        "artist": "Colter Wall",
        "duration": 274.0,
    }
    arguments.update(overrides)
    return lrclib.choose(entries, **arguments)


def test_the_studio_take_is_chosen_over_a_live_one_by_its_length() -> None:
    """The case that set the tolerance: one song, two recordings, a 274 s video.

    The live entry has the same title, the same artist and the same words, and its line
    stamps are wrong for this audio by seconds. Only the length tells them apart.
    """
    studio = _entry(3882174, duration=272.0)
    live = _entry(17, duration=262.0)

    assert _choose([live, studio]) == studio
    assert _choose([studio, live]) == studio, "the server's order decides nothing"
    assert _choose([live]) is None, "a live take alone is a miss, not a fallback"


def test_the_length_tolerance_is_ten_seconds_either_way() -> None:
    assert _choose([_entry(1, duration=264.0)]) is not None
    assert _choose([_entry(1, duration=284.0)]) is not None
    assert _choose([_entry(1, duration=263.9)]) is None
    assert _choose([_entry(1, duration=284.1)]) is None


def test_entries_without_words_are_never_chosen() -> None:
    marked = _entry(1, instrumental=True)
    empty = _entry(2, synced=None, plain="  \n")
    assert _choose([marked]) is None
    assert _choose([empty]) is None
    assert _choose([marked, empty, _entry(3)]) == _entry(3)


def test_synced_beats_plain_and_then_the_closer_length_wins() -> None:
    plain_but_exact = _entry(1, duration=274.0, synced=None)
    synced_but_off = _entry(2, duration=268.0)
    synced_closer = _entry(3, duration=272.0)

    assert _choose([plain_but_exact, synced_but_off]) == synced_but_off
    assert _choose([plain_but_exact, synced_but_off, synced_closer]) == synced_closer
    assert _choose([plain_but_exact]) == plain_but_exact
    assert _choose([_entry(9), _entry(4)]) == _entry(4), "a tie goes to the lower id"


def test_the_title_must_be_the_same_song_and_not_a_version_of_it() -> None:
    assert _choose([_entry(1, track="You Look To Yours")]) is not None
    assert _choose([_entry(1, track="You Look to Yours (Live)")]) is None
    assert _choose([_entry(1, track="You Look to Yours Tonight")]) is None
    assert _choose([_entry(1, track="Look to Yours")]) is None


def test_the_artist_matches_when_either_name_contains_the_other() -> None:
    band = _entry(1, artist="Colter Wall & The Scary Prairie Boys")
    assert _choose([band]) == band
    assert _choose([_entry(1)], artist="Colter Wall - Topic") is not None
    assert _choose([_entry(1, artist="Wall")]) is not None
    assert _choose([_entry(1, artist="Someone Else")]) is None
    assert _choose([_entry(1, artist="")]) is None
    unknown = {"title": "You Look to Yours (Audio)", "artist": ""}
    assert _choose([_entry(1, artist="Anyone")], **unknown) is not None, "no artist, no test"


def test_titles_are_cleaned_of_what_a_video_adds() -> None:
    assert lrclib.clean_title("Colter Wall - You Look to Yours (Audio)", "Colter Wall") == (
        "You Look to Yours"
    )
    assert lrclib.clean_title("Song [Official Video] (HD)", "") == "Song"
    assert lrclib.clean_title("Song feat. Somebody Else", "") == "Song"
    assert lrclib.clean_title("Song ft. Somebody", "") == "Song"
    assert lrclib.clean_title("Other Artist - Song", "Colter Wall") == "Other Artist - Song"
    assert lrclib.clean_title("(Intro)", "") == "(Intro)", "never cleaned down to nothing"


def test_names_compare_without_case_accents_or_punctuation() -> None:
    assert lrclib.normalize("Honky-Tonkin'") == lrclib.normalize("honky tonkin")
    assert lrclib.normalize("Beyoncé") == lrclib.normalize("BEYONCE")
    assert lrclib.normalize("  a__b  ") == "a b"


def test_the_query_uses_separate_fields_only_when_the_artist_is_known() -> None:
    assert lrclib.query("Colter Wall - You Look to Yours (Audio)", "Colter Wall - Topic") == {
        "track_name": "You Look to Yours",
        "artist_name": "Colter Wall",
    }
    assert lrclib.query("You Look to Yours (Audio)", "") == {"q": "You Look to Yours"}


# --------------------------------------------------------------------------- #
# The second request, for a title that carries its artist
# --------------------------------------------------------------------------- #

_BRETT = {
    "title": "Ain't no reason - Brett Dennen (Lyrics)",
    "artist": "Borja Alonso-Majagranzas Mendicote",
    "duration": 217.8,
}
_REASON = _entry(
    4210, track="Ain't No Reason", artist="Brett Dennen", duration=217.0, synced=_SYNCED
)


class _Searches:
    """`lrclib.search`'s stand-in: one answer per query, and a record of every query."""

    def __init__(self, *answers: list[Entry]) -> None:
        self.answers = list(answers)
        self.asked: list[dict[str, str]] = []

    def __call__(self, parameters: dict[str, str]) -> list[Entry]:
        self.asked.append(dict(parameters))
        return self.answers.pop(0) if self.answers else []


def test_an_uploader_artist_gets_one_more_request_with_the_whole_title() -> None:
    """The real project: the uploader is not the singer, and the title names the singer."""
    searches = _Searches([], [_REASON])

    found = lrclib.lookup(**_BRETT, search=searches)

    assert found.entry == _REASON
    assert searches.asked == [
        {
            "track_name": "Ain't no reason - Brett Dennen",
            "artist_name": "Borja Alonso-Majagranzas Mendicote",
        },
        {"q": "Ain't no reason - Brett Dennen"},
    ]
    assert found.queries == tuple(searches.asked)
    assert found.query == {"q": "Ain't no reason - Brett Dennen"}, "the query that found it"


def test_no_second_request_when_the_first_one_matches() -> None:
    whole = _entry(1, track="Ain't no reason - Brett Dennen", artist=_BRETT["artist"])
    searches = _Searches([replace_duration(whole, 218.0)], [_REASON])

    found = lrclib.lookup(**_BRETT, search=searches)

    assert found.entry is not None and found.entry.id == 1
    assert len(searches.asked) == 1 and found.query == searches.asked[0]


def test_the_first_request_is_not_relaxed_by_the_halves() -> None:
    """An entry only the second pass may accept is not accepted from the first answer."""
    searches = _Searches([_REASON], [_REASON])

    found = lrclib.lookup(**_BRETT, search=searches)

    assert found.entry == _REASON
    assert len(searches.asked) == 2 and found.query == {"q": "Ain't no reason - Brett Dennen"}


def test_no_second_request_for_a_title_without_a_dash() -> None:
    for title in ("Ain't No Reason (Lyrics)", "Jay-Z's Song", "A-B"):
        searches = _Searches([], [_REASON])
        found = lrclib.lookup(title, "Somebody", duration=217.8, search=searches)
        assert found.entry is None and found.query is None
        assert len(searches.asked) == 1, title


def test_never_more_than_two_requests() -> None:
    searches = _Searches([], [], [_REASON])
    found = lrclib.lookup(**_BRETT, search=searches)
    assert found.entry is None and len(found.queries) == 2 == len(searches.asked)


def test_an_unknown_artist_asks_once_and_reads_the_halves_from_that_answer() -> None:
    """With no artist the first request already is the whole title as `q`."""
    searches = _Searches([_REASON], [_REASON])
    found = lrclib.lookup(_BRETT["title"], "", duration=217.8, search=searches)
    assert found.entry == _REASON
    assert searches.asked == [{"q": "Ain't no reason - Brett Dennen"}]


def test_the_second_pass_relaxes_names_and_nothing_else() -> None:
    halves = lrclib.title_halves(_BRETT["title"], _BRETT["artist"])
    assert halves == ("Ain't no reason", "Brett Dennen")
    relaxed = {**_BRETT, "halves": halves}
    assert _choose([_REASON], **relaxed) == _REASON
    assert _choose([_REASON], **_BRETT) is None, "the first pass stays strict"
    by_uploader = _entry(5, track="Ain't No Reason", artist=_BRETT["artist"], duration=217.0)
    assert _choose([by_uploader], **relaxed) == by_uploader, "the project artist still counts"
    assert _choose([replace_duration(_REASON, 200.0)], **relaxed) is None, "length still binds"
    other_song = _entry(6, track="Heaven", artist="Brett Dennen", duration=217.0)
    assert _choose([other_song], **relaxed) is None, "the track must still be a title key"
    other_artist = _entry(7, track="Ain't No Reason", artist="Somebody", duration=217.0)
    assert _choose([other_artist], **relaxed) is None


def test_every_dash_a_title_is_written_with_splits_it() -> None:
    for dash in ("-", "\u2013", "\u2014"):
        assert lrclib.title_halves(f"Song {dash} Artist", "") == ("Song", "Artist")
    assert lrclib.title_halves("Jay-Z", "") is None, "a hyphen inside a word is no separator"
    assert lrclib.clean_title("Colter Wall \u2013 You Look to Yours", "Colter Wall") == (
        "You Look to Yours"
    )


def replace_duration(entry: Entry, duration: float) -> Entry:
    return dataclasses.replace(entry, duration=duration)


# --------------------------------------------------------------------------- #
# Reading an answer
# --------------------------------------------------------------------------- #


def _record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "id": 3882174,
        "trackName": "You Look to Yours",
        "artistName": "Colter Wall",
        "albumName": "Colter Wall",
        "duration": 272.0,
        "instrumental": False,
        "plainLyrics": "Two folks in Nashville",
        "syncedLyrics": "[00:12.34] Two folks in Nashville",
    }
    record.update(overrides)
    return record


def test_an_answer_keeps_whole_records_and_skips_the_rest() -> None:
    answer = [
        _record(),
        _record(id=True),
        _record(id=-1),
        _record(id="7"),
        _record(trackName=None),
        _record(duration=0),
        _record(duration=float("nan")),
        _record(duration=False),
        "not a record",
    ]
    entries = lrclib.parse_entries(json.dumps(answer).encode())
    assert [entry.id for entry in entries] == [3882174]
    assert entries[0].as_json() == _record()
    assert lrclib.entry_from_json(entries[0].as_json()) == entries[0]


def test_an_answer_is_read_up_to_its_entry_cap() -> None:
    answer = [_record(id=index) for index in range(lrclib.MAX_ENTRIES + 5)]
    assert len(lrclib.parse_entries(json.dumps(answer).encode())) == lrclib.MAX_ENTRIES


@pytest.mark.parametrize(
    "data",
    [b"{not json", b"\xff\xfe", b"[" * 100_000, b'{"id": 1}', b"null"],
    ids=["malformed", "not-utf8", "nested", "object", "null"],
)
def test_an_unreadable_answer_is_a_provider_failure(data: bytes) -> None:
    with pytest.raises(ProviderFailedError, match="LRCLIB"):
        lrclib.parse_entries(data)


# --------------------------------------------------------------------------- #
# The transport, against a server on loopback
# --------------------------------------------------------------------------- #

Respond = Callable[[BaseHTTPRequestHandler], None]


class _Loopback:
    def __init__(self) -> None:
        self.respond: Respond = lambda handler: handler.send_error(500)
        self.requests: list[tuple[str, dict[str, str]]] = []
        loopback = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                loopback.requests.append((self.path, dict(self.headers.items())))
                loopback.respond(self)

            def log_message(self, *_args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host!s}:{port}/api/search"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _send(status: int, body: bytes) -> Respond:
    def respond(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    return respond


@pytest.fixture
def loopback(monkeypatch: pytest.MonkeyPatch, lrclib_network: Any) -> Iterator[_Loopback]:
    """The real `_fetch`, pointed at a server on 127.0.0.1, with no proxy in the way."""
    for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setattr(lrclib, "_fetch", lrclib_network.real_fetch)
    server = _Loopback()
    monkeypatch.setattr(lrclib, "SEARCH_URL", server.url)
    yield server
    server.close()


def test_a_search_sends_the_query_and_a_user_agent_and_nothing_else(loopback: _Loopback) -> None:
    loopback.respond = _send(200, json.dumps([_record()]).encode())

    entries = lrclib.search(lrclib.query("Colter Wall - You Look to Yours (Audio)", "Colter Wall"))

    assert [entry.id for entry in entries] == [3882174]
    [(path, headers)] = loopback.requests
    parsed = urllib.parse.urlsplit(path)
    assert parsed.path == "/api/search"
    assert urllib.parse.parse_qs(parsed.query) == {
        "track_name": ["You Look to Yours"],
        "artist_name": ["Colter Wall"],
    }
    assert headers["User-Agent"] == f"kilix-playalong/{__version__}"
    assert "http" not in headers["User-Agent"], "no URL in the User-Agent"
    assert "Cookie" not in headers and "Authorization" not in headers


def test_a_refusal_is_reported_without_the_url(loopback: _Loopback) -> None:
    loopback.respond = _send(404, b'{"message": "not found"}')
    with pytest.raises(ProviderUnavailableError, match="LRCLIB answered HTTP 404") as raised:
        lrclib.search({"q": "anything"})
    assert "127.0.0.1" not in str(raised.value)


def test_a_server_that_does_not_answer_in_time_is_unavailable(
    loopback: _Loopback, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lrclib, "TIMEOUT_SECONDS", 0.3)

    def stall(handler: BaseHTTPRequestHandler) -> None:
        time.sleep(1.5)
        _send(200, b"[]")(handler)

    loopback.respond = stall
    started = time.monotonic()
    with pytest.raises(ProviderUnavailableError, match="did not answer in time") as raised:
        lrclib.search({"q": "anything"})
    assert time.monotonic() - started < 1.4
    assert "127.0.0.1" not in str(raised.value)


def test_an_answer_that_is_not_json_is_a_provider_failure(loopback: _Loopback) -> None:
    loopback.respond = _send(200, b"<html>maintenance</html>")
    with pytest.raises(ProviderFailedError, match="not readable JSON"):
        lrclib.search({"q": "anything"})


def test_an_answer_past_the_limit_is_refused_before_it_is_parsed(
    loopback: _Loopback, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lrclib, "MAX_RESPONSE_BYTES", 4096)
    body = json.dumps([_record(id=index) for index in range(40)]).encode()
    assert len(body) > 4096
    loopback.respond = _send(200, body)
    with pytest.raises(ProviderFailedError, match="exceeds the 4096 byte limit"):
        lrclib.search({"q": "anything"})


def test_the_production_limit_reads_in_mebibytes() -> None:
    assert lrclib._size_label(lrclib.MAX_RESPONSE_BYTES) == "2 MiB"


def test_a_body_cut_short_is_unavailable_and_not_a_traceback(loopback: _Loopback) -> None:
    def truncate(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Length", "5000")
        handler.end_headers()
        handler.wfile.write(b"[")
        handler.wfile.flush()
        handler.close_connection = True

    loopback.respond = truncate
    with pytest.raises((ProviderUnavailableError, ProviderFailedError), match="LRCLIB"):
        lrclib.search({"q": "anything"})


def test_nothing_listening_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, lrclib_network: Any
) -> None:
    for name in ("http_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(lrclib, "_fetch", lrclib_network.real_fetch)
    monkeypatch.setattr(lrclib, "SEARCH_URL", f"http://127.0.0.1:{port}/api/search")
    with pytest.raises(ProviderUnavailableError, match="could not be reached") as raised:
        lrclib.search({"q": "anything"})
    assert str(port) not in str(raised.value)
