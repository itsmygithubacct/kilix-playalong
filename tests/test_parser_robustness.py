"""Fuzzing the Python parsers that read a file, a tag, or another program's JSON.

The native JSON reader has a 200,000-mutation loop (``tests/native``); nothing
equivalent existed on this side, and every entry point exercised below reads
either a file the user supplied, a tag a crafted container carries, or a
document ``ffprobe`` produced. This file is that loop.

**The assertion.** For every input, the entry point must either return a
well-formed result or raise ``PlayalongError``. ``cli.py`` catches
``PlayalongError`` and nothing else, so a bare ``ValueError``, ``KeyError``,
``IndexError``, ``UnicodeError``, ``RecursionError``, ``OverflowError`` or
``MemoryError`` reaches the user as a traceback and is a defect. Every result
that does come back is also checked against the bound its own module declares,
so "refused or well-formed" covers unbounded growth as well as crashes.

**Determinism.** One seed, ``_SEED``, printed by every test that uses it and
offset per test so no two loops draw the same stream. A failure reproduces by
running that test again; the failure message carries the seed and the exact
input.

**Budget.** 2,180 generated inputs in all: 800 mutated lyric files (five seed
documents x five suffixes), 160 hostile lyric documents built rather than
mutated, 320 mutated lyric tags, 200 caption-track name sets, 300 source
strings, 200 crafted probe documents, 200 alignment pairs. Mutated payloads are
capped at ``_MUTATION_CEILING`` (16 KiB) to hold the loops down; the real
ceilings -- a 4 MiB file, 8,192 cues, 65,536 timed words, 6,000 alignment tokens
-- are exercised once each in the bounds tests below, where the cost is paid
once rather than on every round. Measured at the time of writing: pytest reports
3.3 s to 3.7 s for this file across four runs, and its three slowest tests are
the mutated-tag loop (1.12 s), the 65,536-word document (0.80 s) and the
mutated-file loop (0.51 s).

Each loop also asserts a floor on how many of its inputs came back as a
*result*, with the measured number beside it. A fuzz loop whose corpus has
decayed into one that every parser refuses at the first gate still passes the
"raise PlayalongError" assertion while testing nothing behind that gate, and
the floors are what makes that decay a failure instead.

**Escapes found, and closed.** Five distinct escapes were found when this
suite was written -- a deeply nested lyrics document recursing out of
``json.loads``, a 400-digit integer overflowing every ``float()`` that followed
an ``isinstance(x, int | float)`` test, a lone surrogate out of a probe
document that no encoder can write, and an unchecked ``words`` field -- and all
five were fixed on 2026-08-25. The tests at the bottom are now ordinary tests,
and ``_KNOWN_DEFECTS`` is empty.

Two of them are refused; four drop the one bad value instead, which is the
other half of the contract above and the better half here: dropping a corrupt
timestamp costs one word, while raising would send ``_apply_alignment`` to its
fallback and cost every word its timing.
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path
from typing import TypeVar, cast

import pytest

from kilix_playalong import LYRICS_SCHEMA, alignment, source
from kilix_playalong import lyrics as lyrics_module
from kilix_playalong.errors import PlayalongError
from kilix_playalong.lyrics import (
    MAX_CUES,
    MAX_EMBEDDED_TAGS,
    MAX_LYRICS_BYTES,
    MAX_WORDS,
    EmbeddedLyrics,
    LyricsDocument,
)
from kilix_playalong.source import MAX_EMBEDDED_LYRICS_BYTES, MAX_SOURCE_LENGTH
from kilix_playalong.text import MAX_DISPLAY_TEXT
from kilix_playalong.types import LyricCue, LyricWord

_SEED = 20260824
#: Bytes a mutated payload is allowed to reach. Not a property of any parser --
#: `MAX_LYRICS_BYTES` is 256 times this -- but the knob that keeps the 1,120
#: mutation rounds to the 1.6 s they currently take (0.51 s for the file loop,
#: 1.12 s for the tag loop, measured). The declared ceilings are covered once
#: each in the bounds tests instead, where the cost is paid once rather than on
#: every round.
_MUTATION_CEILING = 16 * 1024

_T = TypeVar("_T")

#: Escapes that are live defects rather than test bugs, tolerated here so the
#: loops stay green, with the reproducer pinned by the named test. Keyed by the
#: label passed to `_refuse_or_return`.
_KNOWN_DEFECTS: dict[str, tuple[type[Exception], ...]] = {
    # Empty, and it must stay empty unless something is genuinely being
    # tolerated. The five escapes this suite found were fixed on 2026-08-25 and
    # their entries went with them, as the note at the top of this file
    # requires. The tolerance is keyed on (entry point, exception type) and is
    # blunt: an entry left here after its defect is closed would silently
    # absorb the *next* defect that leaves the same entry point the same way.
}

# --------------------------------------------------------------------------- #
# Hostile material
# --------------------------------------------------------------------------- #

#: Text that is legal in a `str` and hostile somewhere downstream: lone
#: surrogates (which no encoder can write back out), NUL, C0 controls, the BOM
#: and the byte-swapped noncharacter beside it, bidi overrides and an isolate,
#: an astral-plane character and the last codepoint there is, a soft hyphen, and
#: the delimiters every parser in `lyrics` keys on.
_HOSTILE_TEXT: tuple[str, ...] = (
    "\ud800",
    "\udfff",
    "\udc80",
    "\x00",
    "\x1b[31m",
    "\x07",
    "\x0b",
    "\ufeff",
    "\ufffe",
    "\u202e",
    "\u200f",
    "\u2066",
    "\U0001f3b8",
    "\U0010ffff",
    "\u00ad",
    "\r\n",
    "\r",
    "\n\n",
    "-->",
    "[",
    "]",
    "<",
    ">",
    "&#0;",
    "&amp;",
    "&#x1b;",
    "[00:00.00]",
    "<00:00.00>",
    "\t",
)

#: Byte sequences a strict UTF-8 decoder has to reject, and the delimiters that
#: are legal but change what a document means: overlong forms of NUL and "/",
#: CESU-8's spelling of U+D800, an encoding of U+110000, a bare continuation
#: byte, two lead bytes UTF-8 never uses, all three BOMs, and an ANSI erase.
_HOSTILE_BYTES: tuple[bytes, ...] = (
    b"\x00",
    b"\xff",
    b"\xfe",
    b"\x80",
    b"\xc0\x80",
    b"\xc0\xaf",
    b"\xe0\x80\xaf",
    b"\xed\xa0\x80",
    b"\xf4\x90\x80\x80",
    b"\xef\xbb\xbf",
    b"\xff\xfe",
    b"\xfe\xff",
    b"\x1b[2J",
    b"-->",
    b"\r\n\r\n",
    b"[00:00.00]",
)

#: Numbers a crafted document can carry where a duration or a stamp is expected.
#: `10 ** 400` is the one that has no float at all; the rest are finite, huge,
#: negative, reversed, or not numbers.
_HOSTILE_NUMBERS: tuple[object, ...] = (
    0,
    -1,
    -1.0e9,
    1.0e9,
    1.0e308,
    float("inf"),
    float("-inf"),
    float("nan"),
    10**400,
    -(10**400),
    10**300,
    True,
    False,
    "1.0",
    "NaN",
    "-0",
    "",
    None,
    [],
    {},
)

_LRC_SEED = "[ti:Song]\n[00:01.00]First line here\n[00:04.50]Second line here\n[00:09.00]Third\n"
_SRT_SEED = (
    "1\n00:00:01,000 --> 00:00:04,000\nFirst line here\n\n"
    "2\n00:00:04,500 --> 00:00:09,000\nSecond line here\n\n"
    "3\n00:00:09,000 --> 00:00:12,000\nThird\n"
)
_VTT_SEED = (
    "WEBVTT\n\n"
    "00:00:01.000 --> 00:00:04.000\nFirst line here\n\n"
    "00:00:04.500 --> 00:00:09.000\n<c>Second</c> line here\n\n"
    "00:00:09.000 --> 00:00:12.000\nThird\n"
)
_TXT_SEED = "First line here\nSecond line here\nThird\n"
_JSON_SEED = json.dumps(
    {
        "schema": LYRICS_SCHEMA,
        "source": "imported-json",
        "language": "en",
        "cues": [
            {
                "start": 1.0,
                "end": 4.0,
                "text": "First line here",
                "words": [{"start": 1.0, "end": 2.0, "text": "First"}],
            },
            {"start": 4.5, "end": 9.0, "text": "Second line here", "words": []},
        ],
    }
)

_SEED_DOCUMENTS: tuple[tuple[str, str], ...] = (
    ("lrc", _LRC_SEED),
    ("srt", _SRT_SEED),
    ("vtt", _VTT_SEED),
    ("txt", _TXT_SEED),
    ("json", _JSON_SEED),
)
#: A parser is chosen by suffix, so every document is fed under every suffix:
#: an `.lrc` holding JSON and a `.json` holding subtitle blocks are both things
#: a user can hand over.
_SUFFIXES: tuple[str, ...] = (".lrc", ".srt", ".vtt", ".json", ".txt")
#: Deep enough to be nesting, shallow enough that `json.loads` returns rather
#: than recursing past CPython's limit -- so this mutator exercises the cue
#: validator's refusals rather than the RecursionError in `_KNOWN_DEFECTS`. The
#: huge-repeat mutator is what reaches that one, by repeating a `[{` slice.
_NESTING_DEPTHS: tuple[int, ...] = (2, 8, 40)


def _as_words(raw: Sequence[dict[str, object]]) -> Sequence[LyricWord]:
    """The generated mappings under the type the aligner declares they have.

    `LyricWord` is a TypedDict, so this is a static claim and nothing else: the
    values inside are deliberately not what it says they are, which is the whole
    input this file exists to feed the aligner.
    """

    return cast("Sequence[LyricWord]", raw)


def _as_cues(raw: Sequence[dict[str, object]]) -> Sequence[LyricCue]:
    """`_as_words` for cues, and for the same reason."""

    return cast("Sequence[LyricCue]", raw)


def _brief(value: object) -> str:
    """The head of a reproducer, short enough to read in a failure message.

    240 characters of `repr`, which is the whole of a short input and the front
    of a long one; the seed printed beside it is what reproduces the rest.
    """

    return repr(value)[:240]


def _refuse_or_return(label: str, evidence: str, call: Callable[[], _T]) -> _T | None:
    """Run `call`; return its result, or None if it refused properly.

    A `PlayalongError` is the contract. Anything else is a defect unless
    `_KNOWN_DEFECTS` lists it for this label, in which case it is a defect that
    is already pinned by an xfail test below and cannot be fixed from here.
    """

    try:
        return call()
    except PlayalongError:
        return None
    except Exception as error:  # the whole point of this file is what escapes
        if isinstance(error, _KNOWN_DEFECTS.get(label, ())):
            return None
        raise AssertionError(
            f"{label} raised {type(error).__name__}({error}) instead of a PlayalongError"
            f"\n  seed: {_SEED}"
            f"\n  input: {evidence}"
        ) from error


def _mutate(rng: random.Random, data: bytes) -> bytes:
    """One mutation of `data`, capped at `_MUTATION_CEILING`."""

    if not data:
        data = b"x"
    cut = rng.randrange(len(data))
    kind = rng.randrange(8)
    if kind == 0:  # bit flip
        index = rng.randrange(len(data))
        flipped = bytes([data[index] ^ (1 << rng.randrange(8))])
        mutated = data[:index] + flipped + data[index + 1 :]
    elif kind == 1:  # truncation
        mutated = data[:cut]
    elif kind == 2:  # duplication of a slice
        end = min(len(data), cut + rng.randrange(1, 64))
        mutated = data[:end] + data[cut:end] + data[end:]
    elif kind == 3:  # delimiter injection
        mutated = data[:cut] + rng.choice(_HOSTILE_BYTES) + data[cut:]
    elif kind == 4:  # huge repeat
        end = min(len(data), cut + rng.randrange(1, 16))
        chunk = data[cut:end] or b"a"
        times = min(rng.choice((64, 1024, 16384)), _MUTATION_CEILING // len(chunk))
        mutated = data[:cut] + chunk * max(1, times) + data[end:]
    elif kind == 5:  # deletion
        end = min(len(data), cut + rng.randrange(1, 64))
        mutated = data[:cut] + data[end:]
    elif kind == 6:  # hostile text, as the bytes it encodes to
        injected = rng.choice(_HOSTILE_TEXT).encode("utf-8", "surrogatepass")
        mutated = data[:cut] + injected + data[cut:]
    else:  # nesting
        depth = rng.choice(_NESTING_DEPTHS)
        mutated = data[:cut] + b"[" * depth + b"]" * depth + data[cut:]
    return mutated[:_MUTATION_CEILING]


def _mutations(rng: random.Random, text: str) -> bytes:
    data = text.encode("utf-8")
    for _round in range(rng.randrange(1, 4)):
        data = _mutate(rng, data)
    return data


def _hostile_string(rng: random.Random, *, letters: int = 10) -> str:
    pieces = [rng.choice(_HOSTILE_TEXT) for _ in range(rng.randrange(0, 4))]
    pieces.append("".join(rng.choice("abcXYZ 019.-_:/") for _ in range(rng.randrange(0, letters))))
    rng.shuffle(pieces)
    return "".join(pieces)


# --------------------------------------------------------------------------- #
# What a well-formed result has to look like
# --------------------------------------------------------------------------- #


def _check_cues(cues: Sequence[LyricCue], evidence: str) -> None:
    """What `lyrics`' module docstring says every cue list leaving it satisfies.

    The two load-bearing ones first: sorted by start with `end >= start`,
    adjacent cues not overlapping, timed words non-decreasing inside their cue;
    and both counts inside the ceilings the native reader enforces (`MAX_CUES`,
    `MAX_WORDS`). Then two the passes establish without listing them there --
    `_normalize_once` floors every start at 0.0, and every text has been through
    `_plain_text`, so none of them can carry an unprintable.
    """

    context = f"\n  seed: {_SEED}\n  input: {evidence}"
    assert len(cues) <= MAX_CUES, f"{len(cues)} cues exceeds MAX_CUES{context}"
    total_words = sum(len(cue["words"]) for cue in cues)
    assert total_words <= MAX_WORDS, f"{total_words} words exceeds MAX_WORDS{context}"
    for index, cue in enumerate(cues):
        assert math.isfinite(cue["start"]) and math.isfinite(cue["end"]), f"non-finite{context}"
        assert cue["start"] >= 0.0, f"cue {index} starts before zero{context}"
        assert cue["end"] >= cue["start"], f"cue {index} ends before it starts{context}"
        assert cue["text"].isprintable(), f"cue {index} text is not printable{context}"
        if index:
            previous = cues[index - 1]
            assert cue["start"] >= previous["start"], f"cue {index} is out of order{context}"
            assert previous["end"] <= cue["start"], f"cue {index - 1} overlaps {index}{context}"
        for position, word in enumerate(cue["words"]):
            assert word["end"] >= word["start"], f"word {index}.{position} reverses{context}"
            if position:
                earlier = cue["words"][position - 1]
                assert word["start"] >= earlier["start"], (
                    f"word {index}.{position} is out of order{context}"
                )


def _check_document(document: LyricsDocument, evidence: str) -> None:
    _check_cues(document.cues, evidence)
    context = f"\n  seed: {_SEED}\n  input: {evidence}"
    assert len(document.lines) <= MAX_CUES, f"{len(document.lines)} lines exceeds MAX_CUES{context}"
    assert isinstance(document.source, str), f"source is not a string{context}"
    assert isinstance(document.language, str), f"language is not a string{context}"


def _check_alignment(result: alignment.AlignmentResult, given: int, evidence: str) -> None:
    context = f"\n  seed: {_SEED}\n  input: {evidence}"
    lines = result.lines
    assert len(lines) <= given, f"{len(lines)} lines from {given} given{context}"
    characters = sum(len(word.text) for line in lines for word in line.words)
    assert characters <= alignment.MAX_REFERENCE_CHARS, (
        f"{characters} characters exceeds MAX_REFERENCE_CHARS{context}"
    )
    for index, line in enumerate(lines):
        assert line.end >= line.start, f"line {index} ends before it starts{context}"
        assert math.isfinite(line.start) and math.isfinite(line.end), f"non-finite{context}"
        if index:
            assert line.start >= lines[index - 1].start, f"line {index} is out of order{context}"
        for position, word in enumerate(line.words):
            assert word.end >= word.start, f"word {index}.{position} reverses{context}"
            if position:
                earlier = line.words[position - 1]
                assert word.start >= earlier.start, f"word {index}.{position} reverses{context}"
    report = result.report
    assert 0.0 <= report.matched_fraction <= 1.0, f"matched_fraction out of range{context}"
    assert 0.0 <= report.exact_fraction <= 1.0, f"exact_fraction out of range{context}"
    assert report.mean_displacement >= 0.0, f"negative displacement{context}"


def _check_metadata(metadata: source.MediaMetadata, evidence: str) -> None:
    """The component bounds `MediaMetadata.as_json` works its ceiling out from.

    Not the 7,081-character total itself -- `tests/test_source.py` measures that
    one -- but the per-field caps it is the sum of, checked here against
    documents that were built to break them.
    """

    context = f"\n  seed: {_SEED}\n  input: {evidence}"
    for name, value in (
        ("title", metadata.title),
        ("artist", metadata.artist),
        ("title_tag", metadata.title_tag),
        ("artist_tag", metadata.artist_tag),
    ):
        assert len(value) <= MAX_DISPLAY_TEXT, f"{name} exceeds MAX_DISPLAY_TEXT{context}"
        assert value == " ".join(value.split()), f"{name} is not one collapsed line{context}"
        assert value.isprintable(), f"{name} is not printable{context}"
    assert len(metadata.container) <= 64, f"container exceeds 64 characters{context}"
    assert len(metadata.ignored_tags) <= source._MAX_IGNORED_TAGS, (
        f"{len(metadata.ignored_tags)} ignored tags exceeds the cap{context}"
    )
    for tag in metadata.ignored_tags:
        assert len(tag) <= MAX_DISPLAY_TEXT, f"ignored tag exceeds MAX_DISPLAY_TEXT{context}"
    if metadata.lyrics is not None:
        assert len(metadata.lyrics.text) <= MAX_EMBEDDED_LYRICS_BYTES, (
            f"lyric tag exceeds MAX_EMBEDDED_LYRICS_BYTES{context}"
        )


# --------------------------------------------------------------------------- #
# Lyric files
# --------------------------------------------------------------------------- #


def test_mutated_lyric_files_are_parsed_or_refused(tmp_path: Path) -> None:
    """800 mutated lyric files: five seed documents under all five suffixes.

    The suffix picks the parser, so every document is fed to every one of them:
    a `.json` holding mutated LRC exercises the JSON arm's refusals, an `.lrc`
    holding mutated JSON exercises the stamp scanner on bytes it was never meant
    to see, and `.txt` is how the plain arm and `looks_like_lrc` are reached at
    all -- nothing routes there by suffix.
    """

    print(f"seed {_SEED}")
    rng = random.Random(_SEED + 1)
    rounds = 0
    parsed = 0
    for name, seed_text in _SEED_DOCUMENTS:
        for suffix in _SUFFIXES:
            path = tmp_path / f"lyrics-{name}{suffix}"
            for _round in range(32):
                data = _mutations(rng, seed_text)
                path.write_bytes(data)
                duration = rng.choice((0.5, 7.0, 300.0, 1.0e6))
                label = "lyrics.load_lyrics_document"
                if suffix == ".json":
                    label = "lyrics.load_lyrics_document[json]"
                evidence = f"{suffix} duration={duration} bytes={_brief(data)}"
                document = _refuse_or_return(
                    label,
                    evidence,
                    partial(lyrics_module.load_lyrics_document, path, duration=duration),
                )
                if document is not None:
                    _check_document(document, evidence)
                    parsed += 1
                rounds += 1
    assert rounds == 800
    # 222 of the 800 came back as a document when this was written. The floor is
    # half of that, and it is here so a corpus that decays into one every parser
    # refuses on sight cannot go on passing: a loop that never gets past the
    # first gate is testing the first gate.
    assert parsed >= 111, f"only {parsed} of {rounds} mutations parsed at all"


def test_hostile_text_reaches_every_lyric_text_parser(tmp_path: Path) -> None:
    """Structurally hostile documents, generated rather than mutated.

    Timestamps that are negative, out of order, enormous or malformed; stamps
    at the declared digit limits and past them; bidi overrides, astral text and
    NUL inside cue bodies; and empty or delimiter-only blocks.
    """

    print(f"seed {_SEED}")
    rng = random.Random(_SEED + 2)
    parsed = 0
    stamps = (
        "[00:00.00]",
        "[-1:00.00]",
        "[999:99.999]",
        "[9999:00.00]",
        "[00:00.0000]",
        "[00:60.00]",
        "[]",
        "[00:]",
    )
    clocks = (
        "00:00:00,000",
        "99:99:99,999",
        "-00:00:01,000",
        "00:00:00.0000",
        "0:0:0",
        "",
        "9999999999:00:00",
    )
    for index in range(160):
        style = index % 4
        if style == 0:
            body = "".join(
                f"{rng.choice(stamps)}{_hostile_string(rng)}\n" for _ in range(rng.randrange(0, 8))
            )
            suffix = ".lrc"
        elif style == 1:
            body = "".join(
                f"{count}\n{rng.choice(clocks)} --> {rng.choice(clocks)}\n"
                f"{_hostile_string(rng)}\n\n"
                for count in range(rng.randrange(0, 8))
            )
            suffix = rng.choice((".srt", ".vtt"))
        elif style == 2:
            body = json.dumps(
                {
                    # Weighted toward the real schema: a wrong one is refused
                    # two lines into `load_lyrics_document`, and what needs
                    # exercising is `_json_cues` behind it.
                    "schema": LYRICS_SCHEMA if rng.random() < 0.8 else rng.choice(("x", None, 7)),
                    "source": rng.choice(("imported-json", "x-estimated", 7, None)),
                    "language": rng.choice(("en", 7, None)),
                    "cues": [
                        {
                            "start": rng.choice(_HOSTILE_NUMBERS),
                            "end": rng.choice(_HOSTILE_NUMBERS),
                            "text": rng.choice((_hostile_string(rng), 7, None)),
                            "words": [
                                {
                                    "start": rng.choice(_HOSTILE_NUMBERS),
                                    "end": rng.choice(_HOSTILE_NUMBERS),
                                    "text": rng.choice((_hostile_string(rng), 7, None)),
                                }
                                for _ in range(rng.randrange(0, 4))
                            ],
                        }
                        for _ in range(rng.randrange(0, 6))
                    ],
                }
            )
            suffix = ".json"
        else:
            body = "\n".join(_hostile_string(rng, letters=24) for _ in range(rng.randrange(0, 12)))
            suffix = ".txt"
        path = tmp_path / f"hostile{suffix}"
        path.write_bytes(body.encode("utf-8", "surrogatepass"))
        duration = rng.choice((0.5, 7.0, 300.0))
        label = "lyrics.load_lyrics_document"
        if suffix == ".json":
            label = "lyrics.load_lyrics_document[json]"
        evidence = f"{suffix} duration={duration} body={_brief(body)}"
        document = _refuse_or_return(
            label,
            evidence,
            partial(lyrics_module.load_lyrics_document, path, duration=duration),
        )
        if document is not None:
            _check_document(document, evidence)
            parsed += 1
    # 25 of 160 when this was written; see the floor above for why there is one.
    # Lower than the mutation loop's share on purpose: these documents are built
    # hostile rather than damaged, so most of them have no usable cue in them.
    assert parsed >= 12, f"only {parsed} of 160 hostile documents parsed at all"


def test_mutated_lyric_tags_are_parsed_or_refused() -> None:
    """320 mutated tags through the embedded arm, which never touches a file.

    `parse_embedded_lyrics` takes a `str`, so the mutated bytes are decoded with
    `surrogateescape`: that is how a filename or a JSON document hands this
    module a string no encoder can write back out, and `_embedded_text` is what
    has to refuse it.
    """

    print(f"seed {_SEED}")
    rng = random.Random(_SEED + 3)
    rounds = 0
    documents = 0
    for _name, seed_text in _SEED_DOCUMENTS:
        for _round in range(64):
            text = _mutations(rng, seed_text).decode("utf-8", "surrogateescape")
            duration = rng.choice((0.5, 7.0, 300.0))
            tag = EmbeddedLyrics(
                tag=rng.choice(("lyrics", "UNSYNCEDLYRICS", "lyrics-eng", _hostile_string(rng))),
                text=text,
                language=rng.choice(("en", "eng", "auto", _hostile_string(rng))),
            )
            evidence = f"duration={duration} text={_brief(text)}"
            document = _refuse_or_return(
                "lyrics.parse_embedded_lyrics",
                evidence,
                partial(lyrics_module.parse_embedded_lyrics, tag, duration=duration),
            )
            if document is not None:
                _check_document(document, evidence)
                documents += 1
            # The chooser and the key parser see the same hostile text.
            mapping: dict[str, object] = {
                tag.tag: text,
                "lyrics": rng.choice((text, text.encode("utf-8", "surrogateescape"), 7, None)),
                _hostile_string(rng): text,
            }
            chosen = _refuse_or_return(
                "lyrics.select_embedded_lyrics",
                evidence,
                partial(lyrics_module.select_embedded_lyrics, mapping, language="en"),
            )
            if chosen is not None:
                assert len(chosen.text) <= MAX_LYRICS_BYTES, f"tag text unbounded\n  seed: {_SEED}"
            _refuse_or_return(
                "lyrics.embedded_lyrics_document",
                evidence,
                partial(
                    lyrics_module.embedded_lyrics_document,
                    mapping,
                    duration=duration,
                    language="auto",
                ),
            )
            key = _refuse_or_return(
                "lyrics.embedded_tag_key",
                evidence,
                partial(lyrics_module.embedded_tag_key, tag.tag),
            )
            if key is not None:
                assert key[0] in lyrics_module._EMBEDDED_PREFIXES
            rounds += 1
    assert rounds == 320
    # 239 of 320 when this was written; see the floor in the file loop above.
    assert documents >= 120, f"only {documents} of {rounds} mutated tags parsed at all"


def test_hostile_filenames_reach_the_caption_track_chooser() -> None:
    """200 caption-name sets, including every part the classifier keys on."""

    print(f"seed {_SEED}")
    rng = random.Random(_SEED + 4)
    parts = (
        "a",
        "auto",
        "asr",
        "orig",
        "en",
        "en-orig",
        "zh-hans-cn",
        "es-419",
        "live_chat",
        "",
        ".",
        "..",
        "\u202e",
        "\U0001f3b8",
        "x" * 300,
    )
    endings = (".vtt", ".srt", ".lrc", ".txt", "", ".VTT")
    for _round in range(200):
        paths = []
        for _ in range(rng.randrange(0, 6)):
            name = ".".join(rng.choice(parts) for _ in range(rng.randrange(0, 5)))
            paths.append(Path(name + rng.choice(endings)))
        language = rng.choice(("auto", "en", "", "\u202e", "x" * 300, "eng"))
        original = rng.choice((None, "en", "", "zz", "\U0001f3b8"))
        evidence = (
            f"paths={_brief([p.name for p in paths])} language={language!r} original={original!r}"
        )
        ranked = _refuse_or_return(
            "lyrics.rank_subtitles",
            evidence,
            partial(lyrics_module.rank_subtitles, paths, language, original_language=original),
        )
        if ranked is None:
            continue
        assert len(ranked) <= len(paths), f"chooser grew the list\n  seed: {_SEED}\n  {evidence}"
        for choice in ranked:
            # `SubtitleChoice` states both of these: `_LANGUAGE_TOKEN` bounds a
            # declared tag at twelve characters, and `reason` is built from
            # `lyrics`' own fixed phrases with nothing of the filename in it.
            assert len(choice.language) <= 12, f"language tag unbounded\n  {evidence}"
            assert choice.reason.isprintable(), f"reason is not printable\n  {evidence}"
            assert choice.source in set(lyrics_module._KIND_SOURCE.values()), (
                f"source id is not one of the three\n  {evidence}"
            )
        best = lyrics_module.choose_subtitle_track(paths, language, original_language=original)
        assert (best is None) == (not ranked)


# --------------------------------------------------------------------------- #
# Source strings and probe documents
# --------------------------------------------------------------------------- #


def test_hostile_source_strings_are_parsed_or_refused() -> None:
    """300 source strings: schemes, hosts, paths, and text no path can hold."""

    print(f"seed {_SEED}")
    rng = random.Random(_SEED + 5)
    parsed = 0
    heads = (
        "",
        "./",
        "../",
        "/",
        "~",
        "~nosuchuser/",
        "//",
        "http://",
        "https://",
        "file://",
        "file:///",
        "file://localhost/",
        "file://remote/",
        "ftp://",
        "javascript:",
        "C:\\",
        "https://www.youtube.com/watch?v=",
        "https://youtu.be/",
    )
    tails = (
        "song.mp3",
        "foo.com/bar",
        "%00",
        "%ff",
        "%c0%af",
        "a" * (MAX_SOURCE_LENGTH + 10),
        "..",
        "?q=1#f",
        "[",
        "\u202esong.mp3",
        "\U0001f3b8.mp3",
        "\x00",
        "\ud800",
        " ",
        "",
    )
    for _round in range(300):
        text = rng.choice(heads) + rng.choice(tails) + rng.choice(("", rng.choice(tails)))
        evidence = _brief(text)
        spec = _refuse_or_return(
            "source.parse_source", evidence, partial(source.parse_source, text)
        )
        if spec is not None:
            parsed += 1
            assert isinstance(spec, source.YouTubeSource | source.FileSource), evidence
            if isinstance(spec, source.FileSource):
                assert spec.path.is_absolute(), f"path is not absolute\n  {evidence}"
                assert spec.display_name.isprintable(), f"name is not printable\n  {evidence}"
        # `file_source` is reachable directly from an intake screen, without the
        # dispatch above having vetted the string first.
        direct = _refuse_or_return(
            "source.file_source", evidence, partial(source.file_source, text)
        )
        if direct is not None:
            assert direct.path.is_absolute(), f"path is not absolute\n  {evidence}"
            assert len(direct.display_name) <= MAX_DISPLAY_TEXT, f"name unbounded\n  {evidence}"
    # 85 of 300 were accepted as a source when this was written.
    assert parsed >= 42, f"only {parsed} of 300 source strings were accepted"


def _probe_document(rng: random.Random) -> dict[str, object]:
    """A crafted ffprobe document, round-tripped through JSON.

    Through JSON deliberately: `media.probe` hands `read_metadata` whatever
    `json.loads` made of ffprobe's stdout, so the round trip is what puts a lone
    surrogate or a 400-digit integer into the document the way the real boundary
    would.
    """

    keys = (
        "title",
        "artist",
        "album_artist",
        "performer",
        "lyrics",
        "LYRICS",
        "lyrics-eng",
        "UNSYNCEDLYRICS",
        "syncedlyrics",
        "uslt",
        "encoder",
        "duration",
        "comment",
        "\u00a9lyr",
    )

    def tags() -> dict[str, object]:
        out: dict[str, object] = {}
        for _ in range(rng.randrange(0, 6)):
            key = rng.choice(keys) if rng.random() < 0.6 else _hostile_string(rng)
            out[key] = rng.choice(
                (_hostile_string(rng, letters=40), rng.choice(_HOSTILE_NUMBERS), _LRC_SEED)
            )
        return out

    streams: list[object] = []
    for _ in range(rng.randrange(0, 4)):
        stream: object = {
            "codec_type": rng.choice(("audio", "video", "subtitle", 7, None)),
            "duration": rng.choice(_HOSTILE_NUMBERS),
            "tags": rng.choice((tags(), None, [], "x")),
        }
        streams.append(rng.choice((stream, None, "x", 5)))
    document: dict[str, object] = {
        "format": rng.choice(
            (
                {
                    "format_name": rng.choice((_hostile_string(rng, letters=80), 7, None)),
                    "duration": rng.choice(_HOSTILE_NUMBERS),
                    "tags": rng.choice((tags(), None, [])),
                },
                None,
                "x",
            )
        ),
        "streams": rng.choice((streams, None, "x")),
    }
    loaded = json.loads(json.dumps(document))
    assert isinstance(loaded, dict)
    return loaded


def test_crafted_probe_documents_are_read_or_refused() -> None:
    """200 crafted ffprobe documents through the metadata reader and its gates.

    `read_metadata` is the public entry; `_document_duration`, `_has_audio` and
    `_collect_tags` are the rest of what `inspect_file` runs against the same
    document, and they are called directly because reaching them the other way
    needs a real ffprobe and a real file. Existing tests read those three the
    same way (`tests/test_source.py`).
    """

    print(f"seed {_SEED}")
    rng = random.Random(_SEED + 6)
    stems = ("song", "", "\u202e", "\U0001f3b8", "x" * 400, "\x1b[2J")
    for _round in range(200):
        document = _probe_document(rng)
        path = Path("/library") / (rng.choice(stems) + rng.choice((".mp3", "", ".x" * 40)))
        duration = rng.choice((0.5, 7.0, 300.0))
        evidence = f"path={path.name!r} document={_brief(json.dumps(document))}"
        metadata = _refuse_or_return(
            "source.read_metadata",
            evidence,
            partial(source.read_metadata, path, document, duration),
        )
        if metadata is not None:
            _check_metadata(metadata, evidence)
        seconds = _refuse_or_return(
            "source._document_duration",
            evidence,
            partial(source._document_duration, document),
        )
        if seconds is not None:
            assert math.isfinite(seconds) and seconds > 0, f"unusable duration\n  {evidence}"
        _refuse_or_return("source._has_audio", evidence, partial(source._has_audio, document))
        merged = _refuse_or_return(
            "source._collect_tags", evidence, partial(source._collect_tags, document)
        )
        if merged is not None:
            assert len(merged) <= source._MAX_TAGS, f"tag map unbounded\n  {evidence}"


# --------------------------------------------------------------------------- #
# Alignment
# --------------------------------------------------------------------------- #


def test_hostile_alignment_inputs_are_aligned_or_refused() -> None:
    """200 reference/hypothesis pairs, both sides hostile.

    The reference carries markers, pure punctuation, digit runs at the lengths
    `_number_tokens` branches on, bidi and astral text; the hypothesis carries
    reversed, negative, non-finite and non-numeric stamps, missing keys and
    words that are not text at all.
    """

    print(f"seed {_SEED}")
    rng = random.Random(_SEED + 7)
    aligned = 0
    line_shapes = (
        "[Chorus]",
        "(Verse 2)",
        "(I love you)",
        "...",
        "1985",
        "0" * 12,
        "9" * 40,
        "don't stop",
        "\u202ehello world",
        "\U0001f3b8 \U0010ffff",
        "hello world again",
        "",
        "   ",
        "\t\t",
    )
    for _round in range(200):
        lines = [
            rng.choice(line_shapes) if rng.random() < 0.7 else _hostile_string(rng, letters=30)
            for _ in range(rng.randrange(0, 10))
        ]
        words: list[dict[str, object]] = []
        for _ in range(rng.randrange(0, 12)):
            word: dict[str, object] = {}
            if rng.random() < 0.9:
                word["start"] = rng.choice(_HOSTILE_NUMBERS)
            if rng.random() < 0.9:
                word["end"] = rng.choice(_HOSTILE_NUMBERS)
            if rng.random() < 0.9:
                word["text"] = rng.choice(
                    (rng.choice(line_shapes), _hostile_string(rng), 7, None, [])
                )
            words.append(word)
        duration = rng.choice((None, 0.5, 300.0, 1.0e9))
        evidence = f"lines={_brief(lines)}\n  words={_brief(words)}\n  duration={duration!r}"
        result = _refuse_or_return(
            "alignment.align_lines",
            evidence,
            partial(alignment.align_lines, lines, _as_words(words), audio_duration=duration),
        )
        if result is not None:
            _check_alignment(result, len(lines), evidence)
            aligned += 1
        text = "\n".join(lines)
        from_text = _refuse_or_return(
            "alignment.align_reference_text",
            evidence,
            partial(alignment.align_reference_text, text, _as_words(words)),
        )
        if from_text is not None:
            _check_alignment(from_text, len(text.splitlines()), evidence)
        cues: list[dict[str, object]] = [
            {
                "start": rng.choice(_HOSTILE_NUMBERS),
                "end": rng.choice(_HOSTILE_NUMBERS),
                "text": rng.choice((rng.choice(line_shapes), 7, None)),
                # A list or absent, never a non-list: `words` is the one field
                # here `hypothesis_from_cues` does not type-check, and feeding
                # it a non-list is pinned by
                # `test_a_cue_whose_words_field_is_not_a_list_is_refused`
                # rather than absorbed into this loop's tolerance, which would
                # have to be a bare TypeError to hold it.
                "words": rng.choice((words[:2], None, [])),
            }
            for _ in range(rng.randrange(0, 5))
        ]
        hypothesis = _refuse_or_return(
            "alignment.hypothesis_from_cues",
            evidence,
            partial(alignment.hypothesis_from_cues, _as_cues(cues)),
        )
        if hypothesis is None:
            continue
        # A cue contributes either its own words or one word per text piece,
        # and never both, so this is the whole of what the helper can produce.
        ceiling = sum(
            max(
                len(cue["words"]) if isinstance(cue["words"], list) else 0,
                len(cue["text"].split()) if isinstance(cue["text"], str) else 0,
            )
            for cue in cues
        )
        assert len(hypothesis) <= ceiling, f"hypothesis grew\n  seed: {_SEED}\n  {evidence}"
    # 68 of the 200 pairs aligned rather than being refused when this was
    # written; the rest are references with no alignable word in them, or
    # transcripts carrying the 400-digit integer pinned below.
    assert aligned >= 34, f"only {aligned} of 200 pairs aligned at all"


# --------------------------------------------------------------------------- #
# Every declared bound, at it and past it
# --------------------------------------------------------------------------- #


def test_the_lyric_file_byte_ceiling_holds_at_the_limit_and_one_past_it(tmp_path: Path) -> None:
    path = tmp_path / "big.txt"
    path.write_bytes(b"a" * MAX_LYRICS_BYTES)
    text = lyrics_module.read_bounded_text(path, limit=MAX_LYRICS_BYTES, what="lyrics file")
    assert len(text) == MAX_LYRICS_BYTES
    path.write_bytes(b"a" * (MAX_LYRICS_BYTES + 1))
    with pytest.raises(PlayalongError, match="4 MiB"):
        lyrics_module.read_bounded_text(path, limit=MAX_LYRICS_BYTES, what="lyrics file")


def _counted_json(cues: int, words_per_cue: int) -> str:
    """A lyrics document of exactly `cues` cues that normalisation cannot fold.

    Every line ends in a full stop, so `_merge_pair` refuses it (`_ends_open`),
    every text differs, so `_collapse_resends` refuses it, and the stamps sit
    two seconds apart, so nothing is simultaneous.
    """

    body = " ".join(f"w{index}" for index in range(words_per_cue - 1))
    return json.dumps(
        {
            "schema": LYRICS_SCHEMA,
            "source": "imported-json",
            "language": "en",
            "cues": [
                {
                    "start": index * 2.0,
                    "end": index * 2.0 + 1.0,
                    "text": f"L{index} {body}.",
                    "words": [],
                }
                for index in range(cues)
            ],
        }
    )


def test_the_cue_ceiling_holds_at_the_limit_and_one_past_it(tmp_path: Path) -> None:
    path = tmp_path / "cues.json"
    duration = MAX_CUES * 2.0 + 10.0
    path.write_text(_counted_json(MAX_CUES, 2))
    document = lyrics_module.load_lyrics_document(path, duration=duration)
    assert len(document.cues) == MAX_CUES
    _check_cues(document.cues, "MAX_CUES exactly")
    path.write_text(_counted_json(MAX_CUES + 1, 2))
    with pytest.raises(PlayalongError, match=f"more than {MAX_CUES} cues"):
        lyrics_module.load_lyrics_document(path, duration=duration)


def test_the_timed_word_ceiling_holds_at_the_limit_and_one_past_it(tmp_path: Path) -> None:
    """`MAX_WORDS` is 65,536 and `MAX_CUES` is 8,192, so eight words a cue is
    the point where both are exactly met at once."""

    per_cue = MAX_WORDS // MAX_CUES
    assert per_cue * MAX_CUES == MAX_WORDS
    path = tmp_path / "words.json"
    duration = MAX_CUES * 2.0 + 10.0
    path.write_text(_counted_json(MAX_CUES, per_cue))
    document = lyrics_module.load_lyrics_document(path, duration=duration)
    assert sum(len(cue["words"]) for cue in document.cues) == MAX_WORDS
    over = json.loads(_counted_json(MAX_CUES, per_cue))
    over["cues"][0]["text"] = over["cues"][0]["text"] + " extra."
    path.write_text(json.dumps(over))
    with pytest.raises(PlayalongError, match=f"more than {MAX_WORDS} timed words"):
        lyrics_module.load_lyrics_document(path, duration=duration)


def test_the_embedded_tag_walk_stops_at_its_bound() -> None:
    """`select_embedded_lyrics` looks at `MAX_EMBEDDED_TAGS` entries and no more."""

    sheet = "[00:01.00]one\n[00:02.00]two\n"
    inside: dict[str, object] = {f"junk{index}": "x" for index in range(MAX_EMBEDDED_TAGS - 1)}
    inside["lyrics"] = sheet
    assert len(inside) == MAX_EMBEDDED_TAGS
    chosen = lyrics_module.select_embedded_lyrics(inside)
    assert chosen is not None and chosen.text == sheet
    outside: dict[str, object] = {f"junk{index}": "x" for index in range(MAX_EMBEDDED_TAGS)}
    outside["lyrics"] = sheet
    assert lyrics_module.select_embedded_lyrics(outside) is None


def test_the_embedded_lyric_byte_ceiling_holds_at_the_limit_and_one_past_it() -> None:
    at_limit = "a" * MAX_EMBEDDED_LYRICS_BYTES
    document: dict[str, object] = {"format": {"tags": {"lyrics": at_limit}}, "streams": []}
    metadata = source.read_metadata(Path("/library/song.mp3"), document, 10.0)
    assert metadata.lyrics is not None
    assert len(metadata.lyrics.text) == MAX_EMBEDDED_LYRICS_BYTES
    over: dict[str, object] = {"format": {"tags": {"lyrics": at_limit + "a"}}, "streams": []}
    beyond = source.read_metadata(Path("/library/song.mp3"), over, 10.0)
    assert beyond.lyrics is None
    assert "lyrics" in beyond.ignored_tags


def test_the_tag_walk_and_the_ignored_list_stay_bounded() -> None:
    """4,000 distinct tags reduce to `_MAX_TAGS` read and `_MAX_IGNORED_TAGS` named."""

    tags = {f"tag{index:05d}": f"value {index}" for index in range(4000)}
    document: dict[str, object] = {"format": {"tags": tags}, "streams": []}
    metadata = source.read_metadata(Path("/library/song.mp3"), document, 10.0)
    _check_metadata(metadata, "4000 tags")
    assert len(source._collect_tags(document)) == source._MAX_TAGS
    assert len(metadata.ignored_tags) == source._MAX_IGNORED_TAGS


def test_the_reference_character_ceiling_holds_at_the_limit_and_one_past_it() -> None:
    """One long word per line, so the character ceiling is reached before the
    token ceiling is."""

    limit = alignment.MAX_REFERENCE_CHARS
    word = "x" * 1000
    lines = [word] * (limit // 1000)
    lines.append("y" * (limit % 1000 or 1))
    assert sum(len(line) for line in lines) in (limit, limit + 1)
    lines = [word] * (limit // 1000) + ["y" * (limit - 1000 * (limit // 1000))]
    lines = [line for line in lines if line]
    assert sum(len(line) for line in lines) == limit
    hypothesis: list[LyricWord] = [{"start": 0.0, "end": 1.0, "text": "x"}]
    result = alignment.align_lines(lines, hypothesis)
    _check_alignment(result, len(lines), "MAX_REFERENCE_CHARS exactly")
    with pytest.raises(PlayalongError, match=str(limit)):
        alignment.align_lines([*lines, "z"], hypothesis)


def test_the_token_ceiling_holds_at_the_limit_and_one_past_it() -> None:
    limit = alignment.MAX_TOKENS
    hypothesis: list[LyricWord] = [{"start": 0.0, "end": 1.0, "text": "la"}]
    at_limit = ["la " * limit]
    result = alignment.align_lines(at_limit, hypothesis)
    _check_alignment(result, 1, "MAX_TOKENS exactly")
    with pytest.raises(PlayalongError, match=str(limit)):
        alignment.align_lines(["la " * (limit + 1)], hypothesis)
    # And the same ceiling on the transcript side.
    long_hypothesis: list[LyricWord] = [
        {"start": float(index), "end": index + 0.5, "text": "la"} for index in range(limit + 1)
    ]
    with pytest.raises(PlayalongError, match=str(limit)):
        alignment.align_lines(["la"], long_hypothesis)


def test_the_alignment_cell_ceiling_refuses_a_pair_past_it() -> None:
    """Only past it, not at it.

    `MAX_ALIGNMENT_CELLS` is 6,000,000 and the check is made before any cell is
    filled, so a pair one cell over is refused instantly. A pair exactly at the
    bound would actually be aligned -- six million scored cells, which is
    minutes rather than the seconds this file is allowed, so it is deliberately
    not exercised here.
    """

    rows = alignment.MAX_TOKENS
    columns = alignment.MAX_ALIGNMENT_CELLS // rows + 1
    hypothesis: list[LyricWord] = [
        {"start": float(index), "end": index + 0.5, "text": "la"} for index in range(columns)
    ]
    with pytest.raises(PlayalongError, match=str(alignment.MAX_ALIGNMENT_CELLS)):
        alignment.align_lines(["la " * rows], hypothesis)


# --------------------------------------------------------------------------- #
# The six escapes this suite found, now closed.
#
# Each was a parser leaving through an exception that is not a PlayalongError,
# which `cli.py` does not catch and a user therefore meets as a traceback. Two
# are refused outright; four are dropped, because the contract this file states
# is "a well-formed result OR a PlayalongError", and for these four dropping the
# one bad value is the better half of it: `_prepare_hypothesis` exists to "drop
# what cannot be trusted", and `pipeline._apply_alignment` answers an
# InvalidInputError by falling back to evenly spread timing -- so raising over a
# single corrupt timestamp would throw away every other word's timing to punish
# it. These tests therefore assert what the fix does, not the raise the original
# reproducers guessed at.
# --------------------------------------------------------------------------- #


def test_deeply_nested_lyrics_json_is_refused(tmp_path: Path) -> None:
    """`json.loads` recurses once per nesting level; the byte bound is no depth bound."""

    path = tmp_path / "deep.json"
    path.write_text("[" * 40000 + "]" * 40000)
    with pytest.raises(PlayalongError):
        lyrics_module.load_lyrics_document(path, duration=10.0)


def test_a_lyrics_json_integer_too_large_for_a_float_is_refused(tmp_path: Path) -> None:
    """A 401-digit integer satisfies `int | float` and overflows the conversion."""

    path = tmp_path / "huge.json"
    path.write_text(
        '{"schema":"'
        + LYRICS_SCHEMA
        + '","cues":[{"start":1'
        + "0" * 400
        + ',"end":1,"text":"hi"}]}'
    )
    with pytest.raises(PlayalongError):
        lyrics_module.load_lyrics_document(path, duration=10.0)


def test_a_probe_document_carrying_a_lone_surrogate_lyrics_tag_is_dropped() -> None:
    """No encoder can write a lone surrogate, and ffprobe's JSON can carry one.

    The tag is declined rather than the file refused: a container whose lyrics
    tag is unreadable is still a playable container, and the intake's job is the
    audio.
    """

    document = json.loads('{"format":{"tags":{"lyrics":"\\ud800bad"}},"streams":[]}')
    metadata = source.read_metadata(Path("/library/song.mp3"), document, 10.0)
    assert metadata.lyrics is None


def test_an_alignment_timestamp_too_large_for_a_float_is_dropped() -> None:
    """The word goes; the alignment does not."""

    huge = int("1" + "0" * 400)
    words: list[dict[str, object]] = [
        {"start": huge, "end": 1.0, "text": "hello"},
        {"start": 0.5, "end": 1.0, "text": "world"},
    ]
    result = alignment.align_lines(["hello world"], _as_words(words))
    assert result is not None


def test_a_cue_timestamp_too_large_for_a_float_is_dropped() -> None:
    """Reached through `hypothesis_from_cues`, which reads a supplied caption track."""

    huge = int("1" + "0" * 400)
    cues: list[dict[str, object]] = [
        {"start": huge, "end": 1.0, "text": "hi", "words": []},
        {"start": 0.0, "end": 1.0, "text": "ok", "words": []},
    ]
    words = alignment.hypothesis_from_cues(_as_cues(cues))
    assert all(math.isfinite(word["start"]) for word in words)
    assert not any(word["start"] > 1e6 for word in words)


def test_a_cue_whose_words_field_is_not_a_list_is_dropped() -> None:
    """`words` is now checked the way `start`, `end` and `text` already were.

    No path inside this package delivers a non-list -- `lyrics._json_cues`
    rejects one and the pipeline feeds cues straight from
    `load_lyrics_document` -- so this closed a hole in a defence the function
    already mounted for its other three fields, not a live traceback.
    """

    cues: list[dict[str, object]] = [{"start": 0.0, "end": 1.0, "text": "hi", "words": 7}]
    assert alignment.hypothesis_from_cues(_as_cues(cues)) is not None
