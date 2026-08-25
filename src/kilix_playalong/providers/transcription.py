"""Optional isolated faster-whisper lyrics transcription.

This module owns the boundary to the heavy worker: it decides *what* the worker
is asked to do and hands it a fixed argv, an allowlisted environment and a
disposable HOME through ``runner.run_command``. The tuning of the model itself
lives in ``_whisper_worker`` and is not reachable from here or from a caller.

It also owns the receipt -- the ``source`` string written into the lyrics
document and copied into the project manifest -- because the receipt is what a
later reader has to work from, and the format has to be parseable by the
pipeline as well as writeable by the worker. ``format_receipt`` writes one and
``parse_receipt`` reads one; nothing outside this module should take a recorded
``source`` apart by hand, because the format grew a tail and the obvious
prefix-slice now returns the whole record where it used to return the model.

And it owns where the weights live: ``model_cache_path`` names that directory
and ``transcribe`` is the only thing that creates it, so a surface describing a
machine -- which model is cached, what ``auto`` would run -- can ask the same
question the worker's ``--cache`` answers without making a directory to find out.
"""

from __future__ import annotations

import importlib
import importlib.util
import math
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from ..errors import InvalidInputError, ProviderUnavailableError
from ..paths import cache_home, ensure_private_directory
from ..runner import run_command, usable_seconds

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

#: Which audio a transcription pass listens to.
#:
#: The isolated vocal stem is right most of the time, which is why it is the
#: default, but it is not right always: Demucs leaves artefacts -- smearing,
#: dropped consonants, a whole quiet line lost to the "other" stem -- and on a
#: sparse mix (voice and one guitar) the stem can be *worse* than the untouched
#: audio, because separation had nothing to gain and still cost something.
AUDIO_SOURCE_VOCALS = "vocals"
AUDIO_SOURCE_MIX = "mix"
AUDIO_SOURCE_AUTO = "auto"
AUDIO_SOURCE_CHOICES = (AUDIO_SOURCE_VOCALS, AUDIO_SOURCE_MIX, AUDIO_SOURCE_AUTO)

#: The vocal stem, deliberately, and not ``auto``. ``auto`` transcribes both
#: sources and keeps the better transcript, which means it decodes the song
#: twice: transcription is the longest stage in the pipeline, so defaulting to
#: it would roughly double the wall-clock of every job to improve a minority of
#: them. It is offered, described, and left for the user to choose.
DEFAULT_AUDIO_SOURCE = AUDIO_SOURCE_VOCALS

#: How many decoding passes each audio-source choice costs. Used to scale the
#: subprocess wall-clock bound, so that asking for two passes does not simply
#: time out halfway through the second one.
_AUDIO_SOURCE_PASSES = {
    AUDIO_SOURCE_VOCALS: 1,
    AUDIO_SOURCE_MIX: 1,
    AUDIO_SOURCE_AUTO: 2,
}

AUTO_LANGUAGE = "auto"

#: A BCP 47-ish tag, matching the pipeline's own option validation. Whisper
#: itself only knows base language subtags, so ``normalize_language`` reduces a
#: tag to its first subtag before the worker ever sees it; without that,
#: ``--language en-GB`` reaches faster-whisper and dies inside the subprocess
#: with a stack trace instead of being handled here.
_LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")

#: A small, renderable set for an intake screen, not the model's full 99. Any
#: valid tag is still accepted by ``transcribe``; this is what a surface offers
#: without a search box, and "auto" leads because detection is usually right.
LANGUAGE_CHOICES = (
    AUTO_LANGUAGE,
    "en",
    "es",
    "fr",
    "de",
    "it",
    "pt",
    "nl",
    "sv",
    "pl",
    "ru",
    "uk",
    "tr",
    "ar",
    "hi",
    "ja",
    "ko",
    "zh",
)


def normalize_language(value: str) -> str:
    """Return ``auto`` or a lowercase base language subtag.

    Raises ``InvalidInputError`` on anything else. This is a boundary check, not
    a convenience: ``transcribe`` is importable and its ``language`` argument
    becomes an argv element, so a value like ``--device`` or ``-x`` must be
    rejected here rather than reinterpreted as an option by the worker's
    argument parser.
    """
    if value == AUTO_LANGUAGE:
        return AUTO_LANGUAGE
    if not _LANGUAGE_TAG.fullmatch(value):
        raise InvalidInputError("language must be 'auto' or a short BCP 47 language tag")
    return value.split("-", 1)[0].lower()


WHISPER_SOURCE_PREFIX = "faster-whisper:"

#: Receipt field order. Fixed so the string is stable, greppable and diffable
#: across runs; ``parse_receipt`` does not depend on the order, but a manifest
#: that reorders itself between runs is unreadable.
_RECEIPT_FIELDS = ("audio", "audio_from", "lang", "lang_from", "lang_confidence")


@dataclass(frozen=True)
class Receipt:
    """What produced a transcript, as recorded in the lyrics document's ``source``.

    Every field ``format_receipt`` writes comes from a closed vocabulary or a
    number: a model name from ``MODEL_CHOICES``, an audio source from
    ``AUDIO_SOURCE_CHOICES``, a normalised language subtag, and a probability.
    Nothing user-identifying -- no path, no URL, no title, no lyric text -- can
    reach it, which is what lets it be copied into a manifest and shown on both
    surfaces. A ``Receipt`` handed back by ``parse_receipt`` echoes whatever the
    manifest already held instead of re-deriving it, so it is as trustworthy as
    that file and no more; see that function for why it does not re-validate.

    The defaults are not neutral placeholders. They are what a receipt written
    before the format grew its tail *means*: that release transcribed the vocal
    stem because the pipeline handed it no other path, and its receipt said
    nothing at all about the language. That is why ``language_from`` defaults to
    "unknown" and not to "detected" -- an absent field must not be read back as a
    provenance for a language the receipt does not even name.
    """

    model: str
    audio_source: str = AUDIO_SOURCE_VOCALS
    audio_from: str = "requested"
    language: str = "unknown"
    language_from: str = "unknown"
    language_confidence: float = 0.0


def _probability(value: float) -> float:
    """Clamp a probability into [0, 1], reading a non-number as *no* confidence.

    ``max(0.0, min(1.0, value))`` on its own answers 1.00 for both NaN and
    infinity, because ``min(1.0, nan)`` returns 1.0 -- so a corrupt number would
    be recorded as the highest confidence this receipt can express. In the one
    module whose job is to keep the receipt honest, an unusable number has to
    read as "we do not know", which is 0.0. The worker never produces one (it
    runs every number through ``_finite`` first); ``parse_receipt`` reads a
    manifest off disk, which nothing in this package controls.
    """
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, value))


def format_receipt(
    *,
    model: str,
    audio_source: str,
    audio_from: str,
    language: str,
    language_from: str,
    language_confidence: float,
) -> str:
    """Render the receipt written into the lyrics document's ``source`` field.

    The model stays first and immediately after ``faster-whisper:``, so the
    string still *reads* as the provider string it has always been, and the
    fields that answer "which audio, which language, how sure" follow it.
    """
    fields = {
        "audio": audio_source,
        "audio_from": audio_from,
        "lang": language,
        "lang_from": language_from,
        "lang_confidence": f"{_probability(language_confidence):.2f}",
    }
    tail = "".join(f";{name}={fields[name]}" for name in _RECEIPT_FIELDS)
    return f"{WHISPER_SOURCE_PREFIX}{model}{tail}"


def parse_receipt(value: str) -> Receipt | None:
    """Read a lyrics document's ``source`` back as a receipt, or None.

    This is the only supported way to take a recorded ``source`` apart, and the
    reason it exists as a public function is that slicing the prefix off by hand
    used to be enough and no longer is: ``value[len("faster-whisper:"):]`` was
    the model name when the receipt was only a model name, and is now the whole
    record. ``parse_receipt(value).model`` is the model name in *both* formats.

    None means "not a receipt": some other lyrics source (an imported ``.lrc``),
    or a string that starts with the prefix but names no model. A caller that
    only wants the model writes ``receipt = parse_receipt(source)`` and then
    ``None if receipt is None else receipt.model``.

    Tolerant by design, in both directions, because a project directory outlives
    the release that wrote it: a receipt from an older version carries only
    ``faster-whisper:<model>`` and parses, with every other field taking the
    default that version's behaviour implied, and an unknown field added by a
    newer version is ignored rather than making the whole receipt unreadable.

    Field *values* are not validated against this release's vocabularies --
    ``model`` need not be in ``MODEL_CHOICES``, ``audio_source`` need not be in
    ``AUDIO_SOURCE_CHOICES``. A receipt records what did run, which is not the
    same question as what this build would accept if asked to run it now.
    """
    if not value.startswith(WHISPER_SOURCE_PREFIX):
        return None
    model, *rest = value[len(WHISPER_SOURCE_PREFIX) :].split(";")
    if not model:
        # A receipt with no model names nothing; handing "" back as a model
        # would put an empty model name into the pipeline's stage key.
        return None
    fields: dict[str, str] = {}
    for item in rest:
        name, separator, field_value = item.partition("=")
        if separator:
            fields[name] = field_value
    try:
        confidence = float(fields.get("lang_confidence", "0"))
    except ValueError:
        confidence = 0.0
    return Receipt(
        model=model,
        audio_source=fields.get("audio", AUDIO_SOURCE_VOCALS),
        audio_from=fields.get("audio_from", "requested"),
        language=fields.get("lang", "unknown"),
        language_from=fields.get("lang_from", "unknown"),
        language_confidence=_probability(confidence),
    )


_GIB = 1024**3
#: Best first: what ``auto`` picks from once the memory ladder below has said which
#: of these this machine can run. Public because two other modules already reason
#: about it -- ``options_registry`` describes ``auto`` in these terms and
#: ``pipeline`` ranks a *recorded* configuration against it -- and a name reached
#: across a module boundary is public whatever it is spelled.
#:
#: ``pipeline`` still keeps a snapshot rather than importing this, and that is the
#: deliberate half: this order decides which recorded configurations its lyrics
#: stage accepts without re-transcribing, so importing it would let a reorder here
#: silently re-key every project on a user's machine. Its
#: ``_WHISPER_QUALITY_ORDER`` and the check that compares the two are what turn
#: that into a decision someone makes.
QUALITY_ORDER = ("large-v3", "large-v3-turbo", "medium", "small")
_MODEL_REPOSITORIES = {
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "medium": "Systran/faster-whisper-medium",
    "small": "Systran/faster-whisper-small",
}


def model_cache_path() -> Path:
    """Where this provider keeps faster-whisper's weights, *without* creating it.

    The only supported spelling of that path, and public because more than one
    caller needs it: ``transcribe`` wraps this in ``ensure_private_directory``
    and hands the result to the worker as both ``--cache`` and ``HF_HOME``, so
    this is where the weights actually land. A caller that only wants to
    *describe* a machine -- which model is already cached, what ``auto`` would
    run here -- needs the same answer to the same question and must not create a
    directory to get it, which is exactly why the creation lives in
    ``transcribe`` and not in here.
    """
    return cache_home() / "faster-whisper"


def is_available() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


#: Every path through which a CUDA device can announce itself to a Linux
#: process, checked before the import in ``cuda_available`` because that import
#: costs about a second of wall-clock (1.05-1.07 s measured on the machine this
#: was written on) and the CLI pays it on ``--help``.
#:
#: Only the *joint absence* of these is load-bearing, and only in the negative
#: direction: a CUDA runtime reaches a device through one of them, so finding
#: none of them is a proof of zero visible devices rather than a guess, while
#: finding one proves nothing at all and falls through to the real probe.
#:
#: * ``/proc/driver/nvidia`` -- published by the proprietary kernel module the
#:   moment it loads. This is what covers the machine whose device nodes do not
#:   exist yet: libcuda creates those on first use through ``nvidia-modprobe``,
#:   which can only succeed once that module is loaded.
#: * ``/dev/nvidiactl`` -- the same driver's control device, the node libcuda
#:   opens to initialise, and the one a GPU container is given even when it has
#:   no ``/proc/driver`` of its own.
#: * ``/dev/dxg`` -- WSL2, where the GPU is paravirtualised and *neither* of the
#:   two above exists on a perfectly working driver.
#: * ``/dev/nvhost-ctrl-gpu`` -- Tegra, whose integrated GPU is not the
#:   proprietary desktop driver at all.
#:
#: What it costs if this list is ever wrong for a fifth flavour of Linux: on a
#: machine that would have used the GPU, ``auto`` picks its model from the
#: memory ladder rather than from the quality order, and the pipeline keys the
#: lyrics stage on "cpu". The decode itself still runs on the GPU -- the worker
#: is handed ``--device auto`` and ctranslate2 makes that choice inside the
#: subprocess -- and an explicit ``--device cuda`` never consults this probe at
#: all, so the damage is a weaker model and a mislabelled key, not a failure.
#: ``test_the_cuda_shortcut_agrees_with_the_real_probe`` re-checks the inference
#: against the unshortcut answer on whatever machine the suite runs on.
_LINUX_CUDA_DEVICE_PATHS = (
    "/proc/driver/nvidia",
    "/dev/nvidiactl",
    "/dev/dxg",
    "/dev/nvhost-ctrl-gpu",
)


def _cuda_driver_is_absent() -> bool:
    """True only where this Linux kernel exposes no path to a CUDA device.

    False on every other platform, where these names mean nothing and a
    shortcut taken on them would turn a real GPU into a greyed-out choice.
    """
    if sys.platform != "linux":
        return False
    return not any(os.path.exists(path) for path in _LINUX_CUDA_DEVICE_PATHS)


def cuda_available() -> bool:
    """Whether a CUDA runtime here can see at least one device.

    Public, and named without an underscore because it is reached from outside:
    ``options_registry`` turns it into ``_Machine.cuda`` -- which decides whether
    an intake screen greys the CUDA choice and what its resolved note says -- and
    ``pipeline`` uses it to resolve the device half of a lyrics stage key. Both
    are contracts between modules, and this one used to be spelled as if it were
    not one.

    Asked, never cached: the answer is per process, and the CLI is one process per
    command, so a ``functools.cache`` would save nothing. The cheap negative
    pre-check in front of the import is what makes asking affordable.
    """
    if _cuda_driver_is_absent():
        return False
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
    if device == "cuda" or (device == "auto" and cuda_available()):
        return QUALITY_ORDER

    available = _available_memory_bytes()
    if available is None:
        return QUALITY_ORDER[1:]
    if available >= 10 * _GIB:
        return QUALITY_ORDER
    if available >= 6 * _GIB:
        return QUALITY_ORDER[1:]
    if available >= 3 * _GIB:
        return QUALITY_ORDER[2:]
    return QUALITY_ORDER[3:]


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
        raise InvalidInputError("unsupported faster-whisper model")
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


def _existing_audio(path: Path, *, label: str) -> Path:
    """Resolve a caller-supplied audio path to an absolute, existing file.

    Two things are bought here. The obvious one is a clear error before a
    subprocess is spawned. The less obvious one is that an absolute path always
    begins with "/", so it can never be mistaken for an option by the worker's
    argument parser -- the reason this resolves rather than merely checking
    ``is_file``. Nothing is ever handed to a shell, here or in ``run_command``.
    """
    try:
        resolved = Path(path).resolve(strict=True)
    except OSError as error:
        raise InvalidInputError(f"{label} audio does not exist") from error
    if not resolved.is_file():
        raise InvalidInputError(f"{label} audio is not a file")
    return resolved


def transcribe(
    source: Path,
    output: Path,
    *,
    language: str = AUTO_LANGUAGE,
    model: str = DEFAULT_MODEL,
    device: str = "auto",
    allow_model_downloads: bool = False,
    timeout: float = 60 * 60,
    audio_source: str = DEFAULT_AUDIO_SOURCE,
    mix: Path | None = None,
) -> Path:
    """Transcribe sung audio to the app's timed-lyrics document.

    ``source`` is the isolated vocal stem and ``mix`` the full normalised audio.
    ``audio_source`` selects between them; ``auto`` transcribes both in one
    worker process -- one model load, two decoding passes -- and keeps the
    better transcript by the criterion documented in ``_whisper_worker``. Its
    cost is that second pass, which is why it is not the default, and why the
    wall-clock bound is scaled by ``_AUDIO_SOURCE_PASSES`` rather than leaving
    the second pass to run into the first pass's deadline.

    ``timeout`` is the bound for a single decoding pass. Whether the worker
    performs one or two, no configuration here can extend the process beyond
    ``timeout`` times the number of passes the caller asked for.
    """
    if model not in MODEL_CHOICES:
        # A value outside a closed set, like the audio source below it: an input
        # this build will not accept, not a package this machine is missing.
        raise InvalidInputError("unsupported faster-whisper model")
    if audio_source not in AUDIO_SOURCE_CHOICES:
        raise InvalidInputError("audio source must be vocals, mix, or auto")
    requested_language = normalize_language(language)
    passes = _AUDIO_SOURCE_PASSES[audio_source]
    seconds = usable_seconds(timeout)
    if seconds is None or usable_seconds(seconds * passes) is None:
        # Checked here rather than left to ``run_command``, which raises a bare
        # ValueError: this is a caller-supplied bound, and multiplying it for a
        # two-pass run is what makes a merely large value reach infinity.
        raise InvalidInputError("transcription timeout must be a positive, finite duration")
    if not is_available():
        raise ProviderUnavailableError(
            "timed lyrics are unavailable: supply --lyrics or run `uv sync --all-extras`"
        )
    primary = _existing_audio(source, label="vocal stem")
    full_mix: Path | None = None
    if audio_source in (AUDIO_SOURCE_MIX, AUDIO_SOURCE_AUTO):
        if mix is None:
            raise InvalidInputError(
                f"audio source '{audio_source}' needs the full mix as well as the vocal stem"
            )
        full_mix = _existing_audio(mix, label="full mix")
    resolved_output = Path(output).resolve()

    model_cache = ensure_private_directory(model_cache_path())
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
        str(primary),
        str(resolved_output),
        "--model",
        selected_model,
        "--device",
        device,
        "--cache",
        str(model_cache),
        "--audio-source",
        audio_source,
    ]
    if full_mix is not None:
        arguments.extend(("--mix", str(full_mix)))
    if requested_language != AUTO_LANGUAGE:
        arguments.extend(("--language", requested_language))
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
        timeout=seconds * passes,
        env=environment,
        redact=(
            str(primary),
            str(source),
            str(full_mix or ""),
            str(mix or ""),
            str(resolved_output),
            str(output),
            str(model_cache),
        ),
    )
    output.chmod(0o600)
    return output
