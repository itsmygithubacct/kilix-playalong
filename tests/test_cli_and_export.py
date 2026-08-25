from __future__ import annotations

import json
from pathlib import Path

import pytest

from kilix_playalong import cli
from kilix_playalong.cli import build_parser, main
from kilix_playalong.errors import PlayalongError
from kilix_playalong.export import render_printable
from kilix_playalong.lyrics import write_lyrics
from kilix_playalong.options_registry import build_options_document
from kilix_playalong.optionspec import OPTIONS_SCHEMA
from kilix_playalong.state import new_manifest
from kilix_playalong.tablature import write_tab
from kilix_playalong.types import ProjectManifest


def test_create_requires_explicit_rights_confirmation(capsys: object) -> None:
    assert main(["create", "https://youtu.be/abcdef12345"]) == 2
    captured = capsys.readouterr()
    assert "--i-have-rights" in captured.err


def test_printable_export_escapes_user_text(tmp_path: Path) -> None:
    lyrics = tmp_path / "lyrics.json"
    tab = tmp_path / "tab.json"
    output = tmp_path / "print.html"
    write_lyrics(
        lyrics,
        [
            {
                "start": 0.0,
                "end": 1.0,
                "text": "<script>alert(1)</script>",
                "words": [],
            }
        ],
        source="fixture",
        language="en",
    )
    write_tab(
        tab,
        [{"start": 0.0, "end": 1.0, "positions": [{"string": 0, "fret": 0, "pitch": 40}]}],
        source_midi="midi/guitar.mid",
    )
    render_printable(
        output,
        title="<img src=x onerror=alert(1)>",
        artist="A & B",
        lyrics_path=lyrics,
        tab_path=tab,
    )
    document = output.read_text()
    assert "<img src=x" not in document
    assert "<script>alert(1)</script>" not in document
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in document
    assert "A &amp; B" in document


def test_doctor_json_is_machine_readable(capsys: object) -> None:
    result = main(["doctor", "--json"])
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert result in {0, 1}
    assert report["schema"] == "kilix.playalong.doctor/v1"
    assert report["packages"]["yt-dlp"]


# --------------------------------------------------------------------------- #
# The CLI takes its defaults, and its whole vocabulary, from the intake document
# --------------------------------------------------------------------------- #

#: Every option id the document publishes, paired with the argparse attribute that
#: carries it. `options_registry.test_cli_defaults_agree_with_the_document` checks the
#: half of this table that existed before the source union; the rows it does not know
#: about are exactly the ones this release added, which is where the gap would be.
_FLAGS = {
    "language": "language",
    "model": "model",
    "whisper_model": "whisper_model",
    "audio_source": "audio_source",
    "align_supplied_text": "align_supplied_text",
    "lyrics_source": "lyrics_source",
    "lyrics_path": "lyrics",
    "device": "device",
    "tuning": "tuning",
    "max_fret": "max_fret",
    "title": "title",
    "artist": "artist",
    "allow_model_downloads": "allow_model_downloads",
    "rights_confirmed": "i_have_rights",
}

_URL = "https://www.youtube.com/watch?v=abcdefghijk"


def test_every_backend_option_has_a_flag_that_defaults_to_the_document() -> None:
    """`--help` and the intake screens read one description of a default, or they drift.

    The table is checked in both directions. Every option the document publishes must
    reach a flag -- `source_path` and `url` excepted, which are the positional argument --
    and every flag must default to what the document says, so that adding an option to the
    registry without wiring it here fails instead of shipping a control the CLI cannot set.
    """
    document = build_options_document()
    arguments = build_parser().parse_args(["create", _URL])
    described = set(document.defaults()) - {"url", "source_path", "max_duration"}
    assert described == set(_FLAGS), "an option exists that no flag can set"
    for option_id, attribute in _FLAGS.items():
        assert getattr(arguments, attribute) == cli._default(document, option_id), option_id
    assert arguments.max_duration_minutes * 60 == document.defaults()["max_duration"]


def test_resume_help_says_what_leaving_each_flag_out_means(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """On a resume every default is None, and argparse goes quiet on the one boolean flag.

    `--align-supplied-text` is this parser's only `BooleanOptionalAction`. Argparse writes
    its own "(default: ...)" only when the default is not None, which is true on `create`
    and false on every flag of `resume` -- so without `_describe` filling it in, this is
    the single line in `resume --help` that never says what leaving it out does, while the
    twelve flags around it all say "keep what the project recorded".
    """
    with pytest.raises(SystemExit):
        main(["resume", "--help"])
    resuming = " ".join(capsys.readouterr().out.split())
    assert "which drifts badly. (default: keep what the project recorded)" in resuming
    # Every default line on a resume names the project's own answer -- eleven flags say
    # "keep what the project recorded", the positional says "the source the project
    # recorded" -- so none of them quotes a fresh project's value.
    assert resuming.count("(default:") == resuming.count("recorded)") == 12

    with pytest.raises(SystemExit):
        main(["create", "--help"])
    creating = " ".join(capsys.readouterr().out.split())
    assert "which drifts badly. (default: True)" in creating, "argparse's own sentence"
    assert "keep what the project recorded" not in creating


def test_the_positional_source_takes_either_arm_of_the_union(tmp_path: Path) -> None:
    """One box for a link and for a path, because that is how both get pasted."""
    document = build_options_document()
    song = tmp_path / "song.mp3"
    song.write_bytes(b"not really media, and this never reaches a provider")

    linked = cli._pipeline_options(build_parser().parse_args(["create", _URL]), document)
    assert linked.url == _URL and linked.source_path is None

    local = cli._pipeline_options(build_parser().parse_args(["create", str(song)]), document)
    assert local.source_path == song and local.url == ""

    explicit = cli._pipeline_options(
        build_parser().parse_args(["create", "--file", str(song)]), document
    )
    assert explicit.source_path == song and explicit.url == ""


def test_a_source_that_reads_both_ways_is_refused_with_the_way_out(capsys: object) -> None:
    """`source.parse_source` never guesses, and the CLI must not turn its refusal into a crash."""
    assert main(["create", "foo.com/bar", "--i-have-rights"]) == 2
    captured = capsys.readouterr()
    assert "./" in captured.err and "https://" in captured.err


def test_naming_a_source_twice_is_refused(tmp_path: Path, capsys: object) -> None:
    assert main(["create", _URL, "--file", str(tmp_path / "song.mp3"), "--i-have-rights"]) == 2
    captured = capsys.readouterr()
    assert "not both" in captured.err


def test_creating_without_a_source_says_what_is_missing(capsys: object) -> None:
    assert main(["create", "--i-have-rights"]) == 2
    captured = capsys.readouterr()
    assert "source is required" in captured.err


def _manifest(kind: str, **source: object) -> ProjectManifest:
    manifest = new_manifest("song-fixture0001", url_sha256="0" * 64, rights_statement="fixture")
    manifest["source"].update(kind=kind, **source)
    return manifest


def test_a_file_project_resumes_without_being_told_where_the_file_was() -> None:
    """The copy is why: a resume that demanded the library back would defeat it.

    The URL arm is the exception and stays one -- `youtube.download` cannot re-fetch
    without the link, so the project hands it back.
    """
    arguments = build_parser().parse_args(["resume", "song-fixture0001"])
    assert cli._resumed_source(arguments, _manifest("file", path="/music/song.mp3")) == {}
    assert cli._resumed_source(arguments, _manifest("youtube", url=_URL)) == {"url": _URL}


def test_a_legacy_project_with_no_recorded_link_asks_for_one() -> None:
    arguments = build_parser().parse_args(["resume", "song-fixture0001"])
    with pytest.raises(PlayalongError, match="needs its source link"):
        cli._resumed_source(arguments, _manifest("youtube"))


def test_the_options_subcommand_prints_the_document_as_json(capsys: object) -> None:
    """Both surfaces and any script need this; printing it is cheaper than reimplementing it."""
    assert main(["options"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["schema"] == OPTIONS_SCHEMA
    printed = {option["id"] for group in payload["groups"] for option in group["options"]}
    assert printed == set(build_options_document().defaults())
    for group in payload["groups"]:
        for option in group["options"]:
            assert option["available"] == (option["unavailable_reason"] is None)
