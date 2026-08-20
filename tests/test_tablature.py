from __future__ import annotations

import json
from pathlib import Path

from kilix_playalong import TAB_SCHEMA
from kilix_playalong.midi import Note
from kilix_playalong.tablature import (
    STANDARD_TUNING,
    group_notes,
    infer_fingerings,
    read_tab,
    render_ascii,
    tuning_labels,
    write_tab,
)


def test_note_grouping_and_fingering_are_deterministic() -> None:
    notes = [
        Note(1.0, 1.8, 40, 0.9),
        Note(1.02, 1.7, 47, 0.8),
        Note(2.0, 2.5, 52, 0.95),
    ]
    chords = group_notes(notes)
    assert [chord.pitches for chord in chords] == [(40, 47), (52,)]

    first, omitted = infer_fingerings(notes)
    second, repeated_omitted = infer_fingerings(list(reversed(notes)))
    assert first == second
    assert omitted == repeated_omitted == 0
    assert first[0]["positions"] == [
        {"string": 0, "fret": 0, "pitch": 40},
        {"string": 1, "fret": 2, "pitch": 47},
    ]


def test_unplayable_notes_are_omitted() -> None:
    events, omitted = infer_fingerings([Note(0, 1, 20)], max_fret=12)
    assert events == []
    assert omitted == 1


def test_tuning_labels_follow_the_selected_open_strings() -> None:
    assert tuning_labels(STANDARD_TUNING) == ("E", "A", "D", "G", "B", "e")
    assert tuning_labels((38, 45, 50, 55, 57, 62)) == ("D", "A", "D", "G", "A", "d")


def test_tab_json_and_ascii_round_trip(tmp_path: Path) -> None:
    events, _omitted = infer_fingerings([Note(0, 1, 64), Note(1, 2, 65)])
    target = tmp_path / "tab.json"
    write_tab(
        target,
        events,
        source_midi="midi/guitar.mid",
        tuning=STANDARD_TUNING,
        max_fret=20,
    )
    document = json.loads(target.read_text())
    assert document["schema"] == TAB_SCHEMA
    loaded, metadata = read_tab(target)
    assert loaded == events
    assert metadata["tuning"]["midi"] == list(STANDARD_TUNING)

    rendered = render_ascii(events, title="A title", artist="An artist")
    assert rendered.startswith("# A title\n# An artist")
    assert "e|" in rendered
    assert "E|" in rendered
