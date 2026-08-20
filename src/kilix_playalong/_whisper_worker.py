"""Heavy faster-whisper worker; emits the app's stable timed-lyrics schema."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import LYRICS_SCHEMA


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--model", default="large-v3")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--language")
    parser.add_argument("--cache", type=Path, required=True)
    arguments = parser.parse_args()

    from faster_whisper import WhisperModel

    compute_type = "int8" if arguments.device == "cpu" else "default"
    model = WhisperModel(
        arguments.model,
        device=arguments.device,
        compute_type=compute_type,
        download_root=str(arguments.cache),
    )
    segments, info = model.transcribe(
        str(arguments.source),
        language=arguments.language,
        beam_size=5,
        vad_filter=True,
        word_timestamps=True,
    )
    cues = []
    for segment in segments:
        words = [
            {
                "start": round(float(word.start if word.start is not None else segment.start), 3),
                "end": round(float(word.end if word.end is not None else segment.end), 3),
                "text": word.word.strip(),
            }
            for word in (segment.words or [])
            if word.word.strip()
        ]
        text = segment.text.strip()
        if text:
            cues.append(
                {
                    "start": round(float(segment.start), 3),
                    "end": round(float(segment.end), 3),
                    "text": text,
                    "words": words,
                }
            )
    document = {
        "schema": LYRICS_SCHEMA,
        "source": f"faster-whisper:{arguments.model}",
        "language": getattr(info, "language", arguments.language or "unknown"),
        "cues": cues,
    }
    arguments.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
