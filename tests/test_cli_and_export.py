from __future__ import annotations

import json
from pathlib import Path

from kilix_playalong.cli import main
from kilix_playalong.export import render_printable
from kilix_playalong.lyrics import write_lyrics
from kilix_playalong.tablature import write_tab


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
