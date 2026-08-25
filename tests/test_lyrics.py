from __future__ import annotations

import html
import json
import random
import re
from pathlib import Path

import pytest

from kilix_playalong import LYRICS_SCHEMA, source
from kilix_playalong import lyrics as lyrics_module
from kilix_playalong.alignment import align_lines
from kilix_playalong.errors import InvalidInputError
from kilix_playalong.lyrics import (
    MAX_CUES,
    MAX_LYRICS_BYTES,
    MAX_WORDS,
    EmbeddedLyrics,
    LyricAlignment,
    choose_subtitle_track,
    embedded_lyrics_document,
    embedded_tag_key,
    find_lyrics_sidecar,
    load_lyrics,
    load_lyrics_document,
    parse_embedded_lyrics,
    rank_subtitles,
    read_bounded_text,
    select_embedded_lyrics,
    write_lyrics,
)
from kilix_playalong.types import LyricCue, LyricWord

_NATIVE_HEADER = (
    Path(__file__).resolve().parents[1] / "include" / "kilix_playalong" / "kpa_project.h"
)


def _reader_complaints(cues: list[LyricCue]) -> list[str]:
    """Everything kpa_project.c:timed_span rejects, plus adjacent overlap.

    `timed_span` (kpa_project.c:1057-1074) rejects exactly two things, for cues
    and for words alike: `end < start`, and a start before its predecessor's.
    Adjacent overlap it accepts, and an overlapping pair does not put two lines
    on screen either: each surface resolves a time to one cue (`active_index` in
    kpa_project.c, `lyricIndexAt` in web/app.js), so the earlier cue leaves the
    screen at its successor's start, before the end it records. An overlapping
    document means something the player never does, and reloads clamped. It is
    checked here because nothing downstream will catch it, not because the
    reader would.
    """

    problems: list[str] = []
    for index, cue in enumerate(cues):
        if cue["end"] < cue["start"]:
            problems.append(f"cue {index} ends before it starts")
        if index and cue["start"] < cues[index - 1]["start"]:
            problems.append(f"cue {index} starts before cue {index - 1}")
        if index and cues[index - 1]["end"] > cue["start"]:
            problems.append(f"cue {index - 1} overlaps cue {index}")
        previous = -1.0
        for position, word in enumerate(cue["words"]):
            if word["end"] < word["start"]:
                problems.append(f"word {index}.{position} ends before it starts")
            if word["start"] < previous:
                problems.append(f"word {index}.{position} starts before its predecessor")
            previous = word["start"]
    return problems


def _whisper_document(cues: list[dict[str, object]], *, source: str = "faster-whisper:tiny") -> str:
    return json.dumps({"schema": LYRICS_SCHEMA, "source": source, "language": "en", "cues": cues})


# --------------------------------------------------------------------------- #
# Existing behaviour that must not move
# --------------------------------------------------------------------------- #


def test_lrc_parsing_orders_cues_and_estimates_words(tmp_path: Path) -> None:
    source = tmp_path / "song.lrc"
    source.write_text("[00:03.00]Second line\n[00:01.00]First <b>line</b>\n")
    cues, provider, language = load_lyrics(source, duration=10)
    assert provider == "imported-lrc"
    assert language == "unknown"
    assert [cue["text"] for cue in cues] == ["First line", "Second line"]
    assert cues[0]["start"] == 1
    assert cues[0]["end"] == 3
    assert [word["text"] for word in cues[0]["words"]] == ["First", "line"]


def test_srt_and_plain_text_are_bounded_by_duration(tmp_path: Path) -> None:
    srt = tmp_path / "song.srt"
    srt.write_text(
        "1\n00:00:01,000 --> 00:00:02,500\nHello &amp; goodbye\n\n"
        "2\n00:00:03,000 --> 00:00:08,000\nLast line\n"
    )
    cues, source, _language = load_lyrics(srt, duration=4)
    assert source == "imported-timed-text"
    assert cues[0]["text"] == "Hello & goodbye"
    assert cues[-1]["end"] == 4

    plain = tmp_path / "song.txt"
    plain.write_text("Line one\n\nLine two\n")
    cues, source, _language = load_lyrics(plain, duration=8)
    assert source == "imported-plain-estimated"
    assert [(cue["start"], cue["end"]) for cue in cues] == [(0.0, 4.0), (4.0, 8.0)]


def test_lyrics_json_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "lyrics.json"
    original: list[LyricCue] = [{"start": 0.0, "end": 1.0, "text": "Hi", "words": []}]
    write_lyrics(target, original, source="test", language="en")
    document = json.loads(target.read_text())
    assert document["schema"] == LYRICS_SCHEMA
    cues, source, language = load_lyrics(target, duration=3)
    assert cues[0]["text"] == "Hi"
    assert source == "test"
    assert language == "en"


def test_invalid_or_empty_lyrics_are_rejected(tmp_path: Path) -> None:
    empty = tmp_path / "empty.txt"
    empty.write_text("\n")
    with pytest.raises(InvalidInputError, match="no usable"):
        load_lyrics(empty, duration=4)

    malformed = tmp_path / "bad.json"
    malformed.write_text('{"schema": "wrong"}')
    with pytest.raises(InvalidInputError, match="unsupported"):
        load_lyrics(malformed, duration=4)

    malformed.write_text("{")
    with pytest.raises(InvalidInputError, match="malformed"):
        load_lyrics(malformed, duration=4)

    malformed.write_text(
        json.dumps({"schema": LYRICS_SCHEMA, "cues": [{"start": "soon", "text": 3}]})
    )
    with pytest.raises(InvalidInputError, match="invalid cue"):
        load_lyrics(malformed, duration=4)


def test_subtitle_choice_prefers_requested_language(tmp_path: Path) -> None:
    english = tmp_path / "source.en.vtt"
    spanish = tmp_path / "source.es.vtt"
    chat = tmp_path / "source.es.live_chat.vtt"
    chosen = choose_subtitle_track([english, chat, spanish], "es-MX")
    assert chosen is not None
    assert chosen.path == spanish
    assert choose_subtitle_track([], "auto") is None


# --------------------------------------------------------------------------- #
# Ordering, overlap, and what the two readers require
# --------------------------------------------------------------------------- #


def test_a_transcript_never_yields_overlapping_or_backwards_cues(tmp_path: Path) -> None:
    """No cue in a 30-segment transcript comes out overlapping or backwards.

    The old `_space_cues` clipped an end to the next start and then floored it at
    start + 0.05, which put it back past that start whenever two segments were
    closer together than the floor. Ten input pairs here are 0.02 s apart, but
    `_merge_fragments` joins all ten before the clamp sees them, so what this
    fixture pins is the guarantee holding across a long transcript, not the old
    overlap -- the pair that reproduces that is in
    `test_a_readable_minimum_never_overlaps_a_neighbour_it_cannot_merge_with`.
    The native reader loads an overlapping pair without complaint
    (`_reader_complaints`), so this pass is the only thing keeping a cue's
    recorded span equal to the span that gets played.
    """

    cues: list[dict[str, object]] = []
    start = 0.0
    for index in range(30):
        cues.append(
            {
                "start": round(start, 3),
                "end": round(start + 2.4, 3),
                "text": f"Sentence number {index} of the synthetic transcript, long enough.",
                "words": [],
            }
        )
        start += 0.02 if index % 3 == 0 else 2.5
    source = tmp_path / "lyrics.json"
    source.write_text(_whisper_document(cues))

    parsed, _source, _language = load_lyrics(source, duration=200)
    assert parsed
    assert _reader_complaints(parsed) == []


def test_supplied_word_timings_are_clamped_into_their_cue_and_into_order(tmp_path: Path) -> None:
    """faster-whisper can report a word outside, or before, the segment it belongs to.

    kpa_project.c:fill_words rejects the whole project for one such word, so the
    disorder has to be gone before the document is written.
    """

    source = tmp_path / "lyrics.json"
    source.write_text(
        _whisper_document(
            [
                {
                    "start": 5.0,
                    "end": 7.0,
                    "text": "alpha beta gamma",
                    "words": [
                        {"start": 6.0, "end": 6.4, "text": "alpha"},
                        {"start": 4.2, "end": 5.1, "text": "beta"},
                        {"start": 6.5, "end": 12.0, "text": "gamma"},
                    ],
                }
            ]
        )
    )
    cues, _source, _language = load_lyrics(source, duration=60)
    assert _reader_complaints(cues) == []
    words = cues[0]["words"]
    assert [word["text"] for word in words] == ["alpha", "beta", "gamma"]
    assert all(cues[0]["start"] <= word["start"] <= cues[0]["end"] for word in words)
    assert all(cues[0]["start"] <= word["end"] <= cues[0]["end"] for word in words)


def test_cue_and_word_bounds_match_the_native_reader() -> None:
    if not _NATIVE_HEADER.is_file():
        pytest.skip("native header is not checked out in this worktree")
    header = _NATIVE_HEADER.read_text()
    cues = re.search(r"#define KPA_MAX_CUES\s+(\d+)u", header)
    words = re.search(r"#define KPA_MAX_WORDS\s+(\d+)u", header)
    assert cues is not None and words is not None
    assert int(cues.group(1)) == MAX_CUES
    assert int(words.group(1)) == MAX_WORDS


def test_a_document_over_the_cue_bound_is_rejected_here(tmp_path: Path) -> None:
    source = tmp_path / "huge.lrc"
    source.write_text(
        "".join(f"[{index // 60:02d}:{index % 60:02d}.00]Line {index}\n" for index in range(9000))
    )
    with pytest.raises(InvalidInputError, match="more than"):
        load_lyrics(source, duration=10_000)


def test_normalization_is_idempotent(tmp_path: Path) -> None:
    """The pipeline reads lyrics.json back and rewrites it; the second pass must be a no-op."""

    source = tmp_path / "lyrics.json"
    source.write_text(
        _whisper_document(
            [
                {"start": 1.0, "end": 2.0, "text": "and the first", "words": []},
                {"start": 2.1, "end": 3.0, "text": "half of a line", "words": []},
                {"start": 3.4, "end": 40.0, "text": "Held across the break", "words": []},
                {"start": 42.0, "end": 42.3, "text": "Quick", "words": []},
            ]
        )
    )
    once, provider, language = load_lyrics(source, duration=90)
    again = tmp_path / "again.json"
    write_lyrics(again, once, source=provider, language=language)
    twice, _source, _language = load_lyrics(again, duration=90)
    assert twice == once


def test_a_readable_minimum_does_not_unlock_a_merge_on_the_next_pass(tmp_path: Path) -> None:
    """One application of the passes is not a fixed point of itself.

    Cue 0 lasts 0.2 s and is 1.3 s clear of cue 1, which is too far apart to
    merge. `_space_cues` then stretches it to the 1.2 s readable minimum, which
    leaves a 0.3 s gap -- inside `_MERGE_GAP_SECONDS`. Loading the document back
    (which `pipeline._lyrics` does on every resume) therefore merged the pair
    and moved the cue count from 3 to 2, under a player, `state.py` and a native
    surface that address cues by index.
    """

    source = tmp_path / "lyrics.json"
    source.write_text(
        _whisper_document(
            [
                {"start": 0.0, "end": 0.2, "text": "and I will", "words": []},
                {"start": 1.5, "end": 3.0, "text": "always be here", "words": []},
                {"start": 20.0, "end": 22.0, "text": "Another line entirely", "words": []},
            ]
        )
    )
    once, provider, language = load_lyrics(source, duration=30)
    again = tmp_path / "again.json"
    write_lyrics(again, once, source=provider, language=language)
    twice, _source, _language = load_lyrics(again, duration=30)
    assert [(cue["start"], cue["end"], cue["text"]) for cue in twice] == [
        (cue["start"], cue["end"], cue["text"]) for cue in once
    ]
    assert twice == once
    assert _reader_complaints(once) == []


def _random_document(rng: random.Random) -> list[dict[str, object]]:
    """A document built out of the shapes the passes disagree about.

    Near-simultaneous starts, sub-second cues, holds long enough to look
    instrumental, repeated lines, lines that read as continuations, and lines
    too long to merge -- these are what make one pass move what another pass
    already decided.
    """

    lines = [
        "Hey Jude",
        "hey jude",
        "Na na na",
        "and I will",
        "always be here",
        "Take a sad song and make it better.",
        "Dont make it bad,",
        "A" * 90,
        "Go",
    ]
    cues: list[dict[str, object]] = []
    clock = rng.uniform(0.0, 3.0)
    for _ in range(rng.randrange(1, 14)):
        start = clock
        end = start + rng.choice([0.01, 0.05, 0.2, 1.0, 2.4, 7.0, 30.0]) * rng.uniform(0.5, 1.5)
        text = rng.choice(lines)
        words: list[dict[str, object]] = []
        if rng.random() < 0.4:
            cursor = start
            for token in text.split()[:6]:
                stop = min(end, cursor + rng.uniform(0.05, 0.9))
                words.append({"start": cursor, "end": stop, "text": token})
                cursor = stop
        cues.append({"start": start, "end": end, "text": text, "words": words})
        clock = start + rng.choice([0.0, 0.02, 0.3, 1.0, 2.5, 9.0]) * rng.uniform(0.5, 1.5)
    return cues


def test_normalization_settles_on_randomised_documents(tmp_path: Path) -> None:
    """Load, write, load again: the second document must be the first one.

    The fixed cases below each pin one pass; this pins the property the pipeline
    depends on across shapes nobody thought to write down. Every one of these
    documents settles after two applications of the passes.
    """

    source = tmp_path / "lyrics.json"
    again = tmp_path / "again.json"
    for seed in range(150):
        rng = random.Random(seed)
        source.write_text(_whisper_document(_random_document(rng)))
        once, provider, language = load_lyrics(source, duration=120)
        write_lyrics(again, once, source=provider, language=language)
        twice, _source, _language = load_lyrics(again, duration=120)
        assert twice == once, f"seed {seed} did not settle"
        assert _reader_complaints(once) == [], f"seed {seed} is not orderable"


def test_a_readable_minimum_never_overlaps_a_neighbour_it_cannot_merge_with(
    tmp_path: Path,
) -> None:
    """The clamp, not the merge, is what keeps two near-simultaneous cues apart.

    These two lines are 0.02 s apart -- closer than the 0.05 s floor the old
    `_space_cues` applied after clipping -- and too long to join (`_merge_pair`
    refuses a joined line over 2 x `_MERGE_CHARS`), so nothing but the clamp
    stands between them and an overlapping pair.
    """

    source = tmp_path / "lyrics.json"
    source.write_text(
        _whisper_document(
            [
                {"start": 5.0, "end": 7.4, "text": "A" * 90, "words": []},
                {"start": 5.02, "end": 7.42, "text": "B" * 90, "words": []},
            ]
        )
    )
    cues, _source, _language = load_lyrics(source, duration=60)
    assert len(cues) == 2, "these two are too long to be joined into one row"
    assert cues[0]["end"] <= cues[1]["start"]
    assert _reader_complaints(cues) == []


def test_a_document_over_the_word_bound_is_rejected_here(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KPA_MAX_WORDS is refused at the parser, where the message can say why."""

    source = tmp_path / "song.lrc"
    source.write_text("[00:01.00]Alpha beta gamma\n[00:05.00]Delta epsilon zeta\n")
    monkeypatch.setattr(lyrics_module, "MAX_WORDS", 5)
    with pytest.raises(InvalidInputError, match="more than 5 timed words"):
        load_lyrics(source, duration=30)
    monkeypatch.setattr(lyrics_module, "MAX_WORDS", 6)
    cues, _source, _language = load_lyrics(source, duration=30)
    assert sum(len(cue["words"]) for cue in cues) == 6


def test_the_bounded_read_tolerates_a_bom_and_refuses_a_nul(tmp_path: Path) -> None:
    """One read for every lyric-bearing file, so every caller gets the same rules.

    `pipeline._embedded_lyrics` had its own copy of these seven steps with two of
    them missing, which is why this is a function rather than a paragraph.
    """

    source = tmp_path / "song.lrc"
    source.write_bytes("\ufeff[00:01.00]Alpha beta\n[00:05.00]Gamma\n".encode())
    assert read_bounded_text(source, limit=MAX_LYRICS_BYTES, what="lyrics file").startswith("[")
    cues, _source, _language = load_lyrics(source, duration=30)
    assert cues[0]["text"] == "Alpha beta"

    binary = tmp_path / "binary.txt"
    binary.write_bytes(b"Alpha\x00beta\n")
    with pytest.raises(InvalidInputError, match="embedded lyrics tag is not readable"):
        read_bounded_text(binary, limit=MAX_LYRICS_BYTES, what="embedded lyrics tag")

    missing = tmp_path / "gone.txt"
    with pytest.raises(InvalidInputError, match="lyrics file is not readable"):
        read_bounded_text(missing, limit=MAX_LYRICS_BYTES, what="lyrics file")

    big = tmp_path / "big.txt"
    big.write_text("Alpha beta gamma\n")
    with pytest.raises(InvalidInputError, match="exceeds the 4 bytes limit"):
        read_bounded_text(big, limit=4, what="lyrics file")
    # The ceiling the user is actually told about, in the spelling they are told
    # it in: the refusal must not turn into "exceeds the 4194304 bytes limit".
    assert lyrics_module._size_label(MAX_LYRICS_BYTES) == "4 MiB"
    assert lyrics_module._size_label(256 * 1024) == "256 KiB"


def test_a_file_over_the_byte_limit_is_read_no_further_than_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound is on the read, so an oversized file is refused, never truncated."""

    source = tmp_path / "song.lrc"
    body = "[00:01.00]Alpha beta\n[00:05.00]Gamma delta\n"
    source.write_text(body)
    monkeypatch.setattr(lyrics_module, "MAX_LYRICS_BYTES", len(body.encode()))
    cues, _source, _language = load_lyrics(source, duration=30)
    assert len(cues) == 2
    monkeypatch.setattr(lyrics_module, "MAX_LYRICS_BYTES", len(body.encode()) - 1)
    with pytest.raises(InvalidInputError, match="exceeds"):
        load_lyrics(source, duration=30)


# --------------------------------------------------------------------------- #
# Segmentation for singing
# --------------------------------------------------------------------------- #


def test_caption_fragments_of_one_sung_line_are_merged(tmp_path: Path) -> None:
    captions = tmp_path / "source.en.vtt"
    captions.write_text(
        "WEBVTT\n\n"
        "00:00:01.000 --> 00:00:03.000\nand I will\n\n"
        "00:00:03.100 --> 00:00:05.000\nalways be here\n\n"
        "00:00:09.000 --> 00:00:11.000\nAnother line entirely\n"
    )
    cues, _source, _language = load_lyrics(captions, duration=30)
    assert [cue["text"] for cue in cues] == ["and I will always be here", "Another line entirely"]
    assert cues[0]["start"] == 1.0
    assert cues[0]["end"] == 5.0
    assert _reader_complaints(cues) == []


def test_separate_sung_lines_are_not_merged(tmp_path: Path) -> None:
    """An .lrc runs each cue to the next stamp, so a zero gap is the normal case."""

    source = tmp_path / "song.lrc"
    source.write_text("[00:01.00]Alpha beta\n[00:03.00]Gamma delta\n[00:05.00]Epsilon zeta\n")
    cues, _source, _language = load_lyrics(source, duration=30)
    assert [cue["text"] for cue in cues] == ["Alpha beta", "Gamma delta", "Epsilon zeta"]


def test_a_rolling_caption_is_collapsed_rather_than_repeated(tmp_path: Path) -> None:
    captions = tmp_path / "source.a.en.vtt"
    captions.write_text(
        "WEBVTT\n\n"
        "00:00:01.000 --> 00:00:03.000\nalpha beta\n\n"
        "00:00:03.000 --> 00:00:05.000\nalpha beta gamma delta\n\n"
        "00:00:08.000 --> 00:00:09.000\nEpsilon\n"
    )
    cues, _source, _language = load_lyrics(captions, duration=30)
    assert [cue["text"] for cue in cues] == ["alpha beta gamma delta", "Epsilon"]
    assert cues[0]["start"] == 1.0


def test_a_cue_is_not_held_across_an_instrumental_gap(tmp_path: Path) -> None:
    source = tmp_path / "song.lrc"
    source.write_text("[00:01.00]Alpha beta gamma\n[00:50.00]Delta epsilon\n")
    cues, _source, _language = load_lyrics(source, duration=90)
    assert cues[0]["start"] == 1.0
    assert cues[0]["end"] < 6.0, "the break is not sung, so no cue should span it"
    assert cues[1]["start"] == 50.0


def test_a_fast_line_gets_a_readable_minimum_on_screen(tmp_path: Path) -> None:
    source = tmp_path / "song.srt"
    source.write_text(
        "1\n00:00:01,000 --> 00:00:01,200\nGo\n\n2\n00:00:09,000 --> 00:00:10,000\nStop\n"
    )
    cues, _source, _language = load_lyrics(source, duration=30)
    assert cues[0]["end"] - cues[0]["start"] >= 1.2
    assert cues[0]["end"] <= cues[1]["start"]


def test_a_minimum_never_pushes_a_cue_over_its_neighbour(tmp_path: Path) -> None:
    source = tmp_path / "song.srt"
    source.write_text(
        "1\n00:00:01,000 --> 00:00:01,200\nGo\n\n"
        "2\n00:00:01,400 --> 00:00:02,000\nStop right there\n\n"
        "3\n00:00:02,100 --> 00:00:02,300\nAgain\n"
    )
    cues, _source, _language = load_lyrics(source, duration=30)
    assert _reader_complaints(cues) == []


def test_enhanced_lrc_word_stamps_become_timed_words(tmp_path: Path) -> None:
    source = tmp_path / "song.lrc"
    source.write_text("[00:01.00]<00:01.00>Alpha <00:01.80>beta <00:02.60>gamma\n[00:05.00]Delta\n")
    cues, _source, _language = load_lyrics(source, duration=30)
    assert cues[0]["text"] == "Alpha beta gamma"
    assert [word["start"] for word in cues[0]["words"]] == [1.0, 1.8, 2.6]
    assert _reader_complaints(cues) == []


def test_a_line_sung_twice_in_a_row_keeps_both_occurrences(tmp_path: Path) -> None:
    """A chorus line stamped twice is sung twice, and both times are its own.

    Folding the pair into one long cue lost the second performance twice over:
    the repeat stopped being a cue at all, and the doubled span then read as a
    hold across an instrumental break, so `_trim_instrumental` cut the folded
    (10.0, 18.0) cue back to 13.0 s -- `_sung_seconds("na na na")` is 2.0, plus
    the 1.0 s linger -- and left the line blank while it was being sung the
    second time.
    """

    source = tmp_path / "song.lrc"
    source.write_text("[00:10.00]na na na\n[00:14.00]na na na\n[00:18.00]Hey Jude\n")
    cues, _source, _language = load_lyrics(source, duration=40)
    assert [(cue["start"], cue["end"], cue["text"]) for cue in cues] == [
        (10.0, 14.0, "na na na"),
        (14.0, 18.0, "na na na"),
        (18.0, 23.0, "Hey Jude"),
    ]
    doubled = [cue["text"] for cue in cues if cue["text"].count("na na na") > 1]
    assert not doubled, "a line was joined to a copy of itself"
    assert _reader_complaints(cues) == []


def test_a_re_sent_caption_line_is_folded_back_into_the_one_it_repeats(tmp_path: Path) -> None:
    """The other half of the repeat rule: a copy that adds no time is one performance.

    A caption track re-sends the line it is holding and an ASR track emits the
    same words twice milliseconds apart. Kept as its own cue that copy is a row
    too short to read, and merged it doubles the line's own text.
    """

    captions = tmp_path / "source.a.en.vtt"
    captions.write_text(
        "WEBVTT\n\n"
        "00:00:01.000 --> 00:00:03.000\nna na na\n\n"
        "00:00:03.000 --> 00:00:03.100\nna na na\n\n"
        "00:00:08.000 --> 00:00:09.000\nEpsilon\n"
    )
    cues, _source, _language = load_lyrics(captions, duration=30)
    assert [(cue["start"], cue["end"], cue["text"]) for cue in cues] == [
        (1.0, 3.1, "na na na"),
        (8.0, 9.2, "Epsilon"),
    ]


def test_a_folded_re_send_is_not_then_read_as_an_instrumental_break(tmp_path: Path) -> None:
    """A folded cue's span is several observed spans, not a hold across a break.

    Six re-sends of one line, each adding under the readable minimum, fold into
    one seven-second cue -- longer than `_INSTRUMENTAL_SECONDS` and more than
    `_INSTRUMENTAL_RATIO` times what the words take to sing. Every second of it
    was in the file, so cutting it back would blank the line while the caption
    track still had it on screen. `_trim_instrumental` therefore runs before
    `_collapse_resends`, on spans that are still one cue's own.
    """

    captions = tmp_path / "source.a.en.vtt"
    blocks = "".join(
        f"00:00:0{start}.000 --> 00:00:0{start + 1}.000\nna na na\n\n" for start in range(2, 8)
    )
    captions.write_text(f"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nna na na\n\n{blocks}")
    cues, _source, _language = load_lyrics(captions, duration=30)
    assert [(cue["start"], cue["end"], cue["text"]) for cue in cues] == [(1.0, 8.0, "na na na")]


def test_a_repeated_chorus_comes_out_the_same_shape_from_either_path(tmp_path: Path) -> None:
    """The aligner keeps a repeated line; storing its answer must not undo that.

    `alignment._build_reference` appends every non-marker line of the sheet,
    repeats included, so a forced alignment returns one cue per occurrence.
    `pipeline._lyrics` writes that document and loads it back, and this module
    is what it is loaded by: a repeat the aligner timed has to survive here too,
    or the two paths would disagree about the same chorus.
    """

    lines = ["na na na", "na na na", "Hey Jude"]
    hypothesis: list[LyricWord] = [
        {"start": 10.0, "end": 11.0, "text": "na"},
        {"start": 11.2, "end": 12.2, "text": "na"},
        {"start": 12.4, "end": 13.9, "text": "na"},
        {"start": 14.0, "end": 15.0, "text": "na"},
        {"start": 15.2, "end": 16.2, "text": "na"},
        {"start": 16.4, "end": 17.9, "text": "na"},
        {"start": 18.0, "end": 18.6, "text": "hey"},
        {"start": 18.7, "end": 19.6, "text": "jude"},
    ]
    aligned = align_lines(lines, hypothesis, audio_duration=40.0)
    assert [line.text for line in aligned.lines] == lines
    written = tmp_path / "lyrics.json"
    write_lyrics(written, aligned.cues(), source="forced-alignment", language="en")
    reloaded, _source, _language = load_lyrics(written, duration=40)
    assert [cue["text"] for cue in reloaded] == lines
    assert reloaded[1]["end"] - reloaded[1]["start"] >= 3.0, "the repeat kept its own span"
    assert _reader_complaints(reloaded) == []


def test_enhanced_lrc_word_stamps_move_onto_the_stamp_that_repeats_the_line(
    tmp_path: Path,
) -> None:
    """A line stamped twice carries its A2 word times once, against the first stamp.

    Replayed unchanged on the repeat every word stamp sits before that cue
    begins, `_normalize_words` clamps them all into its first instant, and the
    player highlights the whole line at once and then never advances -- worse
    than the even estimate it replaced.
    """

    source = tmp_path / "song.lrc"
    source.write_text(
        "[00:10.00][00:40.00]<00:10.00>Alpha <00:10.80>beta <00:11.60>gamma\n"
        "[00:20.00]Filler line\n"
    )
    cues, _source, _language = load_lyrics(source, duration=50)
    assert [cue["text"] for cue in cues] == ["Alpha beta gamma", "Filler line", "Alpha beta gamma"]
    assert [word["start"] for word in cues[0]["words"]] == [10.0, 10.8, 11.6]
    repeat = cues[2]
    assert [word["start"] for word in repeat["words"]] == [40.0, 40.8, 41.6]
    assert all(word["end"] > word["start"] for word in repeat["words"]), "a zero-length word"
    assert _reader_complaints(cues) == []


def test_html_markup_is_still_stripped_from_lrc(tmp_path: Path) -> None:
    """The enhanced-LRC stamp reader must not start letting <b> through."""

    source = tmp_path / "song.lrc"
    source.write_text("[00:01.00]Alpha <i>beta</i>\n[00:05.00]Gamma\n")
    cues, _source, _language = load_lyrics(source, duration=30)
    assert cues[0]["text"] == "Alpha beta"


def test_control_characters_never_reach_a_cue_from_any_parser(tmp_path: Path) -> None:
    """One escape sequence, five intake arms, one answer.

    `\\s` does not match ESC, so a whitespace collapse alone lets one through, and
    for a while only the embedded arm was guarded at all. Both halves of that are
    now `text.printable_line`'s rule -- an escape becomes a space rather than
    being dropped, so what is left cannot close up into a word the file never
    contained -- and the fifth arm is asserted here beside the other four because
    the whole point is that they agree. They did not: `source._clean_lyrics` used
    to *remove* unprintables, so one tag gave "Alpha[31m beta" through a container
    and "Alpha [31m beta" through every other route into the same `lyrics.json`.
    """

    esc = "\x1b"
    srt = tmp_path / "imported.srt"
    srt.write_text(f"1\n00:00:01,000 --> 00:00:03,000\nAlpha{esc}[31m beta\n")
    lrc = tmp_path / "song.lrc"
    lrc.write_text(f"[00:01.00]Alpha{esc}[31m beta\n[00:05.00]Gamma\n")
    plain = tmp_path / "sheet.txt"
    plain.write_text(f"Alpha{esc}[31m beta\nGamma delta\n")
    imported = tmp_path / "other.json"
    # json.dumps writes the escape as \u001b; json.loads hands back the character.
    imported.write_text(
        _whisper_document(
            [{"start": 1.0, "end": 3.0, "text": f"Alpha{esc}[31m beta", "words": []}],
            source="imported-json",
        )
    )
    for path in (srt, lrc, plain, imported):
        cues, _source, _language = load_lyrics(path, duration=30)
        text = cues[0]["text"]
        assert esc not in text, path.name
        assert text.isprintable(), path.name
        assert text == "Alpha [31m beta", path.name
        assert all(word["text"].isprintable() for word in cues[0]["words"]), path.name

    # The fifth arm: a container's own tag, cleaned by `source` on the way out of
    # the file and parsed here, exactly as `pipeline._embedded_lyrics` does it.
    tag = source._clean_lyrics(f"Alpha{esc}[31m beta\nGamma delta\n")
    document = parse_embedded_lyrics(EmbeddedLyrics(tag="lyrics", text=tag), duration=30)
    assert document.cues[0]["text"] == "Alpha [31m beta"


def test_the_plain_text_fast_path_computes_what_the_long_way_would() -> None:
    """`_plain_text` returns its argument untouched when four passes would be no-ops.

    That guard is what keeps the unprintable fold free -- measured, it makes
    `_normalize` faster than it was before the fold existed rather than slower --
    but it is only allowed to be there if it is exactly equivalent. The
    alphabet below is built out of what actually reaches this function: markup,
    entities, every kind of whitespace, and the control characters the fold is
    for.
    """

    def the_long_way(value: str) -> str:
        text = re.sub(r"<[^>]+>", "", value)
        text = html.unescape(text)
        text = "".join(c if c.isprintable() or c.isspace() else " " for c in text)
        return re.sub(r"\s+", " ", text).strip()

    random.seed(20260822)
    alphabet = "ab <i> </i> &amp; &#144; &#xad; \t\n\r\x0b\x1b\x7f\xa0\u2028 .,'\u00e9\u4e2d"
    tokens = alphabet.split(" ")
    for _ in range(20000):
        value = "".join(random.choice(tokens) for _ in range(random.randrange(0, 10)))
        assert lyrics_module._plain_text(value) == the_long_way(value), repr(value)


def test_an_entity_that_decodes_to_a_control_character_is_neutered_too(tmp_path: Path) -> None:
    """The fold runs after `html.unescape`, which is the only order that works.

    `html.unescape` drops every non-whitespace C0 charref, so `&#27;` is not a
    way in. The C1 range and the soft hyphen are: `&#144;` and `&#xad;` come
    through it as characters, and before the fold they reached a cue.
    """

    plain = tmp_path / "sheet.txt"
    plain.write_text("Alpha&#144;beta&#xad;gamma\nDelta epsilon\n")
    cues, _source, _language = load_lyrics(plain, duration=30)
    assert cues[0]["text"] == "Alpha beta gamma"


def test_an_embedded_tag_is_sanitised_on_the_way_through_too() -> None:
    """`parse_embedded_lyrics` is public, so it cannot lean on `source` having run."""

    document = parse_embedded_lyrics(
        EmbeddedLyrics(tag="lyrics", text="Alpha\x1b[31m beta\nGamma delta\n"),
        duration=60,
    )
    assert document.cues[0]["text"] == "Alpha [31m beta"
    assert all(cue["text"].isprintable() for cue in document.cues)


# --------------------------------------------------------------------------- #
# Untimed text, for the aligner
# --------------------------------------------------------------------------- #


def test_untimed_text_is_reported_as_untimed(tmp_path: Path) -> None:
    source = tmp_path / "song.txt"
    source.write_text("Alpha beta\n\nGamma delta\nEpsilon\n")
    document = load_lyrics_document(source, duration=60)
    assert document.has_timing is False
    assert document.lines == ("Alpha beta", "Gamma delta", "Epsilon")
    assert document.note
    assert document.source == "imported-plain-estimated"
    assert [cue["text"] for cue in document.cues] == list(document.lines)


def test_untimed_lines_survive_a_repeat_and_a_reload(tmp_path: Path) -> None:
    """`lines` is what the aligner is handed, so it must not lose a repeated line.

    The spans of an untimed document are an even spread this module invented.
    Reading two of them as one line re-sent is reasoning about nothing, and it
    cost the aligner two of the five lines it was given -- differently on a
    resume than on a fresh run, because the reload sees the spread.
    """

    source = tmp_path / "song.txt"
    source.write_text("Na na na\nNa na na\nHey Jude\nHey Jude\nDont make it bad\n")
    document = load_lyrics_document(source, duration=60)
    assert document.has_timing is False
    assert list(document.lines) == [
        "Na na na",
        "Na na na",
        "Hey Jude",
        "Hey Jude",
        "Dont make it bad",
    ]
    assert [cue["text"] for cue in document.cues] == list(document.lines)
    written = tmp_path / "lyrics.json"
    write_lyrics(
        written,
        document.cues,
        source=document.source,
        language=document.language,
        timing=document.timing,
    )
    reloaded = load_lyrics_document(written, duration=60)
    assert reloaded.lines == document.lines
    assert reloaded.cues == document.cues


def test_untimed_placeholder_spans_are_not_segmented(tmp_path: Path) -> None:
    """An invented hold is not an instrumental break, and must not be cut short."""

    source = tmp_path / "song.txt"
    source.write_text("Alpha beta\nGamma delta\n")
    document = load_lyrics_document(source, duration=120)
    assert [(cue["start"], cue["end"]) for cue in document.cues] == [(0.0, 60.0), (60.0, 120.0)]


def test_timed_text_is_reported_as_timed(tmp_path: Path) -> None:
    source = tmp_path / "song.lrc"
    source.write_text("[00:01.00]Alpha beta\n[00:05.00]Gamma delta\n")
    document = load_lyrics_document(source, duration=60)
    assert document.has_timing is True
    assert document.lines == ()
    assert document.note == ""


def test_a_reloaded_estimated_document_stays_untimed(tmp_path: Path) -> None:
    """A spread written back out must not read as an observation on the next resume.

    The document says `timing`, so it is the document that has to be asked -- and a
    caller writing one it loaded has to hand `write_lyrics` the provenance it loaded
    with. Passing only the cues, the source and the language writes the default,
    `authored`, over an estimate, and this is the test that catches it.
    """

    source = tmp_path / "song.txt"
    source.write_text("Alpha beta\nGamma delta\n")
    document = load_lyrics_document(source, duration=60)
    assert document.timing == "estimated"
    written = tmp_path / "lyrics.json"
    write_lyrics(
        written,
        document.cues,
        source=document.source,
        language=document.language,
        timing=document.timing,
    )
    reloaded = load_lyrics_document(written, duration=60)
    assert reloaded.timing == "estimated"
    assert reloaded.has_timing is False
    assert reloaded.lines == ("Alpha beta", "Gamma delta")


def test_text_holding_lrc_content_is_parsed_as_lrc(tmp_path: Path) -> None:
    source = tmp_path / "song.txt"
    source.write_text("[00:01.00]Alpha beta\n[00:05.00]Gamma delta\n")
    document = load_lyrics_document(source, duration=60)
    assert document.source == "imported-lrc"
    assert document.has_timing is True
    assert document.cues[0]["start"] == 1.0


# --------------------------------------------------------------------------- #
# Provenance: how the times in a document were arrived at
# --------------------------------------------------------------------------- #


_ONE_CUE: list[LyricCue] = [{"start": 1.0, "end": 3.0, "text": "Alpha beta", "words": []}]
_REPORT: LyricAlignment = {
    "matched_fraction": 0.875,
    "interpolated_words": 2,
    "mean_displacement": 0.125,
    "usable": True,
}


def _document_without_provenance(source: str) -> str:
    """One lyrics.json in the shape everything written before these fields is in."""

    return json.dumps(
        {"schema": LYRICS_SCHEMA, "source": source, "language": "en", "cues": _ONE_CUE}
    )


def test_a_written_document_records_how_its_times_were_arrived_at(tmp_path: Path) -> None:
    """The three provenances, in the document itself, where both surfaces read them.

    A user looking at a highlighted line cannot tell a measurement from a guess unless
    the file says which it is, and these are the fields that say so.
    """

    authored = tmp_path / "authored.json"
    write_lyrics(authored, _ONE_CUE, source="imported-lrc", language="en")
    stored = json.loads(authored.read_text())
    assert stored["timing"] == "authored"
    assert stored["alignment"] is None

    estimated = tmp_path / "estimated.json"
    write_lyrics(
        estimated,
        _ONE_CUE,
        source="imported-plain-estimated",
        language="en",
        timing="estimated",
    )
    assert json.loads(estimated.read_text())["timing"] == "estimated"

    measured = tmp_path / "measured.json"
    write_lyrics(
        measured,
        _ONE_CUE,
        source="imported-plain-aligned",
        language="en",
        timing="measured",
        alignment=_REPORT,
    )
    stored = json.loads(measured.read_text())
    assert stored["timing"] == "measured"
    assert stored["alignment"] == {
        "matched_fraction": 0.875,
        "interpolated_words": 2,
        "mean_displacement": 0.125,
        "usable": True,
    }
    # Additive, and nothing else moved: the fields a reader already knew are untouched.
    assert stored["schema"] == LYRICS_SCHEMA
    assert stored["source"] == "imported-plain-aligned"
    assert stored["cues"] == _ONE_CUE


def test_a_report_belongs_only_to_a_measurement(tmp_path: Path) -> None:
    """An authored .lrc's timing is its author's, and this app has not measured it.

    So there is no honest number to put beside it, and a caller offering one is refused
    rather than quietly having it written into a document that claims a confidence
    nothing computed.
    """

    target = tmp_path / "lyrics.json"
    with pytest.raises(ValueError, match="no alignment to report"):
        write_lyrics(target, _ONE_CUE, source="imported-lrc", language="en", alignment=_REPORT)
    with pytest.raises(ValueError, match="no alignment to report"):
        write_lyrics(
            target,
            _ONE_CUE,
            source="imported-plain-estimated",
            language="en",
            timing="estimated",
            alignment=_REPORT,
        )
    assert not target.exists(), "the refusal happens before anything is written"


def test_every_route_into_this_module_says_what_it_produced(tmp_path: Path) -> None:
    """Each parser knows what it did, so nothing downstream has to guess afterwards."""

    lrc = tmp_path / "song.lrc"
    lrc.write_text("[00:01.00]Alpha beta\n[00:05.00]Gamma delta\n")
    assert load_lyrics_document(lrc, duration=60).timing == "authored"

    srt = tmp_path / "song.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,500\nAlpha beta\n")
    assert load_lyrics_document(srt, duration=60).timing == "authored"

    plain = tmp_path / "song.txt"
    plain.write_text("Alpha beta\nGamma delta\n")
    spread = load_lyrics_document(plain, duration=60)
    assert spread.timing == "estimated"
    assert spread.has_timing is False

    tag = parse_embedded_lyrics(
        EmbeddedLyrics(tag="lyrics", text="[00:01.00]Alpha beta\n[00:05.00]Gamma delta\n"),
        duration=60,
    )
    assert tag.timing == "authored"
    unsynced = parse_embedded_lyrics(
        EmbeddedLyrics(tag="lyrics", text="Alpha beta\nGamma delta\n"), duration=60
    )
    assert unsynced.timing == "estimated"

    # Not one of them claims a measurement, because not one of them measured anything.
    for document in (spread, tag, unsynced):
        assert document.alignment is None


def test_a_document_written_before_provenance_still_loads(tmp_path: Path) -> None:
    """A lyrics.json with neither field is what every finished project on disk holds.

    It has to keep loading, and its timing has to be derived from what such a document
    does carry, which is the source id and nothing else. A spread says so in its id; a
    document with stamps and no field reads back as `authored`, because the stamps
    arrived with it and this build did not place them -- an alignment that ran before the
    field existed left no report to quote, and claiming one would be inventing it.
    """

    timed = tmp_path / "timed.json"
    timed.write_text(_document_without_provenance("imported-lrc"))
    assert "timing" not in json.loads(timed.read_text())
    document = load_lyrics_document(timed, duration=60)
    assert document.timing == "authored"
    assert document.alignment is None
    assert document.has_timing is True
    assert document.lines == ()

    spread = tmp_path / "spread.json"
    spread.write_text(_document_without_provenance("imported-plain-estimated"))
    reloaded = load_lyrics_document(spread, duration=60)
    assert reloaded.timing == "estimated"
    assert reloaded.alignment is None
    assert reloaded.has_timing is False
    assert reloaded.lines == ("Alpha beta",)


def test_the_recorded_timing_decides_and_not_the_source_id(tmp_path: Path) -> None:
    """The coupling this field exists to end.

    A consumer used to read a provenance out of the source id by testing it for an
    `-estimated` tail, which made the spelling of an id something outside this module had
    to know. The id still says which source won, and a document that states its own
    timing is believed over it in both directions.
    """

    measured = tmp_path / "measured.json"
    write_lyrics(
        measured,
        _ONE_CUE,
        source="imported-plain-estimated",
        language="en",
        timing="measured",
        alignment=_REPORT,
    )
    document = load_lyrics_document(measured, duration=60)
    assert document.timing == "measured"
    assert document.has_timing is True
    assert document.lines == (), "a measured document has nothing left to align"

    spread = tmp_path / "spread.json"
    write_lyrics(spread, _ONE_CUE, source="hand-written", language="en", timing="estimated")
    document = load_lyrics_document(spread, duration=60)
    assert document.timing == "estimated"
    assert document.has_timing is False
    assert document.lines == ("Alpha beta",)


def test_a_measured_document_carries_its_report_back(tmp_path: Path) -> None:
    """The four numbers survive a resume, so a surface can still weigh the highlight."""

    target = tmp_path / "lyrics.json"
    write_lyrics(
        target,
        _ONE_CUE,
        source="imported-plain-aligned",
        language="en",
        timing="measured",
        alignment=_REPORT,
    )
    assert load_lyrics_document(target, duration=60).alignment == _REPORT


@pytest.mark.parametrize(
    "alignment",
    [
        {"matched_fraction": 0.5, "interpolated_words": 2, "mean_displacement": 0.1},
        {**_REPORT, "matched_fraction": "most of it"},
        {**_REPORT, "matched_fraction": 1.5},
        {**_REPORT, "mean_displacement": -1.0},
        {**_REPORT, "interpolated_words": True},
        {**_REPORT, "usable": "yes"},
        "measured well",
    ],
)
def test_half_a_report_is_not_a_measurement(tmp_path: Path, alignment: object) -> None:
    """These numbers are a confidence a surface shows a user, so a partial one is none.

    The document is still read -- the words and their times are the point, and they are
    intact -- but nothing reaches `alignment` that was not wholly there.
    """

    target = tmp_path / "lyrics.json"
    target.write_text(
        json.dumps(
            {
                "schema": LYRICS_SCHEMA,
                "source": "imported-plain-aligned",
                "language": "en",
                "timing": "measured",
                "alignment": alignment,
                "cues": _ONE_CUE,
            }
        )
    )
    document = load_lyrics_document(target, duration=60)
    assert document.timing == "measured"
    assert document.alignment is None
    assert [cue["text"] for cue in document.cues] == ["Alpha beta"]


def test_a_report_recorded_against_an_estimate_is_not_carried(tmp_path: Path) -> None:
    """`alignment` describes a measurement, so it travels only with one.

    Reading it off a document that says its spans were estimated and writing it back out
    would put a confidence on a guess.
    """

    target = tmp_path / "lyrics.json"
    target.write_text(
        json.dumps(
            {
                "schema": LYRICS_SCHEMA,
                "source": "imported-plain-estimated",
                "language": "en",
                "timing": "estimated",
                "alignment": dict(_REPORT),
                "cues": _ONE_CUE,
            }
        )
    )
    document = load_lyrics_document(target, duration=60)
    assert document.timing == "estimated"
    assert document.alignment is None
    # And what came back can be written again, which a carried-over report would refuse.
    write_lyrics(
        tmp_path / "again.json",
        document.cues,
        source=document.source,
        language=document.language,
        timing=document.timing,
        alignment=document.alignment,
    )


def test_an_unknown_timing_value_is_refused_on_the_way_out_and_ignored_on_the_way_in(
    tmp_path: Path,
) -> None:
    """`timing` is a closed vocabulary, and the two directions differ on purpose.

    Writing a value outside it would put a word in the document that no reader has a
    meaning for, so it is refused. Reading one back is a document this build does not
    understand, and it is treated as one that never said: the words still load, under the
    same derivation any document without the field gets.
    """

    target = tmp_path / "lyrics.json"
    with pytest.raises(ValueError, match="unknown lyric timing provenance"):
        write_lyrics(
            target,
            _ONE_CUE,
            source="imported-lrc",
            language="en",
            timing="approximate",  # type: ignore[arg-type]
        )

    target.write_text(
        json.dumps(
            {
                "schema": LYRICS_SCHEMA,
                "source": "imported-plain-estimated",
                "language": "en",
                "timing": "approximate",
                "cues": _ONE_CUE,
            }
        )
    )
    document = load_lyrics_document(target, duration=60)
    assert document.timing == "estimated"
    assert [cue["text"] for cue in document.cues] == ["Alpha beta"]


# --------------------------------------------------------------------------- #
# Sidecars beside a local file
# --------------------------------------------------------------------------- #


def test_an_lrc_sidecar_is_found_beside_the_media(tmp_path: Path) -> None:
    media = tmp_path / "song.mp3"
    media.write_bytes(b"\x00")
    (tmp_path / "song.lrc").write_text("[00:01.00]Alpha\n")
    (tmp_path / "unrelated.lrc").write_text("[00:01.00]Beta\n")
    assert find_lyrics_sidecar(media) == tmp_path / "song.lrc"


def test_a_sidecar_case_variant_and_language_suffix_are_found(tmp_path: Path) -> None:
    media = tmp_path / "song.mp3"
    media.write_bytes(b"\x00")
    (tmp_path / "song.LRC").write_text("[00:01.00]Alpha\n")
    assert find_lyrics_sidecar(media) == tmp_path / "song.LRC"

    (tmp_path / "song.LRC").unlink()
    (tmp_path / "song.es.lrc").write_text("[00:01.00]Beta\n")
    (tmp_path / "song.en.lrc").write_text("[00:01.00]Gamma\n")
    assert find_lyrics_sidecar(media, language="en-GB") == tmp_path / "song.en.lrc"
    assert find_lyrics_sidecar(media, language="es") == tmp_path / "song.es.lrc"


def test_an_exact_sidecar_outranks_a_language_variant(tmp_path: Path) -> None:
    media = tmp_path / "song.mp3"
    media.write_bytes(b"\x00")
    (tmp_path / "song.lrc").write_text("[00:01.00]Alpha\n")
    (tmp_path / "song.en.lrc").write_text("[00:01.00]Beta\n")
    assert find_lyrics_sidecar(media, language="en") == tmp_path / "song.lrc"


def test_no_sidecar_is_reported_when_there_is_none(tmp_path: Path) -> None:
    media = tmp_path / "song.mp3"
    media.write_bytes(b"\x00")
    (tmp_path / "song.mp3.lrc").write_text("[00:01.00]Alpha\n")
    assert find_lyrics_sidecar(media) is None
    assert find_lyrics_sidecar(tmp_path / "absent.mp3") is None


def test_sidecar_discovery_refuses_a_symlink_out_of_the_directory(tmp_path: Path) -> None:
    """Discovery happens without the user naming a file, so it must not read
    whatever a link in the media's folder points at."""

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.lrc").write_text("[00:01.00]Alpha\n")
    folder = tmp_path / "music"
    folder.mkdir()
    media = folder / "song.mp3"
    media.write_bytes(b"\x00")
    (folder / "song.lrc").symlink_to(outside / "secret.lrc")
    assert find_lyrics_sidecar(media) is None


def test_a_sidecar_symlink_inside_the_directory_is_still_read(tmp_path: Path) -> None:
    folder = tmp_path / "music"
    folder.mkdir()
    media = folder / "song.mp3"
    media.write_bytes(b"\x00")
    (folder / "real.lrc").write_text("[00:01.00]Alpha\n")
    (folder / "song.lrc").symlink_to(folder / "real.lrc")
    # The target, not the link: the caller opens this later, and returning the
    # link would leave a window in which it is repointed before the read.
    assert find_lyrics_sidecar(media) == folder / "real.lrc"


def test_sidecar_discovery_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    media = tmp_path / "song.mp3"
    media.write_bytes(b"\x00")
    (tmp_path / "song.lrc").write_text("[00:01.00]Alpha\n")
    monkeypatch.setattr(lyrics_module, "MAX_SIDECAR_ENTRIES", 0)
    assert find_lyrics_sidecar(media) is None


def test_an_oversized_sidecar_is_not_offered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = tmp_path / "song.mp3"
    media.write_bytes(b"\x00")
    (tmp_path / "song.lrc").write_text("[00:01.00]Alpha beta gamma\n")
    monkeypatch.setattr(lyrics_module, "MAX_LYRICS_BYTES", 4)
    assert find_lyrics_sidecar(media) is None


# --------------------------------------------------------------------------- #
# Lyrics carried inside the media
# --------------------------------------------------------------------------- #


def test_an_unsynced_tag_holding_lrc_is_detected_rather_than_assumed() -> None:
    """USLT is nominally unsynchronised and is routinely filled with LRC."""

    document = embedded_lyrics_document(
        {"lyrics-eng": "[00:01.00]Alpha beta\n[00:05.00]Gamma delta\n"}, duration=60
    )
    assert document is not None
    assert document.source == "embedded-lrc"
    assert document.has_timing is True
    assert document.language == "eng"
    assert [cue["start"] for cue in document.cues] == [1.0, 5.0]


def test_a_genuinely_unsynced_tag_is_reported_as_untimed() -> None:
    document = embedded_lyrics_document(
        {"UNSYNCEDLYRICS": "Alpha beta\nGamma delta\n"}, duration=60
    )
    assert document is not None
    assert document.source == "embedded-plain-estimated"
    assert document.has_timing is False
    assert document.lines == ("Alpha beta", "Gamma delta")


def test_embedded_tag_selection_prefers_stamps_then_the_requested_language() -> None:
    tags = {
        "UNSYNCEDLYRICS": "Alpha beta\nGamma delta\n",
        "lyrics-eng": "[00:01.00]Alpha beta\n[00:05.00]Gamma delta\n",
        "TITLE": "not lyrics",
    }
    chosen = select_embedded_lyrics(tags)
    assert chosen is not None
    assert chosen.tag == "lyrics"
    assert chosen.language == "eng"

    plain_only = {
        "lyrics-deu": "Alpha beta\nGamma delta\n",
        "lyrics-eng": "Epsilon zeta\nEta theta\n",
    }
    picked = select_embedded_lyrics(plain_only, language="en")
    assert picked is not None
    assert picked.language == "eng"


def test_a_tag_key_names_a_language_only_where_one_is_spelled(tmp_path: Path) -> None:
    """A tag key is file content, and its tail lands in the manifest as the language.

    `parse_embedded_lyrics` copies `EmbeddedLyrics.language` into the document
    it returns, which `pipeline._lyrics` writes into lyrics.json and the
    manifest. Only a language-shaped tail is taken; anything else is unknown.
    """

    named = select_embedded_lyrics({"lyrics-eng": "Alpha beta\nGamma delta\n"})
    assert named is not None
    assert named.language == "eng"

    junk = select_embedded_lyrics({"lyrics-mrs-jones-address-2024": "Alpha beta\nGamma delta\n"})
    assert junk is not None
    assert junk.language == "unknown"


def test_embedded_tag_search_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every candidate value is scanned for stamps, so the count is bounded first."""

    tags = {"lyrics": "[00:01.00]Alpha beta\n[00:05.00]Gamma delta\n"}
    assert select_embedded_lyrics(tags) is not None
    monkeypatch.setattr(lyrics_module, "MAX_EMBEDDED_TAGS", 0)
    assert select_embedded_lyrics(tags) is None


def test_no_lyrics_tag_is_an_ordinary_outcome() -> None:
    assert embedded_lyrics_document({"title": "x", "artist": "y"}, duration=60) is None


def test_an_unusable_embedded_tag_is_rejected_not_repaired() -> None:
    with pytest.raises(InvalidInputError, match="no usable"):
        parse_embedded_lyrics(EmbeddedLyrics(tag="lyrics", text="   \n  \n"), duration=60)
    with pytest.raises(InvalidInputError, match="UTF-8"):
        parse_embedded_lyrics(EmbeddedLyrics(tag="lyrics", text="Alpha\x00beta"), duration=60)


def test_a_non_utf8_or_oversized_embedded_tag_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert select_embedded_lyrics({"lyrics": b"\xff\xfe not utf-8"}) is None
    monkeypatch.setattr(lyrics_module, "MAX_LYRICS_BYTES", 4)
    assert select_embedded_lyrics({"lyrics": "Alpha beta\nGamma delta\n"}) is None


def test_the_tag_bound_stays_tighter_than_the_file_bound() -> None:
    """`MAX_LYRICS_BYTES`' comment says a lyrics *tag* is bounded elsewhere and tighter.

    Two constants in two modules bound lyric bytes, for two different things, and
    the comment explains the difference rather than pretending it is one number.
    An unchecked cross-module claim is exactly the kind that goes stale, so the
    half of it that is checkable is checked.
    """

    assert source.MAX_EMBEDDED_LYRICS_BYTES < MAX_LYRICS_BYTES


def test_the_tag_key_vocabulary_covers_every_key_the_file_arm_lifts() -> None:
    """`embedded_tag_key` is meant to be the package's only lyric-key vocabulary.

    `source` decides which tag it lifts out of a container one step before this
    module ever sees it, and then `pipeline._embedded_lyrics` feeds that one key
    straight back in here to have its language read. Any key `source` accepts and
    this rejects is a declared language dropped on the floor; `unsynced_lyrics`
    was the live example. Listed by spelling rather than by importing `source`,
    so this stays a statement about the vocabulary and not about that module's
    private helpers.
    """

    for key in ("lyrics", "unsyncedlyrics", "unsynced_lyrics", "uslt", "lyrics-eng"):
        assert embedded_tag_key(key) is not None, key
    assert embedded_tag_key("unsynced_lyrics") == ("unsynced_lyrics", "unknown")
    assert embedded_tag_key("unsynced_lyrics_eng") == ("unsynced_lyrics", "eng")
    # And the keys only this side knows about, which is the other half of the
    # divergence: a file carrying these plainly carries lyrics.
    assert embedded_tag_key("syncedlyrics-en") == ("syncedlyrics", "en")
    assert embedded_tag_key("\u00a9lyr") == ("lyrics", "unknown")
    assert embedded_tag_key("com.apple.iTunes:LYRICS") == ("lyrics", "unknown")
    assert embedded_tag_key("title") is None
    assert embedded_tag_key("lyricist") is None


def test_an_underscore_spelled_unsynced_tag_keeps_its_language() -> None:
    chosen = select_embedded_lyrics({"unsynced_lyrics_eng": "Alpha beta\nGamma delta\n"})
    assert chosen is not None
    assert chosen.language == "eng"


def test_a_bytes_lyrics_tag_is_decoded_strictly() -> None:
    chosen = select_embedded_lyrics({"lyrics": b"Alpha beta\nGamma delta\n"})
    assert chosen is not None
    assert chosen.text.startswith("Alpha")


# --------------------------------------------------------------------------- #
# Caption-track selection
# --------------------------------------------------------------------------- #


def test_a_human_track_outranks_an_auto_generated_one(tmp_path: Path) -> None:
    human = tmp_path / "source.en.vtt"
    automatic = tmp_path / "source.a.en.vtt"
    choice = choose_subtitle_track([automatic, human], "en")
    assert choice is not None
    assert choice.path == human
    assert choice.kind == "human"
    assert choice.source == "youtube-captions"


def test_an_auto_generated_track_is_named_as_a_machine_transcript(tmp_path: Path) -> None:
    """It is the same quality class as running Whisper ourselves, and a caller
    that records it as "captions" cannot tell the difference later."""

    automatic = tmp_path / "source.a.en.vtt"
    choice = choose_subtitle_track([automatic], "en")
    assert choice is not None
    assert choice.kind == "automatic"
    assert choice.source == "youtube-captions-automatic"
    assert "machine transcript" in choice.reason


def test_an_original_language_track_outranks_a_translation(tmp_path: Path) -> None:
    translated = tmp_path / "source.en.vtt"
    original = tmp_path / "source.a.es.vtt"
    choice = choose_subtitle_track([translated, original], "en", original_language="es")
    assert choice is not None
    assert choice.path == original, "a translation is the wrong words, not the wrong language"
    ranked = rank_subtitles([translated, original], "en", original_language="es")
    assert [item.kind for item in ranked] == ["automatic", "translated"]
    assert ranked[1].source == "youtube-captions-translated"


def test_an_orig_marked_track_reveals_the_original_language(tmp_path: Path) -> None:
    """yt-dlp writes YouTube's own ASR track as `<lang>-orig`, which names the
    language the video is actually in even when the caller did not."""

    original = tmp_path / "source.en-orig.vtt"
    french = tmp_path / "source.fr.vtt"
    ranked = rank_subtitles([french, original], "fr")
    assert ranked[0].path == original
    assert ranked[0].kind == "automatic"
    assert ranked[1].kind == "translated"


def test_a_track_with_no_marker_is_not_claimed_to_be_human(tmp_path: Path) -> None:
    plain = tmp_path / "source.en.vtt"
    choice = choose_subtitle_track([plain], "en")
    assert choice is not None
    assert choice.kind == "unknown"
    assert choice.source == "youtube-captions"


def test_with_nothing_to_prefer_the_ranking_falls_back_to_english(tmp_path: Path) -> None:
    """The one default language in this module, pinned where it can be seen.

    "auto" means the caller has no preference and no metadata named the video's
    language, so nothing says which track is the sung one. English wins by
    default; it is a guess, and this test is here so that changing it is a
    decision rather than a side effect.
    """

    spanish = tmp_path / "source.es.vtt"
    english = tmp_path / "source.en.vtt"
    chosen = choose_subtitle_track([spanish, english], "auto")
    assert chosen is not None
    assert chosen.path == english
    ranked = rank_subtitles([spanish, english], "auto")
    assert [choice.matches_language for choice in ranked] == [True, False]
    assert ranked[0].path == english


def test_live_chat_and_unreadable_formats_are_not_offered(tmp_path: Path) -> None:
    chat = tmp_path / "source.live_chat.json"
    unreadable = tmp_path / "source.en.json3"
    assert rank_subtitles([chat, unreadable], "en") == []
    assert choose_subtitle_track([chat, unreadable], "en") is None


def test_a_caption_reason_carries_no_path_or_filename(tmp_path: Path) -> None:
    """The reason is built for the manifest and the log, so it carries no input.

    Nothing records it yet; the redaction property is established at the source
    so that whichever surface first does is not the one that has to think about
    it.
    """

    track = tmp_path / "source.a.en.vtt"
    choice = choose_subtitle_track([track], "en")
    assert choice is not None
    assert "source" not in choice.reason
    assert str(tmp_path) not in choice.reason
    assert ".vtt" not in choice.reason


def test_a_hostile_language_fragment_never_reaches_the_reason(tmp_path: Path) -> None:
    track = tmp_path / "source.<script>.vtt"
    choice = choose_subtitle_track([track], "en")
    assert choice is not None
    assert choice.language == ""
    assert "<" not in choice.reason


def test_a_filename_component_is_not_a_language(tmp_path: Path) -> None:
    """A lowercase hyphenated run is not a language token, and this one is a name.

    The reason is written into events and logs under a redaction rule, and the
    language is recorded in the manifest and rendered; a rule of "anything
    lowercase and hyphenated" put a whole component of the user's filename into
    both. The intake screen in this release accepts a local file, so the names
    reaching here are the user's own.
    """

    track = tmp_path / "private-track.mrs-jones-address-2024.vtt"
    choice = choose_subtitle_track([track], "en")
    assert choice is not None
    assert choice.language == ""
    for fragment in ("mrs", "jones", "address", "2024"):
        assert fragment not in choice.reason
    assert str(tmp_path) not in choice.reason


def test_a_reason_names_a_language_only_from_this_module_s_own_table(tmp_path: Path) -> None:
    """What survives into the reason is this module's key, not the file's spelling."""

    spanish = choose_subtitle_track([tmp_path / "source.spa.vtt"], "es")
    assert spanish is not None
    assert spanish.language == "spa"
    assert "in es," in spanish.reason, "the 639-2 spelling is folded onto our own key"

    # A shape the token accepts and the table does not list: bounded, and still
    # a fragment of a name the user chose, so it is described and not quoted.
    unlisted = choose_subtitle_track([tmp_path / "private.mrs-jo.vtt"], "en")
    assert unlisted is not None
    assert unlisted.language == "mrs-jo"
    assert "mrs" not in unlisted.reason
    assert "does not list" in unlisted.reason


def test_a_region_and_script_subtag_are_still_read_as_the_language(tmp_path: Path) -> None:
    """The narrower token still has to accept the tags yt-dlp actually writes."""

    for name, language in (
        ("source.pt-BR.vtt", "pt-br"),
        ("source.es-419.vtt", "es-419"),
        ("source.zh-Hans-CN.vtt", "zh-hans-cn"),
        ("source.eng.vtt", "eng"),
    ):
        choice = choose_subtitle_track([tmp_path / name], "auto")
        assert choice is not None, name
        assert choice.language == language


def test_a_caller_can_name_the_caption_kind_it_downloaded(tmp_path: Path) -> None:
    captions = tmp_path / "source.a.en.vtt"
    captions.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nAlpha beta\n")
    choice = choose_subtitle_track([captions], "en")
    assert choice is not None
    _cues, source, _language = load_lyrics(captions, duration=30, source_hint=choice.source)
    assert source == "youtube-captions-automatic"


def test_an_lrc_keeps_the_lines_its_author_stamped(tmp_path: Path) -> None:
    """A per-line stamp is the author saying where a line begins.

    `_parse_lrc` gives every cue the next stamp's start as its end, so the gap
    between consecutive LRC cues is always zero -- an artefact of how the end
    was invented, not evidence that two lines continue each other. Merging on
    it read this file as one cue holding all three lines for the whole song,
    which is the karaoke display collapsing into a paragraph.
    """

    lyric_file = tmp_path / "song.lrc"
    lyric_file.write_text(
        "[00:00.50]first line here\n[00:02.00]second line here\n[00:04.00]third line here\n"
    )
    cues, source, _language = load_lyrics(lyric_file, duration=6.03)
    assert source == "imported-lrc"
    assert [cue["text"] for cue in cues] == [
        "first line here",
        "second line here",
        "third line here",
    ]
    assert [round(cue["start"], 2) for cue in cues] == [0.5, 2.0, 4.0]


def test_a_subtitle_sentence_split_across_two_cues_is_still_rejoined(tmp_path: Path) -> None:
    """The exclusion that keeps the LRC rule from disarming subtitle repair.

    SRT and WebVTT carry real end times, so a gap there means what it says and a
    sentence genuinely split across two cues should still be one line.
    """

    captions = tmp_path / "imported.vtt"
    captions.write_text(
        "WEBVTT\n\n"
        "00:00:01.000 --> 00:00:02.000\nthe long and winding\n\n"
        "00:00:02.000 --> 00:00:03.000\nroad that leads\n"
    )
    cues, _source, _language = load_lyrics(captions, duration=30)
    assert len(cues) == 1
    assert cues[0]["text"] == "the long and winding road that leads"


def test_a_rolling_caption_stub_is_not_put_on_screen_twice(tmp_path: Path) -> None:
    """Real YouTube auto-captions, and the shape that made every line double.

    A rolling track sends a stub carrying the tail of the screen just shown,
    stamped a hundredth of a second before the full line that repeats that tail
    and adds to it. Both land inside `_SIMULTANEOUS_SECONDS`, and joining them --
    which is right for two stamps that really are two rows of one screen -- put
    the tail on screen twice. Found on the real captions of a 218 s song, where
    all 55 cues came back doubled: "just to have it taken just to have it taken
    away people walk around pushing back".
    """

    captions = tmp_path / "source.en.vtt"
    captions.write_text(
        "WEBVTT\n\n"
        "00:00:35.030 --> 00:00:35.080\nfor change old ladies laughing from the\n\n"
        "00:00:35.040 --> 00:00:38.430\n"
        "for change old ladies laughing from the fire escape cursing my\n\n"
        "00:00:38.430 --> 00:00:38.480\nfire escape cursing my\n\n"
        "00:00:38.440 --> 00:00:41.510\n"
        "fire escape cursing my name I got a basket full of lemons\n"
    )
    cues, _source, _language = load_lyrics(captions, duration=218.0)
    for cue in cues:
        words = cue["text"].split()
        half = len(words) // 2
        assert words[:half] != words[half : half * 2], f"line doubled: {cue['text']!r}"
    assert cues[0]["text"] == "for change old ladies laughing from the fire escape cursing my"
