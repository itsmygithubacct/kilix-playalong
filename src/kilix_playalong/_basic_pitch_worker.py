"""Heavy Basic Pitch worker; invoked only in a bounded child process."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 5:
        print("usage: _basic_pitch_worker INPUT OUTPUT.mid OUTPUT.json MODEL.onnx", file=sys.stderr)
        return 2
    source, midi_path, notes_path, model_path = map(Path, sys.argv[1:])
    # The licensed asset the provider just verified, never the wheel's own copy.
    if not model_path.is_absolute() or not model_path.is_file():
        print("the Basic Pitch model path is not an existing absolute file", file=sys.stderr)
        return 2
    from basic_pitch.inference import Model, predict

    model = Model(model_path)
    _, midi, events = predict(
        source,
        model,
        onset_threshold=0.5,
        frame_threshold=0.3,
        minimum_note_length=90.0,
        minimum_frequency=70.0,
        maximum_frequency=1400.0,
        multiple_pitch_bends=False,
    )
    midi_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    midi.write(str(midi_path))
    notes = [
        {
            "start": round(float(start), 6),
            "end": round(float(end), 6),
            "pitch": int(pitch),
            "confidence": round(float(confidence), 6),
        }
        for start, end, pitch, confidence, _bend in events
    ]
    notes_path.write_text(json.dumps({"provider": "basic-pitch-onnx-0.4.0", "notes": notes}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
