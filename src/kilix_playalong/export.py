"""Self-contained printable play-along document generation."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import cast

from . import LYRICS_SCHEMA, TAB_SCHEMA
from .types import LyricCue, TabEvent
from .util import private_write


def _tab_system(events: list[TabEvent], labels: list[str]) -> str:
    rows = [f"{labels[string]}|" for string in reversed(range(len(labels)))]
    for event in events:
        by_string = {position["string"]: position["fret"] for position in event["positions"]}
        width = max(2, max((len(str(fret)) for fret in by_string.values()), default=1))
        for row, string in enumerate(reversed(range(len(labels)))):
            cell = str(by_string[string]) if string in by_string else "-"
            rows[row] += "-" + cell.rjust(width, "-")
    return "\n".join(row + "-|" for row in rows)


def _clock(seconds: float) -> str:
    minutes, remainder = divmod(max(0, seconds), 60)
    return f"{int(minutes):02d}:{remainder:05.2f}"


def render_printable(
    output: Path,
    *,
    title: str,
    artist: str,
    lyrics_path: Path,
    tab_path: Path,
) -> Path:
    lyrics_value = json.loads(lyrics_path.read_text(encoding="utf-8"))
    tab_value = json.loads(tab_path.read_text(encoding="utf-8"))
    if lyrics_value.get("schema") != LYRICS_SCHEMA or tab_value.get("schema") != TAB_SCHEMA:
        raise ValueError("cannot export unsupported lyrics or tab schema")
    cues = cast(list[LyricCue], lyrics_value["cues"])
    events = cast(list[TabEvent], tab_value["events"])
    labels = cast(list[str], tab_value["tuning"]["labels"])

    lyric_rows = "\n".join(
        '<div class="lyric">'
        f"<time>{_clock(cue['start'])}</time>"
        f"<span>{html.escape(cue['text'])}</span>"
        "</div>"
        for cue in cues
    )
    systems: list[str] = []
    for offset in range(0, len(events), 12):
        chunk = events[offset : offset + 12]
        systems.append(
            '<section class="tab-system">'
            f"<time>{_clock(chunk[0]['start'])}</time>"
            f"<pre>{html.escape(_tab_system(chunk, labels))}</pre>"
            "</section>"
        )
    tab_rows = "\n".join(systems)
    safe_title = html.escape(title or "Untitled")
    safe_artist = html.escape(artist or "Unknown artist")
    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{safe_title} — Kilix Playalong</title>
<style>
:root {{ color-scheme: light; font-family: ui-sans-serif, system-ui, sans-serif; }}
body {{ max-width: 980px; margin: 2rem auto; padding: 0 1.25rem 4rem; color: #17201b; }}
h1 {{ margin-bottom: .1rem; }} h2 {{ margin-top: 2.2rem; border-bottom: 1px solid #9ca79f; }}
.artist,.notice {{ color: #566159; }} .toolbar {{ display:flex; gap:1rem; padding:1rem 0; }}
.lyric {{ display:grid; grid-template-columns:5rem 1fr; gap:.75rem; padding:.22rem 0;
  break-inside:avoid; }}
time {{ color:#607068; font-variant-numeric: tabular-nums; }}
.tab-system {{ break-inside:avoid; margin:1.25rem 0; }}
pre {{ font: 700 13px/1.45 ui-monospace, SFMono-Regular, Consolas, monospace;
  overflow:hidden; }}
body.hide-lyrics #lyrics {{ display:none; }} body.hide-tabs #tabs {{ display:none; }}
@media print {{ body {{ margin:0; max-width:none; padding:0; }} .toolbar {{ display:none; }}
  h2 {{ margin-top:1.2rem; }} .notice {{ font-size:9pt; }} @page {{ margin:14mm; }} }}
</style>
</head>
<body>
<header><h1>{safe_title}</h1><div class="artist">{safe_artist}</div>
<p class="notice">Machine-generated practice draft. Verify the notes, timing, and rights
before use or distribution.</p></header>
<div class="toolbar"><label><input id="show-lyrics" type="checkbox" checked> Lyrics</label>
<label><input id="show-tabs" type="checkbox" checked> Guitar tab</label>
<button onclick="print()">Print</button></div>
<main><section id="tabs"><h2>Guitar tab</h2>{tab_rows}</section>
<section id="lyrics"><h2>Lyrics</h2>{lyric_rows}</section></main>
<script>
document.querySelector('#show-lyrics').addEventListener('change', e =>
  document.body.classList.toggle('hide-lyrics', !e.target.checked));
document.querySelector('#show-tabs').addEventListener('change', e =>
  document.body.classList.toggle('hide-tabs', !e.target.checked));
</script>
</body></html>
"""
    private_write(output, document.encode("utf-8"))
    return output
