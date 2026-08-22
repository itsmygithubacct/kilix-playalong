"""Isolated Basic Pitch invocation for the guitar stem."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from ..errors import ProviderUnavailableError
from ..runner import run_command

_SOURCE_ROOT = str(Path(__file__).resolve().parents[2])


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
    midi_output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    run_command(
        [
            sys.executable,
            "-m",
            "kilix_playalong._basic_pitch_worker",
            str(source),
            str(midi_output),
            str(notes_output),
        ],
        timeout=timeout,
        env={"PYTHONPATH": _SOURCE_ROOT},
        redact=(str(source), str(midi_output), str(notes_output)),
    )
    midi_output.chmod(0o600)
    notes_output.chmod(0o600)
    return midi_output, notes_output
