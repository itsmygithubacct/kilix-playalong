from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path

import mido
import pytest

from kilix_playalong import LYRICS_SCHEMA
from kilix_playalong.errors import (
    InvalidInputError,
    ProviderFailedError,
    ProviderUnavailableError,
    RightsConfirmationRequired,
)
from kilix_playalong.pipeline import (
    _WHISPER_DEVICE_ORDER,
    _WHISPER_QUALITY_ORDER,
    Pipeline,
    PipelineOptions,
    _whisper_model_cache,
    create_project,
    resume,
    run_new,
)
from kilix_playalong.providers import transcription
from kilix_playalong.runner import CommandResult
from kilix_playalong.state import load_manifest
from kilix_playalong.tablature import STANDARD_TUNING
from kilix_playalong.util import private_write

_GIB = 1024**3


def _write_midi(path: Path) -> None:
    midi = mido.MidiFile()
    track = mido.MidiTrack()
    track.append(mido.Message("note_on", note=64, velocity=90, time=0))
    track.append(mido.Message("note_off", note=64, velocity=0, time=480))
    midi.tracks.append(track)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    midi.save(path)


def _install_providers(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, int], list[str]]:
    """Replace every heavy provider with a deterministic synthetic stand-in."""
    calls = {name: 0 for name in ("download", "normalize", "separate", "pitch")}
    whisper_models: list[str] = []

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
    # Drive the real faster-whisper provider, replacing only its worker subprocess, so
    # the pipeline's model resolution and cache location stay under test.
    monkeypatch.setattr("kilix_playalong.providers.transcription.is_available", lambda: True)
    monkeypatch.setattr(
        "kilix_playalong.providers.transcription.run_command",
        _synthetic_whisper_worker(whisper_models),
    )
    return calls, whisper_models


def _synthetic_whisper_worker(whisper_models: list[str]) -> Callable[..., CommandResult]:
    """Stand in for the whisper worker subprocess, recording the model it was handed."""

    def worker(arguments: Sequence[str], **_kwargs: object) -> CommandResult:
        model = arguments[arguments.index("--model") + 1]
        whisper_models.append(model)
        private_write(
            Path(arguments[arguments.index("--model") - 1]),
            json.dumps(
                {
                    "schema": LYRICS_SCHEMA,
                    "source": f"faster-whisper:{model}",
                    "language": "en",
                    "cues": [{"start": 0.0, "end": 2.0, "text": "Synthetic line", "words": []}],
                }
            ).encode(),
        )
        return CommandResult(stdout="", stderr="")

    return worker


_WHISPER_REPOSITORIES = {
    "large-v3": "Systran--faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh--faster-whisper-large-v3-turbo",
    "medium": "Systran--faster-whisper-medium",
    "small": "Systran--faster-whisper-small",
}


def _whisper_model_directory(model: str) -> Path:
    return _whisper_model_cache() / f"models--{_WHISPER_REPOSITORIES[model]}"


def _cache_whisper_model(model: str) -> None:
    """Make one faster-whisper model look locally cached to the adaptive resolver."""
    snapshot = _whisper_model_directory(model) / "snapshots" / "fixture-revision"
    private_write(snapshot / "model.bin", b"synthetic weights")


def _uncache_whisper_model(model: str) -> None:
    """Prune one model's weights, as a cache clean or a fresh container image would."""
    shutil.rmtree(_whisper_model_directory(model))


def _pin_hardware(
    monkeypatch: pytest.MonkeyPatch,
    *,
    available_memory: int,
    cuda: bool,
) -> None:
    """Pin the machine probes the adaptive resolver reads so resumes are decidable."""
    monkeypatch.setattr(
        "kilix_playalong.providers.transcription._available_memory_bytes",
        lambda: available_memory,
    )
    monkeypatch.setattr(
        "kilix_playalong.providers.transcription._cuda_available",
        lambda: cuda,
    )


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
    calls, _whisper_models = _install_providers(monkeypatch)

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


def test_lyrics_stage_reruns_when_model_downloads_are_allowed(
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, whisper_models = _install_providers(monkeypatch)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    _cache_whisper_model("small")
    options = PipelineOptions(url="https://youtu.be/abcdef12345", rights_confirmed=True)

    project_dir, manifest = run_new(options)
    assert whisper_models == ["small"]
    assert manifest["lyrics"] is not None
    assert manifest["lyrics"]["source"] == "faster-whisper:small"

    statuses: list[tuple[str, str]] = []
    upgraded = resume(
        project_dir,
        PipelineOptions(
            url=options.url,
            allow_model_downloads=True,
            rights_confirmed=True,
        ),
        progress=lambda name, status: statuses.append((name, status)),
    )
    assert ("lyrics", "running") in statuses
    assert whisper_models == ["small", "large-v3"]
    assert upgraded["lyrics"] is not None
    assert upgraded["lyrics"]["source"] == "faster-whisper:large-v3"
    assert calls["separate"] == 1

    statuses.clear()
    resume(
        project_dir,
        PipelineOptions(
            url=options.url,
            allow_model_downloads=True,
            rights_confirmed=True,
        ),
        progress=lambda name, status: statuses.append((name, status)),
    )
    assert all(status == "cached" for _name, status in statuses)
    assert whisper_models == ["small", "large-v3"]


def test_lyrics_stage_survives_a_cleared_model_cache(
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, whisper_models = _install_providers(monkeypatch)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    _cache_whisper_model("small")
    options = PipelineOptions(url="https://youtu.be/abcdef12345", rights_confirmed=True)

    project_dir, _manifest = run_new(options)
    assert whisper_models == ["small"]

    shutil.rmtree(_whisper_model_cache())
    statuses: list[tuple[str, str]] = []
    unchanged = resume(
        project_dir,
        options,
        progress=lambda name, status: statuses.append((name, status)),
    )
    assert all(status == "cached" for _name, status in statuses)
    assert whisper_models == ["small"]
    assert unchanged["lyrics"] is not None
    assert unchanged["lyrics"]["source"] == "faster-whisper:small"
    assert calls["pitch"] == 1


def test_lyrics_stage_reruns_when_an_adaptive_resume_finds_a_gpu(
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, whisper_models = _install_providers(monkeypatch)
    _pin_hardware(monkeypatch, available_memory=4 * _GIB, cuda=False)
    _cache_whisper_model("medium")
    _cache_whisper_model("large-v3")
    options = PipelineOptions(url="https://youtu.be/abcdef12345", rights_confirmed=True)

    project_dir, manifest = run_new(options)
    assert whisper_models == ["medium"]
    assert manifest["lyrics"] is not None
    assert manifest["lyrics"]["source"] == "faster-whisper:medium"

    _pin_hardware(monkeypatch, available_memory=4 * _GIB, cuda=True)
    statuses: list[tuple[str, str]] = []
    upgraded = resume(
        project_dir,
        options,
        progress=lambda name, status: statuses.append((name, status)),
    )
    assert ("lyrics", "running") in statuses
    assert whisper_models == ["medium", "large-v3"]
    assert upgraded["lyrics"] is not None
    assert upgraded["lyrics"]["source"] == "faster-whisper:large-v3"
    assert calls["separate"] == 1


def test_lyrics_stage_keeps_a_stronger_recorded_whisper_model(
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, whisper_models = _install_providers(monkeypatch)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    _cache_whisper_model("medium")
    _cache_whisper_model("large-v3")
    options = PipelineOptions(url="https://youtu.be/abcdef12345", rights_confirmed=True)

    project_dir, _manifest = run_new(options)
    assert whisper_models == ["large-v3"]

    _pin_hardware(monkeypatch, available_memory=4 * _GIB, cuda=False)
    statuses: list[tuple[str, str]] = []
    unchanged = resume(
        project_dir,
        options,
        progress=lambda name, status: statuses.append((name, status)),
    )
    assert ("lyrics", "cached") in statuses
    assert whisper_models == ["large-v3"]
    assert unchanged["lyrics"] is not None
    assert unchanged["lyrics"]["source"] == "faster-whisper:large-v3"
    assert calls["pitch"] == 1


def test_a_lyrics_rerun_obeys_the_providers_memory_policy(
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recorded model may outrank this machine in the key, but never in the run."""
    calls, whisper_models = _install_providers(monkeypatch)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    _cache_whisper_model("medium")
    _cache_whisper_model("large-v3")
    options = PipelineOptions(url="https://youtu.be/abcdef12345", rights_confirmed=True)

    project_dir, _manifest = run_new(options)
    assert whisper_models == ["large-v3"]

    # The box shrinks below the provider's large-v3 threshold and the artifact is gone, so
    # the stage has to re-run: it must re-run at the model this machine can actually hold.
    _pin_hardware(monkeypatch, available_memory=4 * _GIB, cuda=False)
    (project_dir / "lyrics" / "lyrics.json").unlink()
    statuses: list[tuple[str, str]] = []
    downgraded = resume(
        project_dir,
        options,
        progress=lambda name, status: statuses.append((name, status)),
    )
    assert ("lyrics", "running") in statuses
    assert whisper_models == ["large-v3", "medium"]
    assert downgraded["lyrics"] is not None
    assert downgraded["lyrics"]["source"] == "faster-whisper:medium"
    assert calls["pitch"] == 2

    # ...and the stage settles on what it ran: one re-run, not a second one afterwards.
    statuses.clear()
    resume(
        project_dir,
        options,
        progress=lambda name, status: statuses.append((name, status)),
    )
    assert all(status == "cached" for _name, status in statuses)
    assert whisper_models == ["large-v3", "medium"]
    assert calls["pitch"] == 2


def test_an_offline_resume_transcribes_with_weights_that_are_present(
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pruned weights must not be pinned into a resume that has no way to fetch them."""
    calls, whisper_models = _install_providers(monkeypatch)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    cached_worker = _synthetic_whisper_worker(whisper_models)

    def offline_worker(arguments: Sequence[str], **kwargs: object) -> CommandResult:
        """Stand in for HF_HUB_OFFLINE: uncached weights cannot be fetched."""
        environment = kwargs.get("env")
        offline = isinstance(environment, dict) and environment.get("HF_HUB_OFFLINE") == "1"
        model = arguments[arguments.index("--model") + 1]
        if offline and not transcription._is_cached(_whisper_model_cache(), model):
            raise ProviderFailedError("faster-whisper worker could not fetch model weights")
        return cached_worker(arguments, **kwargs)

    monkeypatch.setattr("kilix_playalong.providers.transcription.run_command", offline_worker)

    project_dir, first = run_new(
        PipelineOptions(
            url="https://youtu.be/abcdef12345",
            allow_model_downloads=True,
            rights_confirmed=True,
        )
    )
    assert whisper_models == ["large-v3"]
    assert first["lyrics"] is not None
    assert first["lyrics"]["source"] == "faster-whisper:large-v3"

    # The big weights are pruned, only `small` survives, and the resume is offline.
    _cache_whisper_model("small")
    (project_dir / "lyrics" / "lyrics.json").unlink()
    statuses: list[tuple[str, str]] = []
    resumed = resume(
        project_dir,
        PipelineOptions(url="https://youtu.be/abcdef12345", rights_confirmed=True),
        progress=lambda name, status: statuses.append((name, status)),
    )
    assert ("lyrics", "done") in statuses
    assert whisper_models == ["large-v3", "small"]
    assert resumed["lyrics"] is not None
    assert resumed["lyrics"]["source"] == "faster-whisper:small"
    assert all(stage["status"] == "done" for stage in resumed["stages"].values())
    assert calls["pitch"] == 2


def test_an_unresolvable_rerun_fails_before_launching_a_worker(
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When nothing is obtainable the stage reports the provider's advice, it does not guess.

    The recorded model still keys the stage here -- that is what keeps an intact transcript
    cached through an emptied cache -- so this is the case where the key is furthest from
    what the machine can run, and the run must not inherit it.
    """
    _calls, whisper_models = _install_providers(monkeypatch)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    _cache_whisper_model("small")
    options = PipelineOptions(url="https://youtu.be/abcdef12345", rights_confirmed=True)

    project_dir, _manifest = run_new(options)
    assert whisper_models == ["small"]

    shutil.rmtree(_whisper_model_cache())
    (project_dir / "lyrics" / "lyrics.json").unlink()
    with pytest.raises(ProviderUnavailableError, match="--allow-model-downloads"):
        resume(project_dir, options)

    assert whisper_models == ["small"]
    assert load_manifest(project_dir)["stages"]["lyrics"]["status"] == "error"


def test_an_explicit_whisper_model_is_the_model_that_runs(
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--whisper-model` wins outright over an adaptive key, in both directions."""
    calls, whisper_models = _install_providers(monkeypatch)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    _cache_whisper_model("medium")
    _cache_whisper_model("large-v3")
    adaptive = PipelineOptions(url="https://youtu.be/abcdef12345", rights_confirmed=True)

    project_dir, _manifest = run_new(adaptive)
    assert whisper_models == ["large-v3"]

    explicit = PipelineOptions(
        url=adaptive.url,
        whisper_model="medium",
        rights_confirmed=True,
    )
    statuses: list[tuple[str, str]] = []
    downgraded = resume(
        project_dir, explicit, progress=lambda name, status: statuses.append((name, status))
    )
    assert ("lyrics", "running") in statuses
    assert whisper_models == ["large-v3", "medium"]
    assert downgraded["lyrics"] is not None
    assert downgraded["lyrics"]["source"] == "faster-whisper:medium"

    statuses.clear()
    resume(project_dir, explicit, progress=lambda name, status: statuses.append((name, status)))
    assert all(status == "cached" for _name, status in statuses)
    assert whisper_models == ["large-v3", "medium"]

    # Handing the same machine back to `auto` re-transcribes only if `auto` wants something
    # better, so naming the model `auto` would have picked anyway costs nothing.
    _pin_hardware(monkeypatch, available_memory=4 * _GIB, cuda=False)
    statuses.clear()
    resume(project_dir, adaptive, progress=lambda name, status: statuses.append((name, status)))
    assert all(status == "cached" for _name, status in statuses)
    assert whisper_models == ["large-v3", "medium"]
    assert calls["pitch"] == 2


def test_a_missing_transcription_extra_keeps_its_actionable_error(
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keying must not pre-empt the provider's own diagnostic for a missing extra."""
    _calls, whisper_models = _install_providers(monkeypatch)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    monkeypatch.setattr("kilix_playalong.providers.transcription.is_available", lambda: False)
    options = PipelineOptions(url="https://youtu.be/abcdef12345", rights_confirmed=True)

    project_dir, manifest = create_project(options)
    with pytest.raises(ProviderUnavailableError, match="uv sync --all-extras"):
        Pipeline(project_dir, manifest, options).run()

    recorded = manifest["stages"]["lyrics"]["error"]
    assert isinstance(recorded, str)
    assert "uv sync --all-extras" in recorded
    assert whisper_models == []


def test_lyrics_stage_reruns_when_only_the_resolved_device_changes(
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`device='auto'` resolving differently is a key change even when the model does not."""
    calls, whisper_models = _install_providers(monkeypatch)
    _pin_hardware(monkeypatch, available_memory=12 * _GIB, cuda=False)
    _cache_whisper_model("large-v3")
    options = PipelineOptions(url="https://youtu.be/abcdef12345", rights_confirmed=True)

    project_dir, _manifest = run_new(options)
    assert whisper_models == ["large-v3"]

    # A GPU appears. Twelve GiB already cleared the provider's large-v3 threshold, so the
    # model is unchanged -- but the worker's backend and compute type are not.
    _pin_hardware(monkeypatch, available_memory=12 * _GIB, cuda=True)
    statuses: list[tuple[str, str]] = []
    resume(project_dir, options, progress=lambda name, status: statuses.append((name, status)))
    assert ("lyrics", "running") in statuses
    assert whisper_models == ["large-v3", "large-v3"]

    # Losing the GPU again is the machine shrinking, not the user changing their mind.
    _pin_hardware(monkeypatch, available_memory=12 * _GIB, cuda=False)
    statuses.clear()
    resume(project_dir, options, progress=lambda name, status: statuses.append((name, status)))
    assert all(status == "cached" for _name, status in statuses)
    assert whisper_models == ["large-v3", "large-v3"]
    assert calls["separate"] == 1


def test_a_better_recorded_model_survives_a_gpu_arriving_with_the_weights_pruned(
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Growing on one dimension while shrinking on another must not lose the transcript.

    A componentwise rule -- each dimension no worse on its own -- rejects a recorded
    (large-v3, cpu) run on a machine that gained a GPU while the large-v3 weights were
    pruned, because `auto` now resolves to (medium, cuda) and neither dimension of the
    recording matches. That would replace a finished better transcript with a worse one and
    re-run every stage after it.
    """
    calls, whisper_models = _install_providers(monkeypatch)
    _pin_hardware(monkeypatch, available_memory=16 * _GIB, cuda=False)
    _cache_whisper_model("medium")
    _cache_whisper_model("large-v3")
    options = PipelineOptions(url="https://youtu.be/abcdef12345", rights_confirmed=True)

    project_dir, _manifest = run_new(options)
    assert whisper_models == ["large-v3"]

    # The top weights are pruned and a GPU appears: the machine both grew and shrank, and
    # `auto` would now pick medium on cuda -- worse lyrics than the ones already on disk.
    _uncache_whisper_model("large-v3")
    _pin_hardware(monkeypatch, available_memory=16 * _GIB, cuda=True)
    statuses: list[tuple[str, str]] = []
    kept = resume(
        project_dir,
        options,
        progress=lambda name, status: statuses.append((name, status)),
    )
    assert all(status == "cached" for _name, status in statuses)
    assert whisper_models == ["large-v3"]
    assert kept["lyrics"] is not None
    assert kept["lyrics"]["source"] == "faster-whisper:large-v3"
    assert calls["pitch"] == 1

    # Keeping it did not blunt the device rule: once the weights are back, the same model on
    # the better device is a genuine upgrade and does re-run -- exactly once.
    _cache_whisper_model("large-v3")
    statuses.clear()
    upgraded = resume(
        project_dir,
        options,
        progress=lambda name, status: statuses.append((name, status)),
    )
    assert ("lyrics", "running") in statuses
    assert whisper_models == ["large-v3", "large-v3"]
    assert upgraded["lyrics"] is not None
    assert upgraded["lyrics"]["source"] == "faster-whisper:large-v3"

    statuses.clear()
    resume(project_dir, options, progress=lambda name, status: statuses.append((name, status)))
    assert all(status == "cached" for _name, status in statuses)
    assert whisper_models == ["large-v3", "large-v3"]
    assert calls["separate"] == 1


def test_the_lyrics_key_ranks_a_better_model_above_a_better_device(
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance is lexicographic over (model, device), model-major -- not componentwise."""
    _install_providers(monkeypatch)
    _pin_hardware(monkeypatch, available_memory=4 * _GIB, cuda=True)
    _cache_whisper_model("medium")
    adaptive = PipelineOptions(url="https://youtu.be/abcdef12345", rights_confirmed=True)
    project_dir, manifest = create_project(adaptive)

    key, alternates = Pipeline(project_dir, manifest, adaptive)._whisper_keys()
    assert key == {"model": "medium", "device": "cuda"}
    # A strictly better model is kept from either device; an equal model on the worse device
    # is not, so a GPU appearing still re-runs when it is the only thing that changed.
    assert {(value["model"], value["device"]) for value in alternates} == {
        ("large-v3", "cuda"),
        ("large-v3", "cpu"),
        ("large-v3-turbo", "cuda"),
        ("large-v3-turbo", "cpu"),
    }

    # A pinned device still means exactly itself in both directions: the cross-dimension
    # rule only ever relaxes a dimension the caller left on `auto`.
    pinned = PipelineOptions(url=adaptive.url, device="cuda", rights_confirmed=True)
    _key, pinned_alternates = Pipeline(project_dir, manifest, pinned)._whisper_keys()
    assert {(value["model"], value["device"]) for value in pinned_alternates} == {
        ("large-v3", "cuda"),
        ("large-v3-turbo", "cuda"),
    }


def test_an_unranked_explicit_whisper_model_stands_in_for_nothing(
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model outside the provider's adaptive order is comparable only with itself.

    `--whisper-model base` is a supported model the adaptive quality order never mentions.
    Nothing may stand in for it -- not even `large-v3`, which this box could run and which
    tops that order -- because the two are simply not ranked against each other.
    """
    _install_providers(monkeypatch)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    _cache_whisper_model("large-v3")
    options = PipelineOptions(
        url="https://youtu.be/abcdef12345",
        whisper_model="base",
        rights_confirmed=True,
    )
    project_dir, manifest = create_project(options)

    key, alternates = Pipeline(project_dir, manifest, options)._whisper_keys()
    assert key == {"model": "base", "device": "cpu"}
    assert {(value["model"], value["device"]) for value in alternates} == {("base", "cuda")}
    assert all(value["model"] == "base" for value in alternates)


def test_the_lyrics_key_accepts_only_configurations_that_are_no_worse(
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the acceptance rule itself: `auto` stands in for better, a pin for nothing.

    The stage-level tests can only observe the `auto` half of this, because any change to
    `--device` also re-keys the separation stage and cascades into lyrics regardless of the
    Whisper key, so the pinned half is asserted here directly.
    """
    _install_providers(monkeypatch)
    _pin_hardware(monkeypatch, available_memory=4 * _GIB, cuda=False)
    _cache_whisper_model("medium")
    adaptive = PipelineOptions(url="https://youtu.be/abcdef12345", rights_confirmed=True)
    project_dir, manifest = create_project(adaptive)

    key, alternates = Pipeline(project_dir, manifest, adaptive)._whisper_keys()
    assert key == {"model": "medium", "device": "cpu"}
    assert {(value["model"], value["device"]) for value in alternates} == {
        ("large-v3", "cpu"),
        ("large-v3", "cuda"),
        ("large-v3-turbo", "cpu"),
        ("large-v3-turbo", "cuda"),
        ("medium", "cuda"),
    }

    pinned = PipelineOptions(
        url=adaptive.url,
        whisper_model="medium",
        device="cpu",
        rights_confirmed=True,
    )
    assert Pipeline(project_dir, manifest, pinned)._whisper_keys() == (
        {"model": "medium", "device": "cpu"},
        (),
    )


def test_supplied_lyrics_outlive_whisper_option_changes(
    tmp_path: Path,
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, whisper_models = _install_providers(monkeypatch)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    _cache_whisper_model("small")
    lyrics = tmp_path / "lyrics.lrc"
    lyrics.write_text("[00:00.00]Original line\n[00:02.00]Second line\n")
    options = PipelineOptions(
        url="https://youtu.be/abcdef12345",
        lyrics_path=lyrics,
        rights_confirmed=True,
    )

    project_dir, manifest = run_new(options)
    assert manifest["lyrics"] is not None
    assert manifest["lyrics"]["source"] == "imported-lrc"
    assert whisper_models == []

    statuses: list[tuple[str, str]] = []
    unchanged = resume(
        project_dir,
        PipelineOptions(
            url=options.url,
            lyrics_path=None,
            whisper_model="large-v3",
            allow_model_downloads=True,
            rights_confirmed=True,
        ),
        progress=lambda name, status: statuses.append((name, status)),
    )
    assert ("lyrics", "cached") in statuses
    assert whisper_models == []
    assert unchanged["lyrics"] is not None
    assert unchanged["lyrics"]["source"] == "imported-lrc"
    assert calls["pitch"] == 1


def test_whisper_model_cache_matches_the_transcription_provider(
    tmp_path: Path,
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def fake_worker(arguments: Sequence[str], **_kwargs: object) -> CommandResult:
        captured.append(arguments[arguments.index("--cache") + 1])
        private_write(Path(arguments[arguments.index("--model") - 1]), b"{}")
        return CommandResult(stdout="", stderr="")

    monkeypatch.setattr("kilix_playalong.providers.transcription.is_available", lambda: True)
    monkeypatch.setattr("kilix_playalong.providers.transcription.run_command", fake_worker)
    source = tmp_path / "vocals.wav"
    private_write(source, b"synthetic vocals")

    transcription.transcribe(source, tmp_path / "lyrics.json", model="small")

    assert captured == [str(_whisper_model_cache())]


def test_whisper_policy_mirrors_the_transcription_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard both mirrors of the provider's private adaptive policy against drift.

    The pipeline ranks a recorded configuration against the one this machine would pick now.
    If the provider reorders its candidates or changes how `auto` picks a device, the mirrors
    would silently keep the old ranking and no upgrade would ever fire again; this fails
    instead.
    """
    assert _WHISPER_QUALITY_ORDER == transcription._QUALITY_ORDER

    # `auto` follows CUDA availability and nothing else, which is what makes a resolved
    # device -- rather than the raw option -- computable from outside the provider.
    _pin_hardware(monkeypatch, available_memory=4 * _GIB, cuda=True)
    assert transcription._auto_candidates("auto") == transcription._auto_candidates("cuda")
    _pin_hardware(monkeypatch, available_memory=4 * _GIB, cuda=False)
    assert transcription._auto_candidates("auto") == transcription._auto_candidates("cpu")

    # ...and CUDA is genuinely the better end of the device order: it unlocks the whole
    # quality order, and every model the CPU policy allows it allows too.
    assert _WHISPER_DEVICE_ORDER == ("cuda", "cpu")
    assert transcription._auto_candidates("cuda") == transcription._QUALITY_ORDER
    assert set(transcription._auto_candidates("cpu")) <= set(transcription._auto_candidates("cuda"))
