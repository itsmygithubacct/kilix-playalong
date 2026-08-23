"""Explicit, bounded YouTube acquisition through the locked yt-dlp package."""

from __future__ import annotations

import json
import re
import sys
from contextlib import suppress
from pathlib import Path
from typing import cast
from urllib.parse import SplitResult, parse_qs, unquote, urlsplit

from ..errors import InvalidInputError, ProviderFailedError
from ..runner import run_command, usable_seconds

ALLOWED_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
    }
)
MAX_URL_LENGTH = 2048
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_LANGUAGE_LENGTH = 128
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{6,32}$")
_FILESIZE = re.compile(r"^(\d+(?:\.\d+)?)([kmgt]?)$")
_FILESIZE_UNITS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
_PLAYLIST_REJECTION = "playlists are not supported; supply a single video URL"
_MAX_FILESIZE_BYTES = 1024**5
_NON_MEDIA_SUFFIXES = frozenset({".vtt", ".part", ".ytdl", ".json"})


def _is_playlist_url(split: SplitResult, host: str) -> bool:
    """Recognise a playlist URL before any network call. Three rules:

    ``/playlist`` as the whole path, on any host. The path is unquoted and re-split rather
    than compared literally: yt-dlp matches its extractors against the raw URL, and both
    ``//playlist`` and ``/%70laylist`` are still claimed by its playlist and tab
    extractors, so a literal compare gates less than it looks like it does. Unquoting only
    ever adds matches: it decodes a letter (``%70`` -> ``p``) or a separator (``%2F`` ->
    ``/``) and never deletes a character, and neither word matched below contains a ``%``
    of its own, so nothing that matched raw stops matching decoded. Deliberately
    host-agnostic, which over-rejects ``https://youtu.be/playlist`` -- a video id on that
    host, not a playlist -- but ids are 11 characters and this word is 8, so nothing
    reachable is lost, and one rule beats two.

    ``?list=`` with no ``?v=``, on ``/watch``, on any host. Round 2's clause; kept.

    ``?list=`` with no ``?v=`` on any path, on every allowed host except ``youtu.be``.
    This one closes the gap round 2 left: ``YoutubePlaylistIE.suitable`` ignores the path
    entirely, and measured against the locked yt-dlp's own ``suitable()``, every one of
    ``/embed/videoseries?list=`` (the common embed spelling), ``/?list=``,
    ``/playlist/extra?list=``, ``/watch/x?list=`` and ``/foo?list=`` is claimed by
    ``youtube:playlist`` or ``youtube:tab`` while round 2's two clauses accepted all five.
    It is host-dependent rather than yt-dlp's rule verbatim because that rule would also
    take ``https://youtu.be/ID?list=PL...``, which yt-dlp itself routes to ``YoutubeYtBe``
    -- one video, which ``--no-playlist`` keeps to one video, and which
    ``test_youtube_url_gate_accepts_a_single_video_inside_a_playlist`` requires be accepted.

    STILL OPEN, by construction, and measured by driving every host/path/query combination
    through this gate and then through the locked yt-dlp's ``suitable()``: a URL with ``v=``
    present (which ``--no-playlist`` narrows to that one video, so it is not one), any
    spelling on ``youtu.be``, and a channel or tab path carrying no ``list=`` at all --
    ``/foo``, ``/@handle`` -- which yt-dlp's tab extractor also returns as a playlist. Those
    are rejected one layer later, by ``inspect``'s ``_type``/``entries`` check with the same
    message, at the cost of one ``--dump-single-json`` -- which without ``--flat-playlist``
    enumerates the playlist, bounded only by the 60 second timeout and ``MAX_METADATA_BYTES``.
    That cost is why this gate is worth widening; a pre-network path allowlist would be the
    next step out, and is not taken here.
    """
    segments = [segment for segment in unquote(split.path).lower().split("/") if segment]
    if segments == ["playlist"]:
        return True
    query = parse_qs(split.query)
    if "list" not in query or "v" in query:
        return False
    return host != "youtu.be" or segments == ["watch"]


def _parse_filesize(value: str) -> int:
    """Parse a yt-dlp-style byte count, raising only ``InvalidInputError``.

    Accepts a strict subset of ``yt_dlp.utils.parse_bytes`` -- ASCII only, no ``512MB``,
    no padding, no ``1e3`` -- and agrees with it exactly on everything it does accept.
    The ASCII gate is what makes that second half true rather than nearly true:
    ``str.lower`` folds U+212A KELVIN SIGN onto ``k``, so ``512`` followed by U+212A used
    to parse here as 524288 while yt-dlp's own ``s.upper()`` leaves it unmatched -- yt-dlp
    exits 2 with `invalid max filesize`, so the two disagreed about whether the command
    could run at all. What is left is the same expression on both sides,
    ``round(float(number) * unit)``, over ``k/m/g/t`` as a subset of its
    ``K/M/G/T/P/E/Z/Y``; a test pins the agreement across the accepted grammar.

    Every rejection has to be a ``PlayalongError`` or cli.py prints a traceback: this is
    an entry-point validator, in the same class as the port parsing in ``validate_url``.
    """
    if not value.isascii():
        raise InvalidInputError("max_filesize must be a byte count such as '512M'")
    match = _FILESIZE.fullmatch(value.lower())
    if match is None:
        raise InvalidInputError("max_filesize must be a byte count such as '512M'")
    scaled = float(match.group(1)) * _FILESIZE_UNITS[match.group(2)]
    # The regex admits any number of digits, and float("9" * 400) is inf, whose round()
    # raises OverflowError -- not a PlayalongError. The ceiling keeps the conversion total;
    # it is not a policy, and it leaves every unit in the table usable.
    if not 0 < scaled <= _MAX_FILESIZE_BYTES:
        raise InvalidInputError("max_filesize must be a positive byte count below 1 PiB")
    size = round(scaled)
    if size <= 0:
        raise InvalidInputError("max_filesize must be a positive byte count")
    return size


# The error contract of this module's three entry points -- ``validate_url``, ``inspect``
# and ``download`` -- and its exact extent. F22 (round 0) and P7 (round 1) were both a
# caller-supplied value reaching ``run_command`` and coming back out as a bare
# ``ValueError``, which cli.py's top-level ``except PlayalongError`` does not catch; this
# is the third statement of the same property, so it is written out rather than implied.
#
# ENFORCED, by the validators here and pinned by
# ``test_youtube_entry_points_raise_only_playalong_errors``: every caller-supplied *value*
# -- ``url``, ``timeout``, ``language``, ``max_filesize``, ``max_duration`` -- is rejected
# with an ``InvalidInputError``. Not ``run_command``'s ``ValueError``, not an
# ``OverflowError`` out of float arithmetic, not an ``OSError`` for an over-long argv.
#
# NOT ENFORCED, and deliberately not converted:
# * ``destination``. ``mkdir``/``glob``/``stat`` raise ``OSError`` when the path is a file,
#   is unwritable, or vanishes mid-run, and a genuine filesystem fault should not be dressed
#   up as invalid input. BOUNDED BY: the pipeline's ``_download`` creates that directory with
#   ``ensure_private_directory`` before calling in, so the failure is raised there first.
# * Arguments that violate the annotations -- a ``bytes`` url, a ``str`` timeout -- raise
#   ``TypeError``/``AttributeError``. Every in-tree call site is type-checked; mypy is the
#   check that holds this one, not a runtime guard.


def _validate_seconds(value: float, description: str) -> None:
    """Restate ``run_command``'s own precondition as a PlayalongError, using its predicate.

    ``timeout`` reaches ``run_command``, which rejects an unusable one with a bare
    ``ValueError``. ``max_duration`` reaches nothing but a comparison, and a NaN would not
    raise there at all -- it would compare False and switch the duration gate off silently.
    """
    if usable_seconds(value) is None:
        raise InvalidInputError(f"{description} must be a positive, finite number of seconds")


def _validate_language(language: str) -> None:
    """Gate the one caller-supplied value that lands in the argv, as ``--sub-langs``.

    The same shape as the URL gate, for the same two reasons: a NUL reaches ``run_command``
    as a bare ``ValueError``, and a single argv entry past Linux's ``MAX_ARG_STRLEN``
    (128 KiB, measured) reaches ``Popen`` as ``OSError`` E2BIG. Not an argument-injection
    gate: yt-dlp's option parser consumes the token after ``--sub-langs`` as its value
    whatever it starts with (measured: ``--sub-langs --exec`` leaves ``--exec`` consumed,
    not parsed), so this rejects the unprintable and the unbounded, not the exotic.
    """
    if not language or len(language) > MAX_LANGUAGE_LENGTH:
        raise InvalidInputError("language must be a short subtitle-language selector")
    if not language.isprintable() or " " in language:
        raise InvalidInputError("language must be a printable, space-free language selector")


def validate_url(url: str) -> str:
    """Gate a source URL. For any ``str``, nothing but ``InvalidInputError`` leaves here."""
    if len(url) > MAX_URL_LENGTH:
        raise InvalidInputError("YouTube URL is too long")
    if not url.isprintable() or " " in url:
        # A NUL survives urlsplit untouched and only fails later, inside run_command, as a
        # bare ValueError -- the same non-PlayalongError escape the port parsing had. A
        # newline is stripped by urlsplit for parsing but not from the string we return,
        # so without this the gate would validate one URL and hand yt-dlp another.
        raise InvalidInputError("source is not a well-formed URL")
    try:
        split = urlsplit(url)
        host = (split.hostname or "").lower().rstrip(".")
    except ValueError as error:
        raise InvalidInputError("source is not a well-formed URL") from error
    if split.scheme != "https" or host not in ALLOWED_HOSTS:
        raise InvalidInputError("source must be an HTTPS youtube.com or youtu.be URL")
    try:
        port = split.port
    except ValueError:
        port = -1
    if split.username or split.password or port not in (None, 443):
        raise InvalidInputError("YouTube URL must not contain credentials or a custom port")
    if _is_playlist_url(split, host):
        raise InvalidInputError(_PLAYLIST_REJECTION)
    return url


def _base_arguments() -> list[str]:
    return [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-config",
        "--no-playlist",
        "--no-warnings",
        "--socket-timeout",
        "15",
        "--retries",
        "2",
        "--extractor-retries",
        "2",
    ]


def inspect(url: str, *, timeout: float = 60) -> dict[str, object]:
    """Read one video's metadata document. Entry point: see the error contract above."""
    validate_url(url)
    _validate_seconds(timeout, "provider timeout")
    result = run_command(
        [*_base_arguments(), "--dump-single-json", "--skip-download", url],
        timeout=timeout,
        redact=(url,),
        max_output_per_stream=MAX_METADATA_BYTES,
    )
    try:
        metadata = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ProviderFailedError("yt-dlp returned invalid metadata") from error
    if not isinstance(metadata, dict):
        raise ProviderFailedError("yt-dlp returned an unexpected metadata document")
    if metadata.get("_type") in {"playlist", "multi_video"} or "entries" in metadata:
        raise InvalidInputError(_PLAYLIST_REJECTION)
    if metadata.get("is_live") or metadata.get("live_status") in {"is_live", "is_upcoming"}:
        raise InvalidInputError("live and upcoming streams are not supported")
    video_id = metadata.get("id")
    if not isinstance(video_id, str) or not _VIDEO_ID.fullmatch(video_id):
        raise ProviderFailedError("yt-dlp did not return a valid video id")
    return cast(dict[str, object], metadata)


def _accept_media(destination: Path, *, filesize_limit: int, max_filesize: str) -> Path:
    media = [
        path
        for path in destination.glob("source.*")
        if path.is_file() and path.suffix.lower() not in _NON_MEDIA_SUFFIXES
    ]
    if not media:
        # yt-dlp exited 0 and wrote no media. With the flags download() passes, the one
        # silent abort we can produce in the locked version is --max-filesize firing on a
        # declared Content-Length (yt_dlp/downloader/http.py). It announces that through
        # to_screen, which --quiet suppresses, returns False, and YoutubeDL then neither
        # reports an error nor sets a non-zero exit status -- measured against the locked
        # yt-dlp over a local HTTP server: 2 MB body, --max-filesize 1K, exit 0, no file.
        # Every other failure we could provoke -- 404, empty body, unsatisfiable format,
        # connection refused -- reports an error and exits non-zero, in which case
        # run_command raised and this line was never reached. So this is the oversized
        # branch, the one the README documents, and it says so. It is an inference from
        # "exit 0 and no media", not a signal from yt-dlp: a silent abort nobody has
        # produced yet would be reported in these words too.
        raise InvalidInputError(f"source media exceeds the {max_filesize} size limit")
    if len(media) > 1:
        raise ProviderFailedError("yt-dlp did not produce exactly one media file")
    source = media[0]
    if source.stat().st_size > filesize_limit:
        raise InvalidInputError(f"source media exceeds the {max_filesize} size limit")
    return source


def _discard_acquisition(destination: Path) -> None:
    """Remove what this stage wrote whenever it does not hand back a source.

    F25 adjacent, and the reason a rejection is not just a message: the oversized media
    itself, the subtitles yt-dlp writes before the media, and any ``.part`` left by an
    interrupted or timed-out fragment download all live in ``project_dir/source``. The
    pipeline marks the stage failed and records no artifacts for it, so nothing here is
    referenced afterwards. The cost is that a retry re-downloads instead of resuming a
    ``.part``, which is what re-running an invalidated stage does in any case.

    The glob is anchored to the ``--output source.%(ext)s`` template ``download`` passes.
    The one neighbour the pipeline puts in that directory is the user's supplied lyrics
    file, copied to ``source/lyrics-input.*``; that name cannot match, and a test pins it.
    """
    for path in destination.glob("source*"):
        if path.is_file():
            with suppress(OSError):
                path.unlink()


def download(
    url: str,
    destination: Path,
    *,
    language: str = "auto",
    max_duration: float = 30 * 60,
    max_filesize: str = "512M",
    timeout: float = 30 * 60,
) -> tuple[Path, list[Path], dict[str, object]]:
    """Fetch exactly one audio source into ``destination`` under an explicit size ceiling.

    "An oversized source can never consume unbounded disk" is BOUNDED BY NAMED CHECKS,
    not enforced by a byte counter:

    * Declared size, before the first byte: yt-dlp's ``--max-filesize`` aborts the
      transfer when the server sends a Content-Length. ``_accept_media`` turns that
      silent abort into the documented message.
    * Fragmented (DASH/HLS) transfers: that ceiling does not apply. ``max_filesize`` is
      read only by ``yt_dlp/downloader/http.py`` and ``external.py`` in the locked
      version, so nothing counts bytes mid-transfer -- which is the case the post-hoc
      check exists for. Bounded instead by ``--format bestaudio/best`` (one audio stream,
      no video) and the <=30-minute duration gate below: about 29 MB at 128 kbps and
      72 MB at a pathological 320 kbps, against a 512 MiB ceiling.
    * Whatever still slips through: the post-hoc size check in ``_accept_media``.

    OUT OF SCOPE: a true in-flight byte counter. ``run_command`` blocks until the provider
    exits, so counting would mean watching the output directory from a second thread and
    tearing the provider down from there -- more machinery than the bound above is worth.

    Every caller-supplied value is checked before the first network call, so a bad argument
    costs no traffic; see the error contract above this module's entry points for what that
    covers and what it does not.
    """
    filesize_limit = _parse_filesize(max_filesize)
    _validate_seconds(timeout, "provider timeout")
    _validate_language(language)
    _validate_seconds(max_duration, "maximum duration")
    metadata = inspect(url)
    duration = metadata.get("duration")
    if not isinstance(duration, int | float) or duration <= 0:
        raise InvalidInputError("the source has no finite positive duration")
    if float(duration) > max_duration:
        raise InvalidInputError(f"source exceeds the {max_duration / 60:g}-minute limit")

    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    subtitle_language = "en.*,en" if language == "auto" else language
    arguments = [
        *_base_arguments(),
        "--quiet",
        "--format",
        "bestaudio/best",
        "--max-filesize",
        max_filesize,
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs",
        subtitle_language,
        "--sub-format",
        "vtt",
        "--paths",
        str(destination),
        "--output",
        "source.%(ext)s",
        url,
    ]
    try:
        run_command(arguments, timeout=timeout, redact=(url, str(destination)))
        source = _accept_media(
            destination, filesize_limit=filesize_limit, max_filesize=max_filesize
        )
    except BaseException:
        _discard_acquisition(destination)
        raise
    subtitles = sorted(path for path in destination.glob("source*.vtt") if path.is_file())
    return source, subtitles, metadata
