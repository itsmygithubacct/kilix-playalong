from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from test_pipeline import _install_providers
from test_source import make_tone, requires_ffmpeg

from kilix_playalong import cli, songbook
from kilix_playalong.errors import InvalidInputError, ProviderFailedError
from kilix_playalong.lyrics import write_lyrics
from kilix_playalong.pipeline import PipelineOptions, run_new
from kilix_playalong.tablature import write_tab


def _inputs(tmp_path: Path) -> tuple[Path, Path]:
    tab, lyrics = tmp_path / "tab.json", tmp_path / "lyrics.json"
    write_tab(
        tab,
        [{"start": 0.0, "end": 1.0, "positions": [{"string": 0, "fret": 0, "pitch": 38}]}],
        tuning=(38, 45, 50, 55, 59, 64),
        max_fret=22,
        source_midi="midi/guitar.mid",
        omitted_notes=7,
    )
    write_lyrics(
        lyrics,
        [{"start": 0.0, "end": 1.0, "text": "Original test words", "words": []}],
        source="fixture",
        language="en",
    )
    return tab, lyrics


def _data(tmp_path: Path, **kwargs: object) -> dict:
    tab, lyrics = _inputs(tmp_path)
    return songbook.songbook_data(
        project_id="synthetic",
        title="Another Song",
        artist="An Artist",
        duration=6,
        tab_path=tab,
        lyrics_path=lyrics,
        **kwargs,
    )


def _embedded(page: Path) -> dict:
    match = re.search(
        r'<script id="song-data" type="application/json">(.*?)</script>',
        page.read_text(),
        re.DOTALL,
    )
    assert match
    return json.loads(match[1])


def test_songbook_uses_project_metadata_and_tuning(tmp_path: Path) -> None:
    data = _data(tmp_path)
    output = songbook.render_songbook(tmp_path / "book.html", data)
    assert _embedded(output) == json.loads(json.dumps(data))
    assert "Another Song · Guitar Songbook" in output.read_text()
    assert "D A D G B e" in output.read_text()
    assert "omitted 7 detected notes" in output.read_text()
    assert data["events"][0]["positions"][0]["note"] == "D"
    assert data["sections"] == [{"id": "section-1", "label": "Song", "kind": "section", "start": 0}]
    assert data["audio"] == {}


def test_html_and_marker_looking_text_remain_literal(tmp_path: Path) -> None:
    data = _data(tmp_path)
    data["title"] = "<img src=x onerror=alert(1)> /* SONGBOOK_DATA */ {{ARTIST}}"
    data["artist"] = "A & B"
    data["cues"][0]["text"] = "</script><script>alert(1)</script> /* SONGBOOK_JS */"
    page = songbook.render_songbook(tmp_path / "book.html", data)
    assert _embedded(page)["cues"] == data["cues"]
    assert _embedded(page)["title"] == data["title"]
    assert "<img src=x" not in page.read_text()
    assert "<script>alert(1)</script>" not in page.read_text()
    assert "A &amp; B" in page.read_text()


@pytest.mark.parametrize(
    "sections",
    [
        [],
        [{"label": "Verse", "start": 1}],
        [{"id": "audio", "label": "Verse", "start": 0}],
        [{"id": "song", "label": "Verse", "start": 0}],
        [{"id": "lyric-editor", "label": "Verse", "start": 0}],
        [{"id": "row-0", "label": "Verse", "start": 0}],
        [{"label": "Verse", "start": float("nan")}],
        [{"label": "Verse", "start": True}],
        [{"label": "Verse", "start": 0}, {"label": "Chorus", "start": 0}],
        [
            {"id": "verse", "label": "Verse", "start": 0},
            {"id": "verse-title", "label": "Chorus", "start": 2},
        ],
        [{"label": "Verse", "start": 0, "kind": "invalid"}],
    ],
)
def test_invalid_sections_are_rejected(sections: object) -> None:
    with pytest.raises(InvalidInputError):
        songbook.validate_sections(sections, 6)


@pytest.mark.parametrize(
    "changes",
    [
        {"start": float("nan")},
        {"end": float("inf")},
        {"end": 0},
        {"end": 7},
        {"positions": []},
        {"positions": [{"string": 0, "fret": 0, "pitch": 40}]},
        {"positions": [{"string": 0, "fret": 0, "pitch": 38}] * 2},
    ],
)
def test_invalid_notes_are_rejected(tmp_path: Path, changes: dict) -> None:
    tab, lyrics = _inputs(tmp_path)
    raw = json.loads(tab.read_text())
    raw["events"][0].update(changes)
    tab.write_text(json.dumps(raw))
    with pytest.raises(InvalidInputError):
        songbook.songbook_data(
            project_id="test", title="Test", artist="", duration=6, tab_path=tab, lyrics_path=lyrics
        )


def test_corrections_revision_changes_with_transcript_or_sections(tmp_path: Path) -> None:
    before = _data(tmp_path)
    tab, lyrics = _inputs(tmp_path)
    raw = json.loads(lyrics.read_text())
    raw["cues"][0]["text"] = "A changed line"
    lyrics.write_text(json.dumps(raw))
    after = songbook.songbook_data(
        project_id="synthetic",
        title="Another Song",
        artist="An Artist",
        duration=6,
        tab_path=tab,
        lyrics_path=lyrics,
    )
    assert before["revision"] != after["revision"]
    arrangement = tmp_path / "arrangement.json"
    arrangement.write_text(json.dumps({"sections": [{"label": "Verse", "start": 0}]}))
    arranged = _data(tmp_path, arrangement_path=arrangement)
    assert before["revision"] != arranged["revision"]


@pytest.mark.parametrize(
    ("pitches", "expected"),
    [
        ([48, 52, 55], "C"),
        ([52, 55, 60], "C/E"),
        ([45, 48, 52], "Am"),
        ([40, 47], "E5"),
        ([40], ""),
        ([40, 41, 42], ""),
    ],
)
def test_chord_labels_only_claim_exact_matches(pitches: list[int], expected: str) -> None:
    assert songbook.chord_name(pitches) == expected


def test_cli_exports_existing_project_without_running_providers(
    tmp_path: Path,
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, _ = _install_providers(monkeypatch)
    project, _ = run_new(
        PipelineOptions(
            url="https://youtu.be/abcdef12345", rights_confirmed=True, whisper_model="medium.en"
        )
    )
    before = calls.copy()
    output = tmp_path / "book"
    assert cli.main(["songbook", str(project), "--no-audio", "--output", str(output)]) == 0
    assert _embedded(output / "index.html")["title"] == "Synthetic Song"
    assert calls == before
    arrangement = tmp_path / "bad.json"
    arrangement.write_text('{"sections":[]}')
    prior_page = (output / "index.html").read_bytes()
    assert (
        cli.main(
            [
                "songbook",
                str(project),
                "--no-audio",
                "--output",
                str(output),
                "--arrangement",
                str(arrangement),
            ]
        )
        == 2
    )
    assert (output / "index.html").read_bytes() == prior_page


@requires_ffmpeg
def test_audio_export_and_failed_replacement_keep_a_working_book(
    tmp_path: Path,
    private_homes: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_providers(monkeypatch)
    project, _ = run_new(
        PipelineOptions(
            url="https://youtu.be/abcdef12345", rights_confirmed=True, whisper_model="medium.en"
        )
    )
    make_tone(project / "media/normalized.wav", seconds=6)
    make_tone(project / "stems/guitar.wav", seconds=6)
    output = tmp_path / "book"
    page = songbook.export_songbook(project, output)
    data = _embedded(page)
    assert set(data["audio"]) == {"mix", "guitar"}
    assert all((output / name).stat().st_size > 1000 for name in data["audio"].values())
    original = {file.name: file.read_bytes() for file in output.iterdir()}
    (project / "stems/guitar.wav").write_bytes(b"broken audio")
    with pytest.raises(ProviderFailedError):
        songbook.export_songbook(project, output)
    assert {file.name: file.read_bytes() for file in output.iterdir()} == original
