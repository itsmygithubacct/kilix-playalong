"""Published lyrics from LRCLIB, looked up by title, artist and length.

LRCLIB (lrclib.net) is an open database of song lyrics, most of them line-timed
LRC, with a documented public JSON API that asks for no key. It is the one
lyrics source this package consults over the network, and it is consulted only
for words: `pipeline` still has the singing transcribed and forced-aligns these
words against it, because an LRC line stamp says when a line starts and nothing
about the words inside it.

This module is a leaf, like every provider. It knows nothing about a project
or a run: it asks one question, `search`, validates the answer, and picks the
entry that is this song, `choose`. What the pipeline then does with the entry --
save a private copy, record where it came from, time it -- is the pipeline's.

What leaves this machine is stated, because it is the first thing anyone will
ask: one HTTPS GET carrying the cleaned title and the artist (or, with no
artist, the title alone), and a User-Agent naming this package and its version.
No path, no media, no identifier of the user or the machine. The answer is
bounded before it is parsed, and every failure is reported in this module's own
words: no URL, no lyric text.

Matching is strict on purpose, because a near miss here is not harmless. The
words of a different song, aligned against this one's singing, align badly and
are rejected -- that is the good case. A *live* version of the right song is the
bad case: same words, a different arrangement, line stamps that are wrong by
seconds. Hence the title must match exactly after normalising, the artist must
match one way or the other, and the length must be within
`DURATION_TOLERANCE_SECONDS` -- which is what tells a 272 s studio take from a
262 s live one for a 274 s video.

One relaxation, learned from a real project, and only on a second request that
`lookup` makes when the first finds nothing. A video's "artist" is often the
uploader, while its title names the real one: "Ain't no reason - Brett Dennen",
uploaded by somebody else, finds nothing by track and artist. When the cleaned
title reads "A - B", the second request sends that whole title as `q`, and an
entry then matches when its track is the whole title or either half and its
artist is the project's or either half. The length test does not relax. That is
two requests at most, and the provenance records the one that found the entry.
"""

from __future__ import annotations

import http.client
import json
import math
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .. import __version__
from ..errors import ProviderFailedError, ProviderUnavailableError

#: The search endpoint. A module constant so the suite can point it at a server on
#: loopback; nothing in the package changes it.
SEARCH_URL = "https://lrclib.net/api/search"
#: The whole request, connect to last byte. A lookup that takes longer than this is
#: not worth the user's wait: the alternative is transcription, which works offline.
TIMEOUT_SECONDS = 10.0
#: The answer is read up to this many bytes and refused past it. A search returns at
#: most twenty entries, each a few kilobytes of lyrics; two MiB is generous for that
#: and small enough that a hostile or broken server cannot make this process hold
#: more.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
#: Entries past this are ignored rather than refused, so an API that grows its page
#: size degrades to "fewer candidates" instead of "no lyrics".
MAX_ENTRIES = 50
#: How far an entry's length may be from the audio's and still be this recording.
DURATION_TOLERANCE_SECONDS = 10.0
#: Says who is asking, which the service asks clients to do, and nothing else.
USER_AGENT = f"kilix-playalong/{__version__}"

#: A bracketed or parenthesised aside in a title: "(Audio)", "[Official Video]",
#: "(feat. Somebody)", "(Remastered 2011)". None of them is part of the song's name.
_ASIDE = re.compile(r"\s*[\(\[][^\(\)\[\]]*[\)\]]")
#: A featured-artist tail written without brackets.
_FEATURING = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s.*$", re.IGNORECASE)
#: YouTube's auto-generated artist channels are named "<artist> - Topic".
_TOPIC = re.compile(r"\s+-\s+topic$", re.IGNORECASE)
#: The separator of an "A - B" title: a hyphen, an en dash or an em dash, spaced.
_DASH = re.compile("\\s+[-\u2013\u2014]\\s+")
_NOT_WORD = re.compile(r"[^\w]+")


@dataclass(frozen=True)
class Entry:
    """One LRCLIB record, validated: every field is the type it claims to be."""

    id: int
    track: str
    artist: str
    album: str
    duration: float
    instrumental: bool
    plain: str | None
    synced: str | None

    @property
    def has_lyrics(self) -> bool:
        return bool((self.synced or "").strip() or (self.plain or "").strip())

    def as_json(self) -> dict[str, object]:
        """The entry in LRCLIB's own field names, which is the shape a project saves."""
        return {
            "id": self.id,
            "trackName": self.track,
            "artistName": self.artist,
            "albumName": self.album,
            "duration": self.duration,
            "instrumental": self.instrumental,
            "plainLyrics": self.plain,
            "syncedLyrics": self.synced,
        }


def normalize(value: str) -> str:
    """A name reduced to what two spellings of it share.

    Case, accents, punctuation and spacing are dropped: "Honky-Tonkin'" and
    "honky tonkin" are the same words, and so are "Beyoncé" and "Beyonce".
    """
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(_NOT_WORD.sub(" ", stripped).replace("_", " ").split())


def clean_artist(artist: str) -> str:
    """The artist as LRCLIB would spell it: a YouTube "- Topic" channel suffix removed."""
    return _TOPIC.sub("", artist.strip())


def clean_title(title: str, artist: str) -> str:
    """The song's own name, out of a title a video or a tag gave it.

    Asides in brackets go, then a "feat." tail, then a leading "<artist> - " when the
    artist is known and the title starts with it -- which is how a video titled
    "Colter Wall - You Look to Yours (Audio)" becomes "You Look to Yours". A title
    that is nothing but an aside is returned as it was, rather than as nothing.
    """
    cleaned = _FEATURING.sub("", _ASIDE.sub("", title)).strip()
    artist = clean_artist(artist)
    parts = _DASH.split(cleaned, maxsplit=1)
    if artist and len(parts) == 2:
        head, tail = parts
        if normalize(head) == normalize(artist) and tail.strip():
            cleaned = tail.strip()
    return cleaned or title.strip()


def title_halves(title: str, artist: str) -> tuple[str, str] | None:
    """The two sides of a cleaned title that reads "A - B", or None when it does not.

    Which side is the song and which the artist is not decided here, because titles
    are written both ways round; `choose` lets either side be either.
    """
    parts = _DASH.split(clean_title(title, artist), maxsplit=1)
    if len(parts) != 2:
        return None
    head, tail = (part.strip() for part in parts)
    return (head, tail) if head and tail else None


def query(title: str, artist: str) -> dict[str, str]:
    """The search parameters for a song: title and artist, or the title alone.

    Separate fields when the artist is known, because LRCLIB matches each against
    its own column; `q` when it is not, which searches both.
    """
    artist = clean_artist(artist)
    track = clean_title(title, artist)
    if artist:
        return {"track_name": track, "artist_name": artist}
    return {"q": track}


def _fetch(url: str, *, timeout: float, limit: int) -> bytes:
    """GET one URL and return at most `limit` bytes of its body, or `limit + 1` to say "more".

    The only network call in the package outside yt-dlp, and the one the suite
    replaces. urllib verifies the certificate against the system store, follows the
    server's redirects and honours the proxy variables, all by default.
    """
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return bytes(response.read(limit + 1))
    except urllib.error.HTTPError as error:
        raise ProviderUnavailableError(f"LRCLIB answered HTTP {error.code}") from error
    except TimeoutError as error:
        raise ProviderUnavailableError("LRCLIB did not answer in time") from error
    except urllib.error.URLError as error:
        # A timeout while connecting arrives wrapped; one while reading arrives bare, above.
        if isinstance(error.reason, TimeoutError):
            raise ProviderUnavailableError("LRCLIB did not answer in time") from error
        raise ProviderUnavailableError("LRCLIB could not be reached") from error
    except (OSError, ValueError, http.client.HTTPException) as error:
        # URLError's reason can name the host and the path; the message is a literal.
        # HTTPException is not an OSError: a body cut short (IncompleteRead) or a status
        # line that is not HTTP arrives as one, and would otherwise leave as a traceback.
        raise ProviderUnavailableError("LRCLIB could not be reached") from error


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _entry(value: object) -> Entry | None:
    """One record of the answer, or None for anything that is not a whole record."""
    if not isinstance(value, dict):
        return None
    identifier = value.get("id")
    track = value.get("trackName")
    artist = value.get("artistName")
    duration = value.get("duration")
    if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier < 0:
        return None
    if not isinstance(track, str) or not isinstance(artist, str):
        return None
    if isinstance(duration, bool) or not isinstance(duration, int | float):
        return None
    try:
        seconds = float(duration)
    except OverflowError:
        return None
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return Entry(
        id=identifier,
        track=track,
        artist=artist,
        album=_text(value.get("albumName")) or "",
        duration=seconds,
        instrumental=value.get("instrumental") is True,
        plain=_text(value.get("plainLyrics")),
        synced=_text(value.get("syncedLyrics")),
    )


def parse_entries(data: bytes) -> list[Entry]:
    """The records of one search answer. Malformed records are skipped, not fatal."""
    try:
        value = json.loads(data.decode("utf-8"))
    # RecursionError for the reason `lyrics.load_lyrics_document` names it: nesting is
    # bounded only by the byte count, and it is not a JSONDecodeError.
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ProviderFailedError("LRCLIB's answer is not readable JSON") from error
    if not isinstance(value, list):
        raise ProviderFailedError("LRCLIB's answer is not a list of records")
    entries = [_entry(item) for item in value[:MAX_ENTRIES]]
    return [entry for entry in entries if entry is not None]


def search(parameters: dict[str, str]) -> list[Entry]:
    """Ask LRCLIB for the records matching `query`'s parameters."""
    url = f"{SEARCH_URL}?{urllib.parse.urlencode(parameters)}"
    data = _fetch(url, timeout=TIMEOUT_SECONDS, limit=MAX_RESPONSE_BYTES)
    if len(data) > MAX_RESPONSE_BYTES:
        raise ProviderFailedError(
            f"LRCLIB's answer exceeds the {_size_label(MAX_RESPONSE_BYTES)} limit"
        )
    return parse_entries(data)


def _size_label(limit: int) -> str:
    mebibyte = 1024 * 1024
    return f"{limit // mebibyte} MiB" if limit % mebibyte == 0 else f"{limit} byte"


def _contains_either_way(wanted: str, found: str) -> bool:
    return bool(wanted and found) and (wanted in found or found in wanted)


def choose(
    entries: Sequence[Entry],
    *,
    title: str,
    artist: str,
    duration: float,
    halves: tuple[str, str] | None = None,
) -> Entry | None:
    """The entry that is this recording of this song, or None when none is.

    Kept: entries with lyrics, not marked instrumental, whose normalised title equals
    the song's cleaned title, whose artist contains the song's or is contained by it
    (when the song has an artist at all), and whose length is within
    `DURATION_TOLERANCE_SECONDS` of the audio's. Of those, a synced entry beats a
    plain one, and then the closer length wins; the id breaks a tie, so the choice
    does not depend on the order the server answered in.

    `halves` is the second request's relaxation (see the module docstring): the title
    may also equal either half, and the artist may also match either half.
    """
    artist = clean_artist(artist)
    wanted_titles = {normalize(clean_title(title, artist))}
    names = [normalize(artist)] if artist else []
    if halves is not None:
        wanted_titles.update(normalize(half) for half in halves)
        names.extend(normalize(half) for half in halves)
    wanted_titles.discard("")
    candidates: list[tuple[bool, float, int, Entry]] = []
    for entry in entries:
        if entry.instrumental or not entry.has_lyrics:
            continue
        if normalize(entry.track) not in wanted_titles:
            continue
        found_artist = normalize(entry.artist)
        if names and not any(_contains_either_way(name, found_artist) for name in names):
            continue
        gap = abs(entry.duration - duration)
        if gap > DURATION_TOLERANCE_SECONDS:
            continue
        candidates.append((not (entry.synced or "").strip(), gap, entry.id, entry))
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[:3])[3]


@dataclass(frozen=True)
class Lookup:
    """What `lookup` asked and what it found."""

    #: The chosen entry, or None for a miss.
    entry: Entry | None
    #: Every query sent, in order: one, or two when the second request was made.
    queries: tuple[dict[str, str], ...]

    @property
    def query(self) -> dict[str, str] | None:
        """The query whose answer held the entry: the last one sent, on a hit."""
        return self.queries[-1] if self.entry is not None else None


def lookup(
    title: str,
    artist: str,
    *,
    duration: float,
    search: Callable[[dict[str, str]], list[Entry]] = search,
) -> Lookup:
    """Find this song's entry in at most two requests.

    The first is `query`: track and artist, or the title alone when the artist is
    unknown. The second is made only when the first was a track-and-artist search,
    found nothing, and the cleaned title reads "A - B": the whole title as `q`, chosen
    from with `choose`'s `halves`. When the artist is unknown the first request
    already is that `q`, so it is chosen from with the halves and no second request
    is made. `search` is injectable so a caller can memoise or replace the transport.
    """
    first = query(title, artist)
    halves = title_halves(title, artist)
    single = "q" in first
    entry = choose(
        search(first),
        title=title,
        artist=artist,
        duration=duration,
        halves=halves if single else None,
    )
    if entry is not None or single or halves is None:
        return Lookup(entry=entry, queries=(first,))
    second = {"q": clean_title(title, artist)}
    entry = choose(search(second), title=title, artist=artist, duration=duration, halves=halves)
    return Lookup(entry=entry, queries=(first, second))


def entry_from_json(value: object) -> Entry | None:
    """Read back an entry a project saved with `Entry.as_json`."""
    return _entry(value)
