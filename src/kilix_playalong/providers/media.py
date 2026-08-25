"""Bounded ffprobe inspection, normalization, and copy-in of local media."""

from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import cast

from ..errors import InvalidInputError, ProviderFailedError, ProviderUnavailableError
from ..runner import run_command

#: One read/write per megabyte. Large enough that a 512 MiB file costs ~512 pairs
#: of syscalls rather than ~131,000, small enough that the buffer is never a
#: memory decision, and it is also the granularity of the ``on_bytes`` callback --
#: see ``copy_into``, whose caller owns rate-limiting them.
_COPY_BLOCK = 1024 * 1024

#: ffmpeg's own default, passed explicitly. A media file's *bytes* can be a
#: playlist -- an HLS ``.m3u8`` naming ``http://...`` -- and the demuxer would
#: fetch it; ffmpeg 7.1 refuses that by default, but that is the tool's policy
#: and this package's threat model (``source.py``: "the extension is not
#: evidence") makes it this package's problem. Naming it here means a future
#: ffmpeg that widens its default cannot widen this, and pins it to a test rather
#: than to a dependency's release notes. Not a narrowing: ``crypto`` and ``data``
#: are what encrypted and inline-payload local files legitimately need.
_PROTOCOLS = "file,crypto,data"

#: How this package opens a file it did not create. One name for a security
#: property that is otherwise four constants written out twice -- here and in
#: ``source._measure`` -- where a flag dropped from one of the two copies during a
#: later edit produces no failing test and no visible symptom. ``O_NOFOLLOW`` is
#: the one that carries the property: the path was type-checked a moment earlier,
#: and without it a link swapped in since then is followed. ``O_NONBLOCK`` so a
#: FIFO that got past that type check cannot hang the open, ``O_NOCTTY`` so a
#: terminal device cannot become this process's controlling terminal, and
#: ``O_RDONLY`` because nothing here writes to the user's library. The re-check of
#: ``S_ISREG`` on the descriptor is not folded in: what it is worth differs between
#: the two opens, and each states its own case.
SAFE_OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY


def require_media_tools() -> None:
    missing = [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]
    if missing:
        raise ProviderUnavailableError("missing required media tools: " + ", ".join(missing))


def probe(path: Path, *, timeout: float = 30, require_audio: bool = True) -> dict[str, object]:
    """Read one file's ffprobe document, by default insisting it has audio.

    ``require_audio=False`` exists for exactly one caller: ``source.inspect_file``,
    which needs to tell "this is not a media file at all" apart from "this is a
    video with no audio track" so it can say which. Everything else -- the
    pipeline's own check, ``normalize``'s check of its output -- keeps the default,
    because for those two an audio-less file is simply a failure. A test pins that
    the default is unchanged.
    """
    require_media_tools()
    result = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            "-protocol_whitelist",
            _PROTOCOLS,
            str(path),
        ],
        timeout=timeout,
        redact=(str(path),),
    )
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ProviderFailedError("ffprobe returned invalid JSON") from error
    if not isinstance(document, dict) or not isinstance(document.get("streams"), list):
        raise ProviderFailedError("ffprobe returned an unexpected document")
    streams = document["streams"]
    if require_audio and not any(
        isinstance(stream, dict) and stream.get("codec_type") == "audio" for stream in streams
    ):
        # Not "downloaded": media now arrives from the user's own disk too, and a
        # message that names a download it never did sends them looking for one.
        raise ProviderFailedError("media has no audio stream")
    return cast(dict[str, object], document)


def normalize(source: Path, output: Path, *, timeout: float = 10 * 60) -> Path:
    require_media_tools()
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".partial")
    arguments = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-protocol_whitelist",
        _PROTOCOLS,
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "2",
        "-ar",
        "44100",
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        str(temporary),
    ]
    try:
        run_command(arguments, timeout=timeout, redact=(str(source), str(output), str(temporary)))
        probe(temporary)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def _copy_failed(error: OSError) -> ProviderFailedError:
    """Restate an ``OSError`` from the copy without the path it names.

    ``str(OSError)`` appends ``error.filename``, and for the read side that is a
    path in the user's own library. It must not reach an error surface -- and the
    reason is *not* that nothing downstream would catch it. ``pipeline._run_stage``
    redacts with ``public_error(str(error), secrets=self._secrets())``, and the
    file arm's ``_secrets()`` lists the library path, its directory and its
    resolved form, all three learned by ``_acquire_file`` before it calls in. So a
    library on ``/mnt/music`` is redacted there today.

    The reason is that this function has callers who are not that stage:
    ``copy_into`` is public, ``source.acquire`` is reachable from a JSON or IPC
    surface, and a property that holds only because one caller cleans up afterwards
    is that caller's property and not this one's. Here it is ENFORCED.

    Only ``strerror`` -- the C string for the errno, which carries no path -- is
    kept, so "Too many levels of symbolic links" and "No space left on device"
    still tell the user which of the two happened. The ``/`` test makes that a
    check on this call rather than an assumption about libc: an explanation that
    looks like it contains a path is dropped instead of forwarded.
    """
    reason = error.strerror
    if not isinstance(reason, str) or "/" in reason:
        reason = ""
    reason = " ".join(reason.split())[:100]
    message = "the media could not be copied into the project"
    return ProviderFailedError(f"{message}: {reason}" if reason else message)


def _write_all(descriptor: int, block: bytes) -> None:
    offset = 0
    while offset < len(block):
        offset += os.write(descriptor, block[offset:])


def copy_into(
    source: Path,
    destination: Path,
    *,
    max_bytes: int = 512 * 1024**2,
    on_bytes: Callable[[int, int], None] | None = None,
) -> Path:
    """Copy media into the project directory. A copy: never a move, never a link.

    The property, and why it is a copy and not the cheaper thing:

    * the user's library must be exactly as it was afterwards, so nothing here
      renames, truncates, or writes through to ``source``; and
    * the project must keep working when they reorganise that library, so the
      project's file is its own bytes on its own inode, not a symlink into a tree
      this app does not own and not a hardlink whose link count says otherwise.

    ``shutil.copyfile`` would do the bytes and none of the rest: it follows a
    symlink at the destination, it creates the destination under the process
    umask rather than at 0600, and it has no size ceiling -- so a file that grows
    between the check in ``source.inspect_file`` and this call would be copied in
    full. Hence the hand-rolled loop, the ``O_NOFOLLOW`` read, the ``O_EXCL``
    write and the running byte count, which is the backstop for exactly that race.

    Partial output never survives a failure: the bytes land in ``.partial`` and
    are renamed into place only after the last write, so an interrupted copy
    leaves no half file that a later stage would treat as the source. The one
    thing that removal will not do is raise: an ``OSError`` from the cleanup is
    dropped, because letting it replace the failure that caused it would both lose
    the real diagnosis and put a path back into the message.

    No error out of this function names a path -- ENFORCED, by the handler below
    and ``_copy_failed``, which keeps the errno's own ``strerror`` and discards
    ``error.filename``. Every other error raised here is a literal. That matters
    beyond tidiness: it is the redaction rule the whole package holds, and it is
    held *here* because this function's callers are not all ``pipeline._run_stage``
    and cannot inherit that stage's secrets list (see ``_copy_failed``).

    ``on_bytes(copied, total)`` is called after each block, and exists so a caller
    can report a fraction that is *measured* -- bytes written over bytes to write
    -- rather than inferred from a clock. That is this package's rule and not a
    preference: a bar that moves because the clock moved answers "is this stuck",
    the one question a user watching a long stage is actually asking, with a number
    that cannot know. ``total`` is the size the descriptor reports; it is 0 only if
    the file emptied under us. One call per ``_COPY_BLOCK``, so a 512 MiB file
    fires 512 of them: a caller forwarding these to a surface owns rate-limiting
    them, deliberately not done here because this function cannot know what the
    callback costs. A callback that raises aborts the copy and removes the partial
    file, like any other failure here. It propagates unchanged unless it is itself
    an ``OSError``, which is indistinguishable from one of ours and is restated the
    same way -- the caller keeps its errno, not its own message. Pinned both ways by
    ``test_a_callback_that_fails_takes_the_partial_file_with_it``.
    """
    if max_bytes <= 0:
        raise InvalidInputError("maximum source size must be a positive number of bytes")
    for candidate in (source, destination):
        try:
            os.fsencode(candidate)
        except UnicodeEncodeError as error:
            # A lone surrogate -- what ``json.loads('"\\ud800.mp3"')`` yields --
            # cannot be encoded for any syscall, so ``os.open`` (and, in the
            # cleanup, ``unlink``) answers it with a bare ``UnicodeEncodeError``:
            # a traceback rather than a message. ``os.fsencode`` asks the same
            # question those calls will ask, and asks it before anything is
            # created. It refuses only what no filename can be: an undecodable
            # byte in a real filename arrives as surrogateescape's
            # ``\udc80``-``\udcff`` and encodes straight back.
            raise InvalidInputError(
                "a media path must not contain characters the filesystem cannot represent"
            ) from error
    if not destination.name:
        # ``with_name`` below answers a nameless path ("/", ".") with a
        # ``ValueError`` that quotes the path -- which is the one thing nothing
        # raised here is allowed to do. Checked rather than caught, because the
        # caller passing a directory as a destination is a bug, not a condition.
        raise InvalidInputError("the copy destination must name a file")
    temporary = destination.with_name(destination.name + ".partial")
    copied = 0
    try:
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if destination.exists() and source.resolve() == destination.resolve():
            raise InvalidInputError("refusing to copy media onto itself")
        temporary.unlink(missing_ok=True)
        reader = os.open(source, SAFE_OPEN_FLAGS)
        try:
            information = os.fstat(reader)
            if not stat.S_ISREG(information.st_mode):
                # The type was checked before this call; re-checked on the open
                # descriptor because that is the only check that is about the
                # thing actually being read rather than about a path. Word for word
                # ``source._measure``'s sentence for the same refusal: the two guard
                # one property at two moments, and a user who trips it in either
                # place should not have to work out that it was the same rule.
                raise InvalidInputError("source must be a regular file")
            writer = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                total = int(information.st_size)
                while block := os.read(reader, _COPY_BLOCK):
                    copied += len(block)
                    if copied > max_bytes:
                        raise InvalidInputError("source media grew past its size limit")
                    _write_all(writer, block)
                    if on_bytes is not None:
                        on_bytes(copied, max(total, copied))
                os.fsync(writer)
            finally:
                os.close(writer)
        finally:
            os.close(reader)
        if copied == 0:
            raise InvalidInputError("source media is empty")
        temporary.replace(destination)
        destination.chmod(0o600)
    except BaseException as error:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        if isinstance(error, OSError):
            raise _copy_failed(error) from error
        raise
    return destination
