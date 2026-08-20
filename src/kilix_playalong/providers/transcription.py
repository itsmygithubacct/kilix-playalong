"""Optional isolated faster-whisper lyrics transcription."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from ..errors import ProviderUnavailableError
from ..paths import cache_home, ensure_private_directory
from ..runner import run_command

SUPPORTED_MODELS = frozenset(
    {
        "tiny",
        "tiny.en",
        "base",
        "base.en",
        "small",
        "small.en",
        "medium",
        "medium.en",
        "large-v1",
        "large-v2",
        "large-v3",
        "large-v3-turbo",
        "distil-small.en",
        "distil-medium.en",
        "distil-large-v2",
        "distil-large-v3",
    }
)


def is_available() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


def transcribe(
    source: Path,
    output: Path,
    *,
    language: str = "auto",
    model: str = "small",
    device: str = "auto",
    allow_model_downloads: bool = False,
    timeout: float = 60 * 60,
) -> Path:
    if model not in SUPPORTED_MODELS:
        raise ProviderUnavailableError("unsupported faster-whisper model")
    if not is_available():
        raise ProviderUnavailableError(
            "timed lyrics are unavailable: supply --lyrics or run `uv sync --all-extras`"
        )
    model_cache = ensure_private_directory(cache_home() / "faster-whisper")
    arguments = [
        sys.executable,
        "-m",
        "kilix_playalong._whisper_worker",
        str(source),
        str(output),
        "--model",
        model,
        "--device",
        device,
        "--cache",
        str(model_cache),
    ]
    if language != "auto":
        arguments.extend(("--language", language))
    environment = {"HF_HUB_DISABLE_TELEMETRY": "1", "HF_HOME": str(model_cache)}
    if not allow_model_downloads:
        environment["HF_HUB_OFFLINE"] = "1"
        offline_proxy = "http://127.0.0.1:9"
        environment.update(
            http_proxy=offline_proxy,
            https_proxy=offline_proxy,
            HTTP_PROXY=offline_proxy,
            HTTPS_PROXY=offline_proxy,
            ALL_PROXY=offline_proxy,
            NO_PROXY="",
        )
    run_command(
        arguments,
        timeout=timeout,
        env=environment,
        redact=(str(source), str(output), str(model_cache)),
    )
    output.chmod(0o600)
    return output
