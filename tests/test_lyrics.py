from __future__ import annotations

import json
from pathlib import Path

import pytest

from kilix_playalong import LYRICS_SCHEMA
from kilix_playalong.errors import InvalidInputError
from kilix_playalong.lyrics import choose_subtitle, load_lyrics, write_lyrics


def test_lrc_parsing_orders_cues_and_estimates_words(tmp_path: Path) -> None:
    source = tmp_path / "song.lrc"
    source.write_text("[00:03.00]Second line\n[00:01.00]First <b>line</b>\n")
    cues, provider, language = load_lyrics(source, duration=10)
    assert provider == "imported-lrc"
    assert language == "unknown"
    assert [cue["text"] for cue in cues] == ["First line", "Second line"]
    assert cues[0]["start"] == 1
    assert cues[0]["end"] == 3
    assert [word["text"] for word in cues[0]["words"]] == ["First", "line"]


def test_srt_and_plain_text_are_bounded_by_duration(tmp_path: Path) -> None:
    srt = tmp_path / "song.srt"
    srt.write_text(
        "1\n00:00:01,000 --> 00:00:02,500\nHello &amp; goodbye\n\n"
        "2\n00:00:03,000 --> 00:00:08,000\nLast line\n"
    )
    cues, source, _language = load_lyrics(srt, duration=4)
    assert source == "imported-timed-text"
    assert cues[0]["text"] == "Hello & goodbye"
    assert cues[-1]["end"] == 4

    plain = tmp_path / "song.txt"
    plain.write_text("Line one\n\nLine two\n")
    cues, source, _language = load_lyrics(plain, duration=8)
    assert source == "imported-plain-estimated"
    assert [(cue["start"], cue["end"]) for cue in cues] == [(0.0, 4.0), (4.0, 8.0)]


def test_lyrics_json_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "lyrics.json"
    original = [{"start": 0.0, "end": 1.0, "text": "Hi", "words": []}]
    write_lyrics(target, original, source="test", language="en")
    document = json.loads(target.read_text())
    assert document["schema"] == LYRICS_SCHEMA
    cues, source, language = load_lyrics(target, duration=3)
    assert cues[0]["text"] == "Hi"
    assert source == "test"
    assert language == "en"


def test_invalid_or_empty_lyrics_are_rejected(tmp_path: Path) -> None:
    empty = tmp_path / "empty.txt"
    empty.write_text("\n")
    with pytest.raises(InvalidInputError, match="no usable"):
        load_lyrics(empty, duration=4)

    malformed = tmp_path / "bad.json"
    malformed.write_text('{"schema": "wrong"}')
    with pytest.raises(InvalidInputError, match="unsupported"):
        load_lyrics(malformed, duration=4)

    malformed.write_text("{")
    with pytest.raises(InvalidInputError, match="malformed"):
        load_lyrics(malformed, duration=4)

    malformed.write_text(
        json.dumps({"schema": LYRICS_SCHEMA, "cues": [{"start": "soon", "text": 3}]})
    )
    with pytest.raises(InvalidInputError, match="invalid cue"):
        load_lyrics(malformed, duration=4)


def test_subtitle_choice_prefers_requested_language(tmp_path: Path) -> None:
    english = tmp_path / "source.en.vtt"
    spanish = tmp_path / "source.es.vtt"
    chat = tmp_path / "source.es.live_chat.vtt"
    assert choose_subtitle([english, chat, spanish], "es-MX") == spanish
    assert choose_subtitle([], "auto") is None
