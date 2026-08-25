"""Bounded lyric ingestion, from every place lyrics arrive, in one cue shape.

Lyrics reach this module from five untrusted places -- a downloaded caption
track, an ``.lrc`` sidecar sitting beside a local file, a lyrics tag carried
inside the media itself, a file the user picked, and our own generated JSON --
and leave it as one list of cues that both readers accept without repair.

Two properties are load-bearing for those readers and are established here, not
hoped for:

* cues come out sorted by start, with ``end >= start``, and so do the timed
  words inside a cue.  ``kpa_project.c:timed_span`` rejects a document whose
  spans go backwards rather than sorting it on load, because sorting would
  renumber the array and index N would stop meaning the same cue in the player,
  and in the native surface.  Adjacent cues are also kept from
  overlapping, which that reader does *not* reject.  An overlap does not put two
  lines on screen at once: each surface resolves a time to a single cue, the
  newest one that has started.  What it does is make the earlier cue record an
  end it never reaches, because its successor takes the screen first, and leave
  the written document one that a resume loads back *changed*, since this pass
  clamps the overlap away on the way in;
* the cue count and word count stay inside ``KPA_MAX_CUES`` / ``KPA_MAX_WORDS``
  from ``include/kilix_playalong/kpa_project.h``.  A document over those bounds
  is rejected here, where the message can say what is wrong, rather than at
  load time in the surface, where it is only ``KPA_TOO_LARGE``.

Segmentation is shaped for singing rather than for speech.  Sung lines are
longer than spoken ones, they are separated by instrumental gaps that no cue
should be stretched across, and caption tracks arrive fragmented mid-phrase.
The passes in ``_normalize`` merge fragments back into a line, cut a cue short
where it would otherwise be held across an instrumental break, fold away a line
that was merely re-sent while keeping one that is sung again, and give a fast
line long enough on screen to read -- always inside the ordering guarantee
above, never by pushing a cue over its neighbour.  They then run again until
they stop changing the document -- bounded, and ``_normalize`` says what the
bound costs -- because ``pipeline._lyrics`` writes ``lyrics.json`` and a resume
loads back what it wrote.

A document this module *writes* also says how its times were arrived at, in
``timing``: the source's own stamps (``authored``), forced alignment against the
audio (``measured``, and then ``alignment`` carries what it scored), or the even
spread invented here from the text and the duration (``estimated``).  Both fields
are additive to ``LYRICS_SCHEMA`` and both surfaces ignore what they do not know,
so a document written before them still loads -- `_stored_timing` says what such a
document reads back as, and why that is the most it can be asked.

Every parser here reads a file or a tag a user supplied.  The rule throughout
is to reject what cannot be parsed confidently rather than to repair it, and to
bound the work before doing it.
"""

from __future__ import annotations

import html
import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict, cast, get_args

from . import LYRICS_SCHEMA
from .errors import InvalidInputError
from .text import printable_line
from .types import LyricCue, LyricWord
from .util import canonical_json, private_write

#: A lyric *file*: what `read_bounded_text` refuses past and what a sidecar has
#: to be under to be offered. It is not the bound on a lyrics *tag* --
#: `source.MAX_EMBEDDED_LYRICS_BYTES` is that, and is tighter, because a tag is
#: read out of a mapping ffprobe has already parsed into memory. That tighter
#: bound runs first on every path this package ships, so the one below binds
#: only for a caller handing `select_embedded_lyrics` a mapping of its own.
MAX_LYRICS_BYTES = 4 * 1024 * 1024
#: KPA_MAX_CUES and KPA_MAX_WORDS in include/kilix_playalong/kpa_project.h. A
#: document over either bound loads nowhere, so it is refused at the parser.
MAX_CUES = 8192
MAX_WORDS = 65536
#: Directory entries a sidecar search may look at. Discovery happens without the
#: user naming a file, so it must not become an unbounded walk of a huge folder.
MAX_SIDECAR_ENTRIES = 4096
#: Container tags a lyrics search may examine. Each candidate value is scanned
#: for stamps, so the count has to be bounded before the scanning starts.
#: `source._MAX_TAGS` reads like the same policy and is not: it bounds the merge
#: of a container's several tag dictionaries into one mapping, which happens
#: before anything is scanned for stamps. Two walks, two bounds, neither derived
#: from the other.
MAX_EMBEDDED_TAGS = 256

#: Singing runs slower than speech, and a line is usually held a beat past its
#: last syllable. These two turn a line's text into the span it plausibly needs.
_SUNG_LEAD_SECONDS = 0.35
_SUNG_WORD_SECONDS = 0.55
_LINGER_SECONDS = 1.0
#: A line shorter than this is gone before it can be read.
_MIN_CUE_SECONDS = 1.2
#: The least span a cue is given on the way in -- intake and the .lrc parser
#: lift an end to it, and `_estimated_words` spreads a line across no less. It
#: is a starting span, not a floor the cue keeps: `_space_cues` clips an end
#: back to the next start, which is 0.02 s of span in
#: `test_a_readable_minimum_never_overlaps_a_neighbour_it_cannot_merge_with`,
#: and a zero-length span when the neighbour starts at the same instant.
_MIN_SPAN_SECONDS = 0.05
#: Two cues this close together are one instant, not a sequence.
_SIMULTANEOUS_SECONDS = 0.05
#: Fragments of one sung line sit essentially adjacent; a real line break does
#: not. Merging is gated on the gap *and* on the text reading as a continuation.
_MERGE_GAP_SECONDS = 0.35
_MERGE_CHARS = 84
_MERGE_SPAN_SECONDS = 9.0
#: A rolling caption re-sends the line it is extending, so its pair spans longer
#: than a fragment pair does and the join adds no text.
_EXTENSION_SPAN_SECONDS = 14.0
#: Held longer than this, and far longer than the words justify, a cue is
#: spanning an instrumental break rather than being sung.
_INSTRUMENTAL_SECONDS = 6.0
_INSTRUMENTAL_RATIO = 3.0
#: A re-sent line arrives essentially where the line it repeats ended; a line
#: stamped again later in the song is a different matter and is left alone.
_RESEND_GAP_SECONDS = 0.15
#: Extra applications of the passes allowed while the document is still moving.
#: One application is not a fixed point -- see `_normalize` -- and no document in
#: the suite, the randomised ones included, has changed after its second.
_NORMALIZE_ROUNDS = 4

_CLOCK = re.compile(
    r"(?:(?P<hours>\d{1,2}):)?(?P<minutes>\d{1,2}):(?P<seconds>\d{2}(?:[.,]\d{1,3})?)"
)
_LRC_STAMP = re.compile(r"\[(?P<minutes>\d{1,3}):(?P<seconds>\d{1,2}(?:\.\d{1,3})?)\]")
#: The enhanced-LRC (A2) word stamp. Karaoke files carry per-word timing that
#: _TAG would otherwise strip as if it were markup.
_LRC_WORD_STAMP = re.compile(r"<(?P<minutes>\d{1,3}):(?P<seconds>\d{1,2}(?:\.\d{1,3})?)>")
_LRC_LINE = re.compile(r"\s*\[\d{1,3}:\d{1,2}(?:\.\d{1,3})?\]")
_TAG = re.compile(r"<[^>]+>")
_SENTENCE_END = (".", "!", "?", "\u2026")
_CONTINUATION_END = (",", "-", "\u2013", "\u2014", "\u2026", ";", ":")
#: A BCP-47 language tag, in the shape the surrounding names actually carry:
#: a 2-3 letter primary subtag, then at most a script (4 letters) or region (2
#: letters or 3 digits), then at most a region.  "en", "eng", "pt-br", "es-419",
#: "zh-hans-cn" and yt-dlp's "en-orig" all match; the tag is at most 12
#: characters long.  The shape is the gate, not decoration: this pattern decides
#: which part of a user-supplied filename or tag key is a language, and that
#: value is recorded in the manifest and rendered, so a looser rule (anything
#: lowercase and hyphenated) would carry an arbitrary filename fragment there.
_LANGUAGE_TOKEN = re.compile(
    r"[a-z]{2,3}(?:-(?:[a-z]{4}|[a-z]{2}|[0-9]{3}))?(?:-(?:[a-z]{2}|[0-9]{3}))?"
)
_LANGUAGE_BASE = re.compile(r"[a-z]{2,3}")


def _clock_seconds(match: re.Match[str]) -> float:
    """A `[mm:ss.xx]`-style clock match as seconds.

    Named for the clock rather than for the unit: `source._positive_seconds` coerces an
    ffprobe number, which is an unrelated job under a name close enough to be
    mistaken for this one.
    """

    hours = float(match.groupdict().get("hours") or 0)
    minutes = float(match.group("minutes"))
    seconds = float(match.group("seconds").replace(",", "."))
    return hours * 3600 + minutes * 60 + seconds


#: What `_plain_text` can leave exactly as it found it: no markup, no entity, no
#: run of whitespace and nothing whitespace at either end. Together with
#: `str.isprintable` this is precisely the condition under which every pass in
#: that function is a no-op, so the early return computes the same string the
#: long way round would -- an equivalence
#: `test_the_plain_text_fast_path_computes_what_the_long_way_would` fuzzes rather
#: than one this comment asks to be believed.
_ALREADY_PLAIN = re.compile(r"[<&]|\s\s|\A\s|\s\Z")


def _plain_text(value: str) -> str:
    r"""One line of cue text: markup out, entities resolved, unprintables neutered.

    Every cue and every timed word in this module passes through here, which
    makes it where the property `source._clean_lyrics` establishes for an
    embedded tag is established for the intake paths that never touch `source`:
    a downloaded caption track, an imported `.srt`/`.vtt`/`.lrc`, the user's own
    `.txt`, and a `lyrics.json` written elsewhere. The collapse alone does not do
    it -- `\s` does not match ESC -- and none of those four is hypothetical: the
    text formats carry whatever bytes the file held, and JSON carries whatever
    `\u001b` its writer escaped. `html.unescape` is the one step that cannot make
    the C0 range worse: of the controls it decodes it drops every one except
    `&#9;`, `&#10;`, `&#12;` and `&#13;`, which the collapse handles anyway. It
    is no such guard above that range -- `&#144;` and `&#xad;` come through
    intact -- which is why the fold runs after it rather than before.

    The neutering is `text.printable_line`, the package's one display rule, not a
    second spelling of it: unprintables become spaces rather than being dropped,
    because dropping lets `a\x1b[31mb` close up into a word the file never
    contained. Getting that shared mattered here -- the embedded arm reaches a
    cue through `source._clean_lyrics` and every other arm reaches it through
    this function, so while the two disagreed one lyric document came out of
    intake as two different strings depending on which arm it arrived by.

    No length cap, deliberately -- `printable_line`'s `limit=None`. A sung line is
    as long as it is, and what this module bounds is the document (`MAX_CUES`,
    `MAX_WORDS`, `MAX_LYRICS_BYTES`), not any one line inside it.
    """

    if value.isprintable() and _ALREADY_PLAIN.search(value) is None:
        return value
    return printable_line(html.unescape(_TAG.sub("", value)))


#: ISO 639-1 to the 639-2 codes that turn up beside it. An ID3 USLT frame
#: declares its language as a three-letter code, so a caller asking for "en" and
#: a tag declaring "eng" are asking about the same language and have to compare
#: equal. Only the languages this app is likely to meet are listed: an unlisted
#: pair simply fails to match, which costs a preference and not a correct answer.
_LANGUAGE_ALIASES: dict[str, tuple[str, ...]] = {
    "ar": ("ara",),
    "cs": ("ces", "cze"),
    "da": ("dan",),
    "de": ("deu", "ger"),
    "el": ("ell", "gre"),
    "en": ("eng",),
    "es": ("spa",),
    "fi": ("fin",),
    "fr": ("fra", "fre"),
    "he": ("heb",),
    "hi": ("hin",),
    "hu": ("hun",),
    "id": ("ind",),
    "it": ("ita",),
    "ja": ("jpn",),
    "ko": ("kor",),
    "nl": ("nld", "dut"),
    "no": ("nor",),
    "pl": ("pol",),
    "pt": ("por",),
    "ro": ("ron", "rum"),
    "ru": ("rus",),
    "sv": ("swe",),
    "th": ("tha",),
    "tr": ("tur",),
    "uk": ("ukr",),
    "vi": ("vie",),
    "zh": ("zho", "chi"),
}
_LANGUAGE_CANONICAL: dict[str, str] = {
    alias: code for code, aliases in _LANGUAGE_ALIASES.items() for alias in aliases
}


def _language_base(language: str) -> str:
    """The primary subtag of a language selector, canonical, or "" when absent.

    "auto" is not a language: it means the caller has no preference, which is a
    different answer from a language we failed to recognise, and every caller
    below wants to tell those apart. The result is only ever compared, never
    shown, so folding 639-2 onto 639-1 here loses nothing a surface renders.
    """

    token = language.strip().lower()
    if not token or token == "auto":
        return ""
    head = re.split(r"[-_.,]", token, maxsplit=1)[0]
    if not _LANGUAGE_BASE.fullmatch(head):
        return ""
    return _LANGUAGE_CANONICAL.get(head, head)


# --------------------------------------------------------------------------- #
# Timed words
# --------------------------------------------------------------------------- #


def _estimated_words(text: str, start: float, end: float) -> list[LyricWord]:
    tokens = text.split()
    if not tokens:
        return []
    duration = max(_MIN_SPAN_SECONDS, end - start)
    weights = [max(1, len(token.strip(".,!?;:"))) for token in tokens]
    total = sum(weights)
    cursor = start
    result: list[LyricWord] = []
    for index, (token, weight) in enumerate(zip(tokens, weights, strict=False)):
        token_end = end if index == len(tokens) - 1 else cursor + duration * weight / total
        token_end = min(max(token_end, cursor), end)
        result.append({"start": round(cursor, 3), "end": round(token_end, 3), "text": token})
        cursor = token_end
    return result


def _normalize_words(words: Sequence[LyricWord], start: float, end: float) -> list[LyricWord]:
    """Pin supplied word timings inside their cue and into non-decreasing order.

    faster-whisper reports a word start of None for some words and the worker
    substitutes the segment bound, VAD can hand back a word that begins before
    the segment it belongs to, and an imported JSON carries whatever wrote it.
    The native reader rejects the whole project for one word that goes backwards
    (``fill_words`` -> ``timed_span``), so the disorder is clamped out here.
    Word spans are allowed to overlap each other -- real singing slurs, and the
    reader's search is defined for overlapping spans -- but never to reverse.
    """

    result: list[LyricWord] = []
    cursor = start
    for word in words:
        text = _plain_text(str(word.get("text", "")))
        if not text:
            continue
        raw_start = word.get("start")
        raw_end = word.get("end")
        start_value = _finite_number(raw_start)
        end_value = _finite_number(raw_end)
        if start_value is None or end_value is None:
            continue
        word_start = min(max(start_value, cursor), end)
        word_end = min(max(end_value, word_start), end)
        result.append({"start": round(word_start, 3), "end": round(word_end, 3), "text": text})
        cursor = word_start
    return result


# --------------------------------------------------------------------------- #
# Segmentation for singing
# --------------------------------------------------------------------------- #


def _sung_seconds(text: str) -> float:
    return _SUNG_LEAD_SECONDS + len(text.split()) * _SUNG_WORD_SECONDS


def _ends_open(text: str) -> bool:
    return not text.rstrip().endswith(_SENTENCE_END)


def _continues(previous: str, following: str) -> bool:
    """Whether `following` reads as the rest of `previous` rather than a new line.

    Lyric files capitalise every line, so a leading capital proves nothing on its
    own; a leading lowercase word or a trailing comma is the signal that survives
    both a caption track split mid-phrase and an .lrc with one line per stamp.
    """

    head = following[:1]
    return head.islower() or previous.rstrip().endswith(_CONTINUATION_END)


def _is_extension(previous: str, text: str) -> bool:
    """Whether a rolling caption re-sent `previous` with more of the line added."""

    return len(text) > len(previous) and text.startswith(previous) and text[len(previous)] == " "


def _joined_words(previous: LyricCue, cue: LyricCue) -> list[LyricWord]:
    """Word timings for a merged cue, or none when they would cover only half.

    A half-covered cue highlights the first fragment and then stops, which reads
    as a bug in the player. Dropping to an estimate for the whole line is worse
    timing and better behaviour.
    """

    if previous["words"] and cue["words"]:
        return [*previous["words"], *cue["words"]]
    return []


def _merge_pair(previous: LyricCue, cue: LyricCue) -> LyricCue | None:
    if previous["text"] == cue["text"]:
        # A line and a copy of itself are never two fragments of one line.
        # `_collapse_resends` has already folded away the copies that are one
        # performance arriving twice, so what reaches here is the line being
        # sung again -- and joining it would put "Hey Jude Hey Jude" in one row.
        return None
    gap = cue["start"] - previous["end"]
    end = max(previous["end"], cue["end"])
    span = end - previous["start"]
    if cue["start"] - previous["start"] < _SIMULTANEOUS_SECONDS:
        # Two stamps at one instant are two rows of one screen, and leaving them
        # separate forces one of them to a zero-length span in `_space_cues`.
        #
        # Unless one is the other with more of the line added, which is what a
        # rolling caption track sends: a stub carrying the tail of the previous
        # screen, stamped a hundredth of a second before the full line that
        # repeats it. Joining those two puts the tail on screen twice -- real
        # YouTube captions for a 218 s song came back as "just to have it taken
        # just to have it taken away people walk around pushing back", every
        # line doubled. So the extension test runs first here, exactly as it
        # does below the gap test, and takes the longer text instead of the sum
        # of both. Either order: the stub can arrive first or second, and only
        # the timestamps decide which cue this function was handed as previous.
        if _is_extension(previous["text"], cue["text"]):
            return {
                "start": previous["start"],
                "end": end,
                "text": cue["text"],
                "words": cue["words"] or previous["words"] or [],
            }
        if _is_extension(cue["text"], previous["text"]):
            return {
                "start": previous["start"],
                "end": end,
                "text": previous["text"],
                "words": previous["words"] or cue["words"] or [],
            }
        joined = f"{previous['text']} {cue['text']}"
        if len(joined) > _MERGE_CHARS * 2:
            return None
        return {
            "start": previous["start"],
            "end": end,
            "text": joined,
            "words": _joined_words(previous, cue),
        }
    if gap > _MERGE_GAP_SECONDS:
        return None
    if _is_extension(previous["text"], cue["text"]):
        if span > _EXTENSION_SPAN_SECONDS:
            return None
        return {
            "start": previous["start"],
            "end": end,
            "text": cue["text"],
            "words": cue["words"] or [],
        }
    joined = f"{previous['text']} {cue['text']}"
    if len(joined) > _MERGE_CHARS or span > _MERGE_SPAN_SECONDS:
        return None
    if not _ends_open(previous["text"]) or not _continues(previous["text"], cue["text"]):
        return None
    return {
        "start": previous["start"],
        "end": end,
        "text": joined,
        "words": _joined_words(previous, cue),
    }


def _merge_fragments(cues: list[LyricCue]) -> list[LyricCue]:
    """Merge fragments of one sung line, sweeping until nothing more merges.

    One sweep already absorbs a whole run of fragments into the line in front of
    them, because each merge lands back in `merged[-1]` and meets the next cue.
    The repeat is for what a sweep cannot see: a merge that makes its result
    mergeable with a cue the sweep has already emitted behind it. Every round
    that changes anything merges at least one pair, so the loop is bounded by
    the cue count.

    Being a fixed point of itself is still not enough to make `_normalize` one:
    a later pass moves the ends this one reads. That is `_normalize`'s problem
    and it solves it by running the whole sequence again.
    """

    for _round in range(len(cues)):
        merged: list[LyricCue] = []
        changed = False
        for cue in cues:
            if merged:
                candidate = _merge_pair(merged[-1], cue)
                if candidate is not None:
                    merged[-1] = candidate
                    changed = True
                    continue
            merged.append(cue)
        cues = merged
        if not changed:
            break
    return cues


def _trim_instrumental(cues: list[LyricCue]) -> None:
    """Split a held cue into the part that is sung and the gap that is not.

    An .lrc gives no end times, so `_parse_lrc` runs each cue up to the next
    stamp; across an instrumental break that leaves one line on screen for the
    whole break. Only the sung part is kept -- the break becomes an empty gap,
    which is what it is. The text is never divided: half a lyric line with a
    fabricated timestamp would be worse than a line that ends early.

    A cue whose own timed words run to its end is taken at its word and is not
    cut: the words are evidence that the line is sung across that span, and
    nothing here overrides evidence that fine-grained. That is also why this
    pass runs before `_collapse_resends`, whose spans are several of the file's
    own spans added together rather than one cue held across a break.
    """

    for cue in cues:
        span = cue["end"] - cue["start"]
        sung = _sung_seconds(cue["text"])
        if span <= _INSTRUMENTAL_SECONDS or span <= sung * _INSTRUMENTAL_RATIO:
            continue
        last_word = cue["words"][-1]["end"] if cue["words"] else cue["start"]
        cue["end"] = min(
            cue["end"],
            max(
                cue["start"] + _MIN_CUE_SECONDS,
                cue["start"] + sung + _LINGER_SECONDS,
                last_word + _LINGER_SECONDS,
            ),
        )


def _space_cues(cues: list[LyricCue], duration: float | None) -> None:
    """Give a fast line time to be read, and guarantee the ordering the surfaces want.

    The clamp is the guarantee: every end lands in [start, next start], so cues
    can neither overlap nor go backwards no matter what the parser produced.
    Order matters inside it -- the previous version floored the end at
    start + 0.05 *after* clipping it to the next start, so for cues closer
    together than that floor the clip did nothing and the pair came out
    overlapping: (5.0, 5.05) against a neighbour starting at 5.02 for the pair
    in `test_a_readable_minimum_never_overlaps_a_neighbour_it_cannot_merge_with`,
    where this order gives (5.0, 5.02). `kpa_project.c:timed_span` accepts an
    overlapping pair (it rejects only `end < start` and a start before its
    predecessor's), so nothing downstream catches it. What that costs is not two
    lines on screen at once -- both surfaces show one cue, the newest that has
    started, so the pair above renders one line at every instant either way --
    but a cue whose recorded end is one it never reaches: 0.05 s written, 0.02 s
    played. The document stops meaning what it says, and a resume loads it back
    clamped rather than as it was written.
    """

    for index, cue in enumerate(cues):
        if index + 1 < len(cues):
            limit = cues[index + 1]["start"]
        elif duration is not None:
            limit = duration
        else:
            limit = cue["end"] + _MIN_CUE_SECONDS
        wanted = max(cue["end"], cue["start"] + _MIN_CUE_SECONDS)
        cue["end"] = max(cue["start"], min(wanted, max(limit, cue["start"])))


def _collapse_resends(cues: list[LyricCue]) -> list[LyricCue]:
    """Fold a repeated line into the one before it when it is a re-send, not a repeat.

    A caption track re-sends the line it is holding and an ASR track emits the
    same words twice milliseconds apart. Both are one performance arriving
    twice, and leaving them separate puts a duplicate row in the document and,
    for the millisecond case, a cue too short to read.

    A line stamped again with time of its own is the other thing entirely -- a
    chorus line sung twice -- and both occurrences are kept, each with its own
    span. That is what `alignment._build_reference` does with a sheet's repeated
    lines (it keeps every one of them, in order), so a repeated chorus comes out
    the same shape whichever of the two paths produced it.

    The test is what the repeat adds to what is already on screen: a copy that
    arrives where the previous occurrence ended and extends it by less than the
    readable minimum was never a second performance the viewer could see. A
    folded copy does not contribute its words either -- the same words twice in
    one cue would highlight the line through and then start it again.
    """

    result: list[LyricCue] = []
    for cue in cues:
        if result:
            previous = result[-1]
            added = cue["end"] - max(cue["start"], previous["end"])
            if (
                previous["text"] == cue["text"]
                and cue["start"] <= previous["end"] + _RESEND_GAP_SECONDS
                and added < _MIN_CUE_SECONDS
            ):
                previous["end"] = max(previous["end"], cue["end"])
                continue
        result.append(cue)
    return result


def _normalize(
    cues: list[LyricCue],
    duration: float | None = None,
    *,
    timed: bool = True,
    authored_lines: bool = False,
) -> list[LyricCue]:
    """Clean, segment and order cues into the one shape both readers accept.

    Applying the passes once is not a fixed point, and a fixed point is what the
    pipeline needs: `pipeline._lyrics` writes `lyrics.json` and a resume loads
    back what it wrote, so a document that normalises to something else the
    second time renumbers cues under a player and a native surface
    that are already showing them by index. One pass is not enough because the
    passes read each other's output: `_space_cues` raises a fast line's end to
    the readable minimum, which narrows the gap to the next cue, which can unlock
    a merge `_merge_fragments` refused when it looked at the gap before. So the
    whole sequence runs again until it stops changing the document.

    The loop is bounded at `_NORMALIZE_ROUNDS` extra rounds: no document in the
    suite, the randomised ones included, has changed after its second
    application, and the bound is four. Reaching it would cost the fixed point
    and nothing else -- ordering, the duration clip and both count bounds are
    established by their own passes on every application, so a document that ran
    out of rounds would still be one the surfaces load and render correctly.
    """

    result = _normalize_once(cues, duration, timed=timed, authored_lines=authored_lines)
    for _round in range(_NORMALIZE_ROUNDS):
        settled = _normalize_once(result, duration, timed=timed, authored_lines=authored_lines)
        if settled == result:
            break
        result = settled
    return result


def _normalize_once(
    cues: list[LyricCue],
    duration: float | None,
    *,
    timed: bool,
    authored_lines: bool = False,
) -> list[LyricCue]:
    """One application of the passes, in the order they have to run in.

    `timed` is False for text that never had timings -- the spans are then a
    placeholder the aligner replaces, so merging fragments on an invented gap,
    cutting an invented hold short, or reading an invented adjacency as a
    re-sent line would all be reasoning about nothing. An untimed document
    therefore keeps one cue per line, which is what `LyricsDocument.lines`
    promises and what the aligner is handed.

    `authored_lines` carries that same argument one step further, for a format
    that authored its starts but not its ends. `_parse_lrc` gives every cue the
    next stamp's start as its end, so consecutive LRC cues are contiguous by
    construction and their gap is always zero -- which is not evidence that two
    lines continue each other, it is an artefact of how the end was invented.
    Merging on it read a three-line .lrc whose stamps sat 2 s apart as a single
    cue spanning the whole song, because lyrics are rarely punctuated and often
    lowercase, so the two tests that back-stop the gap both pass. A per-line
    stamp is the author saying where a line begins; that structure is
    authoritative and is kept. Subtitle formats are deliberately excluded: SRT
    and WebVTT carry real end times, so their gaps mean what they say and a
    sentence genuinely split across two cues should still be rejoined.
    """

    ordered = sorted(cues, key=lambda cue: (cue["start"], cue["end"], cue["text"]))
    result: list[LyricCue] = []
    for cue in ordered:
        text = _plain_text(cue["text"])
        start = max(0.0, float(cue["start"]))
        end = max(start + _MIN_SPAN_SECONDS, float(cue["end"]))
        if duration is not None:
            if start >= duration:
                continue
            end = min(end, duration)
        if not text:
            continue
        result.append({"start": start, "end": end, "text": text, "words": list(cue["words"])})
    if timed:
        # Trimming first is what keeps a fold honest: after `_collapse_resends`
        # a cue's span is several observed spans added together, and reading
        # that as a hold across an instrumental break would cut time the file
        # said was sung.
        _trim_instrumental(result)
        result = _collapse_resends(result)
        if not authored_lines:
            result = _merge_fragments(result)
    _space_cues(result, duration)
    words_total = 0
    for cue in result:
        cue["start"] = round(cue["start"], 3)
        cue["end"] = round(cue["end"], 3)
        words = _normalize_words(cue["words"], cue["start"], cue["end"])
        if not words:
            words = _estimated_words(cue["text"], cue["start"], cue["end"])
        cue["words"] = words
        words_total += len(words)
    if len(result) > MAX_CUES:
        raise InvalidInputError(f"lyrics contain more than {MAX_CUES} cues")
    if words_total > MAX_WORDS:
        raise InvalidInputError(f"lyrics contain more than {MAX_WORDS} timed words")
    return result


# --------------------------------------------------------------------------- #
# Parsers
# --------------------------------------------------------------------------- #


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
        body = _plain_text(" ".join(lines[timing_index + 1 :]))
        if body:
            cues.append(
                {
                    "start": _clock_seconds(start_match),
                    "end": _clock_seconds(end_match),
                    "text": body,
                    "words": [],
                }
            )
        if len(cues) > MAX_CUES:
            raise InvalidInputError(f"lyrics contain more than {MAX_CUES} cues")
    return cues


def _lrc_line(
    body: str,
    start: float,
    end: float,
    *,
    shift: float = 0.0,
) -> tuple[str, list[LyricWord]]:
    """One .lrc line's text, plus its word timings when it is enhanced LRC.

    `shift` carries a repeat. An .lrc line may carry several `[mm:ss]` stamps,
    which means the line is sung once at each of them, while its enhanced-LRC
    (A2) `<mm:ss>` word stamps are written once and are absolute -- so they can
    belong to only one of those occurrences, the earliest, the one they sit at
    or after. Replayed unchanged on a later stamp every one of them falls before
    that cue even begins, `_normalize_words` clamps them all into its first
    instant, and the player highlights the whole line at once and then never
    moves. Moved with the repeat they keep the offsets the file gave them.
    """

    matches = list(_LRC_WORD_STAMP.finditer(body))
    if not matches:
        return _plain_text(body), []
    pieces: list[tuple[float, str]] = []
    lead = _plain_text(body[: matches[0].start()])
    if lead:
        pieces.append((start, lead))
    for index, match in enumerate(matches):
        stop = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        token = _plain_text(body[match.end() : stop])
        if token:
            pieces.append((_clock_seconds(match) + shift, token))
    if not pieces:
        return "", []
    words: list[LyricWord] = []
    for index, (word_start, token) in enumerate(pieces):
        word_end = pieces[index + 1][0] if index + 1 < len(pieces) else end
        words.append({"start": word_start, "end": max(word_start, word_end), "text": token})
    return " ".join(token for _stamp, token in pieces), words


def _parse_lrc(text: str, duration: float) -> list[LyricCue]:
    # (when the line is sung, the earliest stamp on that line, the line). The
    # second element is what `_lrc_line` needs to move this line's A2 word
    # stamps onto a repeat of it.
    stamped: list[tuple[float, float, str]] = []
    for line in text.splitlines():
        stamps = list(_LRC_STAMP.finditer(line))
        if not stamps:
            continue
        body = _LRC_STAMP.sub("", line)
        if not _plain_text(body):
            continue
        times = [_clock_seconds(stamp) for stamp in stamps]
        base = min(times)
        for value in times:
            stamped.append((value, base, body))
            # Counted per stamp, not per line: one line may carry many.
            if len(stamped) > MAX_CUES:
                raise InvalidInputError(f"lyrics contain more than {MAX_CUES} cues")
    stamped.sort()
    cues: list[LyricCue] = []
    for index, (start, base, body) in enumerate(stamped):
        end = stamped[index + 1][0] if index + 1 < len(stamped) else min(duration, start + 5)
        end = max(start + _MIN_SPAN_SECONDS, end)
        line_text, words = _lrc_line(body, start, end, shift=start - base)
        if not line_text:
            continue
        cues.append({"start": start, "end": end, "text": line_text, "words": words})
    return cues


def looks_like_lrc(text: str) -> bool:
    """Whether text carries LRC stamps, rather than whether it claims to.

    An ID3 USLT frame is nominally *unsynchronised* lyrics and is very often
    LRC-formatted anyway, and users save .lrc content into .txt. Detecting beats
    trusting the container: the cost of guessing wrong in either direction is a
    whole song of lyrics timed by a spread instead of by its own stamps.
    """

    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    stamped = sum(1 for line in lines[: MAX_CUES * 2] if _LRC_LINE.match(line))
    return stamped >= 2 and stamped * 2 >= min(len(lines), MAX_CUES * 2)


def plain_lines(text: str) -> list[str]:
    """The untimed lyric lines of a plain-text document, cleaned and bounded.

    This is what forced alignment consumes: the words in order with no timing
    attached, which is exactly what `LyricsDocument.lines` carries when
    `has_timing` is False.
    """

    lines: list[str] = []
    for raw in text.splitlines():
        line = _plain_text(raw)
        if not line:
            continue
        lines.append(line)
        if len(lines) > MAX_CUES:
            raise InvalidInputError(f"lyrics contain more than {MAX_CUES} cues")
    return lines


def _parse_plain(text: str, duration: float) -> tuple[list[LyricCue], list[str]]:
    """Cues for untimed text, and the lines themselves.

    The even spread is a placeholder so something renders before alignment runs;
    the second element of the pair is the honest part, and a caller that gets a
    non-empty `lines` back knows this document has no timing of its own.
    """

    lines = plain_lines(text)
    if not lines:
        return [], []
    slot = duration / len(lines)
    cues: list[LyricCue] = [
        {
            "start": index * slot,
            "end": min(duration, (index + 1) * slot),
            "text": line,
            "words": [],
        }
        for index, line in enumerate(lines)
    ]
    return cues, lines


def _json_cues(value: object) -> list[LyricCue]:
    if not isinstance(value, list):
        raise InvalidInputError("lyrics JSON cues must be a list")
    if len(value) > MAX_CUES:
        raise InvalidInputError(f"lyrics contain more than {MAX_CUES} cues")
    cues: list[LyricCue] = []
    for item in value:
        if not isinstance(item, dict):
            raise InvalidInputError("lyrics JSON contains an invalid cue")
        start = item.get("start")
        end = item.get("end")
        text = item.get("text")
        words_value = item.get("words", [])
        # Through `_finite_number` rather than isinstance-then-float: a JSON
        # integer of four hundred digits satisfies `int | float` and overflows
        # the conversion, and OverflowError is not a PlayalongError, so it left
        # here as a traceback. Routing every reading of a number in this module
        # through the one helper is also what keeps that guard from having to
        # be repeated at each of the four places a cue carries a time.
        start_value = _finite_number(start)
        end_value = _finite_number(end)
        if (
            start_value is None
            or end_value is None
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
            word_start_value = _finite_number(word_start)
            word_end_value = _finite_number(word_end)
            if word_start_value is None or word_end_value is None or not isinstance(word_text, str):
                raise InvalidInputError("lyrics JSON contains an invalid timed word")
            words.append(
                {
                    "start": word_start_value,
                    "end": word_end_value,
                    "text": word_text,
                }
            )
        cues.append({"start": start_value, "end": end_value, "text": text, "words": words})
    return cues


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #


def _size_label(limit: int) -> str:
    """A byte ceiling as a refusal message spells it: `4 MiB`, `256 KiB`, `41 bytes`.

    Not `source.format_size`, which renders the same kind of number in yt-dlp's
    `--max-filesize` spelling (`512M`) because it is passed to yt-dlp. These are
    two renderings for two audiences, not one helper written twice.
    """

    for unit, scale in (("MiB", 1024**2), ("KiB", 1024)):
        if limit >= scale and limit % scale == 0:
            return f"{limit // scale} {unit}"
    return f"{limit} bytes"


def read_bounded_text(path: Path, *, limit: int, what: str) -> str:
    """One lyric-bearing file as text, refused rather than truncated past `limit`.

    Read the bound rather than stat it: a file that grows between the stat and
    the read, or a path that is a pipe rather than a file, would defeat a size
    check made before the bytes are taken. `limit + 1` bytes are asked for so
    that "exactly at the limit" and "over it" are told apart by the read itself
    and not by a second call.

    `utf-8-sig` because an editor writes a BOM in front of a lyric sheet and that
    BOM is not part of the first line. A NUL joins the decode failures rather
    than being passed on: every format this module parses is text, so a NUL in
    the middle of one means the file was misread, not that a lyric contains one.

    `what` is the noun the refusal uses, so a caller reading the user's own file
    and a caller reading a tag copied beside the media each say which of the two
    was too big. Pass a literal: the message is shown and logged, so a path or a
    filename must never be interpolated in through here.
    """

    try:
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
    except OSError as error:
        raise InvalidInputError(f"{what} is not readable UTF-8 text") from error
    if len(data) > limit:
        raise InvalidInputError(f"{what} exceeds the {_size_label(limit)} limit")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise InvalidInputError(f"{what} is not readable UTF-8 text") from error
    if "\x00" in text:
        raise InvalidInputError(f"{what} is not readable UTF-8 text")
    return text


#: How a document's cue times were arrived at, in the vocabulary ``LYRICS_SCHEMA``
#: carries and both surfaces read.
#:
#: * ``authored`` -- the source handed us the stamps and this build took them as given:
#:   an ``.lrc``, a caption track, an unsynchronised tag that turned out to hold LRC,
#:   and a transcript, whose stamps are the transcriber's own;
#: * ``measured`` -- forced alignment placed the user's words against the audio. It is
#:   the one value with numbers behind it, which is why `LyricAlignment` rides with it;
#: * ``estimated`` -- the spans are the even spread `_parse_plain` invented from the
#:   text and the duration. Nothing observed them.
#:
#: The distinction used to be readable only by taking the source id apart -- a
#: ``-estimated`` tail meant a spread -- which left every consumer coupled to how this
#: module spells a provenance string. The field answers it structurally instead.
LyricTiming = Literal["authored", "measured", "estimated"]
_TIMING_VALUES: frozenset[str] = frozenset(get_args(LyricTiming))
#: The tail of the two source ids this module gives a document it spread out itself,
#: ``imported-plain-estimated`` and ``embedded-plain-estimated``. Only `_stored_timing`
#: reads it, and only for a document written before ``timing`` existed;
#: `pipeline._ESTIMATED_SUFFIX` spells the same tail for the rename an accepted
#: alignment applies to the id.
_ESTIMATED_SUFFIX = "-estimated"


class LyricAlignment(TypedDict):
    """What a forced alignment measured, in the four numbers a reader needs to weigh it.

    The four that `pipeline._apply_alignment` copies straight out of
    `alignment.AlignmentReport` -- nothing is derived a second time here -- and they are
    the subset the document carries; the whole report stays in the manifest, which that
    same method writes. Counts, a fraction, a
    duration and a verdict: no lyric text, no path, no URL, which is the same rule the
    report itself is written under and the reason it can be copied straight into a file
    a surface reads.
    """

    #: Fraction of the alignable reference words the transcript actually matched, 0..1.
    matched_fraction: float
    #: How many words were placed by interpolation between measured neighbours.
    interpolated_words: int
    #: Mean worst-case placement error in seconds -- an upper bound on the error over
    #: the alignable words, not an observation of it. `alignment.AlignmentReport`
    #: documents what that bound rests on.
    mean_displacement: float
    #: The aligner's own verdict that this alignment was worth applying.
    usable: bool


@dataclass(frozen=True)
class LyricsDocument:
    """One parsed lyric source, with what the pipeline needs to route it.

    `timing` is how the cue times were arrived at, and `has_timing` -- the thing forced
    alignment turns on -- is derived from it rather than stored beside it, so the two
    cannot drift apart. `estimated` is the case the pipeline routes on: the cue spans
    are a placeholder this module invented -- a plain-text file or an unsynchronised
    lyrics tag has words in order and nothing else -- and the pipeline should hand
    `lines` to the aligner rather than write these spans out as if they had been
    observed.

    `note` is the only field here safe to copy into the stage note that
    `pipeline._lyrics` builds and `state.finish_stage` writes into the manifest:
    it is drawn from a fixed vocabulary in this module and carries no path, no
    URL and no lyric text.
    """

    cues: list[LyricCue]
    source: str
    language: str
    timing: LyricTiming = "authored"
    #: The report of the alignment that measured these times, when there is one. None
    #: everywhere else, and never invented for an authored source: an ``.lrc``'s timing
    #: is its author's and this build has not measured it.
    alignment: LyricAlignment | None = None
    lines: tuple[str, ...] = ()
    note: str = ""

    @property
    def has_timing(self) -> bool:
        """False only for the even spread, which is the case the aligner exists for."""
        return self.timing != "estimated"


_UNTIMED_NOTE = "no timestamps in this source; cue times are estimated until alignment runs"


def _stored_timing(value: object, *, source: str) -> LyricTiming:
    """How a stored document's spans were arrived at: its own answer, or the best left.

    The recorded field is authoritative whenever it is a value this build knows. A
    document that carries no readable one is a document written before the field
    existed, or by `_whisper_worker`, which writes the shape it always has -- and the
    only evidence left in those is the source id, whose ``-estimated`` tail this
    module's own untimed route put there. Reading a provenance out of that tail is the
    coupling ``timing`` exists to end, and it survives as an inference here alone, for
    the documents that predate the field. (`pipeline._aligned_source` still reads the
    tail, to rewrite an id and not to decide anything about timing.)

    Everything a document with stamps and no field cannot distinguish reads back as
    ``authored``: the stamps came in with the document and this build did not place
    them. That under-reports a resumed project whose spans really were measured, before
    the field was written -- it says the timing arrived with the source rather than that
    forced alignment produced it -- and the alternative is claiming a measurement with
    no report to show for it.
    """

    if isinstance(value, str) and value in _TIMING_VALUES:
        return cast(LyricTiming, value)
    return "estimated" if source.endswith(_ESTIMATED_SUFFIX) else "authored"


def _finite_number(value: object) -> float | None:
    """One JSON number, as a float, or None for anything that is not one.

    `bool` is excluded before `int` because `True` is an `int` in Python and a matched
    fraction of ``true`` is not a measurement.
    """

    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    # A JSON integer of four hundred digits satisfies the isinstance test and
    # then overflows the conversion. OverflowError is not a PlayalongError, so
    # without this it leaves cli.py as a traceback rather than a message.
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _stored_alignment(value: object) -> LyricAlignment | None:
    """A stored alignment report, or None when there is not a whole one to carry.

    Read liberally, written strictly. A document this build wrote carries all four
    numbers; one that arrived from anywhere else is taken only when every field is
    present, of the right kind and in range -- a fraction between 0 and 1, and a count
    and a displacement that no real report can make negative. Anything short of that
    becomes None rather than a half-filled report, because these numbers are a
    confidence a surface will show a user, and half of one is not a measurement.
    """

    if not isinstance(value, dict):
        return None
    matched = _finite_number(value.get("matched_fraction"))
    displacement = _finite_number(value.get("mean_displacement"))
    interpolated = value.get("interpolated_words")
    usable = value.get("usable")
    if matched is None or not 0.0 <= matched <= 1.0:
        return None
    if displacement is None or displacement < 0.0:
        return None
    if isinstance(interpolated, bool) or not isinstance(interpolated, int) or interpolated < 0:
        return None
    if not isinstance(usable, bool):
        return None
    return LyricAlignment(
        matched_fraction=matched,
        interpolated_words=interpolated,
        mean_displacement=displacement,
        usable=usable,
    )


def load_lyrics_document(
    path: Path,
    *,
    duration: float,
    source_hint: str | None = None,
) -> LyricsDocument:
    """Read one lyric file, bounded, and describe what came out of it.

    `source_hint` lets a caller that already knows what it downloaded name the
    source: only the caller can tell an auto-generated caption track from a
    human-authored one, and the difference matters enough that guessing it from
    the filename here would be worse than not answering.
    """

    if not math.isfinite(duration) or duration <= 0:
        raise InvalidInputError("lyrics duration must be finite and positive")
    raw = read_bounded_text(path, limit=MAX_LYRICS_BYTES, what="lyrics file")
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            value = json.loads(raw)
        # RecursionError, because json.loads descends one Python frame per
        # nesting level and a document is only bounded here by its byte count:
        # MAX_LYRICS_BYTES of "[" is thousands of levels deep. It is not a
        # subclass of JSONDecodeError, and it is not a PlayalongError, so it
        # left cli.py as a traceback. RecursionError inherits RuntimeError, so
        # it is named rather than caught by breadth.
        except (json.JSONDecodeError, RecursionError) as error:
            raise InvalidInputError("lyrics JSON is malformed") from error
        if not isinstance(value, dict) or value.get("schema") != LYRICS_SCHEMA:
            raise InvalidInputError("lyrics JSON has an unsupported schema")
        cues = _json_cues(value.get("cues"))
        source_value = value.get("source")
        language_value = value.get("language")
        json_source = source_hint or (
            source_value if isinstance(source_value, str) else "imported-json"
        )
        # A document we wrote from untimed text carries its own spread-out spans.
        # Reading it back must not turn an estimate into an observation, or a
        # resume would hand the aligner a document that claims to need nothing.
        # The document says which it is; `_stored_timing` falls back to the source
        # id only for one written before it did.
        json_timing = _stored_timing(value.get("timing"), source=json_source)
        # An alignment report belongs to measured timing and to nothing else, so one
        # recorded against any other provenance is dropped rather than carried into
        # the document this run writes.
        json_alignment = (
            _stored_alignment(value.get("alignment")) if json_timing == "measured" else None
        )
        json_timed = json_timing != "estimated"
        json_cues = _normalize(cues, duration, timed=json_timed)
        return LyricsDocument(
            cues=json_cues,
            source=json_source,
            language=language_value if isinstance(language_value, str) else "unknown",
            timing=json_timing,
            alignment=json_alignment,
            lines=() if json_timed else tuple(cue["text"] for cue in json_cues),
            note="" if json_timed else _UNTIMED_NOTE,
        )
    lines: list[str] = []
    authored = False
    if suffix == ".lrc":
        cues = _parse_lrc(raw, duration)
        source = "imported-lrc"
        authored = True
    elif suffix in {".vtt", ".srt"}:
        cues = _parse_block_timestamps(raw)
        source = "youtube-captions" if path.name.startswith("source") else "imported-timed-text"
    elif looks_like_lrc(raw):
        # A .txt holding LRC content is common enough that spreading its lines
        # evenly, with the stamps sitting right there, would be a bug.
        cues = _parse_lrc(raw, duration)
        source = "imported-lrc"
        authored = True
    else:
        cues, lines = _parse_plain(raw, duration)
        source = "imported-plain-estimated"
    normalized = _normalize(cues, duration, timed=not lines, authored_lines=authored)
    if not normalized:
        raise InvalidInputError("lyrics file contains no usable lyric cues")
    return LyricsDocument(
        cues=normalized,
        source=source_hint or source,
        language="unknown",
        # Every arm above that produced cues without `lines` read the stamps out of the
        # file: an .lrc, a .srt or .vtt caption track, or a .txt holding LRC. Those are
        # the source's own times, taken as given. `lines` is the plain-text arm, whose
        # spans are the even spread `_parse_plain` invented.
        timing="estimated" if lines else "authored",
        lines=tuple(lines),
        note=_UNTIMED_NOTE if lines else "",
    )


def load_lyrics(
    path: Path,
    *,
    duration: float,
    source_hint: str | None = None,
) -> tuple[list[LyricCue], str, str]:
    """`load_lyrics_document` reduced to its (cues, source, language) triple.

    The pipeline reads the document itself, because it needs the provenance
    fields -- `timing` and `alignment`, and `has_timing` off the first of them --
    along with `lines` and `note`. This is the short form for a caller that wants
    only the three fields, which in this package means the tests.
    """

    document = load_lyrics_document(path, duration=duration, source_hint=source_hint)
    return document.cues, document.source, document.language


# --------------------------------------------------------------------------- #
# Lyrics carried inside the media
# --------------------------------------------------------------------------- #

#: ffprobe surfaces an ID3 USLT frame as `lyrics` or `lyrics-<iso639-2>`, a
#: Vorbis comment as `LYRICS` or `UNSYNCEDLYRICS`, and an MP4 atom as `lyrics`.
#: `unsynced_lyrics` is the underscore spelling taggers also write. It is called
#: out because it is the one key the file arm used to accept off its own list that
#: this one refused, so it had to land here before `source` could be made to defer
#: -- deferring was only allowed to *add* keys, never to lose one a file arm
#: already found.
_EMBEDDED_PREFIXES = (
    "lyrics",
    "unsyncedlyrics",
    "syncedlyrics",
    "unsynced lyrics",
    "unsynced_lyrics",
    "uslt",
)


@dataclass(frozen=True)
class EmbeddedLyrics:
    """One lyrics tag lifted out of the media container."""

    tag: str
    text: str
    language: str = "unknown"


def embedded_tag_key(key: str) -> tuple[str, str] | None:
    """Split a container tag key into its lyrics prefix and language, or reject it.

    Public because this is the package's *only* answer to "does this key name
    lyrics, and in what language". `source` reads the same keys off the same
    ffprobe document one step earlier and calls this rather than keeping a list of
    its own; it used to keep one, and the two disagreed on ten of sixteen real tag
    spellings. Both directions cost something, and both were live: a
    `SYNCEDLYRICS` tag -- the *timed* one -- that no lyric route ever found, and a
    language the file declared that `pipeline._embedded_lyrics` had to drop
    because the key it fed back came out unparsed. One vocabulary is what makes
    the second impossible rather than merely unlikely: a key that reached
    `source` reached it through this function, so feeding it back cannot fail.

    Tag keys come from a file, so the language is taken only when it is a
    recognisable language token: it is recorded in the manifest and rendered.
    """

    lowered = key.strip().lower().replace("\u00a9", "")
    lowered = lowered.rsplit(":", 1)[-1].rsplit("/", 1)[-1].strip()
    if lowered == "lyr":
        return "lyrics", "unknown"
    for prefix in _EMBEDDED_PREFIXES:
        if lowered == prefix:
            return prefix, "unknown"
        for separator in ("-", "_"):
            marker = prefix + separator
            if lowered.startswith(marker):
                tail = lowered[len(marker) :]
                return prefix, tail if _LANGUAGE_TOKEN.fullmatch(tail) else "unknown"
    return None


def select_embedded_lyrics(
    tags: Mapping[str, object],
    *,
    language: str = "auto",
) -> EmbeddedLyrics | None:
    """Pick the best lyrics tag out of an ffprobe tag mapping, or None.

    Synchronised content wins over unsynchronised whatever the tag is called,
    because the tag name is a claim about the content and the stamps are the
    content. The requested language breaks the next tie, then length: a tag
    holding one line is a title, not lyrics.

    All three rules need every lyric tag the file carries. A caller that has
    already reduced them to one -- `pipeline._embedded_lyrics` does, because what
    it reads back is the single tag `source.acquire` chose to write beside the
    copy -- gets a ranking with nothing to rank, and whichever rule *that* caller
    picked with is the rule that decided the outcome. The order above is only
    the package's policy where the whole mapping arrives here.
    """

    best: tuple[tuple[int, int, int], EmbeddedLyrics] | None = None
    wanted = _language_base(language)
    for index, (key, value) in enumerate(tags.items()):
        if index >= MAX_EMBEDDED_TAGS:
            break
        if not isinstance(key, str):
            continue
        parsed = embedded_tag_key(key)
        if parsed is None:
            continue
        text = _embedded_text(value)
        if text is None or not text.strip():
            continue
        tag_language = parsed[1]
        rank = (
            0 if looks_like_lrc(text) else 1,
            0 if wanted and _language_base(tag_language) == wanted else 1,
            -len(text),
        )
        if best is None or rank < best[0]:
            best = (rank, EmbeddedLyrics(tag=parsed[0], text=text, language=tag_language))
    return None if best is None else best[1]


def _embedded_text(value: object) -> str | None:
    """A tag value as text, or None when it is not text we will read.

    bytes are decoded strictly: a lyrics frame that is not valid UTF-8 is
    rejected rather than repaired with replacement characters, which is the same
    rule the file readers follow.
    """

    if isinstance(value, bytes):
        if len(value) > MAX_LYRICS_BYTES:
            return None
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str):
        return None
    if "\x00" in value or len(value) > MAX_LYRICS_BYTES:
        return None
    try:
        # A str carrying lone surrogates is not text we can write back out.
        if len(value.encode("utf-8")) > MAX_LYRICS_BYTES:
            return None
    except UnicodeEncodeError:
        return None
    return value


def parse_embedded_lyrics(
    embedded: EmbeddedLyrics,
    *,
    duration: float,
) -> LyricsDocument:
    """Parse a lyrics tag lifted out of the media, detecting LRC rather than assuming.

    USLT is defined as *unsynchronised* lyrics and is routinely filled with LRC,
    so the tag's name decides nothing here; `looks_like_lrc` decides.
    """

    if not math.isfinite(duration) or duration <= 0:
        raise InvalidInputError("lyrics duration must be finite and positive")
    text = _embedded_text(embedded.text)
    if text is None:
        raise InvalidInputError("embedded lyrics tag is not readable UTF-8 text")
    lines: list[str] = []
    authored = False
    if looks_like_lrc(text):
        cues = _parse_lrc(text, duration)
        source = "embedded-lrc"
        authored = True
    else:
        cues, lines = _parse_plain(text, duration)
        source = "embedded-plain-estimated"
    normalized = _normalize(cues, duration, timed=not lines, authored_lines=authored)
    if not normalized:
        raise InvalidInputError("embedded lyrics tag contains no usable lyric cues")
    return LyricsDocument(
        cues=normalized,
        source=source,
        language=embedded.language,
        # A tag that held LRC carries the stamps its author wrote; a genuinely
        # unsynchronised one is spread out here and says so.
        timing="estimated" if lines else "authored",
        lines=tuple(lines),
        note=_UNTIMED_NOTE if lines else "",
    )


def embedded_lyrics_document(
    tags: Mapping[str, object],
    *,
    duration: float,
    language: str = "auto",
) -> LyricsDocument | None:
    """Select and parse the media's own lyrics tag; None when it carries none.

    None means "this file has no lyrics tag", which is an ordinary outcome. A
    tag that is present and unusable raises instead, because silently falling
    through to a transcription would hide a lyrics source the user has.

    This is the whole-mapping entry point, and the one that makes
    `select_embedded_lyrics`' ranking mean anything. The pipeline does not use
    it: it reads the tag back from the file the acquisition stage wrote rather
    than from an ffprobe document, so it calls the two halves separately on a
    one-entry mapping. Nothing in this package calls this one, then -- it is here
    for a caller holding a whole tag mapping, which is the only position from
    which the ranking has anything to rank.
    """

    embedded = select_embedded_lyrics(tags, language=language)
    if embedded is None:
        return None
    return parse_embedded_lyrics(embedded, duration=duration)


# --------------------------------------------------------------------------- #
# Sidecars beside a local file
# --------------------------------------------------------------------------- #


def _sidecar_rank(name: str, stem: str, wanted: str) -> tuple[int, int, str] | None:
    lowered = name.lower()
    if not lowered.endswith(".lrc"):
        return None
    base = lowered[: -len(".lrc")]
    stem_lowered = stem.lower()
    case_rank = 0 if name.startswith(stem) else 1
    if base == stem_lowered:
        return (0, case_rank, lowered)
    prefix = stem_lowered + "."
    if not base.startswith(prefix):
        return None
    tail = base[len(prefix) :]
    if not _LANGUAGE_TOKEN.fullmatch(tail):
        return None
    return (1 if wanted and _language_base(tail) == wanted else 2, case_rank, lowered)


def _sidecar_file(directory: Path, entry: os.DirEntry[str]) -> Path | None:
    """The entry as a readable regular file inside `directory`, or None.

    A symlink is followed only when it lands in the same directory. Discovery
    runs without the user naming the file, so a `song.lrc` pointing at something
    else on the disk is a file the user never chose to open.

    A link that does pass is returned as its target, not as the link: the caller
    opens the path we hand back some time later, and returning the link would
    leave a window in which the link is repointed between the check and the read.
    """

    candidate = directory / entry.name
    try:
        if entry.is_symlink():
            target = Path(entry.path).resolve(strict=True)
            if target.parent != directory or not target.is_file():
                return None
            candidate = target
        elif not entry.is_file(follow_symlinks=False):
            return None
        if candidate.stat().st_size > MAX_LYRICS_BYTES:
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate


def find_lyrics_sidecar(media: Path, *, language: str = "auto") -> Path | None:
    """The `.lrc` sitting beside a local media file, or None.

    `song.lrc` beside `song.mp3` is the near-universal convention; `song.LRC`
    and `song.en.lrc` are the variants that actually turn up. Nothing else is
    accepted -- an unrelated `.lrc` in the same folder belongs to another song.

    The scan is bounded at `MAX_SIDECAR_ENTRIES` and never leaves the directory
    it is searching.
    """

    try:
        resolved = media.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    directory = resolved.parent
    stem = resolved.stem
    if not stem or resolved == directory:
        return None
    wanted = _language_base(language)
    best: tuple[tuple[int, int, str], Path] | None = None
    try:
        with os.scandir(directory) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_SIDECAR_ENTRIES:
                    break
                rank = _sidecar_rank(entry.name, stem, wanted)
                if rank is None or (best is not None and rank >= best[0]):
                    continue
                candidate = _sidecar_file(directory, entry)
                if candidate is not None:
                    best = (rank, candidate)
    except OSError:
        return None
    return None if best is None else best[1]


# --------------------------------------------------------------------------- #
# Caption-track selection
# --------------------------------------------------------------------------- #

SubtitleKind = Literal["human", "automatic", "translated", "unknown"]

#: Formats the parsers above actually read. A track in any other format is not
#: returned at all: handing back a file that is certain to fail is worse than
#: answering None, which routes the caller to transcription.
_SUBTITLE_SUFFIXES = frozenset({".vtt", ".srt", ".lrc"})
_JUNK_PARTS = frozenset({"live_chat", "livechat", "rechat"})
_AUTOMATIC_PARTS = frozenset({"a", "auto", "asr", "autogenerated", "automatic"})
_KIND_ORDER: dict[SubtitleKind, int] = {
    "human": 0,
    "unknown": 1,
    "automatic": 2,
    "translated": 3,
}
_KIND_SOURCE: dict[SubtitleKind, str] = {
    "human": "youtube-captions",
    "unknown": "youtube-captions",
    "automatic": "youtube-captions-automatic",
    "translated": "youtube-captions-translated",
}
_KIND_REASON: dict[SubtitleKind, str] = {
    "human": "human-authored caption track",
    "unknown": "caption track of unstated origin",
    "automatic": "auto-generated caption track, a machine transcript",
    "translated": "machine-translated caption track, not the sung words",
}


@dataclass(frozen=True)
class SubtitleChoice:
    """One caption track, classified, with why it ranked where it did.

    `source` is the lyric-source id to record for this track. An auto-generated
    track is a machine transcript of the same quality class as running Whisper
    ourselves and a translated track is the wrong content entirely; recording
    all three as "youtube-captions" tells a later reader none of that.

    `reason` is drawn entirely from vocabulary fixed in this module -- the phrases
    in `_KIND_REASON` and, when the track declares a language this module lists,
    that language's own key in `_LANGUAGE_ALIASES`. Nothing from the filename is
    interpolated into it, so it is safe in the manifest, in a progress message
    and in a log: no path, no URL, no lyric text, and no fragment of a name the
    user chose. That is the property, established here so that the first caller
    to record it does not have to redact it; no caller records it yet.

    `language` is the track's declared tag as the filename spelled it, and it is
    a tag rather than a name fragment: `_track_name` takes it only where
    `_LANGUAGE_TOKEN` matches the whole part, which bounds it to a BCP-47 shape
    of at most twelve characters. A surface that renders it is still rendering
    something out of a filename and should escape it like any other such value.
    """

    path: Path
    language: str
    kind: SubtitleKind
    matches_language: bool
    source: str
    reason: str


@dataclass(frozen=True)
class _TrackName:
    language: str
    base: str
    automatic: bool
    explicit_automatic: bool
    original: bool


def _track_name(path: Path) -> _TrackName | None:
    """Classify a yt-dlp subtitle filename, or None when we cannot read it."""

    name = path.name.lower()
    suffix = Path(name).suffix
    if suffix not in _SUBTITLE_SUFFIXES:
        return None
    parts = name[: -len(suffix)].split(".")
    if any(part in _JUNK_PARTS for part in parts):
        return None
    language = ""
    marker_index = -1
    for index in range(len(parts) - 1, 0, -1):
        if _LANGUAGE_TOKEN.fullmatch(parts[index]):
            language = parts[index]
            marker_index = index
            break
    original = language.endswith("-orig")
    if original:
        language = language[: -len("-orig")]
    explicit = marker_index > 0 and parts[marker_index - 1] in _AUTOMATIC_PARTS
    return _TrackName(
        language=language,
        base=_language_base(language),
        # YouTube's "-orig" track is its own ASR output, so it is automatic, but
        # its presence is not evidence that a human track exists beside it.
        automatic=explicit or original,
        explicit_automatic=explicit,
        original=original,
    )


def rank_subtitles(
    paths: Sequence[Path],
    language: str,
    *,
    original_language: str | None = None,
) -> list[SubtitleChoice]:
    """Every usable caption track, best first, classified and explained.

    The order encodes three preferences, in this precedence:

    1. content over language. A translated track is the wrong words entirely, so
       it loses to an original-language track even when the translation is the
       language that was asked for;
    2. the requested language over another language, and any declared language
       over a track that declares none;
    3. human-authored over auto-generated. A track carrying no marker sits
       between the two -- yt-dlp writes an unmarked name for an auto track when
       no human one exists, so an unmarked name is evidence of nothing until a
       marked sibling in the same language proves a human track was also written.

    `original_language` is the only way to detect a translation whose filename
    does not admit to being one, which is the common YouTube case: pass the
    video's own language from the download metadata when it is known.

    With `language="auto"`, no `original_language` and no `-orig` track, nothing
    says what to prefer and the ranking falls back to English. That is a guess,
    and the only default language in this module; it decides which of several
    tracks wins and never how any of them is classified, so a caller that knows
    the language should say so rather than rely on it.
    """

    parsed: list[tuple[Path, _TrackName]] = []
    for path in paths:
        facts = _track_name(path)
        if facts is not None:
            parsed.append((path, facts))
    if not parsed:
        return []
    explicit_auto_bases = {facts.base for _path, facts in parsed if facts.explicit_automatic}
    original_base = _language_base(original_language or "")
    if not original_base:
        original_base = next(
            (facts.base for _path, facts in parsed if facts.original and facts.base), ""
        )
    wanted = _language_base(language) or original_base or "en"
    ranked: list[tuple[tuple[int, int, int, str], SubtitleChoice]] = []
    for path, facts in parsed:
        kind = _classify(facts, explicit_auto_bases, original_base)
        matches = bool(facts.base) and facts.base == wanted
        language_rank = 0 if matches else (1 if not facts.base else 2)
        ranked.append(
            (
                (1 if kind == "translated" else 0, language_rank, _KIND_ORDER[kind], path.name),
                SubtitleChoice(
                    path=path,
                    language=facts.language,
                    kind=kind,
                    matches_language=matches,
                    source=_KIND_SOURCE[kind],
                    reason=_subtitle_reason(kind, facts.language, matches),
                ),
            )
        )
    ranked.sort(key=lambda item: item[0])
    return [choice for _rank, choice in ranked]


def _classify(
    facts: _TrackName,
    explicit_auto_bases: set[str],
    original_base: str,
) -> SubtitleKind:
    if original_base and facts.base and facts.base != original_base:
        return "translated"
    if facts.automatic:
        return "automatic"
    if facts.base and facts.base in explicit_auto_bases:
        return "human"
    return "unknown"


def _subtitle_reason(kind: SubtitleKind, language: str, matches: bool) -> str:
    """Why this track ranked where it did, in words this module chose.

    The language is named only when it is one `_LANGUAGE_ALIASES` lists, and
    then by that table's own key rather than by the tag the file carried: a
    filename is user input, `_LANGUAGE_TOKEN` gates its shape and not its
    meaning (any 2-3 letter run passes, with any region or script subtag after
    it), and this string is written into events and logs that carry no input.
    """

    base = _language_base(language)
    if not language:
        where = "with no declared language"
    elif base in _LANGUAGE_ALIASES:
        where = f"in {base}"
    else:
        where = "in a language this build does not list"
    fit = "matching the requested language" if matches else "not the requested language"
    return f"{_KIND_REASON[kind]} {where}, {fit}"


def choose_subtitle_track(
    paths: Sequence[Path],
    language: str,
    *,
    original_language: str | None = None,
) -> SubtitleChoice | None:
    """The caption track to use, with its classification, or None."""

    ranked = rank_subtitles(paths, language, original_language=original_language)
    return ranked[0] if ranked else None


def write_lyrics(
    output: Path,
    cues: list[LyricCue],
    *,
    source: str,
    language: str,
    timing: LyricTiming = "authored",
    alignment: LyricAlignment | None = None,
) -> Path:
    """Write one lyrics document, saying where the times in it came from.

    `source` names *which* source won -- a caption track, an ``.lrc``, this app's own
    transcriber -- and `timing` says how the times in it were arrived at. Two questions,
    and a reader that wanted the second used to have to take the first apart, because a
    ``-estimated`` tail on the id was the only way to tell a spread from a measurement.
    `timing` answers it in the document itself, where the browser and ``kpa_project.c``
    can each read it without knowing how this module spells an id.

    `alignment` carries what that measurement scored and is refused with any other
    `timing`: those numbers describe a forced alignment, and there is none behind an
    authored ``.lrc`` or an even spread. A measured document *without* a report is still
    written -- a caller can hand one back that was read from a file that had none -- but
    nothing here invents one, and every alignment this app runs itself attaches its own.

    The default is what a caller that names no provenance has done: handed over cues it
    stamped itself, which is what ``authored`` describes. A caller writing back a
    document it *loaded* has to pass that document's own `LyricsDocument.timing`, or the
    default writes an estimate out as if its stamps had been authored and the next read
    believes it -- `test_a_reloaded_estimated_document_stays_untimed` is the guard on
    that, and `pipeline._lyrics` passes both fields explicitly on every route.

    Both fields are additive to ``LYRICS_SCHEMA``. A reader that has never heard of them
    keeps working -- ``kpa_project.c`` looks its members up by name and ignores the rest,
    which ``tests/native/test_project.c:test_unknown_fields`` pins -- and
    `load_lyrics_document` reads back a document that carries neither.
    """

    if timing not in _TIMING_VALUES:
        raise ValueError(f"unknown lyric timing provenance: {timing!r}")
    if alignment is not None and timing != "measured":
        raise ValueError(f"{timing} timing has no alignment to report")
    document = {
        "schema": LYRICS_SCHEMA,
        "source": source,
        "language": language,
        "timing": timing,
        "alignment": alignment,
        "cues": cues,
    }
    private_write(output, canonical_json(document))
    return output
