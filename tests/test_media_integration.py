from __future__ import annotations

import shutil
import struct
import wave
from pathlib import Path

import pytest

from kilix_playalong.providers.media import normalize, probe


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg tools are not installed",
)
def test_ffmpeg_normalizes_a_tiny_synthetic_waveform(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    with wave.open(str(source), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(b"".join(struct.pack("<h", index % 200) for index in range(800)))

    source_metadata = probe(source)
    assert source_metadata["streams"]
    target = normalize(source, tmp_path / "normalized.wav")
    metadata = probe(target)
    audio = next(
        stream
        for stream in metadata["streams"]
        if isinstance(stream, dict) and stream.get("codec_type") == "audio"
    )
    assert audio["sample_rate"] == "44100"
    assert audio["channels"] == 2
