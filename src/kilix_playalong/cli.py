"""Command-line entry point for creation, recovery, playback, and diagnostics."""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from kilix_state import default_library

from . import __version__
from .errors import PlayalongError, RightsConfirmationRequired
from .paths import resolve_project
from .pipeline import PipelineOptions, list_projects, resume, run_new
from .providers import separation, transcription
from .server import PlayalongServer
from .state import load_manifest
from .tablature import STANDARD_TUNING
from .types import ProjectManifest

TUNINGS = {
    "standard": STANDARD_TUNING,
    "drop-d": (38, 45, 50, 55, 59, 64),
    "dadgad": (38, 45, 50, 55, 57, 62),
}


def _progress(name: str, status: str) -> None:
    print(f"[{status:7}] {name}", flush=True)


def _stored_text(settings: dict[str, object], name: str, fallback: str) -> str:
    value = settings.get(name)
    return value if isinstance(value, str) else fallback


def _stored_number(settings: dict[str, object], name: str, fallback: float) -> float:
    value = settings.get(name)
    return float(value) if isinstance(value, int | float) else fallback


def _stored_tuning(settings: dict[str, object]) -> tuple[int, ...]:
    value = settings.get("tuning")
    if (
        isinstance(value, list)
        and len(value) == 6
        and all(isinstance(item, int) and 0 <= item <= 127 for item in value)
    ):
        return tuple(value)
    return STANDARD_TUNING


def _pipeline_options(
    arguments: argparse.Namespace,
    url: str,
    manifest: ProjectManifest | None = None,
) -> PipelineOptions:
    settings = manifest["settings"] if manifest is not None else {}
    language = arguments.language or _stored_text(settings, "language", "auto")
    model = arguments.model or _stored_text(settings, "separation_model", "htdemucs_6s")
    whisper_model = arguments.whisper_model or _stored_text(
        settings, "whisper_model", transcription.DEFAULT_MODEL
    )
    device = arguments.device or _stored_text(settings, "device", "auto")
    max_duration = (
        arguments.max_duration_minutes * 60
        if arguments.max_duration_minutes is not None
        else _stored_number(settings, "max_duration", 30 * 60)
    )
    max_fret = (
        arguments.max_fret
        if arguments.max_fret is not None
        else int(_stored_number(settings, "max_fret", 20))
    )
    tuning = TUNINGS[arguments.tuning] if arguments.tuning else _stored_tuning(settings)
    return PipelineOptions(
        url=url,
        title=getattr(arguments, "title", None)
        or (manifest["title"] if manifest is not None else ""),
        artist=getattr(arguments, "artist", None)
        or (manifest["artist"] if manifest is not None else ""),
        language=language,
        lyrics_path=arguments.lyrics,
        model=model,
        whisper_model=whisper_model,
        device=device,
        max_duration=max_duration,
        max_fret=max_fret,
        tuning=tuning,
        allow_model_downloads=arguments.allow_model_downloads,
        rights_confirmed=manifest is not None or bool(getattr(arguments, "i_have_rights", False)),
    )


def command_create(arguments: argparse.Namespace) -> int:
    if not arguments.i_have_rights:
        raise RightsConfirmationRequired(
            "pass --i-have-rights after confirming permission to process the media and lyrics"
        )
    options = _pipeline_options(arguments, arguments.url)
    project_dir, manifest = run_new(options, progress=_progress)
    print(f"\nCreated {manifest['title'] or manifest['id']}")
    print(f"Project: {project_dir}")
    print(f"Printable: {project_dir / 'exports' / 'playalong.html'}")
    print(f"Play: uv run kilix-playalong serve {manifest['id']}")
    return 0


def command_resume(arguments: argparse.Namespace) -> int:
    project_dir = resolve_project(arguments.project)
    manifest = load_manifest(project_dir)
    stored_url = manifest["source"].get("url")
    url = arguments.url or (stored_url if isinstance(stored_url, str) else "")
    if not url:
        raise PlayalongError("this project needs --url to resume its acquisition stage")
    options = _pipeline_options(arguments, url, manifest)
    resumed = resume(project_dir, options, progress=_progress)
    print(f"\nReady: {resumed['title'] or resumed['id']}")
    return 0


def command_list(arguments: argparse.Namespace) -> int:
    projects = [
        {
            "id": manifest["id"],
            "title": manifest["title"],
            "artist": manifest["artist"],
            "updated_at": manifest["updated_at"],
            "path": str(path),
            "ready": manifest["stages"]["export"]["status"] == "done",
        }
        for path, manifest in list_projects()
    ]
    if arguments.json:
        print(json.dumps({"schema": "kilix.playalong.library/v1", "projects": projects}, indent=2))
    elif not projects:
        print("No projects yet.")
    else:
        for project in projects:
            state = "ready" if project["ready"] else "incomplete"
            byline = f" — {project['artist']}" if project["artist"] else ""
            print(f"{project['id']}  [{state}]  {project['title'] or 'Untitled'}{byline}")
    return 0


def command_show(arguments: argparse.Namespace) -> int:
    project_dir = resolve_project(arguments.project)
    manifest = load_manifest(project_dir)
    if arguments.json:
        safe = dict(manifest)
        safe["source"] = {key: value for key, value in manifest["source"].items() if key != "url"}
        print(json.dumps(safe, indent=2, ensure_ascii=False))
    else:
        print(f"{manifest['title'] or 'Untitled'}")
        if manifest["artist"]:
            print(manifest["artist"])
        print(f"Project: {project_dir}")
        for name, stage in manifest["stages"].items():
            print(f"  {name:20} {stage['status']}")
    return 0


def command_serve(arguments: argparse.Namespace) -> int:
    project_dir = resolve_project(arguments.project)
    server = PlayalongServer(project_dir, port=arguments.port)
    print(f"Serving only on this machine: {server.url}", flush=True)
    server.serve(open_browser=not arguments.no_open)
    return 0


def _version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def command_doctor(arguments: argparse.Namespace) -> int:
    tools = {name: shutil.which(name) for name in ("ffmpeg", "ffprobe")}
    packages = {
        name: _version(name)
        for name in ("yt-dlp", "mido", "kilix-state-py", "basic-pitch", "demucs", "faster-whisper")
    }
    report = {
        "schema": "kilix.playalong.doctor/v1",
        "version": __version__,
        "python": sys.version.split()[0],
        "tools": tools,
        "packages": packages,
        "providers": {
            "separation": importlib.util.find_spec("demucs") is not None,
            "guitar_transcription": importlib.util.find_spec("basic_pitch") is not None,
            "lyrics_transcription": importlib.util.find_spec("faster_whisper") is not None,
        },
        "kilix_state_library": default_library().path,
        "ready": all(tools.values()) and packages["yt-dlp"] is not None,
        "full_pipeline_ready": all(tools.values())
        and all(packages[name] is not None for name in ("basic-pitch", "demucs", "faster-whisper")),
    }
    if arguments.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Kilix Playalong {report['version']} · Python {report['python']}")
        for name, path in tools.items():
            print(f"  {name:24} {path or 'missing'}")
        for name, version in packages.items():
            print(f"  {name:24} {version or 'not installed'}")
        pipeline_status = "ready" if report["full_pipeline_ready"] else "needs uv sync --all-extras"
        print(f"  {'full pipeline':24} {pipeline_status}")
    return 0 if report["ready"] else 1


def _common_pipeline_options(parser: argparse.ArgumentParser, *, resume: bool = False) -> None:
    defaults: dict[str, object] = {
        "language": None if resume else "auto",
        "model": None if resume else "htdemucs_6s",
        "whisper_model": None if resume else transcription.DEFAULT_MODEL,
        "device": None if resume else "auto",
        "tuning": None if resume else "standard",
        "max_fret": None if resume else 20,
        "max_duration_minutes": None if resume else 30,
    }
    parser.add_argument("--lyrics", type=Path, help="LRC, SRT, VTT, JSON, or plain-text lyrics")
    parser.add_argument(
        "--language",
        default=defaults["language"],
        help="caption/Whisper language (default: auto)",
    )
    parser.add_argument(
        "--model",
        choices=tuple(sorted(separation.SUPPORTED_MODELS)),
        default=defaults["model"],
        help="Demucs model (default: htdemucs_6s)",
    )
    parser.add_argument(
        "--whisper-model",
        choices=tuple(sorted(transcription.MODEL_CHOICES)),
        default=defaults["whisper_model"],
        help="lyrics model (default: auto chooses the strongest practical option)",
    )
    parser.add_argument(
        "--device",
        choices=("auto", "cpu", "cuda"),
        default=defaults["device"],
    )
    parser.add_argument("--tuning", choices=tuple(TUNINGS), default=defaults["tuning"])
    parser.add_argument(
        "--max-fret",
        type=int,
        choices=range(12, 31),
        default=defaults["max_fret"],
    )
    parser.add_argument(
        "--max-duration-minutes",
        type=float,
        default=defaults["max_duration_minutes"],
    )
    parser.add_argument(
        "--allow-model-downloads",
        action="store_true",
        help="allow missing model weights to be downloaded for this explicit run",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kilix-playalong")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="create a play-along project from a YouTube URL")
    create.add_argument("url")
    create.add_argument("--title", default="")
    create.add_argument("--artist", default="")
    create.add_argument(
        "--i-have-rights",
        action="store_true",
        help="confirm permission to process",
    )
    _common_pipeline_options(create)
    create.set_defaults(function=command_create)

    resume_parser = commands.add_parser("resume", help="resume a verified incomplete project")
    resume_parser.add_argument("project")
    resume_parser.add_argument("--url", help="needed only for legacy/incomplete acquisition")
    _common_pipeline_options(resume_parser, resume=True)
    resume_parser.set_defaults(function=command_resume)

    list_parser = commands.add_parser("list", help="list private local projects")
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(function=command_list)

    show = commands.add_parser("show", help="show stage and artifact status")
    show.add_argument("project")
    show.add_argument("--json", action="store_true")
    show.set_defaults(function=command_show)

    serve = commands.add_parser("serve", help="open the timed multi-track player")
    serve.add_argument("project")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--no-open", action="store_true")
    serve.set_defaults(function=command_serve)

    doctor = commands.add_parser("doctor", help="inspect the locked runtime and optional providers")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(function=command_doctor)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        return int(arguments.function(arguments))
    except PlayalongError as error:
        print(f"kilix-playalong: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
