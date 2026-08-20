"""Explicit, bounded YouTube acquisition through the locked yt-dlp package."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from ..errors import InvalidInputError, ProviderFailedError
from ..runner import run_command

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
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{6,32}$")


def validate_url(url: str) -> str:
    if len(url) > MAX_URL_LENGTH:
        raise InvalidInputError("YouTube URL is too long")
    split = urlsplit(url)
    host = (split.hostname or "").lower().rstrip(".")
    if split.scheme != "https" or host not in ALLOWED_HOSTS:
        raise InvalidInputError("source must be an HTTPS youtube.com or youtu.be URL")
    if split.username or split.password or split.port not in (None, 443):
        raise InvalidInputError("YouTube URL must not contain credentials or a custom port")
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
    validate_url(url)
    result = run_command(
        [*_base_arguments(), "--dump-single-json", "--skip-download", url],
        timeout=timeout,
        redact=(url,),
        max_output=MAX_METADATA_BYTES,
    )
    try:
        metadata = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ProviderFailedError("yt-dlp returned invalid metadata") from error
    if not isinstance(metadata, dict):
        raise ProviderFailedError("yt-dlp returned an unexpected metadata document")
    if metadata.get("is_live") or metadata.get("live_status") in {"is_live", "is_upcoming"}:
        raise InvalidInputError("live and upcoming streams are not supported")
    video_id = metadata.get("id")
    if not isinstance(video_id, str) or not _VIDEO_ID.fullmatch(video_id):
        raise ProviderFailedError("yt-dlp did not return a valid video id")
    return cast(dict[str, object], metadata)


def download(
    url: str,
    destination: Path,
    *,
    language: str = "auto",
    max_duration: float = 30 * 60,
    max_filesize: str = "512M",
    timeout: float = 30 * 60,
) -> tuple[Path, list[Path], dict[str, object]]:
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
    run_command(arguments, timeout=timeout, redact=(url, str(destination)))
    media = [
        path
        for path in destination.glob("source.*")
        if path.is_file() and path.suffix.lower() not in {".vtt", ".part", ".ytdl", ".json"}
    ]
    if len(media) != 1:
        raise ProviderFailedError("yt-dlp did not produce exactly one media file")
    subtitles = sorted(path for path in destination.glob("source*.vtt") if path.is_file())
    return media[0], subtitles, metadata
