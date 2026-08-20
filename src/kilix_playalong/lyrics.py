"""Bounded LRC, SRT, WebVTT, plain-text, and generated-JSON lyric handling."""

from __future__ import annotations

import html
import json
import math
import re
from pathlib import Path

from . import LYRICS_SCHEMA
from .errors import InvalidInputError
from .types import LyricCue, LyricWord
from .util import canonical_json, private_write

MAX_LYRICS_BYTES = 4 * 1024 * 1024
_CLOCK = re.compile(
    r"(?:(?P<hours>\d{1,2}):)?(?P<minutes>\d{1,2}):(?P<seconds>\d{2}(?:[.,]\d{1,3})?)"
)
_LRC_STAMP = re.compile(r"\[(?P<minutes>\d{1,3}):(?P<seconds>\d{1,2}(?:\.\d{1,3})?)\]")
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def _seconds(match: re.Match[str]) -> float:
    hours = float(match.groupdict().get("hours") or 0)
    minutes = float(match.group("minutes"))
    seconds = float(match.group("seconds").replace(",", "."))
    return hours * 3600 + minutes * 60 + seconds


def _clean_text(value: str) -> str:
    return _SPACE.sub(" ", html.unescape(_TAG.sub("", value))).strip()


def _estimated_words(text: str, start: float, end: float) -> list[LyricWord]:
    tokens = text.split()
    if not tokens:
        return []
    duration = max(0.05, end - start)
    weights = [max(1, len(token.strip(".,!?;:"))) for token in tokens]
    total = sum(weights)
    cursor = start
    result: list[LyricWord] = []
    for index, (token, weight) in enumerate(zip(tokens, weights, strict=False)):
        token_end = end if index == len(tokens) - 1 else cursor + duration * weight / total
        result.append({"start": round(cursor, 3), "end": round(token_end, 3), "text": token})
        cursor = token_end
    return result


def _normalize(cues: list[LyricCue], duration: float | None = None) -> list[LyricCue]:
    ordered = sorted(cues, key=lambda cue: (cue["start"], cue["end"], cue["text"]))
    result: list[LyricCue] = []
    for cue in ordered:
        text = _clean_text(cue["text"])
        start = max(0.0, float(cue["start"]))
        end = max(start + 0.05, float(cue["end"]))
        if duration is not None:
            if start >= duration:
                continue
            end = min(end, duration)
        if not text:
            continue
        if result and result[-1]["text"] == text and start <= result[-1]["end"] + 0.15:
            result[-1]["end"] = round(max(result[-1]["end"], end), 3)
            continue
        words = cue.get("words") or _estimated_words(text, start, end)
        result.append(
            {
                "start": round(start, 3),
                "end": round(end, 3),
                "text": text,
                "words": words,
            }
        )
    for index, cue in enumerate(result[:-1]):
        next_start = result[index + 1]["start"]
        if cue["end"] > next_start:
            cue["end"] = max(cue["start"] + 0.05, next_start)
    return result


def _parse_block_timestamps(text: str) -> list[LyricCue]:
    cues: list[LyricCue] = []
    blocks = re.split(r"\r?\n\s*\r?\n", text)
    for block in blocks:
        lines = [line.strip("\ufeff ") for line in block.splitlines()]
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        left, right = lines[timing_index].split("-->", 1)
        start_match = _CLOCK.search(left)
        end_match = _CLOCK.search(right)
        if start_match is None or end_match is None:
            continue
        body = _clean_text(" ".join(lines[timing_index + 1 :]))
        if body:
            cues.append(
                {
                    "start": _seconds(start_match),
                    "end": _seconds(end_match),
                    "text": body,
                    "words": [],
                }
            )
    return cues


def _parse_lrc(text: str, duration: float) -> list[LyricCue]:
    stamped: list[tuple[float, str]] = []
    for line in text.splitlines():
        stamps = list(_LRC_STAMP.finditer(line))
        if not stamps:
            continue
        body = _clean_text(_LRC_STAMP.sub("", line))
        if not body:
            continue
        for stamp in stamps:
            stamped.append((_seconds(stamp), body))
    stamped.sort()
    cues: list[LyricCue] = []
    for index, (start, body) in enumerate(stamped):
        end = stamped[index + 1][0] if index + 1 < len(stamped) else min(duration, start + 5)
        cues.append({"start": start, "end": max(start + 0.05, end), "text": body, "words": []})
    return cues


def _parse_plain(text: str, duration: float) -> list[LyricCue]:
    lines = [_clean_text(line) for line in text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return []
    slot = duration / len(lines)
    return [
        {
            "start": index * slot,
            "end": min(duration, (index + 1) * slot),
            "text": line,
            "words": [],
        }
        for index, line in enumerate(lines)
    ]


def _json_cues(value: object) -> list[LyricCue]:
    if not isinstance(value, list):
        raise InvalidInputError("lyrics JSON cues must be a list")
    cues: list[LyricCue] = []
    for item in value:
        if not isinstance(item, dict):
            raise InvalidInputError("lyrics JSON contains an invalid cue")
        start = item.get("start")
        end = item.get("end")
        text = item.get("text")
        words_value = item.get("words", [])
        if (
            isinstance(start, bool)
            or not isinstance(start, int | float)
            or isinstance(end, bool)
            or not isinstance(end, int | float)
            or not math.isfinite(float(start))
            or not math.isfinite(float(end))
            or not isinstance(text, str)
            or not isinstance(words_value, list)
        ):
            raise InvalidInputError("lyrics JSON contains an invalid cue")
        words: list[LyricWord] = []
        for word in words_value:
            if not isinstance(word, dict):
                raise InvalidInputError("lyrics JSON contains an invalid timed word")
            word_start = word.get("start")
            word_end = word.get("end")
            word_text = word.get("text")
            if (
                isinstance(word_start, bool)
                or not isinstance(word_start, int | float)
                or isinstance(word_end, bool)
                or not isinstance(word_end, int | float)
                or not math.isfinite(float(word_start))
                or not math.isfinite(float(word_end))
                or not isinstance(word_text, str)
            ):
                raise InvalidInputError("lyrics JSON contains an invalid timed word")
            words.append(
                {
                    "start": float(word_start),
                    "end": float(word_end),
                    "text": word_text,
                }
            )
        cues.append({"start": float(start), "end": float(end), "text": text, "words": words})
    return cues


def load_lyrics(path: Path, *, duration: float) -> tuple[list[LyricCue], str, str]:
    if not math.isfinite(duration) or duration <= 0:
        raise InvalidInputError("lyrics duration must be finite and positive")
    try:
        size = path.stat().st_size
    except OSError as error:
        raise InvalidInputError("lyrics file is not readable UTF-8 text") from error
    if size > MAX_LYRICS_BYTES:
        raise InvalidInputError("lyrics file exceeds the 4 MiB limit")
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as error:
        raise InvalidInputError("lyrics file is not readable UTF-8 text") from error
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise InvalidInputError("lyrics JSON is malformed") from error
        if not isinstance(value, dict) or value.get("schema") != LYRICS_SCHEMA:
            raise InvalidInputError("lyrics JSON has an unsupported schema")
        cues = _json_cues(value.get("cues"))
        source_value = value.get("source")
        language_value = value.get("language")
        return (
            _normalize(cues, duration),
            source_value if isinstance(source_value, str) else "imported-json",
            language_value if isinstance(language_value, str) else "unknown",
        )
    if suffix == ".lrc":
        cues = _parse_lrc(raw, duration)
        source = "imported-lrc"
    elif suffix in {".vtt", ".srt"}:
        cues = _parse_block_timestamps(raw)
        source = "youtube-captions" if path.name.startswith("source") else "imported-timed-text"
    else:
        cues = _parse_plain(raw, duration)
        source = "imported-plain-estimated"
    normalized = _normalize(cues, duration)
    if not normalized:
        raise InvalidInputError("lyrics file contains no usable lyric cues")
    return normalized, source, "unknown"


def choose_subtitle(paths: list[Path], language: str) -> Path | None:
    if not paths:
        return None
    needle = language.lower().split("-", 1)[0] if language != "auto" else "en"

    def rank(path: Path) -> tuple[int, int, str]:
        name = path.name.lower()
        return (0 if f".{needle}" in name else 1, 1 if "live_chat" in name else 0, name)

    return min(paths, key=rank)


def write_lyrics(
    output: Path,
    cues: list[LyricCue],
    *,
    source: str,
    language: str,
) -> Path:
    document = {"schema": LYRICS_SCHEMA, "source": source, "language": language, "cues": cues}
    private_write(output, canonical_json(document))
    return output
