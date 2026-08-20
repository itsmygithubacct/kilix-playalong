from __future__ import annotations

import importlib.util
import json
import math
import struct
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from kilix_playalong.midi import validate_midi


@pytest.mark.ml
@pytest.mark.skipif(
    importlib.util.find_spec("basic_pitch") is None,
    reason="ML extras not installed",
)
def test_basic_pitch_onnx_transcribes_a_synthetic_tone(tmp_path: Path) -> None:
    source = tmp_path / "tone.wav"
    sample_rate = 22_050
    with wave.open(str(source), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        frames = (
            struct.pack(
                "<h",
                int(2_000 * math.sin(2 * math.pi * 329.63 * index / sample_rate)),
            )
            for index in range(2 * sample_rate)
        )
        output.writeframes(b"".join(frames))

    midi = tmp_path / "tone.mid"
    notes = tmp_path / "notes.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "kilix_playalong._basic_pitch_worker",
            str(source),
            str(midi),
            str(notes),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert validate_midi(midi) >= 1
    document = json.loads(notes.read_text())
    assert document["provider"] == "basic-pitch-onnx-0.4.0"
    assert document["notes"]
