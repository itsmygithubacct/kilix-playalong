"""Validation and stable note-event loading for generated MIDI artifacts."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import mido

from .errors import ProviderFailedError


@dataclass(frozen=True, order=True)
class Note:
    start: float
    end: float
    pitch: int
    confidence: float = 1.0


def validate_midi(path: Path) -> int:
    try:
        midi = mido.MidiFile(path)
    except (OSError, EOFError, ValueError) as error:
        raise ProviderFailedError("Basic Pitch produced an invalid MIDI file") from error
    count = sum(
        1
        for track in midi.tracks
        for message in track
        if message.type == "note_on" and message.velocity
    )
    if count == 0:
        raise ProviderFailedError("Basic Pitch found no guitar notes")
    return count


def load_note_events(path: Path) -> list[Note]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        raise ProviderFailedError("Basic Pitch note output could not be read as JSON") from error
    if not isinstance(value, dict) or not isinstance(value.get("notes"), list):
        raise ProviderFailedError("Basic Pitch note output has an unexpected schema")
    result: list[Note] = []
    for item in value["notes"]:
        if not isinstance(item, dict):
            continue
        try:
            values = (item["start"], item["end"], item["pitch"], item.get("confidence", 1.0))
            if any(isinstance(number, bool) for number in values):
                continue
            start, end, raw_pitch, confidence = map(float, values)
            if not all(math.isfinite(number) for number in (start, end, raw_pitch, confidence)):
                continue
            if not raw_pitch.is_integer() or not 0 <= confidence <= 1:
                continue
            pitch = int(raw_pitch)
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if 0 <= start < end and 0 <= pitch <= 127:
            result.append(Note(start, end, pitch, confidence))
    if not result:
        raise ProviderFailedError("Basic Pitch produced no valid note events")
    return sorted(result)
