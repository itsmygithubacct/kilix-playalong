"""Small deterministic helpers shared by providers and persistence."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .text import printable_line


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: Path, *, block_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(block_size):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def private_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def public_error(value: str, *, secrets: tuple[str, ...] = ()) -> str:
    """A provider's own words, with what a user must not be shown taken out of them.

    Redaction first, then `text.printable_line`. The second half is not decoration:
    what arrives here is a subprocess's stderr -- ffmpeg's, yt-dlp's, Demucs' -- and
    yt-dlp's in particular echoes strings a remote server chose. It lands in the
    manifest, which `cli.command_show` prints to a terminal, `server` serves and the
    progress callback's ``error`` arm carries. So it goes through the same rule as a
    title: unprintables become spaces, whitespace collapses, and the result is
    bounded -- at 500 rather than `text.MAX_DISPLAY_TEXT`, because a diagnosis has to
    survive being read where a song title only has to be recognised.

    Truncation is deliberately last. Redacting after a cut could leave the head of a
    URL or a home path standing as a shorter string that is still the secret.
    """
    result = value
    for secret in secrets:
        if secret:
            result = result.replace(secret, "<redacted>")
    result = re.sub(r"https?://\S+", "<redacted-url>", result)
    result = re.sub(r"/(?:home|Users)/[^/\s]+", "<home>", result)
    return printable_line(result, limit=500)
