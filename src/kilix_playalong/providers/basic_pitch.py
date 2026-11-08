"""Isolated Basic Pitch invocation for the guitar stem.

The ONNX model is the ``basic-pitch-icassp-2022-onnx`` asset that Kilix
installs under its licence record, verified before every run. The copy inside
the basic-pitch wheel is the same bytes and is never loaded.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from .. import licensed
from ..errors import ProviderUnavailableError
from ..runner import run_command

_SOURCE_ROOT = str(Path(__file__).resolve().parents[2])
MODEL_FILE = "nmp.onnx"


def is_available() -> bool:
    return importlib.util.find_spec("basic_pitch") is not None


def transcribe(
    source: Path,
    midi_output: Path,
    notes_output: Path,
    *,
    timeout: float = 45 * 60,
) -> tuple[Path, Path]:
    if not is_available():
        raise ProviderUnavailableError(
            "Basic Pitch is not installed; run `uv sync --all-extras` from the repository"
        )
    model = licensed.verify(licensed.BASIC_PITCH) / MODEL_FILE
    midi_output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    run_command(
        [
            sys.executable,
            "-m",
            "kilix_playalong._basic_pitch_worker",
            str(source),
            str(midi_output),
            str(notes_output),
            str(model),
        ],
        timeout=timeout,
        env={"PYTHONPATH": _SOURCE_ROOT},
        redact=(str(source), str(midi_output), str(notes_output), str(model)),
    )
    midi_output.chmod(0o600)
    notes_output.chmod(0o600)
    return midi_output, notes_output
