from __future__ import annotations

import json
from pathlib import Path

import mido
import pytest

from kilix_playalong.errors import InvalidInputError, RightsConfirmationRequired
from kilix_playalong.pipeline import PipelineOptions, create_project, resume, run_new
from kilix_playalong.state import load_manifest
from kilix_playalong.tablature import STANDARD_TUNING
from kilix_playalong.util import private_write


def _write_midi(path: Path) -> None:
    midi = mido.MidiFile()
    track = mido.MidiTrack()
    track.append(mido.Message("note_on", note=64, velocity=90, time=0))
    track.append(mido.Message("note_off", note=64, velocity=0, time=480))
    midi.tracks.append(track)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    midi.save(path)


def test_python_pipeline_api_also_requires_rights(private_homes: Path) -> None:
    with pytest.raises(RightsConfirmationRequired):
        create_project(PipelineOptions(url="https://youtu.be/abcdef12345"))


@pytest.mark.parametrize(
    "options",
    [
        PipelineOptions(
            url="https://youtu.be/abcdef12345", max_duration=float("inf"), rights_confirmed=True
        ),
        PipelineOptions(url="https://youtu.be/abcdef12345", max_fret=99, rights_confirmed=True),
        PipelineOptions(
            url="https://youtu.be/abcdef12345",
            tuning=(40, 40, 50, 55, 59, 64),
            rights_confirmed=True,
        ),
        PipelineOptions(
            url="https://youtu.be/abcdef12345",
            language="../../captions",
            rights_confirmed=True,
        ),
    ],
)
def test_pipeline_options_are_bounded(
    options: PipelineOptions,
    private_homes: Path,
) -> None:
    with pytest.raises(InvalidInputError):
        create_project(options)


def test_synthetic_pipeline_resumes_and_invalidates_by_settings(
    tmp_path: Path,
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {name: 0 for name in ("download", "normalize", "separate", "pitch")}

    def fake_download(
        _url: str,
        destination: Path,
        **_kwargs: object,
    ) -> tuple[Path, list[Path], dict[str, object]]:
        calls["download"] += 1
        source = destination / "source.webm"
        private_write(source, b"original synthetic audio")
        return source, [], {"id": "abcdef12345", "duration": 6.0, "title": "Synthetic Song"}

    def fake_probe(_path: Path, **_kwargs: object) -> dict[str, object]:
        return {"streams": [{"codec_type": "audio"}]}

    def fake_normalize(_source: Path, output: Path, **_kwargs: object) -> Path:
        calls["normalize"] += 1
        private_write(output, b"normalized synthetic audio")
        return output

    def fake_separate(
        _source: Path,
        destination: Path,
        **_kwargs: object,
    ) -> dict[str, Path]:
        calls["separate"] += 1
        result = {}
        for stem in ("vocals", "drums", "bass", "guitar", "piano", "other"):
            target = destination / f"{stem}.wav"
            private_write(target, f"synthetic {stem}".encode())
            result[stem] = target
        return result

    def fake_pitch(
        _source: Path,
        midi_output: Path,
        notes_output: Path,
        **_kwargs: object,
    ) -> tuple[Path, Path]:
        calls["pitch"] += 1
        _write_midi(midi_output)
        private_write(
            notes_output,
            json.dumps(
                {
                    "provider": "synthetic",
                    "notes": [
                        {"start": 0, "end": 1, "pitch": 64, "confidence": 0.95},
                        {"start": 1, "end": 2, "pitch": 67, "confidence": 0.9},
                    ],
                }
            ).encode(),
        )
        return midi_output, notes_output

    monkeypatch.setattr("kilix_playalong.pipeline.youtube.download", fake_download)
    monkeypatch.setattr("kilix_playalong.pipeline.media.probe", fake_probe)
    monkeypatch.setattr("kilix_playalong.pipeline.media.normalize", fake_normalize)
    monkeypatch.setattr("kilix_playalong.pipeline.separation.separate", fake_separate)
    monkeypatch.setattr("kilix_playalong.pipeline.basic_pitch.transcribe", fake_pitch)

    lyrics = tmp_path / "lyrics.lrc"
    lyrics.write_text("[00:00.00]Original line\n[00:02.00]Second line\n")
    options = PipelineOptions(
        url="https://youtu.be/abcdef12345",
        lyrics_path=lyrics,
        allow_model_downloads=False,
        rights_confirmed=True,
    )
    project_dir, manifest = run_new(options)
    assert all(stage["status"] == "done" for stage in manifest["stages"].values())
    assert manifest["title"] == "Synthetic Song"
    assert len(manifest["tracks"]) == 6
    assert (project_dir / "exports" / "playalong.html").is_file()
    assert load_manifest(project_dir)["id"] == manifest["id"]
    assert calls == {"download": 1, "normalize": 1, "separate": 1, "pitch": 1}

    statuses: list[tuple[str, str]] = []
    resume(project_dir, options, progress=lambda name, status: statuses.append((name, status)))
    assert all(status == "cached" for _name, status in statuses)
    assert calls == {"download": 1, "normalize": 1, "separate": 1, "pitch": 1}

    drop_d = PipelineOptions(
        url=options.url,
        tuning=(38, *STANDARD_TUNING[1:]),
        lyrics_path=None,
        rights_confirmed=True,
    )
    statuses.clear()
    updated = resume(
        project_dir, drop_d, progress=lambda name, status: statuses.append((name, status))
    )
    assert ("tablature", "running") in statuses
    assert ("export", "running") in statuses
    assert ("transcribe-guitar", "cached") in statuses
    assert updated["tablature"] is not None
    assert updated["tablature"]["tuning"] == [38, 45, 50, 55, 59, 64]

    guitar = project_dir / "stems" / "guitar.wav"
    guitar.write_bytes(b"tampered")
    statuses.clear()
    resume(project_dir, drop_d, progress=lambda name, status: statuses.append((name, status)))
    assert ("separate", "running") in statuses
    assert calls["separate"] == 2
    assert calls["pitch"] == 2
