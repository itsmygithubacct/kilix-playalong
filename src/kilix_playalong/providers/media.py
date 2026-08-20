"""Bounded ffprobe inspection and normalization."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import cast

from ..errors import ProviderFailedError, ProviderUnavailableError
from ..runner import run_command


def require_media_tools() -> None:
    missing = [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]
    if missing:
        raise ProviderUnavailableError("missing required media tools: " + ", ".join(missing))


def probe(path: Path, *, timeout: float = 30) -> dict[str, object]:
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
    if not any(
        isinstance(stream, dict) and stream.get("codec_type") == "audio" for stream in streams
    ):
        raise ProviderFailedError("downloaded media has no audio stream")
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
