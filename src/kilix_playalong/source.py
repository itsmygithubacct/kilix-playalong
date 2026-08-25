"""The source union: one YouTube URL, or one local file the user already owns.

Until now a project could only begin with a YouTube URL, so "what are we working
on" and "which provider fetches it" were the same question. A local file makes
them two questions, and this module is where they are separated: it turns one
user-supplied string into a discriminated spec, and, for the file arm, resolves
and bounds that file before any provider is handed it.

Four properties this module exists to hold:

* **It never guesses.** ``foo.com/bar`` is a host with a path to one reader and a
  relative directory to another, and picking one silently is how a user ends up
  watching the app fail to open a file they never meant to name. Anything that
  can be read both ways is an ``InvalidInputError`` that says which prefix would
  settle it.
* **A file is bounded exactly like a video.** The same ``max_duration`` gate, in
  the same words, so a three-hour file and a three-hour video are refused for the
  same reason; a size ceiling; a real-content probe, because the extension is a
  user-supplied string and says nothing about what is inside.
* **The user's library is read-only.** Acquisition copies (``media.copy_into``);
  it never moves and never links. A project that depends on a file the user later
  moves is a broken project, and a library that changed because you practised
  against it is a bug report.
* **Nothing the file says about itself is trusted.** Titles, artists, lyric text,
  tag *names* and the filename are all bytes chosen by whoever made the file, and
  all of them reach a browser page, a terminal and the manifest. Every one of them
  goes through ``_clean_text`` (or ``_clean_lyrics``, which keeps line breaks and
  tabs and neuters the rest), so ``MediaMetadata.as_json()`` is printable,
  single-line and capped in characters rather than merely in entries -- which
  bounds it in bytes too, at four bytes a character; ``MediaMetadata.as_json``
  states the ceiling that works out to and names the test that measured it.
  Both are ``text``'s rule and not a spelling of it local to this module: the
  download arm cleans the same two manifest fields with the same function, and
  the one lyric document this app builds can arrive through either arm.

Errors carry no path. Every ``InvalidInputError`` raised below is a literal, so it
can be shown, logged and put on a browser page unedited -- the same rule
``public_error`` applies to provider output before ``pipeline._run_stage`` writes
it into the manifest, where a browser page and a terminal both read it. Two errors
reach a caller from elsewhere and neither carries the user's path either:
``media.copy_into`` restates its own ``OSError``s without one, and an ``OSError``
from writing the lyric file can name only something inside the ``destination`` the
caller chose -- which ``pipeline._run_stage`` already passes to ``public_error`` as
a secret. That last one is STATED, not enforced here: this
module does not own ``destination``.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit

from .errors import InvalidInputError, ProviderFailedError
from .lyrics import embedded_tag_key
from .providers import media, youtube
from .runner import require_seconds, usable_seconds
from .text import MAX_DISPLAY_TEXT, printable_block, printable_line
from .util import private_write, sha256_file, sha256_text

#: Ceiling on the raw source string. Linux' PATH_MAX; a URL is capped lower still
#: by ``youtube.MAX_URL_LENGTH``.
MAX_SOURCE_LENGTH = 4096
#: Default size ceiling for a local file, matching the ``512M`` that
#: ``youtube.download`` passes to ``--max-filesize``.
MAX_FILE_BYTES = 512 * 1024**2
#: Default duration ceiling, matching ``youtube.download``.
DEFAULT_MAX_DURATION = 30 * 60
#: An embedded lyric tag larger than this is not lyrics. Ten minutes of dense
#: singing is a few kilobytes; a quarter of a megabyte is a payload. Sixteen times
#: tighter than ``lyrics.MAX_LYRICS_BYTES`` and deliberately so: that one bounds a
#: *file* the user chose to hand over, which may carry a whole album's sheet, and
#: this one bounds one *tag* inside a media container, which cannot.
MAX_EMBEDDED_LYRICS_BYTES = 256 * 1024
#: Filename ``acquire`` writes embedded lyric text to, beside the copied media.
#: Deliberately outside ``youtube._discard_acquisition``'s ``source*`` glob and
#: distinct from the pipeline's own ``lyrics-input.*``.
EMBEDDED_LYRICS_NAME = "embedded-lyrics.txt"

#: How many distinct tags are read out of one probe document at all. A file with
#: more than 256 is not a tagged song. What the number bounds is the *result* --
#: what ``read_metadata`` sorts and ``MediaMetadata.as_json`` publishes -- and not
#: the walk: the guard fires only once 256 *distinct* cleaned names exist, so a
#: crafted container carrying one name 4,000 times, or 255 distinct ones, is
#: walked to the end (``test_the_number_of_tags_read_is_capped``). What bounds
#: that walk is the 4 MiB ceiling ``runner.run_command`` puts on ffprobe's output,
#: which is the only reason the document being walked is a bounded size at all.
#: Not ``lyrics.MAX_EMBEDDED_TAGS``, which happens to be 256 too: that one bounds
#: how far ``select_embedded_lyrics`` walks a mapping a caller already holds, so
#: the two bound different walks and are free to move apart.
_MAX_TAGS = 256
#: How many of those are named in ``MediaMetadata.ignored_tags``. That list is for
#: a human to read -- "your file also carries these" -- and a list nobody reads to
#: the end is not doing that job. With ``text.MAX_DISPLAY_TEXT`` it is the whole of what
#: bounds ``as_json``: 32 names of 200 characters is nine tenths of that document.
#: The bound those two give is in characters, not bytes -- see ``as_json``.
_MAX_IGNORED_TAGS = 32
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_HOSTLIKE = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,63}(?::\d{1,5})?$"
)
#: What may be copied through from a user-supplied filename into the name this
#: writes. Eight covers every container this app can open (``.opus``, ``.webm``,
#: ``.matroska``) with room to spare; anything else becomes ``.media`` rather than
#: being trusted, because the suffix is the one part of the user's path that ends
#: up as a filename of ours.
_SAFE_SUFFIX = re.compile(r"^\.[a-z0-9]{1,8}$")
_TITLE_KEYS = ("title",)
_ARTIST_KEYS = ("artist", "album_artist", "albumartist", "performer")
#: Container plumbing rather than anything the user wrote about the song; left
#: out of ``MediaMetadata.ignored_tags`` so that list stays worth reading.
_PLUMBING_KEYS = frozenset(
    {
        "encoder",
        "encoded_by",
        "major_brand",
        "minor_version",
        "compatible_brands",
        "handler_name",
        "vendor_id",
        "language",
        "creation_time",
        "duration",
    }
)
_AMBIGUOUS = "source could be a host or a path: prefix ./ for a local file, https:// for a URL"
_SCHEME_REJECTION = (
    "source must be an https:// YouTube URL, a file:// URL, or a local path "
    "(prefix a relative path with ./)"
)


@dataclass(frozen=True)
class YouTubeSource:
    """A single video, already through ``youtube.validate_url``."""

    url: str
    kind: Literal["youtube"] = "youtube"

    @property
    def display_name(self) -> str:
        split = urlsplit(self.url)
        return (split.hostname or "youtube.com").lower()

    def as_json(self) -> dict[str, object]:
        # The URL in full, where ``FileSource`` withholds the directory. Not an
        # inconsistency: the URL *is* what the user chose and typed, and a project
        # that cannot say which video it is working on is not a project. Where in
        # their filesystem a user keeps their music is not something they chose to
        # disclose by naming one file in it. (``public_error`` blanket-redacts URLs
        # from *provider diagnostics*, where a URL can arrive carrying a signed
        # token this app never saw; that is a different document.)
        return {"kind": self.kind, "name": self.display_name, "url": self.url}


@dataclass(frozen=True)
class FileSource:
    """A local file, as an absolute path that has not been to the filesystem yet.

    Syntax only: ``path`` is expanded and made absolute but not resolved, because
    resolving is a filesystem question and belongs with the rest of them in
    ``inspect_file``. Holding the two apart is what lets an intake screen parse
    what the user typed without touching a disk that may be a stalled network
    mount.
    """

    path: Path
    kind: Literal["file"] = "file"

    @property
    def display_name(self) -> str:
        # ``_clean_text`` for the same reason a tag name gets it: a filename is
        # user-supplied text on its way to a browser page and a terminal, it is
        # not bounded by anything the filesystem enforces (``FileSource`` is a
        # plain dataclass, so ``MAX_SOURCE_LENGTH`` is not a bound on this), and
        # ``\x1b[2J`` is a legal character in a filename on every OS this runs on.
        return _clean_text(self.path.name)

    def as_json(self) -> dict[str, object]:
        # The name, never the directory: this document is rendered by a browser
        # page, and where in their filesystem the user keeps their music is not
        # something the app needs to publish to show which file is loaded.
        return {"kind": self.kind, "name": self.display_name}


SourceSpec = YouTubeSource | FileSource


@dataclass(frozen=True)
class EmbeddedLyrics:
    """Raw lyric text carried by the media file itself.

    Raw on purpose. This module extracts and bounds; ``lyrics.py`` decides what
    the text *is* -- plain verses, an LRC transcript with ``[mm:ss]`` stamps, or
    something that only looked like lyrics -- and this module states which tag it
    came from so that decision has the evidence it needs. Which tag *keys* count at
    all is that module's answer too (``lyrics.embedded_tag_key``), for the same
    reason: the key lifted here is the key handed back there to be parsed.
    """

    text: str
    #: The tag key exactly as ffprobe reported it, e.g. ``lyrics-eng`` or
    #: ``UNSYNCEDLYRICS``.
    tag: str
    #: Where that key comes from: ``id3-uslt``, ``vorbis-unsyncedlyrics``,
    #: ``vorbis-syncedlyrics``, ``vorbis-lyrics``, ``id3-lyrics`` or ``tag-lyrics``.
    origin: str

    def as_json(self) -> dict[str, object]:
        # Text is deliberately absent: a lyric sheet is exactly the kind of
        # payload the event and log redaction rules exist to keep out of
        # anything that gets rendered or written by default.
        return {"tag": self.tag, "origin": self.origin, "characters": len(self.text)}


@dataclass(frozen=True)
class MediaMetadata:
    """What the file says about itself, and where each answer came from."""

    title: str
    artist: str
    duration: float
    container: str
    #: The tag key ``title`` came from, or "" when it came from the filename.
    title_tag: str
    #: The tag key ``artist`` came from, or "" when the file did not name one.
    artist_tag: str
    lyrics: EmbeddedLyrics | None = None
    #: Keys the file carries, that a human wrote, and that nothing above used.
    ignored_tags: tuple[str, ...] = ()

    @property
    def title_from_tag(self) -> bool:
        return bool(self.title_tag)

    def as_json(self) -> dict[str, object]:
        """The renderable document. Bounded and printable by construction.

        Every string in it is either a fixed vocabulary (``kind``, ``origin``) or
        has been through ``_clean_text``: at most ``text.MAX_DISPLAY_TEXT`` characters, one
        line, printable -- pinned by
        ``test_a_tag_name_is_bounded_and_sanitised_exactly_like_a_tag_value``.
        ``ignored_tags`` holds at most ``_MAX_IGNORED_TAGS`` of those, and every
        other field is capped the same way, so the document carries at most 7,081
        characters of file-supplied text whatever the file says -- ENFORCED at the
        two places that text enters (``_collect_tags`` for names, ``read_metadata``
        for values and the filename).

        That cap is on *characters*, and a character is up to four UTF-8 bytes, so
        the byte ceiling is four times what an ASCII reading of those numbers
        suggests. Measured, not assumed: the maximal document -- every field at its
        cap, every character astral-plane -- is 7,081 characters and 28,481 bytes
        through ``util.canonical_json``, both measured by
        ``test_the_document_is_capped_in_characters_and_what_that_costs_in_bytes``.
        8 KiB holds only while that text is ASCII (the 4,000-tag ASCII document in
        ``test_the_number_of_tags_read_is_capped`` stays under it); a caller that
        needs a hard byte bound should take 32 KiB.
        """
        return {
            "title": self.title,
            "artist": self.artist,
            "duration": self.duration,
            "container": self.container,
            "title_tag": self.title_tag,
            "artist_tag": self.artist_tag,
            "lyrics": None if self.lyrics is None else self.lyrics.as_json(),
            "ignored_tags": list(self.ignored_tags),
        }


@dataclass(frozen=True)
class FileInspection:
    """A local file that passed every gate, with what it says about itself."""

    #: Fully resolved: no symlink components remain.
    path: Path
    size: int
    metadata: MediaMetadata


@dataclass(frozen=True)
class AcquiredFile:
    """The project's own copy of a local source. Mirrors ``youtube.download``."""

    path: Path
    metadata: MediaMetadata
    #: Raw embedded lyric text written beside the copy, when the file had any.
    lyrics_path: Path | None = None


def _file_url_path(text: str) -> str:
    try:
        split = urlsplit(text)
    except ValueError as error:
        raise InvalidInputError("source is not a well-formed file:// URL") from error
    if split.netloc not in ("", "localhost"):
        # file://server/share is a remote path this app cannot honour, and
        # quietly dropping the host would read a *different*, local file.
        raise InvalidInputError("a file:// URL must not name a remote host")
    if split.query or split.fragment:
        raise InvalidInputError("a file:// URL must be a plain absolute path")
    try:
        path = unquote(split.path, errors="strict")
    except UnicodeDecodeError as error:
        raise InvalidInputError("a file:// URL must be UTF-8 percent-encoded") from error
    if not path.startswith("/"):
        raise InvalidInputError("a file:// URL must contain an absolute path")
    if "\x00" in path:
        raise InvalidInputError("a file:// URL must not contain NUL")
    return path


def file_source(value: str | Path) -> FileSource:
    """Make a ``FileSource`` from a path, expanding ``~`` and making it absolute.

    No filesystem access, and no ``~`` expansion for a ``file://`` URL: a URL
    path is literal, and a file genuinely named ``~`` is reachable exactly one
    way.
    """
    text = str(value)
    if not text.strip():
        raise InvalidInputError("a file source needs a path")
    if "\x00" in text:
        raise InvalidInputError("a file path must not contain NUL")
    if len(text) > MAX_SOURCE_LENGTH:
        raise InvalidInputError("source path is too long")
    try:
        expanded = Path(text).expanduser()
    except RuntimeError as error:
        raise InvalidInputError("cannot expand ~: this account has no home directory") from error
    return FileSource(path=_absolute(expanded))


def parse_source(value: str) -> SourceSpec:
    """Read one user-supplied string as exactly one arm of the union, or refuse.

    The dispatch, in order, and what each rule is protecting:

    * an explicit scheme decides: ``http``/``https`` is the YouTube arm (and a
      non-YouTube or non-TLS URL is rejected there, by ``validate_url``, which
      keeps one host allowlist rather than two), ``file`` is the file arm, and
      any other scheme is refused by name rather than treated as a relative path
      that happens to contain a colon;
    * ``/``, ``./``, ``../`` and ``~`` are unambiguously paths;
    * what is left is a bare relative reference, and it is ambiguous exactly when
      its first segment reads as a hostname *and* something follows it --
      ``foo.com/bar``, ``youtu.be/dQw4w9WgXcQ``. Those are refused with the two
      prefixes that would settle it.

    A bare ``song.mp3`` is a path: no ``/`` follows, so nothing about it reads as
    a host with a path. A bare ``youtube.com`` is therefore also a path, and
    fails as a missing file -- whose message names the ``https://`` prefix, since
    that is overwhelmingly what was meant. Erring that way keeps every real
    filename working; erring the other way would need a TLD list to tell
    ``song.mp3`` from ``youtu.be``, and a TLD list is a thing that goes stale.

    ``./my.music/song.mp3`` is the price: a dotted *directory* name at the head
    of a relative path is refused until it is prefixed. The message says so, and
    the prefix is one gesture.
    """
    text = value.strip()
    if not text:
        raise InvalidInputError("a source is required: a YouTube URL or a local file path")
    if len(text) > MAX_SOURCE_LENGTH:
        raise InvalidInputError("source is too long")
    if not text.isprintable():
        # NUL, newline, tab. urlsplit strips some of these for parsing but not
        # from the string itself, which is how a validated URL and the URL that
        # reaches a provider stop being the same string.
        raise InvalidInputError("source contains unprintable characters")
    scheme_match = _SCHEME.match(text)
    if scheme_match is not None:
        scheme = scheme_match.group(0)[:-1].lower()
        if scheme in ("http", "https"):
            return YouTubeSource(url=youtube.validate_url(text))
        if scheme == "file":
            return file_source(_file_url_path(text))
        raise InvalidInputError(_SCHEME_REJECTION)
    if text.startswith(("/", "./", "../", "~")):
        return file_source(text)
    head = text.split("/", 1)[0]
    if "/" in text and _HOSTLIKE.fullmatch(head):
        raise InvalidInputError(_AMBIGUOUS)
    return file_source(text)


def _absolute(path: Path) -> Path:
    """``os.path.abspath``, with its one failure mode turned into an input error.

    A relative path needs ``os.getcwd()``, and that raises ``FileNotFoundError``
    when the working directory has been removed underneath the process. Bare, that
    is an ``OSError`` out of ``parse_source`` and ``inspect_file``, both of which
    promise ``PlayalongError`` and nothing else -- and ``cli.py`` prints a
    traceback for anything else.
    """
    try:
        return Path(os.path.abspath(path))
    except OSError as error:
        raise InvalidInputError(
            "a relative path cannot be resolved: the working directory no longer exists"
        ) from error


def _home_tree() -> Path | None:
    try:
        return Path.home().resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _reject_symlink_escape(given: Path, real: Path) -> None:
    """A link may not redirect the read out of the user's own tree.

    The threat is a path that *looks* like the user's music and resolves
    somewhere else -- ``~/Music/song.mp3`` pointing at ``/proc/self/environ``.
    A path typed out in full is the user's own explicit choice and is allowed to
    name anything; a link that was followed to get somewhere else is not.

    ENFORCED here. The cost, stated rather than hidden: the common setup where
    ``~/Music`` is itself a link onto a mounted drive is refused, and the message
    says the real path works -- which it does, unchanged, with no flag. Widening
    this to "wherever the link lands is fine" would delete the property; adding a
    per-user allowlist of extra roots is a configuration surface this release has
    no place to put. OUT OF SCOPE, deliberately, until there is somewhere to put
    it.
    """
    # ``file_source`` always hands over an absolute path, but ``FileSource`` is a
    # plain dataclass a caller can build by hand: a relative path would otherwise
    # differ from its resolved form on every comparison and read as a link.
    if _absolute(given) == real:
        return
    home = _home_tree()
    if home is not None and _is_within(real, home):
        return
    raise InvalidInputError(
        "a symbolic link must resolve inside your home directory; supply the real path instead"
    )


def _resolve_existing(path: Path) -> Path:
    if "\x00" in str(path):
        # ``file_source`` refuses NUL in the string it was handed, but ``FileSource``
        # is a plain dataclass and a JSON or IPC surface can build one directly.
        # Every filesystem call below answers a NUL path with a bare ``ValueError``
        # ("embedded null byte"), which is not the error this module promises.
        raise InvalidInputError("a file path must not contain NUL")
    try:
        os.fsencode(path)
    except UnicodeEncodeError as error:
        # The other half of the same door, and not covered by the check above:
        # ``os.fsencode("a\x00b.mp3")`` succeeds. ``json.loads('"\\ud800.mp3"')``
        # yields a lone surrogate, ``file_source`` returns it unchanged, and every
        # filesystem call below answers it with a bare ``UnicodeEncodeError``.
        # ``os.fsencode`` asks exactly what those calls will ask, so it refuses
        # this and nothing else: a filename that is undecodable *bytes* arrives as
        # surrogateescape's ``\udc80``-``\udcff``, encodes straight back to the
        # original byte, and is read normally
        # (``test_a_filename_the_shell_could_not_decode_is_still_a_file``).
        raise InvalidInputError(
            "a file path must not contain characters the filesystem cannot represent"
        ) from error
    try:
        return path.resolve(strict=True)
    except FileNotFoundError as error:
        raise InvalidInputError(
            "no such file; check the path, or prefix a URL with https://"
        ) from error
    except (OSError, RuntimeError) as error:
        # ELOOP, ENOTDIR, EACCES on a parent directory, a path component that is
        # not a directory: a real filesystem answer, not a media problem.
        raise InvalidInputError("the source path cannot be resolved") from error


def _reject_by_type(real: Path) -> None:
    try:
        info = os.lstat(real)
    except OSError as error:
        raise InvalidInputError("the source path cannot be read") from error
    if stat.S_ISDIR(info.st_mode):
        raise InvalidInputError("source is a directory; supply a media file")
    if not stat.S_ISREG(info.st_mode):
        # A FIFO would block an unguarded open forever, a device may have side
        # effects on open, and a socket is not a file at all. Checked before
        # anything opens it, which is the only order in which that is true.
        raise InvalidInputError("source must be a regular file")


def _measure(real: Path, *, max_bytes: int) -> int:
    """Open the file once, and take size and readability from that descriptor.

    ``os.access`` answers a different question (the real uid's, not this
    process's, and blind to ACLs) and answers it about a path rather than about
    the thing that will actually be read. ``O_NOFOLLOW`` plus a re-check of the
    file type on the descriptor is what closes the gap between the stat above and
    the read below.

    The flag set is ``media.SAFE_OPEN_FLAGS`` and not a second spelling of the same
    four constants, because it is one open made twice: this one closes its
    descriptor again, and whatever reads the bytes afterwards -- ``copy_into``,
    ``util.sha256_file`` -- opens the path a second time. The read that actually
    happens is guarded by *that* open, so a flag added here and not there, or
    ``O_NOFOLLOW`` dropped from either during a later edit, is a security property
    lost with no failing test and no visible symptom.
    """
    try:
        descriptor = os.open(real, media.SAFE_OPEN_FLAGS)
    except PermissionError as error:
        raise InvalidInputError("the source file is not readable") from error
    except OSError as error:
        raise InvalidInputError("the source file could not be opened") from error
    try:
        info = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if not stat.S_ISREG(info.st_mode):
        raise InvalidInputError("source must be a regular file")
    if info.st_size <= 0:
        # Also every zero-length regular file in /proc and /sys, which is the
        # class this catches that a user could plausibly be talked into naming.
        raise InvalidInputError("the source file is empty")
    if info.st_size > max_bytes:
        raise InvalidInputError(f"source media exceeds the {format_size(max_bytes)} size limit")
    return int(info.st_size)


def format_size(value: int) -> str:
    """Render a byte ceiling the way ``yt-dlp``'s ``--max-filesize`` spells it."""
    for unit, scale in (("G", 1024**3), ("M", 1024**2), ("K", 1024)):
        if value >= scale and value % scale == 0:
            return f"{value // scale}{unit}"
    return f"{value} bytes"


def _positive_seconds(value: object) -> float | None:
    """One value out of an ffprobe document as a usable duration, or None.

    ffprobe writes durations as strings, so the ``str`` arm is the one that runs;
    the numeric arms are here because this reads a JSON document another program
    produced, and that document's shape is not a promise. The numeric answer comes
    from ``runner.usable_seconds`` rather than from a second ``isfinite`` written
    out here -- the same reason that predicate is shared with the provider entry
    points, and it closes a door as well as a duplicate: ``float(10 ** 400)``
    raises OverflowError rather than answering, and a JSON integer of 400 digits is
    a thing ``json.loads`` will hand over, so this was one way to leave
    ``inspect_file`` raising something other than the ``PlayalongError`` it
    promises. ``bool`` is refused first, because ``isinstance(True, int)`` is what
    would otherwise make ``True`` a one-second duration.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
        return usable_seconds(number)
    if isinstance(value, int | float):
        return usable_seconds(value)
    return None


def _document_duration(document: dict[str, object]) -> float | None:
    """Prefer the container's duration, fall back to the audio stream's.

    ffprobe reports ``format.duration`` for every container this can open, but a
    stream-copied or truncated file can leave it out while the audio stream still
    carries one, and a duration gate that gives up is a duration gate that lets a
    three-hour file through.
    """
    candidates: list[object] = []
    section = document.get("format")
    if isinstance(section, dict):
        candidates.append(section.get("duration"))
    streams = document.get("streams")
    if isinstance(streams, list):
        candidates.extend(
            stream.get("duration")
            for stream in streams
            if isinstance(stream, dict) and stream.get("codec_type") == "audio"
        )
    for candidate in candidates:
        seconds = _positive_seconds(candidate)
        if seconds is not None:
            return seconds
    return None


def _container_name(document: dict[str, object]) -> str:
    # ffprobe's demuxer name, from a fixed vocabulary -- cleaned anyway, because
    # this string is published by ``MediaMetadata.as_json`` and "it comes from a
    # fixed vocabulary" is an assumption about another program's output.
    section = document.get("format")
    name = section.get("format_name") if isinstance(section, dict) else None
    return _clean_text(name).lower()[:64] if isinstance(name, str) else ""


def _has_audio(document: dict[str, object]) -> bool:
    streams = document.get("streams")
    if not isinstance(streams, list):
        return False
    return any(
        isinstance(stream, dict) and stream.get("codec_type") == "audio" for stream in streams
    )


def _collect_tags(document: dict[str, object]) -> dict[str, tuple[str, str]]:
    """Merge every tag section into one case-folded map, first writer winning.

    Both sections are needed, and the order matters: an MP3 or FLAC carries its
    tags on the container, while an Ogg/Opus file carries its Vorbis comment on
    the *stream* -- measured, not assumed, and the reason a metadata reader that
    looks only at ``format.tags`` silently finds nothing on half a music library.

    A tag *name* goes through ``_clean_text`` here, exactly as a tag value does
    everywhere else. A name is file content: no container format bounds one, and a
    Vorbis field name of 60,001 characters, or an ID3 ``TXXX`` description holding
    a raw ESC and a newline, is a legal file that any tagger can write. Every name
    this returns is what ``MediaMetadata.title_tag``, ``artist_tag``,
    ``EmbeddedLyrics.tag`` and ``ignored_tags`` publish, and those go to a browser
    page, a terminal and the manifest -- so this is the single place all four are
    bounded, rather than four places that have to stay in step.

    Cleaning cannot lose a lookup: every key this module searches for is ASCII
    lowercase and shorter than the bound, so ``title`` still folds to ``title``.
    """
    sections: list[object] = []
    section = document.get("format")
    if isinstance(section, dict):
        sections.append(section.get("tags"))
    streams = document.get("streams")
    if isinstance(streams, list):
        audio = [
            stream
            for stream in streams
            if isinstance(stream, dict) and stream.get("codec_type") == "audio"
        ]
        other = [
            stream
            for stream in streams
            if isinstance(stream, dict) and stream.get("codec_type") != "audio"
        ]
        sections.extend(stream.get("tags") for stream in (*audio, *other))
    merged: dict[str, tuple[str, str]] = {}
    for candidate in sections:
        if not isinstance(candidate, dict):
            continue
        for key, value in candidate.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            name = _clean_text(key)
            if not name:
                # A name that was nothing but control characters names nothing a
                # user can be shown, and an empty ``title_tag`` already means
                # "this came from the filename".
                continue
            if len(merged) >= _MAX_TAGS:
                return merged
            merged.setdefault(name.lower(), (name, value))
    return merged


def _clean_text(value: str) -> str:
    """Any piece of tag text, at the package's display rule and this module's ceiling.

    Applied to titles, artists and tag *names* alike -- see ``_collect_tags`` for
    why a name is no more trusted than a value. The rule itself is
    ``text.printable_line``, which the download arm's reported metadata also goes
    through: two arms, one ``manifest["title"]``, so one rule and one ceiling.
    """
    return printable_line(value, limit=MAX_DISPLAY_TEXT)


def _clean_lyrics(value: str) -> str:
    """A lyric tag: the display rule, but with the line breaks kept.

    ``text.printable_block`` rather than ``printable_line`` because line breaks are
    the only formatting a lyric sheet has, and an LRC transcript stuffed into a
    USLT frame is one line per stamp -- reflowing here would destroy the very thing
    ``lyrics.py`` parses. No character ceiling either: what bounds this value is
    ``MAX_EMBEDDED_LYRICS_BYTES``, applied to the tag before it reaches here.
    """
    return printable_block(value)


def _lyrics_origin(key: str, prefix: str, container: str) -> str:
    """Where a lyric tag key came from, in the vocabulary ``EmbeddedLyrics`` states.

    ``prefix`` is ``lyrics.embedded_tag_key``'s answer, so the family names are not
    restated here. The one thing read off the raw key is the ``lyrics-<language>``
    spelling, and deliberately: that spelling is libavformat's ID3v2 reader and
    nothing else, while ``lyrics_eng`` is some other tagger's -- both parse to the
    same prefix, and calling the second one a USLT frame would be a claim about the
    file that this cannot make.
    """
    if key.startswith("lyrics-"):
        return "id3-uslt"
    if prefix.startswith("unsynced"):
        return "vorbis-unsyncedlyrics"
    if prefix == "syncedlyrics":
        return "vorbis-syncedlyrics"
    if any(name in container for name in ("ogg", "flac", "matroska", "webm")):
        return "vorbis-lyrics"
    if "mp3" in container:
        return "id3-lyrics"
    return "tag-lyrics"


def _extract_lyrics(
    tags: dict[str, tuple[str, str]], container: str
) -> tuple[EmbeddedLyrics | None, list[str]]:
    """Pick at most one lyric tag, and name every lyric tag not picked.

    Which keys count is ``lyrics.embedded_tag_key``'s answer, not a second list
    here. The two lists used to disagree on ten of sixteen real tag spellings, and
    that mattered in both directions: this module lifts the tag out and
    ``pipeline._embedded_lyrics`` hands it straight back to ``lyrics`` to be
    parsed, so a key only one side knew was either a sheet no lyric route ever saw
    or a declared language dropped on the way through. ``SYNCEDLYRICS`` is the one
    that cost the most while it was missing here -- measured, that name reaches
    ffprobe verbatim off a FLAC, an Ogg or a Matroska file, and it is the *timed*
    tag, so a file whose only lyrics were in it produced no lyric sheet at all.

    A file can carry several -- a Vorbis ``LYRICS`` beside an ``UNSYNCEDLYRICS``,
    or one USLT frame per language. The longest wins: the short one is usually a
    stub or a single language tag line, and length ties break on the tag key so the
    choice is deterministic rather than dependent on dict order.

    Longest, not most-stamped, and that is the one place this ranking deliberately
    differs from ``lyrics.select_embedded_lyrics``, which puts a synchronised tag
    first. The two agree wherever it can matter: a stamp only *adds* characters to
    a line, so when both tags carry the same lines the stamped one is the longer
    one and both rules choose it -- pinned by
    ``test_a_stamped_tag_is_the_longer_one_when_the_lines_are_the_same``. They can
    only disagree when one tag carries lines the other does not, and there the app
    can recover timings for an untimed sheet -- that is what the alignment stage is
    -- while nothing recovers lines a tag never carried. So the rule that keeps the
    most lyrics is the right one *here*, where the choice is made once and the tags
    not picked are discarded.
    """
    candidates: list[tuple[int, str, str, str, str]] = []
    ignored: list[str] = []
    for key, (original, value) in sorted(tags.items()):
        parsed = embedded_tag_key(key)
        if parsed is None:
            continue
        # ffprobe's JSON can carry a \ud800 escape, and json.loads turns that
        # into a lone surrogate that no UTF-8 encoder will accept. Measuring
        # the bound is the first thing that touches the bytes, so without this
        # a crafted container reaches the user as a UnicodeEncodeError rather
        # than as a tag this build declined to read.
        try:
            measured = len(value.encode("utf-8"))
        except UnicodeEncodeError:
            ignored.append(original)
            continue
        if measured > MAX_EMBEDDED_LYRICS_BYTES:
            ignored.append(original)
            continue
        text = _clean_lyrics(value)
        if not text.strip():
            ignored.append(original)
            continue
        candidates.append((-len(text), key, original, text, parsed[0]))
    if not candidates:
        return None, ignored
    candidates.sort()
    _, key, original, text, prefix = candidates[0]
    ignored.extend(entry[2] for entry in candidates[1:])
    origin = _lyrics_origin(key, prefix, container)
    return EmbeddedLyrics(text=text, tag=original, origin=origin), ignored


def read_metadata(path: Path, document: dict[str, object], duration: float) -> MediaMetadata:
    """Read title, artist and embedded lyrics out of an ffprobe document.

    The filename stem is the title of last resort. It is *not* mined for an
    artist: ``Artist - Title.mp3`` is a convention, not a format, and half a
    library is ``01 - Title.mp3``, where that guess writes the track number into
    the artist field and the user has no idea where it came from. An untagged
    file leaves ``artist`` empty and says so through ``artist_tag``.

    Everything this returns is bounded and printable: values by ``_clean_text``
    here, names by ``_clean_text`` in ``_collect_tags``, lyric text by
    ``_clean_lyrics`` and ``MAX_EMBEDDED_LYRICS_BYTES``. ``title_tag`` and
    ``artist_tag`` are those cleaned names, and ``used`` matches on their
    lower-cased form -- which is exactly the key ``_collect_tags`` merged on, so
    cleaning cannot make a tag this module *used* reappear in ``ignored_tags``.
    """
    tags = _collect_tags(document)
    container = _container_name(document)
    title, title_tag = "", ""
    for key in _TITLE_KEYS:
        entry = tags.get(key)
        if entry is not None and (cleaned := _clean_text(entry[1])):
            title, title_tag = cleaned, entry[0]
            break
    if not title:
        # The filename is user-supplied too, and it is the last resort for the one
        # field that always has to be shown: a stem that cleans away to nothing
        # must fall through to a *cleaned* name, not to a raw slice of it. Both
        # can be empty -- a file named entirely in control characters -- and an
        # empty title with ``title_from_tag`` False is the honest answer there.
        title = _clean_text(path.stem) or _clean_text(path.name)
    artist, artist_tag = "", ""
    for key in _ARTIST_KEYS:
        entry = tags.get(key)
        if entry is not None and (cleaned := _clean_text(entry[1])):
            artist, artist_tag = cleaned, entry[0]
            break
    lyrics, ignored_lyrics = _extract_lyrics(tags, container)
    used = {title_tag.lower(), artist_tag.lower()}
    if lyrics is not None:
        used.add(lyrics.tag.lower())
    ignored = [
        original
        for key, (original, value) in sorted(tags.items())
        if key not in used and key not in _PLUMBING_KEYS and _clean_text(value)
    ]
    return MediaMetadata(
        title=title,
        artist=artist,
        duration=duration,
        container=container,
        title_tag=title_tag,
        artist_tag=artist_tag,
        lyrics=lyrics,
        ignored_tags=tuple(sorted(set(ignored) | set(ignored_lyrics))[:_MAX_IGNORED_TAGS]),
    )


def inspect_file(
    spec: FileSource,
    *,
    max_duration: float = DEFAULT_MAX_DURATION,
    max_bytes: int = MAX_FILE_BYTES,
    timeout: float = 30,
) -> FileInspection:
    """Resolve one local file and refuse it unless everything about it is fine.

    The order of the gates is the order of the messages a user will see, and it
    is chosen so the first thing that is wrong is the thing they are told about:
    existence, then where a link went, then what kind of file it is, then whether
    it can be read, then its size, then whether it is media at all, then how long
    it is. Each is an ``InvalidInputError``; nothing here raises anything else for
    any ``FileSource``, which is the same contract ``providers/youtube.py`` states
    for its entry points. For *any* ``FileSource``, not only one that came through
    ``file_source``: this is a plain dataclass, so a JSON or IPC surface can hand
    over a path with a NUL in it, or one holding a lone surrogate that no
    filesystem call can encode (both ``_resolve_existing``), or a relative path
    with no working directory left to resolve it against (``_absolute``) -- three
    doors that were a bare ``ValueError``, ``UnicodeEncodeError`` and
    ``FileNotFoundError``, which is a traceback rather than a message. Three is
    what has been found and closed, not a proof that there is no fourth; the
    enumeration is pinned by
    ``test_inspect_and_fingerprint_raise_nothing_but_a_playalong_error``.

    ``ProviderUnavailableError`` is the one exception and it is not about the
    input: ffprobe missing is a broken installation, and reporting it as a bad
    file would send the user to check a file that is fine.
    """
    require_seconds(max_duration, "maximum duration")
    require_seconds(timeout, "provider timeout")
    if max_bytes <= 0:
        raise InvalidInputError("maximum source size must be a positive number of bytes")
    real = _resolve_existing(spec.path)
    _reject_symlink_escape(spec.path, real)
    _reject_by_type(real)
    size = _measure(real, max_bytes=max_bytes)
    try:
        document = media.probe(real, timeout=timeout, require_audio=False)
    except ProviderFailedError as error:
        # The extension is a user-supplied string; the container is what ffprobe
        # could actually parse. A `.mp3` holding a text file dies here.
        raise InvalidInputError(
            "the source file is not media this can read; supply an audio or video file"
        ) from error
    if not _has_audio(document):
        raise InvalidInputError("the source file has no audio track")
    duration = _document_duration(document)
    if duration is None:
        raise InvalidInputError("the source has no finite positive duration")
    if duration > max_duration:
        # Verbatim from ``youtube.download``: a three-hour file and a three-hour
        # video are one rule, and a user who hits it twice should not have to
        # work out that they were the same rule.
        raise InvalidInputError(f"source exceeds the {max_duration / 60:g}-minute limit")
    return FileInspection(path=real, size=size, metadata=read_metadata(real, document, duration))


def _safe_suffix(path: Path) -> str:
    suffix = path.suffix.lower()
    return suffix if _SAFE_SUFFIX.fullmatch(suffix) else ".media"


def acquire(
    spec: FileSource,
    destination: Path,
    *,
    max_duration: float = DEFAULT_MAX_DURATION,
    max_bytes: int = MAX_FILE_BYTES,
    timeout: float = 30,
    on_bytes: Callable[[int, int], None] | None = None,
) -> AcquiredFile:
    """Validate a local file and copy it into the project. The file arm's ``download``.

    Copies. ``media.copy_into`` is a byte-for-byte read-and-write with no move,
    no hardlink and no symlink anywhere in it, so the user's library is exactly as
    it was afterwards and the project still works when they reorganise it. The
    digest of the copy equals the digest of the original, so a caller that already
    holds an ``AcquiredFile`` can take its fingerprint with ``util.sha256_file``
    on ``path`` instead of sending ``source_sha256`` back to the library. (Not
    ``source_sha256`` on the copy: that re-runs the library gates, including the
    symlink-escape one, against a project directory it knows nothing about.)

    Every failure after the first byte is written removes what it wrote, the same
    way ``youtube._discard_acquisition`` does, so a retry starts from an empty
    source directory rather than from someone else's half-copy.

    ``on_bytes(copied, total)`` is forwarded to the copy so a caller with a screen
    can report a fraction it *measured* rather than one it inferred from a clock --
    ``media.copy_into`` states why that is a rule here and not a preference. It
    fires once per megabyte read -- 512 times for a file at the default ceiling --
    and is not rate-limited here or in ``copy_into``, so a caller forwarding these
    to a surface owns throttling them.

    Optional, and stated rather than left to read as an oversight: whether a
    fraction is worth showing for *this* stage is the caller's call and not this
    function's. Copying a local file is the shortest thing a run does, and a
    surface that shows a bar here and cannot show one for the two multi-minute
    stages teaches its user to read "no bar" as "nothing is happening".

    Two names are written here and no others: ``source<suffix>`` and
    ``embedded-lyrics.txt``. Nothing is swept, so
    a caller re-acquiring into a directory that already holds a *differently*
    suffixed ``source.*`` from an earlier run owns clearing it -- deliberately not
    done here, because this function cannot know that the directory it was handed
    is not the one the user's own file lives in.
    """
    inspection = inspect_file(spec, max_duration=max_duration, max_bytes=max_bytes, timeout=timeout)
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = destination / ("source" + _safe_suffix(inspection.path))
    lyrics_path: Path | None = None
    try:
        media.copy_into(inspection.path, target, max_bytes=max_bytes, on_bytes=on_bytes)
        lyrics = inspection.metadata.lyrics
        if lyrics is not None:
            lyrics_path = destination / EMBEDDED_LYRICS_NAME
            private_write(lyrics_path, lyrics.text.encode("utf-8"))
    except BaseException:
        for path in (target, destination / EMBEDDED_LYRICS_NAME):
            path.unlink(missing_ok=True)
        raise
    return AcquiredFile(path=target, metadata=inspection.metadata, lyrics_path=lyrics_path)


def source_sha256(spec: SourceSpec, *, max_bytes: int = MAX_FILE_BYTES) -> str:
    """A stable identity for one source, for the manifest and for stage keys.

    The YouTube arm is ``sha256_text(url)`` -- the same value ``pipeline`` already
    stores as ``url_sha256``, so existing projects keep their fingerprints and
    nothing re-downloads because this module appeared.

    The file arm hashes the file's *content*, not its path. A path is not an
    identity: two people with the same album have different paths, and one person
    can rewrite the file under an unchanged path. Content-addressing means editing
    the file invalidates the stages that read it, which is exactly the staleness
    the fingerprint exists to catch.

    All four of ``inspect_file``'s filesystem gates run here, in the same order and
    with the same messages, and ``max_bytes`` is one of them rather than advice to
    the caller. This is a second door into a user-supplied path: a door that
    skipped the link check would be the way back in, and hashing *reads every
    byte*, so a door that skipped the size gate would spend unbounded time and I/O
    on the file the other door refuses without touching. Only the media probe is
    left out -- this answers "which bytes", not "are these bytes media".
    """
    if isinstance(spec, YouTubeSource):
        return sha256_text(spec.url)
    if max_bytes <= 0:
        raise InvalidInputError("maximum source size must be a positive number of bytes")
    real = _resolve_existing(spec.path)
    _reject_symlink_escape(spec.path, real)
    _reject_by_type(real)
    _measure(real, max_bytes=max_bytes)
    try:
        return sha256_file(real)
    except OSError as error:
        # No path: ``sha256_file`` opens by name, so this would otherwise be the
        # third door out of this module carrying the user's own path. Losing to
        # the race between ``_measure`` closing its descriptor and this reopening
        # is the only way to get here.
        raise InvalidInputError("the source file could not be read") from error
