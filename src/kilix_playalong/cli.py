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
from .errors import InvalidInputError, PlayalongError, RightsConfirmationRequired
from .options_registry import TUNINGS, build_options_document, tuning_pitches
from .optionspec import OptionsDocument
from .paths import resolve_project
from .pipeline import (
    LYRIC_SOURCE_CHOICES,
    PipelineOptions,
    list_projects,
    resume,
    run_new,
)
from .providers import separation, transcription
from .server import PlayalongServer
from .source import file_source, parse_source
from .state import load_manifest
from .tablature import STANDARD_TUNING
from .types import ProjectManifest

#: Options whose "let this machine decide" value the CLI keeps even when the intake
#: document has preselected a concrete one in its place.
#:
#: `options_registry` preselects a named transcription model when `auto` cannot resolve
#: here, because a form control whose preselected choice is greyed out is a dead end. A
#: flag has no greyed-out state, and the substitution is not free: an explicitly named
#: model is comparable only with itself in `pipeline._whisper_keys`, so a project created
#: with one is pinned to it and never picks up a better model when the machine grows
#: weights or a GPU. `auto` is always submittable and always means the adaptive thing, so
#: the flag keeps it and only the form substitutes. `test_options_registry
#: .test_cli_defaults_agree_with_the_document` is where the two are held together: it
#: permits this divergence only for a default the document itself marks as resolved.
_ADAPTIVE_DEFAULTS = {"whisper_model": transcription.AUTO_MODEL}


def _progress(name: str, status: str, detail: str) -> None:
    """Render one stage transition, including what the stage had to say about itself.

    The detail is the reason this takes three arguments: without it a user whose forced
    alignment was rejected sees ``[done   ] lyrics`` and nothing else, and the only way
    to find out is `show --json` and reading the manifest by hand. See
    `pipeline.ProgressCallback` for what may be in it and where its redaction comes from.
    """
    line = f"[{status:7}] {name}"
    print(f"{line}: {detail}" if detail else line, flush=True)


def _default(document: OptionsDocument, option_id: str) -> object:
    """One option's default, taken from the intake document rather than from a constant.

    This is the whole reason `--help` and the two intake screens cannot disagree about what
    an unspecified option means: there is one description of a default and every surface
    reads it. `_ADAPTIVE_DEFAULTS` is the single, named exception.
    """
    value = document.defaults()[option_id]
    adaptive = _ADAPTIVE_DEFAULTS.get(option_id)
    return adaptive if adaptive is not None and value != adaptive else value


def _stored_text(settings: dict[str, object], name: str, fallback: object) -> str:
    value = settings.get(name)
    return value if isinstance(value, str) else str(fallback)


def _stored_number(settings: dict[str, object], name: str, fallback: object) -> float:
    value = settings.get(name)
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return float(fallback) if isinstance(fallback, int | float) else 0.0


def _stored_flag(settings: dict[str, object], name: str, fallback: object) -> bool:
    value = settings.get(name)
    return value if isinstance(value, bool) else bool(fallback)


def _stored_tuning(settings: dict[str, object], fallback: object) -> tuple[int, ...]:
    value = settings.get("tuning")
    if (
        isinstance(value, list)
        and len(value) == 6
        and all(isinstance(item, int) and 0 <= item <= 127 for item in value)
    ):
        return tuple(value)
    try:
        return tuning_pitches(fallback)
    except PlayalongError:
        return STANDARD_TUNING


def _given_source(arguments: argparse.Namespace) -> dict[str, object]:
    """Read the positional source and `--file` as the two fields of the source union.

    The positional takes either arm, because that is how people actually paste things: a
    link from a browser and a path from a file manager go in the same place, and
    `source.parse_source` refuses anything it could read both ways rather than guessing.
    `--file` is the way to say "this is a path" about a string that reads like a host --
    `./my.music/song.mp3` is the real example -- and about a file whose name begins with
    a dash.
    """
    given = getattr(arguments, "source", None)
    named = getattr(arguments, "file", None)
    if given and named is not None:
        raise InvalidInputError("give one source: a link or --file, not both")
    if named is not None:
        return {"source_path": file_source(named).path}
    if not given:
        return {}
    spec = parse_source(given)
    if spec.kind == "file":
        return {"source_path": spec.path}
    return {"url": spec.url}


def _resumed_source(
    arguments: argparse.Namespace,
    manifest: ProjectManifest,
) -> dict[str, object]:
    """What a resume should name as its source, which is usually nothing at all.

    A project records its source's identity when it is created, so a resume does not have
    to be told again -- and for the file arm it must not have to be, because the point of
    copying the media in was that the library may be gone. The URL arm is the exception
    only because `youtube.download` needs the URL itself to re-fetch anything, so it is
    read back from the project.
    """
    given = _given_source(arguments)
    if given:
        return given
    if manifest["source"].get("kind") == "file":
        return {}
    stored = manifest["source"].get("url")
    if isinstance(stored, str) and stored:
        # Stripped: a project made before `validate_url` refused unprintable
        # characters can hold "\rhttps://..." from a pasted link, and handing
        # that back verbatim fails the gate and makes the project unresumable.
        # Stripping keeps the property the gate exists for -- `urlsplit` drops a
        # newline when parsing but not from the string handed on, so the gate
        # must see exactly what a provider will get, and it does: the stripped
        # value is what is validated and what is used. The fingerprint still
        # matches because `_verify_resume_source` compares the recorded URL and
        # this one with surrounding whitespace removed.
        return {"url": stored.strip()}
    if manifest["stages"]["download"]["status"] == "done":
        return {}
    raise InvalidInputError("this project needs its source link to finish acquiring: pass it")


def _pipeline_options(
    arguments: argparse.Namespace,
    document: OptionsDocument,
    manifest: ProjectManifest | None = None,
) -> PipelineOptions:
    """Build the backend's options from the flags, the project, and the document.

    Three layers, outermost first: what this invocation said, what the project recorded
    last time, and what the intake document says the option means here. A resume leaves
    every flag defaulting to None so that the middle layer is reachable at all -- an
    unspecified `--device` on a resume has to mean "as before", not "as a fresh project
    would".
    """
    settings = manifest["settings"] if manifest is not None else {}
    source = (
        _resumed_source(arguments, manifest) if manifest is not None else _given_source(arguments)
    )
    if manifest is None and not source:
        raise InvalidInputError("a source is required: a YouTube link or a local file path")

    max_duration_minutes = getattr(arguments, "max_duration_minutes", None)
    return PipelineOptions(
        url=str(source.get("url", "")),
        source_path=source.get("source_path"),  # type: ignore[arg-type]
        title=getattr(arguments, "title", "") or "",
        artist=getattr(arguments, "artist", "") or "",
        lyrics_source=str(
            arguments.lyrics_source
            if arguments.lyrics_source is not None
            else _stored_text(settings, "lyrics_source", _default(document, "lyrics_source"))
        ),
        lyrics_path=arguments.lyrics,
        language=str(
            arguments.language
            if arguments.language is not None
            else _stored_text(settings, "language", _default(document, "language"))
        ),
        model=str(
            arguments.model
            if arguments.model is not None
            else _stored_text(settings, "separation_model", _default(document, "model"))
        ),
        whisper_model=str(
            arguments.whisper_model
            if arguments.whisper_model is not None
            else _stored_text(settings, "whisper_model", _default(document, "whisper_model"))
        ),
        audio_source=str(
            arguments.audio_source
            if arguments.audio_source is not None
            else _stored_text(settings, "audio_source", _default(document, "audio_source"))
        ),
        align_supplied_text=bool(
            arguments.align_supplied_text
            if arguments.align_supplied_text is not None
            else _stored_flag(
                settings, "align_supplied_text", _default(document, "align_supplied_text")
            )
        ),
        device=str(
            arguments.device
            if arguments.device is not None
            else _stored_text(settings, "device", _default(document, "device"))
        ),
        max_duration=(
            max_duration_minutes * 60
            if max_duration_minutes is not None
            else _stored_number(settings, "max_duration", _default(document, "max_duration"))
        ),
        max_fret=int(
            arguments.max_fret
            if arguments.max_fret is not None
            else _stored_number(settings, "max_fret", _default(document, "max_fret"))
        ),
        tuning=(
            tuning_pitches(arguments.tuning)
            if arguments.tuning is not None
            else _stored_tuning(settings, _default(document, "tuning"))
        ),
        allow_model_downloads=arguments.allow_model_downloads,
        rights_confirmed=manifest is not None or bool(getattr(arguments, "i_have_rights", False)),
    )


def command_create(arguments: argparse.Namespace) -> int:
    if not arguments.i_have_rights:
        raise RightsConfirmationRequired(
            "pass --i-have-rights after confirming permission to process the media and lyrics"
        )
    options = _pipeline_options(arguments, build_options_document())
    project_dir, manifest = run_new(options, progress=_progress)
    print(f"\nCreated {manifest['title'] or manifest['id']}")
    print(f"Project: {project_dir}")
    print(f"Printable: {project_dir / 'exports' / 'playalong.html'}")
    print(f"Play: uv run kilix-playalong serve {manifest['id']}")
    return 0


def command_resume(arguments: argparse.Namespace) -> int:
    project_dir = resolve_project(arguments.project)
    manifest = load_manifest(project_dir)
    options = _pipeline_options(arguments, build_options_document(), manifest)
    resumed = resume(project_dir, options, progress=_progress)
    print(f"\nReady: {resumed['title'] or resumed['id']}")
    return 0


def command_options(arguments: argparse.Namespace) -> int:
    """Print the intake contract, which is what both surfaces render and this CLI defaults to."""
    document = build_options_document(allow_model_downloads=arguments.allow_model_downloads)
    print(json.dumps(document.as_json(), indent=2, ensure_ascii=False))
    return 0


def command_list(arguments: argparse.Namespace) -> int:
    projects = [
        {
            "id": manifest["id"],
            "title": manifest["title"],
            "artist": manifest["artist"],
            "updated_at": manifest["updated_at"],
            "path": str(path),
            "source": manifest["source"].get("kind", "youtube"),
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
        # The link and the library path are the two things in a manifest that say where
        # the user was; both are stripped, for the same reason and by the same rule.
        safe["source"] = {
            key: value for key, value in manifest["source"].items() if key not in ("url", "path")
        }
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


def _describe(
    document: OptionsDocument,
    option_id: str,
    *,
    resuming: bool,
    append_default: bool = True,
) -> str:
    """Help text that ends in the default the flag will actually use.

    On a resume that is the project's own recorded answer, which is why the two spellings
    differ: printing a fresh project's default beside a flag that will not use it is the
    same disagreement this whole arrangement exists to prevent.

    `append_default` is off where `BooleanOptionalAction` would print the sentence itself
    and the two would read as a duplicate. That is not "always": argparse appends its own
    "(default: ...)" only when the default is neither None nor SUPPRESS, and on a resume
    every flag here defaults to None. So `--align-supplied-text`, the only
    `BooleanOptionalAction` in this parser, passes `append_default=resuming` -- silent on
    `create`, where argparse writes "(default: True)" after it, and spoken on `resume`,
    where argparse writes nothing and this would otherwise be the one line in
    `resume --help` that does not say what leaving the flag out means.
    """
    option = document.option(option_id)
    help_text = option.help if option is not None else ""
    if not append_default:
        return help_text
    if resuming:
        return f"{help_text} (default: keep what the project recorded)".strip()
    return f"{help_text} (default: {_default(document, option_id)})".strip()


def _common_pipeline_options(
    parser: argparse.ArgumentParser,
    document: OptionsDocument,
    *,
    resuming: bool = False,
) -> None:
    """Every option the backend takes, defaulting to what the intake document says.

    On a resume every flag defaults to None so an unspecified one means "as the project
    recorded it"; `_pipeline_options` is where that middle layer is read.
    """

    def default(option_id: str) -> object:
        return None if resuming else _default(document, option_id)

    parser.add_argument(
        "--lyrics",
        type=Path,
        default=None,
        metavar="PATH",
        help=_describe(document, "lyrics_path", resuming=resuming),
    )
    parser.add_argument(
        "--lyrics-source",
        choices=LYRIC_SOURCE_CHOICES,
        default=default("lyrics_source"),
        help=_describe(document, "lyrics_source", resuming=resuming),
    )
    parser.add_argument(
        "--language",
        default=default("language"),
        help=_describe(document, "language", resuming=resuming),
    )
    parser.add_argument(
        "--model",
        choices=tuple(sorted(separation.SUPPORTED_MODELS)),
        default=default("model"),
        help=_describe(document, "model", resuming=resuming),
    )
    parser.add_argument(
        "--whisper-model",
        choices=tuple(sorted(transcription.MODEL_CHOICES)),
        default=default("whisper_model"),
        help=_describe(document, "whisper_model", resuming=resuming),
    )
    parser.add_argument(
        "--audio-source",
        choices=transcription.AUDIO_SOURCE_CHOICES,
        default=default("audio_source"),
        help=_describe(document, "audio_source", resuming=resuming),
    )
    parser.add_argument(
        "--align-supplied-text",
        action=argparse.BooleanOptionalAction,
        default=default("align_supplied_text"),
        help=_describe(document, "align_supplied_text", resuming=resuming, append_default=resuming),
    )
    parser.add_argument(
        "--device",
        choices=("auto", "cpu", "cuda"),
        default=default("device"),
        help=_describe(document, "device", resuming=resuming),
    )
    parser.add_argument(
        "--tuning",
        choices=tuple(TUNINGS),
        default=default("tuning"),
        help=_describe(document, "tuning", resuming=resuming),
    )
    parser.add_argument(
        "--max-fret",
        type=int,
        choices=range(12, 31),
        default=default("max_fret"),
        metavar="{12..30}",
        help=_describe(document, "max_fret", resuming=resuming),
    )
    minutes = float(_default(document, "max_duration")) / 60  # type: ignore[arg-type]
    parser.add_argument(
        "--max-duration-minutes",
        type=float,
        default=None if resuming else minutes,
        help=(
            "longest source accepted, in minutes "
            + ("(default: keep what the project recorded)" if resuming else f"(default: {minutes})")
        ),
    )
    parser.add_argument(
        "--allow-model-downloads",
        action="store_true",
        default=bool(document.defaults()["allow_model_downloads"]),
        help="allow missing model weights to be downloaded for this explicit run",
    )


def _source_arguments(parser: argparse.ArgumentParser, *, resuming: bool) -> None:
    parser.add_argument(
        "source",
        nargs="?",
        default=None,
        help=(
            "a YouTube link or a local audio/video file"
            + (" (default: the source the project recorded)" if resuming else "")
        ),
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=None,
        metavar="PATH",
        help="a local file, said explicitly: use this for a path that reads like a host",
    )


def build_parser() -> argparse.ArgumentParser:
    document = build_options_document()
    parser = argparse.ArgumentParser(prog="kilix-playalong")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="create a play-along project from a link or a file")
    _source_arguments(create, resuming=False)
    create.add_argument(
        "--title",
        default=str(_default(document, "title")),
        help=_describe(document, "title", resuming=False),
    )
    create.add_argument(
        "--artist",
        default=str(_default(document, "artist")),
        help=_describe(document, "artist", resuming=False),
    )
    create.add_argument(
        "--i-have-rights",
        action="store_true",
        default=bool(_default(document, "rights_confirmed")),
        help="confirm permission to process",
    )
    _common_pipeline_options(create, document)
    create.set_defaults(function=command_create)

    resume_parser = commands.add_parser("resume", help="resume a verified incomplete project")
    resume_parser.add_argument("project")
    _source_arguments(resume_parser, resuming=True)
    resume_parser.add_argument("--title", default="", help="rename the project")
    resume_parser.add_argument("--artist", default="", help="re-credit the project")
    _common_pipeline_options(resume_parser, document, resuming=True)
    resume_parser.set_defaults(function=command_resume)

    options_parser = commands.add_parser(
        "options",
        help="print every backend option, its default here, and why one is unavailable",
    )
    options_parser.add_argument(
        "--allow-model-downloads",
        action="store_true",
        help="describe the machine as it would be with downloads permitted",
    )
    options_parser.set_defaults(function=command_options)

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
