from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path

import mido
import pytest
from test_source import make_tone, make_uslt_mp3, requires_ffmpeg

from kilix_playalong import LYRICS_SCHEMA
from kilix_playalong.errors import (
    InvalidInputError,
    ProviderFailedError,
    ProviderUnavailableError,
    RightsConfirmationRequired,
)
from kilix_playalong.lyrics import load_lyrics_document
from kilix_playalong.pipeline import (
    _WHISPER_DEVICE_ORDER,
    _WHISPER_QUALITY_ORDER,
    LYRIC_SOURCE_TRANSCRIBE,
    Pipeline,
    PipelineOptions,
    _LyricsPlan,
    _recorded_whisper_receipt,
    _verify_resume_source,
    create_project,
    list_projects,
    resume,
    run_new,
)
from kilix_playalong.providers import transcription
from kilix_playalong.runner import CommandResult
from kilix_playalong.state import load_manifest, new_manifest, save_manifest
from kilix_playalong.tablature import STANDARD_TUNING
from kilix_playalong.types import ProjectManifest
from kilix_playalong.util import (
    canonical_json,
    private_write,
    sha256_bytes,
    sha256_file,
    sha256_text,
)

_GIB = 1024**3
_URL = "https://youtu.be/abcdef12345"

#: The plan the direct key tests below are asking about: this run will transcribe.
#: Passed explicitly because `_whisper_keys` is only ever handed a resolved plan, and
#: resolving one needs a project whose acquisition stage has already run.
_TRANSCRIBE = _LyricsPlan(route=LYRIC_SOURCE_TRANSCRIBE, document=None, digest=None, aligns=False)


def _recorded_model(manifest: ProjectManifest) -> str | None:
    """The model a project's receipt names, read exactly the way the pipeline reads it."""
    receipt = _recorded_whisper_receipt(manifest)
    return None if receipt is None else receipt.model


def _write_midi(path: Path) -> None:
    midi = mido.MidiFile()
    track = mido.MidiTrack()
    track.append(mido.Message("note_on", note=64, velocity=90, time=0))
    track.append(mido.Message("note_off", note=64, velocity=0, time=480))
    midi.tracks.append(track)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    midi.save(path)


def _install_providers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    real_probe: bool = False,
    cues: list[dict[str, object]] | None = None,
    subtitles: tuple[str, ...] = (),
    title: str = "Synthetic Song",
) -> tuple[dict[str, int], list[str]]:
    """Replace every heavy provider with a deterministic synthetic stand-in.

    `real_probe` leaves `media.probe` alone, which the file-arm tests need: their source
    really is media, and `source.inspect_file` is one of the things under test.
    `cues` and `subtitles` decide what the transcriber and the downloader hand back, so a
    test can state the transcript or the caption tracks its case is about. `title` is the
    same idea for the one piece of third-party *text* the download arm reports.
    """
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
        tracks = []
        for name in subtitles:
            track = destination / name
            private_write(track, _VTT.encode())
            tracks.append(track)
        return source, tracks, {"id": "abcdef12345", "duration": 6.0, "title": title}

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
    if not real_probe:
        monkeypatch.setattr("kilix_playalong.pipeline.media.probe", fake_probe)
    monkeypatch.setattr("kilix_playalong.pipeline.media.normalize", fake_normalize)
    monkeypatch.setattr("kilix_playalong.pipeline.separation.separate", fake_separate)
    monkeypatch.setattr("kilix_playalong.pipeline.basic_pitch.transcribe", fake_pitch)
    # Drive the real faster-whisper provider, replacing only its worker subprocess, so
    # the pipeline's model resolution and cache location stay under test.
    monkeypatch.setattr("kilix_playalong.providers.transcription.is_available", lambda: True)
    monkeypatch.setattr(
        "kilix_playalong.providers.transcription.run_command",
        _synthetic_whisper_worker(whisper_models, cues),
    )
    return calls, whisper_models


def _synthetic_whisper_worker(
    whisper_models: list[str],
    cues: list[dict[str, object]] | None = None,
) -> Callable[..., CommandResult]:
    """Stand in for the whisper worker subprocess, writing what the real worker writes.

    The receipt matters as much as the model does. `_whisper_worker` records its run with
    `transcription.format_receipt`, and a stand-in that wrote the older bare
    `faster-whisper:<model>` would let every test in this file pass against a string no
    worker produces -- which is exactly how `pipeline._recorded_whisper_receipt`'s
    predecessor came to slice a whole receipt and hand it back as a model name.
    """

    def worker(arguments: Sequence[str], **_kwargs: object) -> CommandResult:
        model = arguments[arguments.index("--model") + 1]
        audio_source = arguments[arguments.index("--audio-source") + 1]
        whisper_models.append(model)
        private_write(
            Path(arguments[arguments.index("--model") - 1]),
            json.dumps(
                {
                    "schema": LYRICS_SCHEMA,
                    "source": transcription.format_receipt(
                        model=model,
                        audio_source=audio_source,
                        audio_from="requested",
                        language="en",
                        language_from="detected",
                        language_confidence=0.94,
                    ),
                    "language": "en",
                    "cues": (
                        cues
                        if cues is not None
                        else [{"start": 0.0, "end": 2.0, "text": "Synthetic line", "words": []}]
                    ),
                }
            ).encode(),
        )
        return CommandResult(stdout="", stderr="")

    return worker


#: One WebVTT caption track, for the download stand-in. Two cues so the parser has
#: something to normalise and the classification tests have real lyrics to land.
_VTT = """WEBVTT

00:00:00.500 --> 00:00:02.500
Hello darkness my old friend

00:00:03.000 --> 00:00:05.500
I have come to talk with you again
"""

#: A word-timed transcript of the same two lines, as `_whisper_worker` writes them.
#: The alignment tests hand the pipeline a lyric sheet carrying these words and no
#: timing at all, and read back whether the timing below reached it.
_TRANSCRIPT_CUES: list[dict[str, object]] = [
    {
        "start": 0.5,
        "end": 2.5,
        "text": "hello darkness my old friend",
        "words": [
            {"start": 0.5, "end": 0.9, "text": "hello"},
            {"start": 0.9, "end": 1.4, "text": "darkness"},
            {"start": 1.4, "end": 1.7, "text": "my"},
            {"start": 1.7, "end": 2.0, "text": "old"},
            {"start": 2.0, "end": 2.5, "text": "friend"},
        ],
    },
    {
        "start": 3.0,
        "end": 5.5,
        "text": "i have come to talk with you again",
        "words": [
            {"start": 3.0, "end": 3.2, "text": "i"},
            {"start": 3.2, "end": 3.5, "text": "have"},
            {"start": 3.5, "end": 3.8, "text": "come"},
            {"start": 3.8, "end": 4.0, "text": "to"},
            {"start": 4.0, "end": 4.4, "text": "talk"},
            {"start": 4.4, "end": 4.7, "text": "with"},
            {"start": 4.7, "end": 5.0, "text": "you"},
            {"start": 5.0, "end": 5.5, "text": "again"},
        ],
    },
]

#: A transcript of a different song entirely, for the case where alignment must be
#: rejected rather than applied.
_WRONG_CUES: list[dict[str, object]] = [
    {
        "start": 0.5,
        "end": 2.5,
        "text": "completely unrelated words here now",
        "words": [
            {"start": 0.5 + 0.4 * index, "end": 0.9 + 0.4 * index, "text": word}
            for index, word in enumerate(["completely", "unrelated", "words", "here", "now"])
        ],
    }
]

_SHEET = "Hello darkness my old friend\nI have come to talk with you again\n"


def _fingerprint(provider: str, inputs: object) -> str:
    """`Pipeline._run_stage`'s own fingerprint, so a test can plant a recorded one."""
    return sha256_bytes(canonical_json({"provider": provider, "inputs": inputs}))


_WHISPER_REPOSITORIES = {
    "large-v3": "Systran--faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh--faster-whisper-large-v3-turbo",
    "medium": "Systran--faster-whisper-medium",
    "small": "Systran--faster-whisper-small",
}


def _whisper_model_directory(model: str) -> Path:
    return transcription.model_cache_path() / f"models--{_WHISPER_REPOSITORIES[model]}"


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
        "kilix_playalong.providers.transcription.cuda_available",
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
    resume(
        project_dir, options, progress=lambda name, status, _detail: statuses.append((name, status))
    )
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
        project_dir, drop_d, progress=lambda name, status, _detail: statuses.append((name, status))
    )
    assert ("tablature", "running") in statuses
    assert ("export", "running") in statuses
    assert ("transcribe-guitar", "cached") in statuses
    assert updated["tablature"] is not None
    assert updated["tablature"]["tuning"] == [38, 45, 50, 55, 59, 64]

    guitar = project_dir / "stems" / "guitar.wav"
    guitar.write_bytes(b"tampered")
    statuses.clear()
    resume(
        project_dir, drop_d, progress=lambda name, status, _detail: statuses.append((name, status))
    )
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
    assert _recorded_model(manifest) == "small"

    statuses: list[tuple[str, str]] = []
    upgraded = resume(
        project_dir,
        PipelineOptions(
            url=options.url,
            allow_model_downloads=True,
            rights_confirmed=True,
        ),
        progress=lambda name, status, _detail: statuses.append((name, status)),
    )
    assert ("lyrics", "running") in statuses
    assert whisper_models == ["small", "large-v3"]
    assert upgraded["lyrics"] is not None
    assert _recorded_model(upgraded) == "large-v3"
    assert calls["separate"] == 1

    statuses.clear()
    resume(
        project_dir,
        PipelineOptions(
            url=options.url,
            allow_model_downloads=True,
            rights_confirmed=True,
        ),
        progress=lambda name, status, _detail: statuses.append((name, status)),
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

    shutil.rmtree(transcription.model_cache_path())
    statuses: list[tuple[str, str]] = []
    unchanged = resume(
        project_dir,
        options,
        progress=lambda name, status, _detail: statuses.append((name, status)),
    )
    assert all(status == "cached" for _name, status in statuses)
    assert whisper_models == ["small"]
    assert unchanged["lyrics"] is not None
    assert _recorded_model(unchanged) == "small"
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
    assert _recorded_model(manifest) == "medium"

    _pin_hardware(monkeypatch, available_memory=4 * _GIB, cuda=True)
    statuses: list[tuple[str, str]] = []
    upgraded = resume(
        project_dir,
        options,
        progress=lambda name, status, _detail: statuses.append((name, status)),
    )
    assert ("lyrics", "running") in statuses
    assert whisper_models == ["medium", "large-v3"]
    assert upgraded["lyrics"] is not None
    assert _recorded_model(upgraded) == "large-v3"
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
        progress=lambda name, status, _detail: statuses.append((name, status)),
    )
    assert ("lyrics", "cached") in statuses
    assert whisper_models == ["large-v3"]
    assert unchanged["lyrics"] is not None
    assert _recorded_model(unchanged) == "large-v3"
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
        progress=lambda name, status, _detail: statuses.append((name, status)),
    )
    assert ("lyrics", "running") in statuses
    assert whisper_models == ["large-v3", "medium"]
    assert downgraded["lyrics"] is not None
    assert _recorded_model(downgraded) == "medium"
    assert calls["pitch"] == 2

    # ...and the stage settles on what it ran: one re-run, not a second one afterwards.
    statuses.clear()
    resume(
        project_dir,
        options,
        progress=lambda name, status, _detail: statuses.append((name, status)),
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
        if offline and not transcription._is_cached(transcription.model_cache_path(), model):
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
    assert _recorded_model(first) == "large-v3"

    # The big weights are pruned, only `small` survives, and the resume is offline.
    _cache_whisper_model("small")
    (project_dir / "lyrics" / "lyrics.json").unlink()
    statuses: list[tuple[str, str]] = []
    resumed = resume(
        project_dir,
        PipelineOptions(url="https://youtu.be/abcdef12345", rights_confirmed=True),
        progress=lambda name, status, _detail: statuses.append((name, status)),
    )
    assert ("lyrics", "done") in statuses
    assert whisper_models == ["large-v3", "small"]
    assert resumed["lyrics"] is not None
    assert _recorded_model(resumed) == "small"
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

    shutil.rmtree(transcription.model_cache_path())
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
        project_dir,
        explicit,
        progress=lambda name, status, _detail: statuses.append((name, status)),
    )
    assert ("lyrics", "running") in statuses
    assert whisper_models == ["large-v3", "medium"]
    assert downgraded["lyrics"] is not None
    assert _recorded_model(downgraded) == "medium"

    statuses.clear()
    resume(
        project_dir,
        explicit,
        progress=lambda name, status, _detail: statuses.append((name, status)),
    )
    assert all(status == "cached" for _name, status in statuses)
    assert whisper_models == ["large-v3", "medium"]

    # Handing the same machine back to `auto` re-transcribes only if `auto` wants something
    # better, so naming the model `auto` would have picked anyway costs nothing.
    _pin_hardware(monkeypatch, available_memory=4 * _GIB, cuda=False)
    statuses.clear()
    resume(
        project_dir,
        adaptive,
        progress=lambda name, status, _detail: statuses.append((name, status)),
    )
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
    resume(
        project_dir, options, progress=lambda name, status, _detail: statuses.append((name, status))
    )
    assert ("lyrics", "running") in statuses
    assert whisper_models == ["large-v3", "large-v3"]

    # Losing the GPU again is the machine shrinking, not the user changing their mind.
    _pin_hardware(monkeypatch, available_memory=12 * _GIB, cuda=False)
    statuses.clear()
    resume(
        project_dir, options, progress=lambda name, status, _detail: statuses.append((name, status))
    )
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
        progress=lambda name, status, _detail: statuses.append((name, status)),
    )
    assert all(status == "cached" for _name, status in statuses)
    assert whisper_models == ["large-v3"]
    assert kept["lyrics"] is not None
    assert _recorded_model(kept) == "large-v3"
    assert calls["pitch"] == 1

    # Keeping it did not blunt the device rule: once the weights are back, the same model on
    # the better device is a genuine upgrade and does re-run -- exactly once.
    _cache_whisper_model("large-v3")
    statuses.clear()
    upgraded = resume(
        project_dir,
        options,
        progress=lambda name, status, _detail: statuses.append((name, status)),
    )
    assert ("lyrics", "running") in statuses
    assert whisper_models == ["large-v3", "large-v3"]
    assert upgraded["lyrics"] is not None
    assert _recorded_model(upgraded) == "large-v3"

    statuses.clear()
    resume(
        project_dir, options, progress=lambda name, status, _detail: statuses.append((name, status))
    )
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

    key, alternates = Pipeline(project_dir, manifest, adaptive)._whisper_keys(_TRANSCRIBE)
    assert key == {"model": "medium", "device": "cuda", "audio": "vocals"}
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
    _key, pinned_alternates = Pipeline(project_dir, manifest, pinned)._whisper_keys(_TRANSCRIBE)
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

    key, alternates = Pipeline(project_dir, manifest, options)._whisper_keys(_TRANSCRIBE)
    assert key == {"model": "base", "device": "cpu", "audio": "vocals"}
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

    key, alternates = Pipeline(project_dir, manifest, adaptive)._whisper_keys(_TRANSCRIBE)
    assert key == {"model": "medium", "device": "cpu", "audio": "vocals"}
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
    assert Pipeline(project_dir, manifest, pinned)._whisper_keys(_TRANSCRIBE) == (
        {"model": "medium", "device": "cpu", "audio": "vocals"},
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
        progress=lambda name, status, _detail: statuses.append((name, status)),
    )
    assert ("lyrics", "cached") in statuses
    assert whisper_models == []
    assert unchanged["lyrics"] is not None
    assert unchanged["lyrics"]["source"] == "imported-lrc"
    assert calls["pitch"] == 1


def test_the_worker_is_given_the_cache_the_provider_names(
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

    assert captured == [str(transcription.model_cache_path())]


def test_whisper_policy_mirrors_the_transcription_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard both snapshots of the provider's adaptive policy against drift.

    The pipeline ranks a recorded configuration against the one this machine would pick now.
    If the provider reorders its candidates or changes how `auto` picks a device, the mirrors
    would silently keep the old ranking and no upgrade would ever fire again; this fails
    instead.
    """
    assert _WHISPER_QUALITY_ORDER == transcription.QUALITY_ORDER

    # `auto` follows CUDA availability and nothing else, which is what makes a resolved
    # device -- rather than the raw option -- computable from outside the provider.
    _pin_hardware(monkeypatch, available_memory=4 * _GIB, cuda=True)
    assert transcription._auto_candidates("auto") == transcription._auto_candidates("cuda")
    _pin_hardware(monkeypatch, available_memory=4 * _GIB, cuda=False)
    assert transcription._auto_candidates("auto") == transcription._auto_candidates("cpu")

    # ...and CUDA is genuinely the better end of the device order: it unlocks the whole
    # quality order, and every model the CPU policy allows it allows too.
    assert _WHISPER_DEVICE_ORDER == ("cuda", "cpu")
    assert transcription._auto_candidates("cuda") == transcription.QUALITY_ORDER
    assert set(transcription._auto_candidates("cpu")) <= set(transcription._auto_candidates("cuda"))


# --------------------------------------------------------------------------- #
# The source union: a local file is a first-class source
# --------------------------------------------------------------------------- #


def _file_options(path: Path, **overrides: object) -> PipelineOptions:
    values: dict[str, object] = {"source_path": path, "rights_confirmed": True}
    values.update(overrides)
    return PipelineOptions(**values)  # type: ignore[arg-type]


@requires_ffmpeg
def test_a_local_file_flows_through_the_pipeline_like_a_download(
    tmp_path: Path,
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every stage after acquisition must not be able to tell which arm produced the media."""
    calls, whisper_models = _install_providers(monkeypatch, real_probe=True)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    _cache_whisper_model("small")
    song = make_tone(tmp_path / "song.mp3", title="File Title", artist="File Artist")

    project_dir, manifest = run_new(_file_options(song))

    assert all(stage["status"] == "done" for stage in manifest["stages"].values())
    assert manifest["source"]["kind"] == "file"
    assert manifest["source"]["name"] == "song.mp3"
    assert manifest["title"] == "File Title"
    assert manifest["artist"] == "File Artist"
    assert calls["download"] == 0
    assert manifest["stages"]["download"]["provider"] == "kilix-playalong-file-intake:v1"
    # The copy is the project's own bytes and equals the library file exactly.
    copied = project_dir / str(manifest["source"]["media_path"])
    assert copied.is_file() and sha256_file(copied) == sha256_file(song)
    assert song.is_file(), "the user's library must be exactly as it was"
    assert (project_dir / "exports" / "playalong.html").is_file()
    assert whisper_models == ["small"]

    statuses: list[tuple[str, str]] = []
    resume(project_dir, _file_options(song), progress=lambda n, s, _d: statuses.append((n, s)))
    assert all(status == "cached" for _name, status in statuses)


@requires_ffmpeg
def test_a_file_project_resumes_after_the_library_moves(
    tmp_path: Path,
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The copy is what makes a project survive the user reorganising their music.

    Three resumes, and none of them may need the original bytes back: with the file
    deleted, with it renamed and re-supplied, and with nothing named at all.
    """
    _install_providers(monkeypatch, real_probe=True)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    _cache_whisper_model("small")
    song = make_tone(tmp_path / "song.mp3")

    project_dir, _manifest = run_new(_file_options(song))

    moved = tmp_path / "elsewhere" / "renamed.mp3"
    moved.parent.mkdir()
    shutil.move(song, moved)

    statuses: list[tuple[str, str]] = []
    resume(project_dir, _file_options(moved), progress=lambda n, s, _d: statuses.append((n, s)))
    assert all(status == "cached" for _name, status in statuses), "content, not path, is identity"

    moved.unlink()
    statuses.clear()
    resume(
        project_dir,
        PipelineOptions(rights_confirmed=True),
        progress=lambda n, s, _d: statuses.append((n, s)),
    )
    assert all(status == "cached" for _name, status in statuses)


@requires_ffmpeg
def test_raising_the_duration_limit_does_not_destroy_a_finished_file_project(
    tmp_path: Path,
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The file arm's acquisition key holds no duration bound, and it must not.

    The bound admits a file; it does not describe the copy that admission produced. With
    it in the key, a user who widens `--max-duration-minutes` re-acquires -- and if their
    music has moved since, that re-acquisition raises after `_invalidate_from` has already
    wiped every finished stage, so a complete project is lost to a setting the user
    loosened. Both directions are asserted here, because the copy is equally admitted
    under a bound the user later tightens.

    What the bound still gates is every place a *file* is opened, which the third resume
    shows: naming the file again re-runs `inspect_file`, which refuses it, and refuses it
    without touching the project.
    """
    _install_providers(monkeypatch, real_probe=True)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    _cache_whisper_model("small")
    song = make_tone(tmp_path / "song.mp3", seconds=3.0)
    project_dir, _manifest = run_new(_file_options(song))

    statuses: list[tuple[str, str]] = []
    widened = resume(
        project_dir,
        PipelineOptions(max_duration=45 * 60, rights_confirmed=True),
        progress=lambda n, s, _d: statuses.append((n, s)),
    )
    assert all(status == "cached" for _name, status in statuses)
    assert all(stage["status"] == "done" for stage in widened["stages"].values())

    statuses.clear()
    tightened = resume(
        project_dir,
        PipelineOptions(max_duration=60.0, rights_confirmed=True),
        progress=lambda n, s, _d: statuses.append((n, s)),
    )
    assert all(status == "cached" for _name, status in statuses)
    assert all(stage["status"] == "done" for stage in tightened["stages"].values())

    with pytest.raises(InvalidInputError, match="minute limit"):
        resume(project_dir, _file_options(song, max_duration=1.0))
    assert all(stage["status"] == "done" for stage in load_manifest(project_dir)["stages"].values())

    song.unlink()
    statuses.clear()
    moved_away = resume(
        project_dir,
        PipelineOptions(max_duration=45 * 60, rights_confirmed=True),
        progress=lambda n, s, _d: statuses.append((n, s)),
    )
    assert all(status == "cached" for _name, status in statuses)
    assert (project_dir / "exports" / "playalong.html").is_file()
    assert moved_away["lyrics"] is not None


@requires_ffmpeg
def test_resuming_a_project_with_a_different_file_is_refused(
    tmp_path: Path,
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The file arm gets the guard the URL arm has always had, in the same words."""
    _install_providers(monkeypatch, real_probe=True)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    _cache_whisper_model("small")
    song = make_tone(tmp_path / "song.mp3")
    other = make_tone(tmp_path / "other.mp3", seconds=2.0)

    project_dir, _manifest = run_new(_file_options(song))

    with pytest.raises(InvalidInputError, match="does not match"):
        resume(project_dir, _file_options(other))
    with pytest.raises(InvalidInputError, match="not the kind"):
        resume(project_dir, PipelineOptions(url=_URL, rights_confirmed=True))


@requires_ffmpeg
def test_a_file_past_the_duration_limit_leaves_no_project_behind(
    tmp_path: Path,
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate runs before the project directory exists, so a refusal costs no cleanup."""
    _install_providers(monkeypatch, real_probe=True)
    song = make_tone(tmp_path / "long.wav", seconds=3.0)

    with pytest.raises(InvalidInputError, match="minute limit"):
        create_project(_file_options(song, max_duration=1.0))

    assert list_projects() == []


@requires_ffmpeg
def test_an_acquisition_failure_does_not_report_where_the_library_is(
    tmp_path: Path,
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The redaction backstop for the arm that has a library path to leak.

    `public_error` erases URLs and the `/home/<name>` head of a home-tree path, which is
    all the URL arm ever needed. A library on `/mnt`, `/media` or -- as here -- a scratch
    directory is covered by neither, so `Pipeline._secrets` has to name the source path
    itself, and the *resolved* one, because that is the string an opener reports.

    The failure is injected. Every provider on this path promises a path-free error today
    (`source.inspect_file`'s module contract, `media.copy_into`'s ENFORCED claim,
    `_copy_private` above), which is exactly why this is asserted against an injected
    breach: it is the backstop, and a backstop cannot be tested by the cases that never
    reach it.
    """
    _install_providers(monkeypatch, real_probe=True)
    song = make_tone(tmp_path / "song.mp3")

    def refuse(spec: object, _destination: Path, **_kwargs: object) -> None:
        raise OSError(13, "Permission denied", str(song))

    monkeypatch.setattr("kilix_playalong.pipeline.acquire", refuse)

    project_dir, manifest = create_project(_file_options(song))
    events: list[tuple[str, str, str]] = []
    with pytest.raises(ProviderFailedError) as failure:
        Pipeline(
            project_dir,
            manifest,
            _file_options(song),
            lambda name, status, detail: events.append((name, status, detail)),
        ).run()

    recorded = load_manifest(project_dir)["stages"]["download"]["error"]
    assert isinstance(recorded, str)
    # The progress stream and the manifest carry the same string, so a user watching a run
    # and a surface reading it back afterwards cannot be told two different things -- and
    # the redaction below therefore covers both by covering one.
    assert ("download", "error", recorded) in events
    for reported in (recorded, str(failure.value)):
        assert str(song) not in reported
        assert str(tmp_path) not in reported
        assert "<redacted>" in reported


def test_a_lyrics_copy_that_fails_names_no_file(
    tmp_path: Path,
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`shutil.copyfile` fails with both filenames in the message, and this one is un-redacted.

    It happens in `create_project`, before any stage exists, so there is no `_run_stage`
    handler to redact it and a bare `OSError` would reach the CLI as a traceback instead
    of as an error it knows how to print.
    """
    sheet = _sheet(tmp_path)

    def refuse(source: str, destination: str) -> str:
        raise OSError(13, "Permission denied", str(source))

    monkeypatch.setattr("kilix_playalong.pipeline.shutil.copyfile", refuse)

    with pytest.raises(ProviderFailedError) as failure:
        create_project(PipelineOptions(url=_URL, lyrics_path=sheet, rights_confirmed=True))

    assert str(sheet) not in str(failure.value)
    assert "supplied lyrics file could not be copied" in str(failure.value)


@requires_ffmpeg
def test_a_language_change_does_not_re_acquire_a_local_file(
    tmp_path: Path,
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The file arm's acquisition key holds no language, and it must not.

    Language decides which caption track yt-dlp fetches, so the URL arm re-downloads --
    asserted here as the contrast. Nothing about copying a file depends on it, and a
    resume is not entitled to assume the library is still reachable, so keying on it
    would make `--language es` fail on exactly the projects that need it least.
    """
    _install_providers(monkeypatch, real_probe=True)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    _cache_whisper_model("small")
    song = make_tone(tmp_path / "song.mp3")
    project_dir, _manifest = run_new(_file_options(song))
    song.unlink()

    statuses: list[tuple[str, str]] = []
    resume(
        project_dir,
        PipelineOptions(language="es", rights_confirmed=True),
        progress=lambda n, s, _d: statuses.append((n, s)),
    )
    assert ("download", "cached") in statuses
    assert ("lyrics", "running") in statuses


def test_a_language_change_re_downloads_a_video(
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The contrast the file arm is measured against: language decides which captions
    yt-dlp is asked for, so on the URL arm it really does belong in the acquisition key."""
    _install_providers(monkeypatch)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    _cache_whisper_model("small")
    project_dir, _manifest = run_new(PipelineOptions(url=_URL, rights_confirmed=True))

    statuses: list[tuple[str, str]] = []
    resume(
        project_dir,
        PipelineOptions(url=_URL, language="es", rights_confirmed=True),
        progress=lambda n, s, _d: statuses.append((n, s)),
    )
    assert ("download", "running") in statuses


def test_the_youtube_acquisition_key_survived_the_source_union(
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adding a second arm must not re-download every project already on this machine.

    The strongest form of that promise: the fingerprint a finished YouTube acquisition
    records is recomputed here from the fields the pre-union code used, and has to be the
    same string.
    """
    _install_providers(monkeypatch)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    _cache_whisper_model("small")
    project_dir, manifest = run_new(PipelineOptions(url=_URL, rights_confirmed=True))

    assert manifest["stages"]["download"]["fingerprint"] == _fingerprint(
        "yt-dlp:2026.8.19",
        {
            "url_sha256": manifest["source"]["url_sha256"],
            "language": "auto",
            "max_duration": 30 * 60,
        },
    )
    assert manifest["source"]["url_sha256"] == sha256_text(_URL)
    assert project_dir.is_dir()


# --------------------------------------------------------------------------- #
# Where the words come from
# --------------------------------------------------------------------------- #


@requires_ffmpeg
def test_a_lyrics_tag_inside_the_media_becomes_the_lyrics(
    tmp_path: Path,
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file that carries its own words must not be transcribed."""
    _calls, whisper_models = _install_providers(monkeypatch, real_probe=True)
    song = make_uslt_mp3(tmp_path / "uslt.mp3", "[00:00.50]Tagged line\n[00:02.00]Second line")

    _project_dir, manifest = run_new(_file_options(song))

    assert manifest["lyrics"] is not None
    assert manifest["lyrics"]["route"] == "embedded"
    assert manifest["lyrics"]["source"] == "embedded-lrc"
    assert whisper_models == []


@requires_ffmpeg
def test_an_lrc_beside_the_file_becomes_the_lyrics(
    tmp_path: Path,
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The near-universal convention, found without the user naming it, and copied in."""
    _calls, whisper_models = _install_providers(monkeypatch, real_probe=True)
    song = make_tone(tmp_path / "song.mp3")
    (tmp_path / "song.lrc").write_text("[00:00.50]Sidecar line\n[00:02.00]Second line\n")

    project_dir, manifest = run_new(_file_options(song))

    assert manifest["lyrics"] is not None
    assert manifest["lyrics"]["route"] == "sidecar"
    assert manifest["lyrics"]["source"] == "sidecar-lrc"
    assert whisper_models == []
    # Copied, so the project keeps working when the user tidies the folder.
    (tmp_path / "song.lrc").unlink()
    statuses: list[tuple[str, str]] = []
    resume(project_dir, _file_options(song), progress=lambda n, s, _d: statuses.append((n, s)))
    assert all(status == "cached" for _name, status in statuses)


@requires_ffmpeg
def test_an_explicit_lyric_source_never_silently_falls_back(
    tmp_path: Path,
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The option exists for the user who knows one source is wrong. Handing it to them anyway
    -- or quietly handing them a different one -- is the failure it was added to prevent."""
    _calls, whisper_models = _install_providers(monkeypatch, real_probe=True)
    song = make_tone(tmp_path / "song.mp3")
    (tmp_path / "song.lrc").write_text("[00:00.50]Sidecar line\n[00:02.00]Second line\n")

    project_dir, manifest = create_project(_file_options(song, lyrics_source="embedded"))
    with pytest.raises(InvalidInputError, match="no embedded lyrics tag"):
        Pipeline(project_dir, manifest, _file_options(song, lyrics_source="embedded")).run()

    assert whisper_models == []
    assert load_manifest(project_dir)["stages"]["lyrics"]["status"] == "error"


def test_a_lyric_source_with_nothing_to_read_is_refused_at_creation(
    tmp_path: Path,
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two combinations `_validate_options` sends to `create_project` to judge.

    Both are certain failures, so the whole cost of learning about them should be the
    error: no project directory, no download, no separation, and -- for the file arm --
    not even the media gate, since the refusal precedes `_source_identity` and the file
    here is not media at all.
    """
    calls, whisper_models = _install_providers(monkeypatch, real_probe=True)
    not_media = tmp_path / "song.mp3"
    not_media.write_bytes(b"not media at all")

    with pytest.raises(InvalidInputError, match="no lyrics file was supplied"):
        create_project(_file_options(not_media, lyrics_source="file"))
    with pytest.raises(InvalidInputError, match="no embedded lyrics tag"):
        create_project(PipelineOptions(url=_URL, lyrics_source="embedded", rights_confirmed=True))

    assert list_projects() == []
    assert calls == {"download": 0, "normalize": 0, "separate": 0, "pitch": 0}
    assert whisper_models == []


def test_an_auto_generated_caption_track_is_recorded_as_one(
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A machine transcript published by the uploader is still a machine transcript.

    Recording it as plain "youtube-captions" tells a later reader -- and both surfaces --
    that a human wrote these words down. `lyrics.SubtitleChoice` classifies the track and
    this is where that classification has to survive into the manifest.
    """
    _calls, whisper_models = _install_providers(monkeypatch, subtitles=("source.a.en.vtt",))

    _project_dir, manifest = run_new(PipelineOptions(url=_URL, rights_confirmed=True))

    assert manifest["lyrics"] is not None
    assert manifest["lyrics"]["route"] == "captions"
    assert manifest["lyrics"]["source"] == "youtube-captions-automatic"
    assert whisper_models == []


def test_choosing_the_source_auto_already_resolved_to_changes_nothing(
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lyrics key holds the *resolved* route, so naming it costs no re-run.

    Same reason `_whisper_keys` keys on the model `auto` resolved to: two option values
    that make the identical artifact have to make the identical key, or every user who
    pins down what the app already chose pays for a re-transcription.
    """
    _calls, whisper_models = _install_providers(monkeypatch)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    _cache_whisper_model("small")
    project_dir, _manifest = run_new(PipelineOptions(url=_URL, rights_confirmed=True))
    assert whisper_models == ["small"]

    statuses: list[tuple[str, str]] = []
    resume(
        project_dir,
        PipelineOptions(url=_URL, lyrics_source="transcribe", rights_confirmed=True),
        progress=lambda n, s, _d: statuses.append((n, s)),
    )
    assert all(status == "cached" for _name, status in statuses)
    assert whisper_models == ["small"]


def test_changing_the_transcribed_audio_re_runs_the_lyrics_stage(
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`audio_source` decides what the worker listens to, so it decides the transcript.

    Matched exactly rather than ranked: the (model, device) ranking exists because a
    machine shrinks and grows underneath a user who asked for neither, and this is a
    preference the user states.
    """
    _calls, whisper_models = _install_providers(monkeypatch)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    _cache_whisper_model("small")
    handed: list[str] = []
    worker = _synthetic_whisper_worker(whisper_models)

    def recording(arguments: Sequence[str], **kwargs: object) -> CommandResult:
        handed.append(arguments[arguments.index("--audio-source") + 1])
        return worker(arguments, **kwargs)

    monkeypatch.setattr("kilix_playalong.providers.transcription.run_command", recording)
    project_dir, _manifest = run_new(PipelineOptions(url=_URL, rights_confirmed=True))
    assert handed == ["vocals"]

    mixed = PipelineOptions(url=_URL, audio_source="mix", rights_confirmed=True)
    statuses: list[tuple[str, str]] = []
    resume(project_dir, mixed, progress=lambda n, s, _d: statuses.append((n, s)))
    assert ("lyrics", "running") in statuses
    assert handed == ["vocals", "mix"]

    statuses.clear()
    resume(project_dir, mixed, progress=lambda n, s, _d: statuses.append((n, s)))
    assert all(status == "cached" for _name, status in statuses)
    assert handed == ["vocals", "mix"]


def test_a_legacy_bare_receipt_still_keeps_a_transcript_through_an_emptied_cache(
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Projects made before the receipt grew fields carry `faster-whisper:<model>` and resume.

    The pipeline asks the provider to read the receipt precisely so both spellings work;
    this plants the old one and checks the undecidable-machine path still recovers the
    model from it.
    """
    _calls, whisper_models = _install_providers(monkeypatch)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    _cache_whisper_model("small")
    options = PipelineOptions(url=_URL, rights_confirmed=True)
    project_dir, manifest = run_new(options)

    assert manifest["lyrics"] is not None
    manifest["lyrics"]["source"] = "faster-whisper:small"
    save_manifest(project_dir, manifest)
    assert _recorded_model(load_manifest(project_dir)) == "small"

    shutil.rmtree(transcription.model_cache_path())
    statuses: list[tuple[str, str]] = []
    resume(project_dir, options, progress=lambda n, s, _d: statuses.append((n, s)))
    assert all(status == "cached" for _name, status in statuses)
    assert whisper_models == ["small"]


# --------------------------------------------------------------------------- #
# Forced alignment of words that arrived without timing
# --------------------------------------------------------------------------- #


def _sheet(tmp_path: Path) -> Path:
    path = tmp_path / "sheet.txt"
    path.write_text(_SHEET)
    return path


def test_untimed_supplied_words_are_timed_from_a_transcript(
    tmp_path: Path,
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The case the aligner exists for: right words, no timing.

    Without alignment those two lines are spread evenly across the whole six seconds,
    which is wrong by seconds on the first line. With it they land where they were sung.
    """
    _calls, whisper_models = _install_providers(monkeypatch, cues=_TRANSCRIPT_CUES)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    _cache_whisper_model("small")
    options = PipelineOptions(url=_URL, lyrics_path=_sheet(tmp_path), rights_confirmed=True)

    project_dir, manifest = run_new(options)

    assert whisper_models == ["small"], "alignment needs a transcript of the same audio"
    assert manifest["lyrics"] is not None
    assert manifest["lyrics"]["source"] == "imported-plain-aligned"
    alignment = manifest["lyrics"]["alignment"]
    assert isinstance(alignment, dict)
    assert alignment["applied"] is True and alignment["grade"] == "good"
    document = json.loads((project_dir / "lyrics" / "lyrics.json").read_text())
    assert [cue["text"] for cue in document["cues"]] == [
        "Hello darkness my old friend",
        "I have come to talk with you again",
    ]
    assert document["cues"][0]["start"] == pytest.approx(0.5, abs=0.05)
    assert document["cues"][1]["start"] == pytest.approx(3.0, abs=0.05)
    assert document["language"] == "en", "the transcript knows what was sung; the sheet does not"


def test_an_aligned_document_reads_back_as_measured(
    tmp_path: Path,
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The interlock with `lyrics.load_lyrics_document`, which is easy to break silently.

    A document that reads back as untimed is re-aligned on every single resume, and
    nothing else in the system complains. What that reader asks is now the document's own
    `timing`, so this pins the two ends of the round trip together: what
    `_apply_alignment` recorded is what comes back, `measured` and with its report, and
    the words have nothing left to hand an aligner.

    The source id's `-estimated` tail still has to go with it (`_aligned_source`), and the
    id is asserted whole here: the tail would otherwise claim a spread that is no longer
    in the file, and it is still what `lyrics._stored_timing` falls back to for any
    document that does not carry `timing` -- so the two modules have to keep spelling it
    the same way.
    """
    _install_providers(monkeypatch, cues=_TRANSCRIPT_CUES)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    _cache_whisper_model("small")
    options = PipelineOptions(url=_URL, lyrics_path=_sheet(tmp_path), rights_confirmed=True)

    project_dir, _manifest = run_new(options)

    stored = load_lyrics_document(project_dir / "lyrics" / "lyrics.json", duration=6.0)
    assert stored.timing == "measured"
    assert stored.alignment is not None and stored.alignment["usable"] is True
    assert stored.has_timing is True
    assert stored.lines == ()
    assert stored.note == ""
    assert stored.source == "imported-plain-aligned"


def test_a_measured_document_carries_the_report_that_measured_it(
    tmp_path: Path,
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Four of the aligner's own numbers reach the document a surface reads.

    A highlighted line that was measured and one that was guessed deserve different
    trust, and until now the only place that could be told apart was the manifest, which
    neither surface reads for this. The numbers are the report's, copied: the manifest
    holds the whole of `AlignmentReport.as_json`, the document holds these four, and this
    asserts the two agree rather than re-deriving either.
    """
    _install_providers(monkeypatch, cues=_TRANSCRIPT_CUES)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    _cache_whisper_model("small")
    options = PipelineOptions(url=_URL, lyrics_path=_sheet(tmp_path), rights_confirmed=True)

    project_dir, manifest = run_new(options)

    assert manifest["lyrics"] is not None
    recorded = manifest["lyrics"]["alignment"]
    assert isinstance(recorded, dict)
    document = json.loads((project_dir / "lyrics" / "lyrics.json").read_text())
    assert document["timing"] == "measured"
    assert document["alignment"] == {
        "matched_fraction": recorded["matched_fraction"],
        "interpolated_words": recorded["interpolated_words"],
        "mean_displacement": recorded["mean_displacement"],
        "usable": recorded["usable"],
    }
    # And the numbers are a real measurement of this run, not a placeholder: every word
    # of the sheet is in the transcript, so nothing had to be interpolated.
    assert document["alignment"]["matched_fraction"] == 1.0
    assert document["alignment"]["interpolated_words"] == 0
    assert document["alignment"]["usable"] is True


def test_a_route_that_brought_its_own_stamps_is_recorded_as_authored(
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two routes whose times this app did not place, and does not claim to have.

    A caption track carries the publisher's stamps. A transcript carries the
    transcriber's: the words and their times came out of the same decode of the same
    audio, and no forced alignment put them there -- so `measured`, which says forced
    alignment did, would claim a placement and a report that do not exist. Both are
    recorded as `authored`, the honest reading of a stamp that arrived with its document,
    and neither carries a confidence.
    """
    _install_providers(monkeypatch, subtitles=("source.en.vtt",))

    captions_dir, captions = run_new(PipelineOptions(url=_URL, rights_confirmed=True))

    assert captions["lyrics"] is not None
    assert captions["lyrics"]["route"] == "captions"
    document = json.loads((captions_dir / "lyrics" / "lyrics.json").read_text())
    assert document["timing"] == "authored"
    assert document["alignment"] is None

    _install_providers(monkeypatch)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    _cache_whisper_model("small")

    transcribed_dir, transcribed = run_new(PipelineOptions(url=_URL, rights_confirmed=True))

    assert transcribed["lyrics"] is not None
    assert transcribed["lyrics"]["route"] == LYRIC_SOURCE_TRANSCRIBE
    document = json.loads((transcribed_dir / "lyrics" / "lyrics.json").read_text())
    assert document["timing"] == "authored"
    assert document["alignment"] is None


def test_a_rejected_alignment_keeps_the_estimate_and_says_so(
    tmp_path: Path,
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`AlignmentReport.usable` is the threshold, and it is the aligner's own predicate.

    A transcript of a different song matches nothing, so transferring its timings would
    be worse than the even spread. The estimate is kept, the `-estimated` suffix with it,
    and the report says how badly it scored rather than the failure being silent.
    """
    _calls, whisper_models = _install_providers(monkeypatch, cues=_WRONG_CUES)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    _cache_whisper_model("small")
    options = PipelineOptions(url=_URL, lyrics_path=_sheet(tmp_path), rights_confirmed=True)

    project_dir, manifest = run_new(options)

    assert whisper_models == ["small"]
    assert manifest["lyrics"] is not None
    assert manifest["lyrics"]["source"] == "imported-plain-estimated"
    alignment = manifest["lyrics"]["alignment"]
    assert isinstance(alignment, dict)
    assert alignment["applied"] is False and alignment["grade"] == "poor"
    assert manifest["stages"]["lyrics"]["note"].startswith("lyrics from file; alignment rejected")
    # The document says the same thing the manifest does: these spans are the spread this
    # app invented, and there is no measurement behind them to report.
    document = json.loads((project_dir / "lyrics" / "lyrics.json").read_text())
    assert document["timing"] == "estimated"
    assert document["alignment"] is None


def test_a_stage_reports_what_it_did_and_not_only_that_it_finished(
    tmp_path: Path,
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The alignment verdict is the most useful thing the lyrics stage produces.

    It is recorded as the stage's note, and a callback carrying only a name and a status
    cannot report it: the user sees `[done] lyrics` and the only way to learn that their
    forced alignment was thrown away is `show --json` and reading the manifest by hand.
    The cached arm matters as much, because a resume is usually being run to see exactly
    this -- so "cached" re-reports the recorded note rather than saying nothing.
    """
    _install_providers(monkeypatch, cues=_WRONG_CUES)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    _cache_whisper_model("small")
    options = PipelineOptions(url=_URL, lyrics_path=_sheet(tmp_path), rights_confirmed=True)

    events: list[tuple[str, str, str]] = []
    project_dir, manifest = run_new(
        options, progress=lambda name, status, detail: events.append((name, status, detail))
    )

    recorded = manifest["stages"]["lyrics"]["note"]
    assert "alignment rejected" in recorded
    assert ("lyrics", "running", "") in events, "nothing is known yet when a stage starts"
    assert ("lyrics", "done", recorded) in events

    events.clear()
    resume(project_dir, options, progress=lambda n, s, d: events.append((n, s, d)))
    assert ("lyrics", "cached", recorded) in events


def test_a_reported_title_cannot_carry_terminal_escapes(
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """yt-dlp reports the title of a video it did not write, and that title gets printed.

    `cli.command_show` prints `manifest["title"]` straight to a terminal and
    `tablature.render_ascii` puts it in the header of a `.txt` a user will `cat`, so an
    ESC surviving this far would be executed by the terminal rather than read. It becomes
    a space rather than being dropped, so what is left cannot close up into a shorter
    string the source never said -- the same rule, and the same reason, as
    `source._clean_text` applies to the tags of a local file.
    """
    _install_providers(monkeypatch, title="Song\x1b[31m TITLE\x07 here")
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    _cache_whisper_model("small")

    _project_dir, manifest = run_new(PipelineOptions(url=_URL, rights_confirmed=True))

    assert manifest["title"] == "Song [31m TITLE here"
    assert "\x1b" not in manifest["title"] and "\x07" not in manifest["title"]


def test_alignment_without_a_transcriber_still_delivers_the_lyrics(
    tmp_path: Path,
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alignment is an improvement the user asked for, not the lyrics they already have.

    Failing the stage would cost them the words over the timing. The words land, the
    spacing stays estimated, and the note says which of the two happened.
    """
    _calls, whisper_models = _install_providers(monkeypatch)
    monkeypatch.setattr("kilix_playalong.providers.transcription.is_available", lambda: False)
    options = PipelineOptions(url=_URL, lyrics_path=_sheet(tmp_path), rights_confirmed=True)

    _project_dir, manifest = run_new(options)

    assert whisper_models == []
    assert manifest["lyrics"] is not None
    assert manifest["lyrics"]["source"] == "imported-plain-estimated"
    assert manifest["lyrics"]["alignment"] == {"applied": False, "reason": "no transcriber"}
    assert "alignment skipped" in manifest["stages"]["lyrics"]["note"]


def test_alignment_survives_a_provider_that_will_not_run(
    tmp_path: Path,
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`is_available()` answers about the import, and that is not the whole question.

    A machine with the `transcribe` extra installed, an empty model cache and no
    `--allow-model-downloads` passes that check and then raises `ProviderUnavailableError`
    out of `resolve_model` -- which is the state of the machine this was found on. It is
    the same "no transcriber will run" case as a missing extra, so it must cost the user
    the timing they asked for and not the words they supplied. Every other alignment test
    in this file caches a model first, which is why this one deliberately does not.
    """
    _calls, whisper_models = _install_providers(monkeypatch, cues=_TRANSCRIPT_CUES)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    options = PipelineOptions(url=_URL, lyrics_path=_sheet(tmp_path), rights_confirmed=True)

    project_dir, manifest = run_new(options)

    assert whisper_models == [], "no worker can be launched with no weights to launch it on"
    assert all(stage["status"] == "done" for stage in manifest["stages"].values())
    assert manifest["lyrics"] is not None
    assert manifest["lyrics"]["source"] == "imported-plain-estimated"
    alignment = manifest["lyrics"]["alignment"]
    assert isinstance(alignment, dict)
    assert alignment["applied"] is False
    assert "no suitable cached faster-whisper model" in str(alignment["reason"])
    assert "alignment skipped" in manifest["stages"]["lyrics"]["note"]
    document = json.loads((project_dir / "lyrics" / "lyrics.json").read_text())
    assert [cue["text"] for cue in document["cues"]] == [
        "Hello darkness my old friend",
        "I have come to talk with you again",
    ], "the supplied words are the thing that must survive"

    # And the improvement is not lost for good: the skipped run recorded the unresolved
    # `auto`/`auto` key, so the moment weights exist the key resolves to something else
    # and the stage runs again.
    _cache_whisper_model("small")
    statuses: list[tuple[str, str]] = []
    aligned = resume(project_dir, options, progress=lambda n, s, _d: statuses.append((n, s)))
    assert ("lyrics", "running") in statuses
    assert aligned["lyrics"] is not None
    assert aligned["lyrics"]["source"] == "imported-plain-aligned"
    assert whisper_models == ["small"]


def test_a_worker_that_fails_mid_alignment_fails_the_stage_and_is_retried(
    tmp_path: Path,
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other side of the rule above, and the reason it is drawn where it is.

    A transcriber that resolves, starts and *fails* is not "no transcriber": that failure
    may not repeat, and only a stage left in `error` is re-run by the next resume. Keeping
    the lyrics and recording the stage as done would bury the alignment for good, because
    the recorded key would be this run's own. So the stage fails, and the resume that
    follows aligns.
    """
    _calls, whisper_models = _install_providers(monkeypatch, cues=_TRANSCRIPT_CUES)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    _cache_whisper_model("small")
    working = transcription.transcribe
    crashed: list[bool] = []

    def flaky(*arguments: object, **keywords: object) -> object:
        if not crashed:
            crashed.append(True)
            raise ProviderFailedError("the faster-whisper worker exited with status 1")
        return working(*arguments, **keywords)  # type: ignore[arg-type]

    monkeypatch.setattr("kilix_playalong.pipeline.transcription.transcribe", flaky)
    options = PipelineOptions(url=_URL, lyrics_path=_sheet(tmp_path), rights_confirmed=True)

    project_dir, manifest = create_project(options)
    with pytest.raises(ProviderFailedError, match="worker exited"):
        Pipeline(project_dir, manifest, options).run()
    assert load_manifest(project_dir)["stages"]["lyrics"]["status"] == "error"

    resumed = resume(project_dir, options)
    assert resumed["lyrics"] is not None
    assert resumed["lyrics"]["source"] == "imported-plain-aligned"
    assert whisper_models == ["small"]


def test_turning_alignment_off_re_runs_the_lyrics_stage(
    tmp_path: Path,
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`align_supplied_text` changes the artifact, so it is in the key -- but only when it can.

    The flag is in the key exactly when the words arrived untimed. Flipping it on a project
    whose lyrics came with their own timing changes nothing and must invalidate nothing,
    which is the second half of this test.
    """
    _calls, whisper_models = _install_providers(monkeypatch, cues=_TRANSCRIPT_CUES)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    _cache_whisper_model("small")
    sheet = _sheet(tmp_path)
    aligned = PipelineOptions(url=_URL, lyrics_path=sheet, rights_confirmed=True)
    project_dir, _manifest = run_new(aligned)

    plain = PipelineOptions(
        url=_URL,
        lyrics_path=sheet,
        align_supplied_text=False,
        rights_confirmed=True,
    )
    statuses: list[tuple[str, str]] = []
    reverted = resume(project_dir, plain, progress=lambda n, s, _d: statuses.append((n, s)))
    assert ("lyrics", "running") in statuses
    assert reverted["lyrics"] is not None
    assert reverted["lyrics"]["source"] == "imported-plain-estimated"

    statuses.clear()
    resume(project_dir, plain, progress=lambda n, s, _d: statuses.append((n, s)))
    assert all(status == "cached" for _name, status in statuses)

    # ...and on timed lyrics the flag is not in the key at all.
    timed = tmp_path / "timed.lrc"
    timed.write_text("[00:00.50]Timed line\n[00:02.00]Second line\n")
    timed_on = PipelineOptions(url=_URL, lyrics_path=timed, rights_confirmed=True)
    timed_dir, _timed_manifest = run_new(timed_on)
    statuses.clear()
    resume(
        timed_dir,
        PipelineOptions(
            url=_URL,
            lyrics_path=timed,
            align_supplied_text=False,
            rights_confirmed=True,
        ),
        progress=lambda n, s, _d: statuses.append((n, s)),
    )
    assert all(status == "cached" for _name, status in statuses)
    assert whisper_models == ["small"], "a timed import never asks for a transcript"


#: The lyrics stage fingerprint the run below recorded on the build immediately before
#: `timing` and `alignment` were added to the written document -- captured by running
#: this test's first half there against a placeholder and pasting back what it printed.
#: A literal on purpose: recomputing it from `_lyrics_inputs` would only restate the code
#: and would move with it, and what this has to catch is a stage key moving because the
#: artifact's *bytes* changed, which is a mistake this repo has made before.
_LYRICS_KEY_BEFORE_PROVENANCE = "7ae3ca91a448c89aa06644e5548c694c7d9d94ff498eac98a076ecfff791d6ae"


def test_recording_lyric_provenance_moves_no_stage_key(
    tmp_path: Path,
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adding fields to lyrics.json changes its bytes. It must not change its key.

    Two halves, because two mechanisms decide whether a finished project re-runs.

    The fingerprint is `_run_stage`'s digest of `_lyrics_inputs`, which names the route,
    the language, the digest of the file that route reads and the resolved Whisper
    configuration -- and no output of the stage at all. Pinned to the value the build
    before this change produced.

    `state.stage_is_current` then re-digests the recorded artifacts. A project finished
    before the fields existed has a lyrics.json without them recorded under its own
    digest, so it still matches and the stage stays current. What that user sees on the
    next resume is `[cached] lyrics` and a document unchanged on disk; the new fields
    appear when the stage next runs for a reason of its own.
    """
    _install_providers(monkeypatch, cues=_TRANSCRIPT_CUES)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    _cache_whisper_model("small")
    options = PipelineOptions(url=_URL, lyrics_path=_sheet(tmp_path), rights_confirmed=True)

    project_dir, manifest = run_new(options)

    assert manifest["stages"]["lyrics"]["fingerprint"] == _LYRICS_KEY_BEFORE_PROVENANCE

    # The same project as it would have been finished before this change: the document
    # without the two fields, recorded under the digest it really has.
    document_path = project_dir / "lyrics" / "lyrics.json"
    stored = json.loads(document_path.read_text())
    assert stored["timing"] == "measured", "the run under test does write the fields"
    stored.pop("timing", None)
    stored.pop("alignment", None)
    private_write(document_path, canonical_json(stored))
    recorded = load_manifest(project_dir)
    for artifact in recorded["stages"]["lyrics"]["artifacts"]:
        if artifact["path"] == "lyrics/lyrics.json":
            artifact["sha256"] = sha256_file(document_path)
            artifact["size"] = document_path.stat().st_size
    save_manifest(project_dir, recorded)

    statuses: list[tuple[str, str]] = []
    resume(project_dir, options, progress=lambda n, s, _d: statuses.append((n, s)))

    assert all(status == "cached" for _name, status in statuses)
    assert ("lyrics", "cached") in statuses
    assert "timing" not in json.loads(document_path.read_text())


# --------------------------------------------------------------------------- #
# Renaming
# --------------------------------------------------------------------------- #


def test_renaming_a_project_re_renders_what_carries_the_name(
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`title` and `artist` are printed into two artifacts, so they invalidate those two.

    Nothing else: a rename must not re-download the song, re-separate it, or re-transcribe
    a word of it.
    """
    _install_providers(monkeypatch)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    _cache_whisper_model("small")
    project_dir, manifest = run_new(PipelineOptions(url=_URL, rights_confirmed=True))
    assert manifest["title"] == "Synthetic Song"

    statuses: list[tuple[str, str]] = []
    renamed = resume(
        project_dir,
        PipelineOptions(url=_URL, title="Better Name", artist="The Band", rights_confirmed=True),
        progress=lambda n, s, _d: statuses.append((n, s)),
    )
    running = {name for name, status in statuses if status == "running"}
    assert running == {"tablature", "export"}
    assert renamed["title"] == "Better Name"
    assert "Better Name" in (project_dir / "exports" / "guitar-tab.txt").read_text()
    assert "The Band" in (project_dir / "exports" / "playalong.html").read_text()

    statuses.clear()
    resume(
        project_dir,
        PipelineOptions(url=_URL, title="Better Name", artist="The Band", rights_confirmed=True),
        progress=lambda n, s, _d: statuses.append((n, s)),
    )
    assert all(status == "cached" for _name, status in statuses)


def test_a_lyrics_file_that_will_not_parse_reports_itself(
    tmp_path: Path,
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolving the route happens while the key is being computed, and it can fail there.

    Keying degrades to the undecidable-machine keys when that happens, which is the older
    behaviour and is right for a machine that cannot answer. It must not turn a broken
    *file* into a stage that reports itself cached: the placeholder route those keys carry
    is `auto`, which no finished stage ever records, so nothing can match and the stage
    runs and raises the reader's own message.
    """
    _calls, whisper_models = _install_providers(monkeypatch)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    _cache_whisper_model("small")
    broken = tmp_path / "broken.json"
    broken.write_text('{"schema": "not.a.lyrics.schema", "cues": []}')
    options = PipelineOptions(url=_URL, lyrics_path=broken, rights_confirmed=True)

    project_dir, manifest = create_project(options)
    with pytest.raises(InvalidInputError, match="unsupported schema"):
        Pipeline(project_dir, manifest, options).run()

    assert whisper_models == []
    assert load_manifest(project_dir)["stages"]["lyrics"]["status"] == "error"


def test_a_project_whose_recorded_link_carries_a_stray_control_character_still_resumes() -> None:
    """A real project on this machine held "\\rhttps://youtube.com/watch?v=..".

    It was created when `validate_url` still accepted an unprintable character,
    and the strict gate that replaced it then refused every resume: the recorded
    fingerprint covers the raw string, so a stripped URL could never match it and
    an unstripped one could never pass the gate. The project worked for days and
    then could not be resumed at all.

    The check exists to refuse a resume that names a *different song*, so it now
    asks that question with surrounding whitespace removed. Nothing is rewritten
    and no digest moves; the gate still sees exactly the string a provider would
    be handed.
    """

    legacy_url = "\rhttps://youtube.com/watch?v=abcdef12345"
    manifest = new_manifest(
        "song-legacy00000001",
        url_sha256=sha256_text(legacy_url),
        rights_statement="confirmed",
    )
    manifest["source"].update({"kind": "youtube", "url": legacy_url})

    # The stripped URL a current build produces has a different digest ...
    stripped = legacy_url.strip()
    assert sha256_text(stripped) != manifest["source"]["url_sha256"]

    # ... and is nonetheless accepted as the same song.
    _verify_resume_source(manifest, PipelineOptions(url=stripped))

    # A genuinely different link is still refused.
    with pytest.raises(InvalidInputError, match="fingerprint"):
        _verify_resume_source(manifest, PipelineOptions(url="https://youtu.be/zzzzzzzzzzz"))


def test_a_failed_reacquisition_leaves_a_finished_project_exactly_as_it_was(
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The link arm's key holds the duration bound, and a failed re-run must not cost the project.

    The file arm was fixed by dropping the bound from its key. The link arm cannot
    be fixed that way: moving that key re-downloads every project on the machine,
    which destroys exactly the ones whose video has since gone -- the same harm,
    delivered to everyone at once instead of to one person on a widened setting.

    So the invariant is the fix. `_invalidate_from` wipes every stage below the one
    re-running and the manifest is saved before the action is attempted, so a stage
    that then fails used to leave stems, lyrics, MIDI, tab and printable on disk
    with a manifest admitting to none of them. The stages below a failure were
    never attempted; their artifacts are untouched and still describe the inputs
    they always did, because the stage that would have replaced those inputs is the
    one that failed. They are put back.
    """

    calls, _providers = _install_providers(monkeypatch)
    _pin_hardware(monkeypatch, available_memory=64 * _GIB, cuda=False)
    _cache_whisper_model("small")
    options = PipelineOptions(url="https://youtu.be/abcdef12345", rights_confirmed=True)
    project_dir, _manifest = run_new(options)

    finished = load_manifest(project_dir)
    assert all(stage["status"] == "done" for stage in finished["stages"].values())
    downstream = {
        name: dict(finished["stages"][name])
        for name in ("normalize", "separate", "lyrics", "tablature", "export")
    }

    # The video is gone. Widening the limit re-keys acquisition, so it re-runs.
    def gone(*_args: object, **_kwargs: object) -> tuple[Path, list[Path], dict[str, object]]:
        raise ProviderFailedError("the video is no longer available")

    monkeypatch.setattr("kilix_playalong.providers.youtube.download", gone)

    with pytest.raises(ProviderFailedError):
        resume(
            project_dir,
            PipelineOptions(max_duration=45 * 60, rights_confirmed=True),
        )

    after = load_manifest(project_dir)
    assert after["stages"]["download"]["status"] == "error"
    for name, before in downstream.items():
        assert after["stages"][name]["status"] == "done", f"{name} was lost"
        assert after["stages"][name]["artifacts"] == before["artifacts"], name
        assert after["stages"][name].get("fingerprint") == before.get("fingerprint"), name

    # And the artifacts the restored records point at are really still there.
    for name in downstream:
        for artifact in after["stages"][name]["artifacts"]:
            assert (project_dir / artifact["path"]).is_file()
