"""Deterministic guitar-fingering inference and timed/printable tab rendering."""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass
from pathlib import Path
from statistics import median

from . import TAB_SCHEMA
from .midi import Note
from .types import TabEvent, TabPosition
from .util import canonical_json, private_write

STANDARD_TUNING = (40, 45, 50, 55, 59, 64)  # low E to high e
STANDARD_LABELS = ("E", "A", "D", "G", "B", "e")
_PITCH_CLASSES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def tuning_labels(tuning: tuple[int, ...]) -> tuple[str, ...]:
    labels = [_PITCH_CLASSES[pitch % 12] for pitch in tuning]
    if labels:
        labels[-1] = labels[-1].lower()
    return tuple(labels)


@dataclass(frozen=True)
class Chord:
    start: float
    end: float
    pitches: tuple[int, ...]


@dataclass(frozen=True)
class Fingering:
    positions: tuple[tuple[int, int, int], ...]  # string, fret, pitch
    hand: float
    local_cost: float


def group_notes(notes: list[Note], *, window: float = 0.09, max_polyphony: int = 6) -> list[Chord]:
    groups: list[list[Note]] = []
    for note in sorted(notes):
        if groups and note.start - groups[-1][0].start <= window:
            groups[-1].append(note)
        else:
            groups.append([note])
    chords: list[Chord] = []
    for group in groups:
        strongest: dict[int, Note] = {}
        for note in group:
            previous = strongest.get(note.pitch)
            if previous is None or note.confidence > previous.confidence:
                strongest[note.pitch] = note
        selected = sorted(strongest.values(), key=lambda note: (-note.confidence, -note.pitch))[
            :max_polyphony
        ]
        selected.sort(key=lambda note: note.pitch)
        chords.append(
            Chord(
                start=min(note.start for note in selected),
                end=max(note.end for note in selected),
                pitches=tuple(note.pitch for note in selected),
            )
        )
    return chords


def _positions(pitch: int, tuning: tuple[int, ...], max_fret: int) -> list[tuple[int, int, int]]:
    return [
        (string, pitch - open_pitch, pitch)
        for string, open_pitch in enumerate(tuning)
        if 0 <= pitch - open_pitch <= max_fret
    ]


def _fingerings(chord: Chord, tuning: tuple[int, ...], max_fret: int) -> list[Fingering]:
    options = [_positions(pitch, tuning, max_fret) for pitch in chord.pitches]
    options = [item for item in options if item]
    if not options:
        return []
    # Keep the most useful six pitches if inference emitted an impossible chord.
    options = options[-len(tuning) :]
    candidates: list[Fingering] = []
    for combination in itertools.product(*options):
        strings = [position[0] for position in combination]
        if len(strings) != len(set(strings)):
            continue
        frets = [position[1] for position in combination if position[1] > 0]
        span = max(frets) - min(frets) if frets else 0
        if span > 5:
            continue
        hand = float(median(frets)) if frets else 0.0
        gaps = sum(max(0, right - left - 1) for left, right in itertools.pairwise(sorted(strings)))
        open_strings = sum(position[1] == 0 for position in combination)
        local = span * 1.7 + hand * 0.055 + gaps * 0.2 + open_strings * 0.05
        candidates.append(
            Fingering(tuple(sorted(combination, key=lambda value: value[0])), hand, local)
        )
    candidates.sort(key=lambda item: (item.local_cost, item.hand, item.positions))
    return candidates[:192]


def _transition(left: Fingering, right: Fingering) -> float:
    shift = abs(left.hand - right.hand)
    left_by_pitch = {pitch: string for string, _fret, pitch in left.positions}
    string_changes = sum(
        abs(left_by_pitch[pitch] - string) * 0.18
        for string, _fret, pitch in right.positions
        if pitch in left_by_pitch
    )
    return shift * 0.85 + string_changes


def infer_fingerings(
    notes: list[Note],
    *,
    tuning: tuple[int, ...] = STANDARD_TUNING,
    max_fret: int = 20,
) -> tuple[list[TabEvent], int]:
    chords = group_notes(notes)
    playable: list[tuple[Chord, list[Fingering]]] = []
    omitted = 0
    for chord in chords:
        candidates = _fingerings(chord, tuning, max_fret)
        if not candidates:
            omitted += len(chord.pitches)
            continue
        represented = len(candidates[0].positions)
        omitted += max(0, len(chord.pitches) - represented)
        playable.append((chord, candidates))
    if not playable:
        return [], omitted

    costs = [candidate.local_cost for candidate in playable[0][1]]
    parents: list[list[int]] = []
    for index in range(1, len(playable)):
        previous = playable[index - 1][1]
        current = playable[index][1]
        next_costs: list[float] = []
        next_parents: list[int] = []
        for candidate in current:
            options = [
                (costs[parent] + _transition(previous[parent], candidate), parent)
                for parent in range(len(previous))
            ]
            best_cost, best_parent = min(options, key=lambda item: (item[0], item[1]))
            next_costs.append(best_cost + candidate.local_cost)
            next_parents.append(best_parent)
        parents.append(next_parents)
        costs = next_costs

    selected = [0] * len(playable)
    selected[-1] = min(range(len(costs)), key=lambda index: (costs[index], index))
    for layer in range(len(playable) - 2, -1, -1):
        selected[layer] = parents[layer][selected[layer + 1]]

    events: list[TabEvent] = []
    for (chord, candidates), selection in zip(playable, selected, strict=True):
        positions: list[TabPosition] = [
            {"string": string, "fret": fret, "pitch": pitch}
            for string, fret, pitch in candidates[selection].positions
        ]
        events.append(
            {
                "start": round(chord.start, 3),
                "end": round(chord.end, 3),
                "positions": positions,
            }
        )
    return events, omitted


def write_tab(
    output: Path,
    events: list[TabEvent],
    *,
    source_midi: str,
    tuning: tuple[int, ...] = STANDARD_TUNING,
    max_fret: int = 20,
    omitted_notes: int = 0,
) -> Path:
    document = {
        "schema": TAB_SCHEMA,
        "provider": "kilix-playalong-fingering-v1",
        "source_midi": source_midi,
        "tuning": {
            "midi": list(tuning),
            "labels": list(tuning_labels(tuning)),
            "max_fret": max_fret,
        },
        "stats": {"events": len(events), "omitted_notes": omitted_notes},
        "events": events,
    }
    private_write(output, canonical_json(document))
    return output


def timestamp(seconds: float) -> str:
    minutes, remainder = divmod(max(0.0, seconds), 60)
    return f"{int(minutes):02d}:{remainder:06.3f}"


def render_ascii(
    events: list[TabEvent],
    *,
    title: str,
    artist: str,
    labels: tuple[str, ...] = STANDARD_LABELS,
    events_per_system: int = 12,
) -> str:
    lines = [
        f"# {title}",
        f"# {artist}" if artist else "# Unknown artist",
        "# Generated draft — verify by ear.",
        "",
    ]
    for offset in range(0, len(events), events_per_system):
        chunk = events[offset : offset + events_per_system]
        lines.append(f"[{timestamp(chunk[0]['start'])}]")
        system = [f"{labels[string]}|" for string in reversed(range(len(labels)))]
        for event in chunk:
            by_string = {position["string"]: position["fret"] for position in event["positions"]}
            width = max(2, max((len(str(fret)) for fret in by_string.values()), default=1))
            for row, string in enumerate(reversed(range(len(labels)))):
                cell = str(by_string[string]) if string in by_string else "-"
                system[row] += "-" + cell.rjust(width, "-")
        lines.extend(line + "-|" for line in system)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def read_tab(path: Path) -> tuple[list[TabEvent], dict[str, object]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != TAB_SCHEMA:
        raise ValueError("unsupported tab schema")
    events = value.get("events")
    if not isinstance(events, list):
        raise ValueError("tab document has no events")
    return events, value
