from __future__ import annotations

import json
from pathlib import Path

import pytest

from kilix_playalong.errors import ProviderFailedError
from kilix_playalong.midi import Note, load_note_events


@pytest.mark.parametrize(
    "field,value",
    [
        ("end", float("inf")),
        ("confidence", float("nan")),
        ("confidence", -0.1),
        ("confidence", 1.1),
        ("pitch", 64.9),
        ("pitch", float("inf")),
        ("pitch", True),
        ("start", False),
    ],
)
def test_invalid_numbers_cannot_become_frets_or_poison_scoring(
    tmp_path: Path, field: str, value: object
) -> None:
    good = {"start": 0.0, "end": 1.0, "pitch": 64, "confidence": 0.8}
    path = tmp_path / "notes.json"
    path.write_text(json.dumps({"notes": [good, {**good, field: value}]}))
    assert load_note_events(path) == [Note(0, 1, 64, 0.8)]


@pytest.mark.parametrize("data", [b"{", b"\xff", b'{"notes":[]}'])
def test_broken_note_output_is_a_provider_error(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / "notes.json"
    path.write_bytes(data)
    with pytest.raises(ProviderFailedError):
        load_note_events(path)
