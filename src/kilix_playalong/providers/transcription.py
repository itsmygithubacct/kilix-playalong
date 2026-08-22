"""Optional isolated faster-whisper lyrics transcription."""

from __future__ import annotations

import importlib
import importlib.util
import os
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
AUTO_MODEL = "auto"
DEFAULT_MODEL = AUTO_MODEL
MODEL_CHOICES = SUPPORTED_MODELS | {AUTO_MODEL}

_GIB = 1024**3
_QUALITY_ORDER = ("large-v3", "large-v3-turbo", "medium", "small")
_MODEL_REPOSITORIES = {
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "medium": "Systran/faster-whisper-medium",
    "small": "Systran/faster-whisper-small",
}


def is_available() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


def _cuda_available() -> bool:
    if importlib.util.find_spec("ctranslate2") is None:
        return False
    try:
        ctranslate2 = importlib.import_module("ctranslate2")
        get_count = getattr(ctranslate2, "get_cuda_device_count", None)
        return bool(callable(get_count) and get_count() > 0)
    except (ImportError, OSError, RuntimeError):
        return False


def _read_positive_int(path: Path) -> int | None:
    try:
        value = path.read_text(encoding="ascii").strip()
        parsed = int(value)
    except (OSError, UnicodeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _proc_available_memory_bytes() -> int | None:
    try:
        lines = Path("/proc/meminfo").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        return None
    for line in lines:
        name, separator, value = line.partition(":")
        fields = value.split()
        if name == "MemAvailable" and separator and fields:
            try:
                return int(fields[0]) * 1024
            except ValueError:
                return None
    return None


def _cgroup_v2_directory() -> Path | None:
    try:
        records = Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        return None
    for record in records:
        hierarchy, separator, relative = record.partition("::")
        if hierarchy == "0" and separator:
            return Path("/sys/fs/cgroup") / relative.lstrip("/")
    return None


def _available_memory_bytes() -> int | None:
    estimates: list[int] = []
    host_available = _proc_available_memory_bytes()
    if host_available is None:
        try:
            pages = int(os.sysconf("SC_AVPHYS_PAGES"))
            page_size = int(os.sysconf("SC_PAGE_SIZE"))
            if pages > 0 and page_size > 0:
                host_available = pages * page_size
        except (OSError, TypeError, ValueError):
            pass
    if host_available is not None:
        estimates.append(host_available)

    cgroup = _cgroup_v2_directory()
    while cgroup is not None and cgroup != cgroup.parent:
        cgroup_limit = _read_positive_int(cgroup / "memory.max")
        cgroup_used = _read_positive_int(cgroup / "memory.current")
        if cgroup_limit is not None and cgroup_used is not None and cgroup_limit > cgroup_used:
            estimates.append(cgroup_limit - cgroup_used)
        if cgroup == Path("/sys/fs/cgroup"):
            break
        cgroup = cgroup.parent
    return min(estimates) if estimates else None


def _auto_candidates(device: str) -> tuple[str, ...]:
    if device == "cuda" or (device == "auto" and _cuda_available()):
        return _QUALITY_ORDER

    available = _available_memory_bytes()
    if available is None:
        return _QUALITY_ORDER[1:]
    if available >= 10 * _GIB:
        return _QUALITY_ORDER
    if available >= 6 * _GIB:
        return _QUALITY_ORDER[1:]
    if available >= 3 * _GIB:
        return _QUALITY_ORDER[2:]
    return _QUALITY_ORDER[3:]


def _is_cached(model_cache: Path, model: str) -> bool:
    repository = _MODEL_REPOSITORIES[model]
    snapshots = model_cache / ("models--" + repository.replace("/", "--")) / "snapshots"
    try:
        return any((snapshot / "model.bin").is_file() for snapshot in snapshots.iterdir())
    except OSError:
        return False


def resolve_model(
    requested: str,
    *,
    device: str,
    model_cache: Path,
    allow_model_downloads: bool,
) -> str:
    """Resolve the adaptive model to the best practical local configuration."""
    if requested not in MODEL_CHOICES:
        raise ProviderUnavailableError("unsupported faster-whisper model")
    if requested != AUTO_MODEL:
        return requested

    candidates = _auto_candidates(device)
    if allow_model_downloads:
        return candidates[0]
    for candidate in candidates:
        if _is_cached(model_cache, candidate):
            return candidate
    raise ProviderUnavailableError(
        "no suitable cached faster-whisper model is available; "
        "rerun with --allow-model-downloads or select a cached model explicitly"
    )


def transcribe(
    source: Path,
    output: Path,
    *,
    language: str = "auto",
    model: str = DEFAULT_MODEL,
    device: str = "auto",
    allow_model_downloads: bool = False,
    timeout: float = 60 * 60,
) -> Path:
    if model not in MODEL_CHOICES:
        raise ProviderUnavailableError("unsupported faster-whisper model")
    if not is_available():
        raise ProviderUnavailableError(
            "timed lyrics are unavailable: supply --lyrics or run `uv sync --all-extras`"
        )
    model_cache = ensure_private_directory(cache_home() / "faster-whisper")
    selected_model = resolve_model(
        model,
        device=device,
        model_cache=model_cache,
        allow_model_downloads=allow_model_downloads,
    )
    arguments = [
        sys.executable,
        "-m",
        "kilix_playalong._whisper_worker",
        str(source),
        str(output),
        "--model",
        selected_model,
        "--device",
        device,
        "--cache",
        str(model_cache),
    ]
    if language != "auto":
        arguments.extend(("--language", language))
    environment = {
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "HF_HOME": str(model_cache),
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
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
    run_command(
        arguments,
        timeout=timeout,
        env=environment,
        redact=(str(source), str(output), str(model_cache)),
    )
    output.chmod(0o600)
    return output
