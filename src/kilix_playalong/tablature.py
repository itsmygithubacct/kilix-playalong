"""Deterministic guitar-fingering inference and timed/printable tab rendering."""

from __future__ import annotations

import itertools
import json
import math
from dataclasses import dataclass
from pathlib import Path
from statistics import median

from . import TAB_SCHEMA
from .midi import Note
from .types import TabEvent, TabPosition
from .util import canonical_json, private_write

STANDARD_TUNING = (40, 45, 50, 55, 59, 64)  # low E to high e
STANDARD_LABELS = ("E", "A", "D", "G", "B", "e")
FINGERING_VERSION = 3
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
    notes: tuple[Note, ...] = ()
    omitted_notes: int = 0


@dataclass(frozen=True)
class Fingering:
    positions: tuple[tuple[int, int, int], ...]  # string, fret, pitch
    hand: float
    local_cost: float


def group_notes(
    notes: list[Note],
    *,
    window: float = 0.09,
    max_polyphony: int = 6,
    playable_pitches: set[int] | None = None,
) -> list[Chord]:
    if not math.isfinite(window) or window < 0 or max_polyphony < 1:
        raise ValueError("grouping needs a nonnegative window and positive polyphony")
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
            if playable_pitches is not None and note.pitch not in playable_pitches:
                continue
            previous = strongest.get(note.pitch)
            if previous is None or note.confidence > previous.confidence:
                strongest[note.pitch] = note
        selected = sorted(strongest.values(), key=lambda note: (-note.confidence, -note.pitch))[
            :max_polyphony
        ]
        selected.sort(key=lambda note: note.pitch)
        # Keep the onset groups anchored to the original detections even when
        # all pitches in a group are out of range. Omission counts include them.
        timed_notes = selected or group
        chords.append(
            Chord(
                start=min(note.start for note in timed_notes),
                end=max(note.end for note in timed_notes),
                pitches=tuple(note.pitch for note in selected),
                notes=tuple(selected),
                omitted_notes=len(group) - len(selected),
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
    options = options[-len(tuning) :]
    confidence = {note.pitch: note.confidence for note in chord.notes}
    # A colliding or over-wide note must not erase the other notes at that onset.
    # Try the largest playable subsets first; among equally sized subsets retain
    # the strongest detected pitches before optimizing their hand positions.
    for size in range(len(options), 0, -1):
        candidates: list[Fingering] = []
        best_confidence = -1.0
        for subset in itertools.combinations(options, size):
            strength = sum(confidence.get(choices[0][2], 1.0) for choices in subset)
            if strength < best_confidence:
                continue
            found = _complete_fingerings(subset)
            if not found:
                continue
            if strength > best_confidence:
                candidates = []
                best_confidence = strength
            candidates.extend(found)
        if candidates:
            candidates.sort(key=lambda item: (item.local_cost, item.hand, item.positions))
            return candidates[:192]
    return []


def _complete_fingerings(
    options: tuple[list[tuple[int, int, int]], ...],
) -> list[Fingering]:
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
    return candidates


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
    playable_pitches = {open_pitch + fret for open_pitch in tuning for fret in range(max_fret + 1)}
    chords = group_notes(notes, playable_pitches=playable_pitches)
    playable: list[tuple[Chord, list[Fingering]]] = []
    omitted = sum(chord.omitted_notes for chord in chords)
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
        kept = {position["pitch"] for position in positions}
        retained = [note for note in chord.notes if note.pitch in kept]
        events.append(
            {
                "start": min(note.start for note in retained),
                "end": max(note.end for note in retained),
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
        "provider": f"kilix-playalong-fingering-v{FINGERING_VERSION}",
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
