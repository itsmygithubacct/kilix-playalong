"""Portable songbooks built from existing project artifacts, without running ML."""

from __future__ import annotations

import html
import json
import math
import re
import tempfile
from pathlib import Path
from typing import Any

from . import TAB_SCHEMA
from .errors import InvalidInputError
from .lyrics import load_lyrics_document
from .paths import project_artifact
from .runner import run_command
from .state import load_manifest
from .tablature import tuning_labels
from .text import printable_line
from .util import canonical_json, private_write, sha256_bytes, sha256_file

ASSETS = Path(__file__).with_name("songbook_assets")
PITCHES = ("C", "C♯", "D", "D♯", "E", "F", "F♯", "G", "G♯", "A", "A♯", "B")
# Exact pitch-class matches, in the same preference order as the native fretboard.
CHORDS = (
    ("", (0, 4, 7)),
    ("m", (0, 3, 7)),
    ("7", (0, 4, 7, 10)),
    ("m7", (0, 3, 7, 10)),
    ("maj7", (0, 4, 7, 11)),
    ("6", (0, 4, 7, 9)),
    ("m6", (0, 3, 7, 9)),
    ("sus4", (0, 5, 7)),
    ("sus2", (0, 2, 7)),
    ("add9", (0, 2, 4, 7)),
    ("madd9", (0, 2, 3, 7)),
    ("dim", (0, 3, 6)),
    ("aug", (0, 4, 8)),
    ("m7b5", (0, 3, 6, 10)),
    ("dim7", (0, 3, 6, 9)),
)
KINDS = {"intro", "verse", "chorus", "bridge", "interlude", "outro", "section"}


def _json(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(32 * 1024 * 1024 + 1)
        if len(raw) > 32 * 1024 * 1024:
            raise InvalidInputError("songbook input exceeds 32 MiB")
        result = json.loads(raw)
    except (OSError, ValueError, RecursionError) as error:
        raise InvalidInputError("songbook input is not readable JSON") from error
    if not isinstance(result, dict):
        raise InvalidInputError("songbook input must be a JSON object")
    return result


def _number(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidInputError(f"{what} must be a finite number")
    try:
        number = float(value)
    except OverflowError as error:
        raise InvalidInputError(f"{what} must be a finite number") from error
    if not math.isfinite(number):
        raise InvalidInputError(f"{what} must be a finite number")
    return number


def _integer(value: object, low: int, high: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise InvalidInputError(f"{what} must be an integer from {low} to {high}")
    return value


def chord_name(pitches: list[int]) -> str:
    classes = {pitch % 12 for pitch in pitches}
    if len(classes) < 2:
        return ""
    bass = min(pitches) % 12
    choices: list[tuple[bool, int, int, str]] = []
    templates = (("5", (0, 7)),) if len(classes) == 2 else CHORDS
    for root in sorted(classes):
        intervals = {(pitch - root) % 12 for pitch in classes}
        for rank, (suffix, pattern) in enumerate(templates):
            if intervals == set(pattern):
                name = PITCHES[root] + suffix
                if root != bass:
                    name += "/" + PITCHES[bass]
                choices.append((root != bass, rank, root, name))
    return min(choices)[-1] if choices else ""


def validate_sections(value: object, duration: float) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 200:
        raise InvalidInputError("arrangement needs between 1 and 200 sections")
    reserved = set(re.findall(r'id="([^"]+)"', (ASSETS / "template.html").read_text()))
    reserved.update({"lyric-editor", "section-editor-list", "add-section"})
    result: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise InvalidInputError("each section must be an object")
        identity = item.get("id", f"section-{index + 1}")
        if (
            not isinstance(identity, str)
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,79}", identity)
            or identity.startswith("row-")
            or identity in reserved | ids
            or identity + "-title" in reserved
        ):
            raise InvalidInputError("section IDs must be unique, nonreserved lowercase names")
        ids.add(identity)
        label = item.get("label")
        if not isinstance(label, str) or not label.strip() or len(label) > 80:
            raise InvalidInputError("section labels must contain 1-80 characters")
        label = printable_line(label).strip()
        if not label:
            raise InvalidInputError("section labels must contain visible text")
        start = _number(item.get("start"), "section start")
        if not 0 <= start < duration or (index == 0 and start != 0):
            raise InvalidInputError("sections must start at zero and remain inside the song")
        if result and start <= result[-1]["start"]:
            raise InvalidInputError("section starts must be in increasing order")
        kind = item.get("kind", "section")
        if not isinstance(kind, str) or kind not in KINDS:
            raise InvalidInputError("unsupported section kind")
        result.append(
            {"id": identity, "label": printable_line(label), "kind": kind, "start": start}
        )
    if ids & {identity + "-title" for identity in ids}:
        raise InvalidInputError("section IDs collide with section headings")
    return result


def songbook_data(
    *,
    project_id: str,
    title: str,
    artist: str,
    duration: float,
    tab_path: Path,
    lyrics_path: Path,
    arrangement_path: Path | None = None,
    audio: dict[str, str] | None = None,
) -> dict[str, Any]:
    duration = _number(duration, "song duration")
    if not 0 < duration <= 7200:
        raise InvalidInputError("songbook duration must be positive and at most two hours")
    tab = _json(tab_path)
    if tab.get("schema") != TAB_SCHEMA or not isinstance(tab.get("events"), list):
        raise InvalidInputError("songbook needs a supported tab document")
    tuning = tab.get("tuning")
    if not isinstance(tuning, dict) or not isinstance(tuning.get("midi"), list):
        raise InvalidInputError("tab document has no tuning")
    if len(tuning["midi"]) != 6:
        raise InvalidInputError("songbook supports six-string guitar tunings")
    open_pitches = [_integer(p, 0, 127, "open-string pitch") for p in tuning["midi"]]
    max_fret = _integer(tuning.get("max_fret"), 1, 36, "maximum fret")
    events = []
    for event in tab["events"]:
        if not isinstance(event, dict) or not isinstance(event.get("positions"), list):
            raise InvalidInputError("invalid tab event")
        start, end = (_number(event.get(key), "tab time") for key in ("start", "end"))
        if not 0 <= start < end <= duration:
            raise InvalidInputError("tab events must have positive spans inside the song")
        positions = []
        used: set[int] = set()
        for point in event["positions"]:
            if not isinstance(point, dict):
                raise InvalidInputError("invalid tab position")
            string = _integer(point.get("string"), 0, 5, "string index")
            fret = _integer(point.get("fret"), 0, max_fret, "fret")
            pitch = _integer(point.get("pitch"), 0, 127, "note pitch")
            if string in used or open_pitches[string] + fret != pitch:
                raise InvalidInputError("tab pitches must match unique strings and frets")
            used.add(string)
            positions.append({**point, "note": PITCHES[pitch % 12], "octave": pitch // 12 - 1})
        if not positions:
            raise InvalidInputError("tab event contains no positions")
        events.append(
            {
                "start": start,
                "end": end,
                "positions": positions,
                "chord": chord_name([point["pitch"] for point in positions]),
            }
        )
    events.sort(key=lambda event: (event["start"], event["end"]))
    for index, event in enumerate(events):
        event["id"] = index
    document = load_lyrics_document(lyrics_path, duration=duration)
    cues = [
        dict(cue, id=f"lyric-{index}")
        for index, cue in enumerate(document.cues)
        if cue["end"] > cue["start"]
    ]
    section_input = (
        _json(arrangement_path).get("sections")
        if arrangement_path
        else [{"id": "section-1", "label": "Song", "kind": "section", "start": 0}]
    )
    sections = validate_sections(section_input, duration)
    tracks = audio or {}
    if set(tracks) - {"mix", "guitar"}:
        raise InvalidInputError("songbook audio must name a mix or a guitar track")
    for name in tracks.values():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
            raise InvalidInputError("songbook audio must use plain filenames beside the page")
    stats = tab.get("stats", {})
    omitted = stats.get("omitted_notes", 0) if isinstance(stats, dict) else 0
    omitted = _integer(omitted, 0, 10_000_000, "omitted note count")
    revision = sha256_bytes(canonical_json({"cues": cues, "sections": sections}))
    return {
        "id": project_id,
        "title": printable_line(title) or "Untitled",
        "artist": printable_line(artist) or "Unknown artist",
        "duration": duration,
        "tuning": {
            "midi": open_pitches,
            "labels": tuning_labels(tuple(open_pitches)),
            "max_fret": max_fret,
        },
        "events": events,
        "cues": cues,
        "sections": sections,
        "audio": tracks,
        "stats": {"events": len(events), "omitted_notes": omitted},
        "revision": revision,
        "lyricsTiming": document.timing,
    }


def render_songbook(output: Path, data: dict[str, Any]) -> Path:
    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    # JSON in a script element must never contain a literal HTML closing tag.
    encoded = encoded.replace("<", "\\u003c")
    template = (ASSETS / "template.html").read_text(encoding="utf-8")
    replacements = {
        "{{TITLE}}": data["title"],
        "{{ARTIST}}": data["artist"],
        "{{TUNING}}": " ".join(data["tuning"]["labels"]),
        "{{OMITTED}}": str(data["stats"]["omitted_notes"]),
    }
    substitutions = {token: html.escape(text) for token, text in replacements.items()}
    substitutions.update(
        {
            "/* SONGBOOK_CSS */": (ASSETS / "style.css").read_text(encoding="utf-8"),
            "/* SONGBOOK_JS */": (ASSETS / "app.js").read_text(encoding="utf-8"),
            "/* SONGBOOK_DATA */": encoded,
        }
    )
    # One pass: marker-looking titles or lyrics are never substituted again.
    template = re.sub(
        "|".join(re.escape(token) for token in substitutions),
        lambda match: substitutions[match.group()],
        template,
    )
    try:
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".html", delete=False) as handle:
            temporary = Path(handle.name)
        try:
            private_write(temporary, template.encode("utf-8"))
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError as error:
        raise InvalidInputError("songbook output could not be written") from error
    return output


def export_songbook(
    project_dir: Path,
    output_dir: Path,
    *,
    lyrics_path: Path | None = None,
    arrangement_path: Path | None = None,
    include_audio: bool = True,
) -> Path:
    manifest = load_manifest(project_dir)
    lyrics, tab = manifest["lyrics"], manifest["tablature"]
    if lyrics is None or tab is None:
        raise InvalidInputError("finish the project's lyrics and tablature before exporting")
    data = songbook_data(
        project_id=manifest["id"],
        title=manifest["title"],
        artist=manifest["artist"],
        duration=_number(manifest["source"].get("duration"), "song duration"),
        tab_path=project_artifact(project_dir, str(tab.get("path", ""))),
        lyrics_path=lyrics_path or project_artifact(project_dir, str(lyrics.get("path", ""))),
        arrangement_path=arrangement_path,
    )
    sources: dict[str, Path] = {}
    if include_audio:
        sources["mix"] = project_artifact(project_dir, "media/normalized.wav")
        for track in manifest["tracks"]:
            if track["id"] == "guitar":
                sources["guitar"] = project_artifact(project_dir, track["path"])
        if "guitar" not in sources:
            raise InvalidInputError("project has no isolated guitar track; use --no-audio")
        if any(not path.is_file() for path in sources.values()):
            raise InvalidInputError("project audio is missing; resume it or use --no-audio")
    try:
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Publish the page only after every audio conversion succeeds. A failed
        # export leaves an existing songbook and its recordings usable.
        with tempfile.TemporaryDirectory(prefix=".songbook-", dir=output_dir) as staging:
            work = Path(staging)
            for track_id, source in sources.items():
                name = f"{track_id}-{sha256_file(source)[:16]}.mp3"
                target = work / name
                run_command(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-v",
                        "error",
                        "-y",
                        "-protocol_whitelist",
                        "file",
                        "-i",
                        str(source),
                        "-map",
                        "0:a:0",
                        "-vn",
                        "-codec:a",
                        "libmp3lame",
                        "-b:a",
                        "160k" if track_id == "mix" else "128k",
                        str(target),
                    ],
                    timeout=600,
                    redact=(str(source), str(target)),
                )
                target.chmod(0o600)
                data["audio"][track_id] = name
            for name in data["audio"].values():
                (work / name).replace(output_dir / name)
            return render_songbook(output_dir / "index.html", data)
    except OSError as error:
        raise InvalidInputError("songbook files could not be written") from error
