"""Demucs provider wrapper with a private cache and validated stem artifacts.

The htdemucs_6s weights come only through Kilix's licence records: each run
first verifies the ``demucs-htdemucs-6s`` asset, then hands Demucs that
directory (``--repo``) and the model's signature, and runs it offline. Demucs
never fetches a model itself.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

from .. import licensed
from ..errors import InvalidInputError, ProviderFailedError, ProviderUnavailableError
from ..paths import cache_home, ensure_private_directory
from ..runner import run_command
from .media import probe

KNOWN_STEMS = ("vocals", "drums", "bass", "guitar", "piano", "other")
SUPPORTED_MODELS = frozenset({"htdemucs_6s"})
OFFLINE_PROXY = "http://127.0.0.1:9"


def is_available() -> bool:
    return importlib.util.find_spec("demucs") is not None


def separate(
    source: Path,
    destination: Path,
    *,
    model: str = "htdemucs_6s",
    device: str = "auto",
    timeout: float = 90 * 60,
) -> dict[str, Path]:
    if model not in SUPPORTED_MODELS:
        raise InvalidInputError("unsupported Demucs model")
    if not is_available():
        raise ProviderUnavailableError(
            "Demucs is not installed; run `uv sync --all-extras` from the repository"
        )
    weights = licensed.verify(licensed.DEMUCS)
    model_cache = ensure_private_directory(cache_home() / "demucs")
    scratch = destination.parent / ".separate-work"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(mode=0o700, parents=True)
    arguments = [
        sys.executable,
        "-m",
        "demucs.separate",
        "--repo",
        str(weights),
        "--name",
        licensed.DEMUCS_SIGNATURE,
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
        "HF_HUB_OFFLINE": "1",
        # Every route to a model download points at a closed port.
        "http_proxy": OFFLINE_PROXY,
        "https_proxy": OFFLINE_PROXY,
        "HTTP_PROXY": OFFLINE_PROXY,
        "HTTPS_PROXY": OFFLINE_PROXY,
        "ALL_PROXY": OFFLINE_PROXY,
        "NO_PROXY": "",
    }
    try:
        run_command(
            arguments,
            timeout=timeout,
            env=environment,
            redact=(str(source), str(destination), str(scratch), str(weights)),
        )
        produced_root = scratch / licensed.DEMUCS_SIGNATURE
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
