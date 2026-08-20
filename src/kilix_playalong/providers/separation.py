"""Demucs provider wrapper with a private cache and validated stem artifacts."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

from ..errors import ProviderFailedError, ProviderUnavailableError
from ..paths import cache_home, ensure_private_directory
from ..runner import run_command
from .media import probe

KNOWN_STEMS = ("vocals", "drums", "bass", "guitar", "piano", "other")
SUPPORTED_MODELS = frozenset({"htdemucs_6s"})


def is_available() -> bool:
    return importlib.util.find_spec("demucs") is not None


def separate(
    source: Path,
    destination: Path,
    *,
    model: str = "htdemucs_6s",
    device: str = "auto",
    allow_model_downloads: bool = False,
    timeout: float = 90 * 60,
) -> dict[str, Path]:
    if model not in SUPPORTED_MODELS:
        raise ProviderFailedError("unsupported Demucs model")
    if not is_available():
        raise ProviderUnavailableError(
            "Demucs is not installed; run `uv sync --all-extras` from the repository"
        )
    model_cache = ensure_private_directory(cache_home() / "demucs")
    scratch = destination.parent / ".separate-work"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(mode=0o700, parents=True)
    arguments = [
        sys.executable,
        "-m",
        "demucs.separate",
        "--name",
        model,
        "--out",
        str(scratch),
        "--filename",
        "{stem}.{ext}",
        "--jobs",
        "1",
    ]
    if device != "auto":
        arguments.extend(("--device", device))
    arguments.append(str(source))
    environment = {
        "TORCH_HOME": str(model_cache / "torch"),
        "HF_HOME": str(model_cache / "huggingface"),
        "HF_HUB_DISABLE_TELEMETRY": "1",
    }
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
    try:
        run_command(
            arguments,
            timeout=timeout,
            env=environment,
            redact=(str(source), str(destination), str(scratch)),
        )
        produced_root = scratch / model.replace("hf://", "").replace("/", "_")
        found = {path.stem: path for path in produced_root.glob("*.wav") if path.is_file()}
        if "vocals" not in found:
            raise ProviderFailedError("Demucs did not produce a vocals stem")
        guitar_source = found.get("guitar") or found.get("other")
        if guitar_source is None:
            raise ProviderFailedError("Demucs did not produce a guitar or other-instruments stem")
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        result: dict[str, Path] = {}
        for stem in KNOWN_STEMS:
            path = found.get(stem)
            if path is None:
                continue
            probe(path)
            target = destination / f"{stem}.wav"
            path.replace(target)
            target.chmod(0o600)
            result[stem] = target
        return result
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
