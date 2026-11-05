from __future__ import annotations

import json
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("out_of_range", [range(6), range(77, 83)])
@pytest.mark.parametrize("tuning", [STANDARD_TUNING, (38, 45, 50, 55, 59, 64)])
def test_out_of_range_detections_do_not_consume_playable_polyphony(
    out_of_range: range,
    tuning: tuple[int, ...],
) -> None:
    notes = [Note(0, 1, pitch, 0.99) for pitch in out_of_range]
    notes.extend(Note(0, 1, pitch, 0.8) for pitch in tuning)
    events, omitted = infer_fingerings(notes, tuning=tuning, max_fret=12)
    assert len(events) == 1
    assert {p["pitch"] for p in events[0]["positions"]} == set(tuning)
    assert omitted == 6
    assert sum(len(event["positions"]) for event in events) + omitted == len(notes)


def test_filtering_out_of_range_pitches_preserves_onset_group_boundaries() -> None:
    notes = [Note(0, 1, 20), Note(0.08, 1, 40), Note(0.1, 1, 45)]
    events, omitted = infer_fingerings(notes)
    assert [event["start"] for event in events] == [0.08, 0.1]
    assert omitted == 1


@pytest.mark.parametrize(("start", "end"), [(0.0001, 0.0002), (0.9996, 1.0004), (0.12345, 1.23456)])
def test_tab_retains_positive_note_spans_and_exact_onsets(start: float, end: float) -> None:
    events, omitted = infer_fingerings([Note(start, end, 40)])
    assert events[0]["start"] == start
    assert events[0]["end"] == end
    assert omitted == 0


def test_a_string_collision_retains_the_stronger_playable_note() -> None:
    notes = [Note(0, 0.5, 40, 0.9), Note(0.02, 4, 41, 0.6)]
    events, omitted = infer_fingerings(notes)
    assert events == [
        {"start": 0, "end": 0.5, "positions": [{"string": 0, "fret": 0, "pitch": 40}]}
    ]
    assert omitted == 1


def test_impossible_full_chord_keeps_its_largest_playable_subset() -> None:
    notes = [Note(0, 1, pitch) for pitch in (40, 41, 52, 59)]
    events, omitted = infer_fingerings(notes)
    positions = events[0]["positions"]
    assert len(positions) == 3
    assert {p["pitch"] for p in positions} >= {52, 59}
    assert len({p["string"] for p in positions}) == 3
    assert omitted == 1
    assert infer_fingerings(list(reversed(notes))) == (events, omitted)


def test_polyphony_and_duplicate_onsets_are_in_the_omission_count() -> None:
    notes = [Note(0, 1, pitch) for pitch in (40, 45, 50, 55, 59, 64, 65)]
    notes.append(Note(0.01, 0.5, 64, 0.5))
    events, omitted = infer_fingerings(notes)
    assert len(events[0]["positions"]) == 6
    assert omitted == 2
    assert sum(len(event["positions"]) for event in events) + omitted == len(notes)


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
