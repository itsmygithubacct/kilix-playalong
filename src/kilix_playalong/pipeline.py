"""Resumable URL-to-stems-to-lyrics-to-tab project pipeline."""

from __future__ import annotations

import math
import re
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .errors import (
    InvalidInputError,
    PlayalongError,
    ProviderFailedError,
    RightsConfirmationRequired,
)
from .export import render_printable
from .lyrics import choose_subtitle, load_lyrics, write_lyrics
from .midi import load_note_events, validate_midi
from .paths import ensure_private_directory, project_artifact, project_directory, projects_home
from .providers import basic_pitch, media, separation, transcription, youtube
from .state import (
    STAGE_NAMES,
    begin_stage,
    fail_stage,
    finish_stage,
    load_manifest,
    new_manifest,
    save_manifest,
    stage_is_current,
)
from .tablature import STANDARD_TUNING, infer_fingerings, render_ascii, tuning_labels, write_tab
from .types import AudioTrack, ProjectManifest
from .util import (
    canonical_json,
    private_write,
    public_error,
    sha256_bytes,
    sha256_file,
    sha256_text,
)

RIGHTS_STATEMENT = "I confirmed that I have permission to process this media and its lyrics."
_LANGUAGE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


@dataclass(frozen=True)
class PipelineOptions:
    url: str
    title: str = ""
    artist: str = ""
    language: str = "auto"
    lyrics_path: Path | None = None
    model: str = "htdemucs_6s"
    whisper_model: str = transcription.DEFAULT_MODEL
    device: str = "auto"
    max_duration: float = 30 * 60
    max_fret: int = 20
    tuning: tuple[int, ...] = STANDARD_TUNING
    allow_model_downloads: bool = False
    rights_confirmed: bool = False


def _validate_options(options: PipelineOptions, *, require_rights: bool) -> None:
    if require_rights and not options.rights_confirmed:
        raise RightsConfirmationRequired("explicit permission confirmation is required")
    youtube.validate_url(options.url)
    if options.language != "auto" and not _LANGUAGE.fullmatch(options.language):
        raise InvalidInputError("language must be 'auto' or a short BCP 47 language tag")
    if options.model not in separation.SUPPORTED_MODELS:
        raise InvalidInputError("unsupported Demucs model")
    if options.whisper_model not in transcription.MODEL_CHOICES:
        raise InvalidInputError("unsupported faster-whisper model")
    if options.device not in {"auto", "cpu", "cuda"}:
        raise InvalidInputError("device must be auto, cpu, or cuda")
    if not math.isfinite(options.max_duration) or not 1 <= options.max_duration <= 2 * 60 * 60:
        raise InvalidInputError("maximum duration must be between one second and two hours")
    if not 12 <= options.max_fret <= 30:
        raise InvalidInputError("maximum fret must be between 12 and 30")
    if (
        len(options.tuning) != 6
        or any(isinstance(note, bool) or not 0 <= note <= 127 for note in options.tuning)
        or len(set(options.tuning)) != 6
        or tuple(sorted(options.tuning)) != options.tuning
    ):
        raise InvalidInputError("tuning must contain six ascending MIDI pitches")


def create_project(options: PipelineOptions) -> tuple[Path, ProjectManifest]:
    _validate_options(options, require_rights=True)
    if options.lyrics_path is not None and not options.lyrics_path.is_file():
        raise ProviderFailedError("the supplied lyrics file does not exist")
    project_id = "song-" + uuid.uuid4().hex[:16]
    project_dir = project_directory(project_id)
    project_dir.mkdir(mode=0o700)
    manifest = new_manifest(
        project_id,
        url_sha256=sha256_text(options.url),
        rights_statement=RIGHTS_STATEMENT,
        title=" ".join(options.title.split())[:200],
        artist=" ".join(options.artist.split())[:200],
        model=options.model,
        language=options.language,
        whisper_model=options.whisper_model,
        device=options.device,
        max_duration=options.max_duration,
        tuning=options.tuning,
        max_fret=options.max_fret,
    )
    manifest["source"]["url"] = options.url
    if options.lyrics_path is not None:
        lyrics_source = ensure_private_directory(project_dir / "source") / (
            "lyrics-input" + options.lyrics_path.suffix.lower()
        )
        shutil.copyfile(options.lyrics_path, lyrics_source)
        lyrics_source.chmod(0o600)
        manifest["source"]["lyrics_input_path"] = lyrics_source.relative_to(project_dir).as_posix()
        manifest["source"]["lyrics_input_sha256"] = sha256_file(lyrics_source)
    save_manifest(project_dir, manifest)
    return project_dir, manifest


class Pipeline:
    def __init__(
        self,
        project_dir: Path,
        manifest: ProjectManifest,
        options: PipelineOptions,
        progress: Callable[[str, str], None] | None = None,
    ):
        self.project_dir = project_dir
        self.manifest = manifest
        self.options = options
        self.progress = progress or (lambda _name, _status: None)

    def _save(self) -> None:
        save_manifest(self.project_dir, self.manifest)

    def _invalidate_from(self, name: str) -> None:
        offset = STAGE_NAMES.index(name)
        for later in STAGE_NAMES[offset + 1 :]:
            stage = self.manifest["stages"][later]
            stage["status"] = "pending"
            stage["started_at"] = None
            stage["finished_at"] = None
            stage["artifacts"] = []
            stage["error"] = None
            stage.pop("note", None)
            stage.pop("fingerprint", None)

    def _run_stage(
        self,
        name: str,
        provider: str,
        action: Callable[[], tuple[list[Path], str]],
        *,
        inputs: object,
    ) -> None:
        fingerprint = sha256_bytes(canonical_json({"provider": provider, "inputs": inputs}))
        if stage_is_current(
            self.project_dir,
            self.manifest,
            name,
            fingerprint=fingerprint,
        ):
            self.progress(name, "cached")
            return
        self._invalidate_from(name)
        begin_stage(self.manifest, name, provider, fingerprint=fingerprint)
        self._save()
        self.progress(name, "running")
        try:
            paths, note = action()
            finish_stage(self.project_dir, self.manifest, name, paths, note=note)
            self._save()
            self.progress(name, "done")
        except Exception as error:
            message = public_error(str(error), secrets=(self.options.url, str(self.project_dir)))
            fail_stage(self.manifest, name, message or error.__class__.__name__)
            self._save()
            self.progress(name, "error")
            if isinstance(error, PlayalongError):
                raise
            raise ProviderFailedError(message or f"{provider} failed") from error

    def run(self) -> ProjectManifest:
        self._run_stage(
            "download",
            "yt-dlp:2026.8.19",
            self._download,
            inputs={
                "url_sha256": sha256_text(self.options.url),
                "language": self.options.language,
                "max_duration": self.options.max_duration,
            },
        )
        self._run_stage("normalize", "ffmpeg:pcm-s16le-44100-stereo", self._normalize, inputs={})
        self._run_stage(
            "separate",
            f"demucs:{self.options.model}@eeac1d1",
            self._separate,
            inputs={"model": self.options.model, "device": self.options.device},
        )
        self._run_stage(
            "lyrics",
            "captions-or-faster-whisper",
            self._lyrics,
            inputs={
                "language": self.options.language,
                "whisper_model": self.options.whisper_model,
                "lyrics_input_sha256": self.manifest["source"].get("lyrics_input_sha256"),
            },
        )
        self._run_stage(
            "transcribe-guitar",
            "basic-pitch-onnx:0.4.0",
            self._transcribe_guitar,
            inputs={},
        )
        self._run_stage(
            "tablature",
            "kilix-playalong-fingering:v1",
            self._tablature,
            inputs={"tuning": self.options.tuning, "max_fret": self.options.max_fret},
        )
        self._run_stage(
            "export",
            "kilix-playalong-print:v1",
            self._export,
            inputs={},
        )
        return self.manifest

    def _download(self) -> tuple[list[Path], str]:
        source_dir = ensure_private_directory(self.project_dir / "source")
        source, subtitles, metadata = youtube.download(
            self.options.url,
            source_dir,
            language=self.options.language,
            max_duration=self.options.max_duration,
        )
        source.chmod(0o600)
        for subtitle in subtitles:
            subtitle.chmod(0o600)
        title = metadata.get("title")
        if not self.manifest["title"] and isinstance(title, str):
            self.manifest["title"] = " ".join(title.split())[:200]
        artist = metadata.get("artist") or metadata.get("uploader")
        if not self.manifest["artist"] and isinstance(artist, str):
            self.manifest["artist"] = " ".join(artist.split())[:200]
        duration = metadata.get("duration")
        if not isinstance(duration, int | float):
            raise ProviderFailedError("yt-dlp returned an invalid source duration")
        self.manifest["source"].update(
            {
                "video_id": metadata["id"],
                "duration": float(duration),
                "media_path": source.relative_to(self.project_dir).as_posix(),
                "media_sha256": sha256_file(source),
                "subtitle_paths": [
                    path.relative_to(self.project_dir).as_posix() for path in subtitles
                ],
            }
        )
        return [source, *subtitles], f"downloaded {float(duration):.1f}s source"

    def _source_path(self) -> Path:
        value = self.manifest["source"].get("media_path")
        if not isinstance(value, str):
            raise ProviderFailedError("project has no downloaded source path")
        return project_artifact(self.project_dir, value)

    def _duration(self) -> float:
        value = self.manifest["source"].get("duration")
        if not isinstance(value, int | float):
            raise ProviderFailedError("project has no source duration")
        return float(value)

    def _normalize(self) -> tuple[list[Path], str]:
        output = self.project_dir / "media" / "normalized.wav"
        media.probe(self._source_path())
        media.normalize(self._source_path(), output)
        output.chmod(0o600)
        return [output], "44.1 kHz stereo PCM"

    def _normalized_path(self) -> Path:
        return self.project_dir / "media" / "normalized.wav"

    def _separate(self) -> tuple[list[Path], str]:
        outputs = separation.separate(
            self._normalized_path(),
            self.project_dir / "stems",
            model=self.options.model,
            device=self.options.device,
            allow_model_downloads=self.options.allow_model_downloads,
        )
        tracks: list[AudioTrack] = []
        labels = {
            "vocals": ("Vocals", "vocals"),
            "drums": ("Drums", "rhythm"),
            "bass": ("Bass", "bass"),
            "guitar": ("Guitar", "guitar"),
            "piano": ("Piano", "keys"),
            "other": ("Other", "other"),
        }
        for stem, path in outputs.items():
            label, kind = labels.get(stem, (stem.title(), "other"))
            tracks.append(
                {
                    "id": stem,
                    "label": label,
                    "kind": kind,
                    "path": path.relative_to(self.project_dir).as_posix(),
                    "sha256": sha256_file(path),
                    "size": path.stat().st_size,
                    "default_muted": False,
                }
            )
        self.manifest["tracks"] = tracks
        return list(outputs.values()), f"{len(outputs)} independently controllable stems"

    def _lyrics(self) -> tuple[list[Path], str]:
        output = self.project_dir / "lyrics" / "lyrics.json"
        duration = self._duration()
        selected: Path | None = self.options.lyrics_path
        stored_lyrics = self.manifest["source"].get("lyrics_input_path")
        if selected is None and isinstance(stored_lyrics, str):
            selected = project_artifact(self.project_dir, stored_lyrics)
        if selected is None:
            raw_paths = self.manifest["source"].get("subtitle_paths", [])
            if not isinstance(raw_paths, list):
                raise ProviderFailedError("project subtitle paths are invalid")
            paths = [
                project_artifact(self.project_dir, value)
                for value in raw_paths
                if isinstance(value, str)
            ]
            selected = choose_subtitle(paths, self.options.language)
        if selected is not None:
            cues, source, language = load_lyrics(selected, duration=duration)
            write_lyrics(output, cues, source=source, language=language)
        else:
            vocals = self._track_path("vocals")
            transcription.transcribe(
                vocals,
                output,
                language=self.options.language,
                model=self.options.whisper_model,
                device=self.options.device,
                allow_model_downloads=self.options.allow_model_downloads,
            )
            cues, source, language = load_lyrics(output, duration=duration)
            write_lyrics(output, cues, source=source, language=language)
        self.manifest["lyrics"] = {
            "path": output.relative_to(self.project_dir).as_posix(),
            "source": source,
            "language": language,
            "visible": True,
        }
        return [output], source

    def _track_path(self, track_id: str) -> Path:
        for track in self.manifest["tracks"]:
            if track["id"] == track_id:
                return project_artifact(self.project_dir, track["path"])
        raise ProviderFailedError(f"project has no {track_id} stem")

    def _guitar_source(self) -> Path:
        try:
            return self._track_path("guitar")
        except ProviderFailedError:
            return self._track_path("other")

    def _transcribe_guitar(self) -> tuple[list[Path], str]:
        midi_path = self.project_dir / "midi" / "guitar.mid"
        notes_path = self.project_dir / "midi" / "guitar-notes.json"
        basic_pitch.transcribe(self._guitar_source(), midi_path, notes_path)
        count = validate_midi(midi_path)
        return [midi_path, notes_path], f"{count} MIDI note-on events"

    def _tablature(self) -> tuple[list[Path], str]:
        notes = load_note_events(self.project_dir / "midi" / "guitar-notes.json")
        events, omitted = infer_fingerings(
            notes,
            tuning=self.options.tuning,
            max_fret=self.options.max_fret,
        )
        if not events:
            raise ProviderFailedError("no playable guitar fingerings could be inferred")
        tab_path = self.project_dir / "tab" / "guitar-tab.json"
        ascii_path = self.project_dir / "exports" / "guitar-tab.txt"
        write_tab(
            tab_path,
            events,
            source_midi="midi/guitar.mid",
            tuning=self.options.tuning,
            max_fret=self.options.max_fret,
            omitted_notes=omitted,
        )
        private_write(
            ascii_path,
            render_ascii(
                events,
                title=self.manifest["title"],
                artist=self.manifest["artist"],
                labels=tuning_labels(self.options.tuning),
            ).encode("utf-8"),
        )
        self.manifest["tablature"] = {
            "path": tab_path.relative_to(self.project_dir).as_posix(),
            "ascii_path": ascii_path.relative_to(self.project_dir).as_posix(),
            "midi_path": "midi/guitar.mid",
            "visible": True,
            "tuning": list(self.options.tuning),
            "max_fret": self.options.max_fret,
        }
        return [tab_path, ascii_path], f"{len(events)} timed events; {omitted} notes omitted"

    def _export(self) -> tuple[list[Path], str]:
        lyrics = self.manifest["lyrics"]
        tablature = self.manifest["tablature"]
        if lyrics is None or tablature is None:
            raise ProviderFailedError("lyrics or tablature are unavailable")
        output = self.project_dir / "exports" / "playalong.html"
        render_printable(
            output,
            title=self.manifest["title"],
            artist=self.manifest["artist"],
            lyrics_path=project_artifact(self.project_dir, str(lyrics["path"])),
            tab_path=project_artifact(self.project_dir, str(tablature["path"])),
        )
        return [output], "self-contained printable HTML"


def run_new(
    options: PipelineOptions,
    *,
    progress: Callable[[str, str], None] | None = None,
) -> tuple[Path, ProjectManifest]:
    project_dir, manifest = create_project(options)
    return project_dir, Pipeline(project_dir, manifest, options, progress).run()


def resume(
    project_dir: Path,
    options: PipelineOptions,
    *,
    progress: Callable[[str, str], None] | None = None,
) -> ProjectManifest:
    _validate_options(options, require_rights=False)
    manifest = load_manifest(project_dir)
    authorization = manifest["source"].get("authorization")
    if not isinstance(authorization, dict) or authorization.get("confirmed") is not True:
        raise RightsConfirmationRequired("project has no recorded permission confirmation")
    if manifest["source"].get("url_sha256") != sha256_text(options.url):
        raise ProviderFailedError("resume URL does not match the project's source fingerprint")
    if options.lyrics_path is not None:
        if not options.lyrics_path.is_file():
            raise ProviderFailedError("the supplied lyrics file does not exist")
        lyrics_source = ensure_private_directory(project_dir / "source") / (
            "lyrics-input" + options.lyrics_path.suffix.lower()
        )
        shutil.copyfile(options.lyrics_path, lyrics_source)
        lyrics_source.chmod(0o600)
        manifest["source"]["lyrics_input_path"] = lyrics_source.relative_to(project_dir).as_posix()
        manifest["source"]["lyrics_input_sha256"] = sha256_file(lyrics_source)
    manifest["settings"].update(
        separation_model=options.model,
        language=options.language,
        whisper_model=options.whisper_model,
        device=options.device,
        max_duration=options.max_duration,
        tuning=list(options.tuning),
        max_fret=options.max_fret,
    )
    save_manifest(project_dir, manifest)
    return Pipeline(project_dir, manifest, options, progress).run()


def list_projects() -> list[tuple[Path, ProjectManifest]]:
    result: list[tuple[Path, ProjectManifest]] = []
    paths = projects_home().iterdir()
    for path in sorted(paths, key=lambda item: item.stat().st_mtime, reverse=True):
        if not path.is_dir():
            continue
        try:
            result.append((path, load_manifest(path)))
        except PlayalongError:
            continue
    return result
