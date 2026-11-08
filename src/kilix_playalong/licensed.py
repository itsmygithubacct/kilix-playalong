"""Model weights that reach this app only through Kilix's licence records.

Playalong never downloads a model itself. Each model it runs is a Kilix
Content asset with a kilix-license record. ``kilix models install <asset>``
shows the record and the licence text, takes the user's answer, writes the
receipt, and fetches the pinned files into Content's store. This module only
reads that store.

Two questions are asked of it, at two prices:

* ``installed`` is a file check under the root that ``kilix models list``
  reports. It costs one short ``kilix`` call per process and hashes nothing,
  so an intake screen or a stage key can ask it freely. It is a hint.
* ``verify`` runs ``kilix models verify <asset>``, which checks the stored
  receipt against the exact catalogued asset and re-hashes every installed
  file. A provider calls it once, immediately before its worker starts, and
  refuses to run on any answer but a verified one. The worker is then handed
  the directory that check returned, never a name it could resolve on its own.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import ProviderUnavailableError


@dataclass(frozen=True)
class LicensedModel:
    """One catalogued asset and the file inside it that proves it is present."""

    asset_id: str
    label: str
    #: Relative to the asset directory. Its presence is the cheap check.
    marker: str
    #: The directory a provider hands its worker, relative to the asset directory.
    payload: str = "."


DEMUCS = LicensedModel(
    asset_id="demucs-htdemucs-6s",
    label="Demucs htdemucs_6s separator",
    marker="models/5c90dfd2-34c22ccb.th",
    payload="models",
)
#: Demucs loads that one file from ``--repo`` by its signature. htdemucs_6s's
#: own yaml names exactly this model with default weights.
DEMUCS_SIGNATURE = "5c90dfd2"

BASIC_PITCH = LicensedModel(
    asset_id="basic-pitch-icassp-2022-onnx",
    label="Basic Pitch guitar-note model",
    marker="models/nmp.onnx",
    payload="models",
)

WHISPER_MEDIUM_EN = LicensedModel(
    asset_id="faster-whisper-medium-en",
    label="Whisper medium.en (English lyrics)",
    marker="model.bin",
)
WHISPER_LARGE_V3 = LicensedModel(
    asset_id="faster-whisper-large-v3",
    label="Whisper large-v3 (lyrics in any language)",
    marker="model.bin",
)

#: Whisper model name -> its asset. Only these names can be run.
WHISPER = {"large-v3": WHISPER_LARGE_V3, "medium.en": WHISPER_MEDIUM_EN}

#: What ``kilix-playalong models install`` installs without being asked for
#: more. large-v3 is offered separately, for songs that are not in English.
DEFAULT_SET = (DEMUCS, BASIC_PITCH, WHISPER_MEDIUM_EN)
ALL = (DEMUCS, BASIC_PITCH, WHISPER_MEDIUM_EN, WHISPER_LARGE_V3)

#: Re-hashing a large Whisper model.bin takes seconds, not minutes; this bounds
#: a stuck call, not a slow one.
VERIFY_TIMEOUT = 300.0
LIST_TIMEOUT = 60.0

_root_answer: tuple[Path | None, str | None] | None = None


def kilix_program() -> str | None:
    """The kilix launcher this app runs under, or None when there is none."""
    home = os.environ.get("KILIX_HOME")
    if home:
        candidate = Path(home) / "kilix"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which("kilix")


def install_hint(model: LicensedModel) -> str:
    return f"kilix models install {model.asset_id}"


def _missing(model: LicensedModel, detail: str | None = None) -> ProviderUnavailableError:
    message = (
        f"the {model.label} is not installed with a licence receipt; "
        f"run: kilix-playalong models install (or {install_hint(model)})"
    )
    if detail:
        message = f"{message} ({detail})"
    return ProviderUnavailableError(message)


def _kilix_json(arguments: list[str], *, timeout: float) -> tuple[int, object]:
    program = kilix_program()
    if program is None:
        raise FileNotFoundError("kilix")
    result = subprocess.run(
        [program, "models", *arguments],
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=timeout,
    )
    try:
        report = json.loads(result.stdout) if result.stdout.strip() else None
    except ValueError:
        report = None
    return result.returncode, report


def content_root() -> tuple[Path | None, str | None]:
    """Content's installer root as Kilix reports it, or (None, why not).

    Asked once per process. ``kilix models list`` loads the verified catalog
    and changes nothing.
    """
    global _root_answer
    if _root_answer is not None:
        return _root_answer
    try:
        status, report = _kilix_json(["list"], timeout=LIST_TIMEOUT)
    except FileNotFoundError:
        _root_answer = (None, "kilix is not installed here")
        return _root_answer
    except (OSError, subprocess.SubprocessError) as error:
        _root_answer = (None, f"kilix models list failed: {type(error).__name__}")
        return _root_answer
    root = report.get("root") if isinstance(report, dict) else None
    if status != 0 or not isinstance(root, str) or not os.path.isabs(root):
        _root_answer = (None, "kilix models list did not report a content root")
    else:
        _root_answer = (Path(root), None)
    return _root_answer


def forget_root() -> None:
    """Drop the cached root. For tests, and for a process that installs."""
    global _root_answer
    _root_answer = None


def asset_directory(root: Path, model: LicensedModel) -> Path:
    return root / "assets" / model.asset_id


def installed(model: LicensedModel) -> bool:
    """True when the model's marker file is present. A hint, not a verification."""
    root, _reason = content_root()
    if root is None:
        return False
    return (asset_directory(root, model) / model.marker).is_file()


def unavailable_reason(model: LicensedModel) -> str | None:
    """None when ``installed`` says yes, else the sentence a screen shows."""
    root, reason = content_root()
    if root is None:
        return f"the {model.label} comes through Kilix, and {reason}"
    if not (asset_directory(root, model) / model.marker).is_file():
        return f"the {model.label} is not installed; run: kilix-playalong models install"
    return None


def verify(model: LicensedModel) -> Path:
    """Verify the receipt and bytes, and return the directory the worker loads.

    Raises ``ProviderUnavailableError`` naming the install command on any
    answer but a verified one: no kilix, a refusal, a timeout, or a report
    that does not name this asset and an absolute root.
    """
    try:
        status, report = _kilix_json(["verify", model.asset_id], timeout=VERIFY_TIMEOUT)
    except FileNotFoundError:
        raise _missing(model, "kilix is not installed here") from None
    except subprocess.TimeoutExpired:
        raise _missing(model, "verification timed out") from None
    except (OSError, subprocess.SubprocessError) as error:
        raise _missing(model, type(error).__name__) from None
    if status != 0 or not isinstance(report, dict) or report.get("verified") != model.asset_id:
        raise _missing(model)
    root = report.get("root")
    if not isinstance(root, str) or not os.path.isabs(root):
        raise _missing(model)
    directory = asset_directory(Path(root), model)
    if not (directory / model.marker).is_file():
        raise _missing(model)
    return (directory / model.payload).resolve()


def install(model: LicensedModel) -> int:
    """Run ``kilix models install`` in this terminal. Returns its exit status.

    Kilix shows the record and the licence text and reads the user's answer
    from this process's own stdin; nothing here answers for them.
    """
    program = kilix_program()
    if program is None:
        raise ProviderUnavailableError("kilix is not installed here; it installs licensed models")
    try:
        status = subprocess.call([program, "models", "install", model.asset_id])
    finally:
        forget_root()
    return status
