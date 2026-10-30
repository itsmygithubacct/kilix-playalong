"""Generate original songbook fixtures for the optional real-browser checks."""

from __future__ import annotations

import sys
from pathlib import Path

from kilix_playalong.lyrics import write_lyrics
from kilix_playalong.runner import run_command
from kilix_playalong.songbook import render_songbook, songbook_data
from kilix_playalong.tablature import write_tab


def main(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    tab, lyrics = destination / "tab.json", destination / "lyrics.json"
    write_tab(
        tab,
        [
            {
                "start": start,
                "end": end,
                "positions": [{"string": string, "fret": 0, "pitch": pitch}],
            }
            for start, end, string, pitch in [
                (0, 3, 0, 38),
                (1, 1.2, 1, 45),
                (3.5, 4, 2, 50),
                (5, 5.5, 3, 55),
            ]
        ],
        source_midi="fixture",
        tuning=(38, 45, 50, 55, 59, 64),
    )
    write_lyrics(
        lyrics,
        [
            {"start": 0.0, "end": 2.0, "text": "An original line", "words": []},
            {"start": 2.5, "end": 4.5, "text": "Another practice phrase", "words": []},
        ],
        source="fixture",
        language="en",
    )
    data = songbook_data(
        project_id="browser-fixture",
        title="Another Song",
        artist="An Artist",
        duration=6,
        tab_path=tab,
        lyrics_path=lyrics,
    )
    render_songbook(destination / "no-audio.html", data)
    for name, frequency in (("mix", 220), ("guitar", 330)):
        run_command(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={frequency}:duration=6",
                "-codec:a",
                "libmp3lame",
                str(destination / f"{name}.mp3"),
            ],
            timeout=30,
        )
    data["audio"] = {name: f"{name}.mp3" for name in ("mix", "guitar")}
    render_songbook(destination / "index.html", data)
    write_lyrics(
        lyrics,
        [
            {"start": float(i), "end": float(i + 1), "text": f"Practice line {i}", "words": []}
            for i in range(20)
        ],
        source="fixture",
        language="en",
    )
    large = songbook_data(
        project_id="large-browser-fixture",
        title="Long Practice",
        artist="An Artist",
        duration=20,
        tab_path=tab,
        lyrics_path=lyrics,
    )
    render_songbook(destination / "large-corrections.html", large)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
