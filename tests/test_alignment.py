"""Forced alignment: does the user's own lyric sheet come back correctly timed?

Every case here is a shape of failure a real song produces. The transcriber
mishears, skips a quiet line, invents an ad-lib, hears a chorus five times and
must give each occurrence its own timing, or is handed the wrong language
entirely. The module is pure -- text in, cues out -- so all of that is testable
without a model, an audio file or a project.
"""

from __future__ import annotations

import random
from itertools import pairwise

import pytest

from kilix_playalong import alignment
from kilix_playalong.alignment import (
    COMPARISON_PAIR_CELLS,
    GAP_PENALTY,
    GOOD_MATCHED_FRACTION,
    GOOD_MEAN_DISPLACEMENT,
    MATCH_THRESHOLD,
    MAX_ALIGNMENT_CELLS,
    MAX_COMPARISON_CELLS,
    MAX_REFERENCE_CHARS,
    MAX_TOKENS,
    MIN_CUE_SPAN_SECONDS,
    USABLE_MATCHED_FRACTION,
    USABLE_MEAN_DISPLACEMENT,
    USABLE_UNALIGNED_RUN,
    AlignmentResult,
    _comparison_cost,
    _letter_mask,
    _pair_score,
    align_lines,
    align_reference_text,
    comparison_tokens,
    hypothesis_from_cues,
    token_similarity,
)
from kilix_playalong.errors import InvalidInputError
from kilix_playalong.types import LyricCue, LyricWord


def _sung(text: str, *, start: float = 0.0, step: float = 0.5) -> list[LyricWord]:
    """Lay `text` out as one hypothesis word every `step` seconds."""

    words: list[LyricWord] = []
    cursor = start
    for token in text.split():
        words.append(
            {"text": token, "start": round(cursor, 3), "end": round(cursor + step * 0.8, 3)}
        )
        cursor += step
    return words


def _cue(start: float, end: float, text: str) -> LyricCue:
    return {"start": start, "end": end, "text": text, "words": []}


def _assert_monotone(result: AlignmentResult) -> None:
    """The invariant every consumer depends on: forwards, and never overlapping."""

    cursor = -1.0
    for line in result.lines:
        assert line.start >= 0.0
        assert line.end >= line.start
        assert line.start >= cursor, "cue starts before the previous cue ends"
        assert line.words, "a cue with no words cannot be rendered"
        assert line.start == line.words[0].start
        assert line.end == line.words[-1].end
        word_cursor = line.start
        for word in line.words:
            assert word.end >= word.start
            assert word.start >= word_cursor - 1e-9, "words go backwards inside a cue"
            word_cursor = word.end
        cursor = line.end


# --------------------------------------------------------------------------- #
# Normalisation is for comparison only
# --------------------------------------------------------------------------- #


def test_comparison_tokens_fold_case_accents_contractions_and_digits() -> None:
    assert comparison_tokens("Don't") == ("do", "not")
    assert comparison_tokens("DON\u2019T") == ("do", "not")
    assert comparison_tokens("Café") == ("cafe",)
    assert comparison_tokens("I'm") == ("i", "am")
    assert comparison_tokens("gonna") == ("going", "to")
    assert comparison_tokens("world's") == ("worlds",)
    assert comparison_tokens("rock & roll") == ("rock", "and", "roll")
    assert comparison_tokens("7") == ("seven",)
    assert comparison_tokens("21") == ("twenty", "one")
    assert comparison_tokens("1985") == ("nineteen", "eighty", "five")
    assert comparison_tokens("2005") == ("two", "thousand", "five")
    assert comparison_tokens("...") == ()


def test_a_numeral_no_int_can_read_is_a_token_and_not_a_crash() -> None:
    """`_PIECE` says which runs are numbers; the branch below it has to agree.

    `\\d+` is the Nd category, but `str.isdigit` is also true for No -- ETHIOPIC
    DIGIT EIGHT, NEW TAI LUE THAM DIGIT ONE, 69 codepoints that survive NFKD --
    and those match `_PIECE`'s *word* alternative, so testing the branch with
    `isdigit` sent them to `int()`, which raises. A bare `ValueError` is not a
    refusal this module is allowed to make: `pipeline._apply_alignment` catches
    `InvalidInputError` to skip alignment and keep the estimated spacing, and
    would have failed the whole lyrics stage instead, over one character in a
    lyric sheet a user handed over.

    So the scan is exhaustive rather than illustrative: no codepoint may crash.
    """

    assert comparison_tokens("Alpha \u1370 beta") == ("alpha", "\u1370", "beta")
    # Reachable through the public entry point, which is where it mattered.
    assert align_reference_text("Alpha \u1370 beta", _sung("alpha beta")) is not None
    for code in range(0x110000):
        comparison_tokens(chr(code))
    # The digits `_PIECE` really does mean are read as numbers, unmoved.
    assert comparison_tokens("\u0663\u0664") == ("thirty", "four")


def test_the_users_own_text_survives_alignment_untouched() -> None:
    reference = "Don't STOP me now, I'm having a good time!"
    result = align_reference_text(reference, _sung("dont stop me now im having a good time"))
    assert [word.text for word in result.lines[0].words] == reference.split()
    assert result.lines[0].text == reference


def test_token_similarity_keeps_variants_together_and_different_words_apart() -> None:
    assert token_similarity("shine", "shine") == 1.0
    assert token_similarity("shining", "shine") >= MATCH_THRESHOLD
    assert token_similarity("shine", "sign") < MATCH_THRESHOLD
    assert token_similarity("river", "") == 0.0


# --------------------------------------------------------------------------- #
# The core transfer
# --------------------------------------------------------------------------- #


def test_exact_hypothesis_gives_every_word_a_measured_time() -> None:
    reference = "Don't stop me now\nI'm having such a good time"
    hypothesis = _sung("don't stop me now i'm having such a good time", start=10.0)
    result = align_reference_text(reference, hypothesis)

    assert [line.text for line in result.lines] == [
        "Don't stop me now",
        "I'm having such a good time",
    ]
    assert all(word.origin == "matched" for line in result.lines for word in line.words)
    assert result.lines[0].start == 10.0
    assert result.lines[1].start == 12.0
    assert result.report.matched_fraction == 1.0
    assert result.report.interpolated_words == 0
    assert result.report.mean_displacement == 0.0
    assert result.report.grade == "good"
    assert result.report.usable
    _assert_monotone(result)


def test_alignment_beats_the_even_spread_it_replaces() -> None:
    """The whole point: a song with a long intro must not start at t=0."""

    reference = "Here comes the sun\nLittle darling"
    hypothesis = _sung("here comes the sun little darling", start=42.0)
    result = align_reference_text(reference, hypothesis, audio_duration=120.0)
    # The even spread this replaces would put line one at 0.0 and line two at 60.0.
    assert result.lines[0].start == 42.0
    assert 44.0 <= result.lines[1].start <= 46.0
    _assert_monotone(result)


def test_words_the_transcriber_missed_are_interpolated_and_labelled() -> None:
    reference = "hold the line for me tonight"
    hypothesis = _sung("hold the line tonight", start=5.0, step=1.0)
    result = align_reference_text(reference, hypothesis)
    words = result.lines[0].words

    assert [word.origin for word in words] == [
        "matched",
        "matched",
        "matched",
        "interpolated",
        "interpolated",
        "matched",
    ]
    # "for me" lands between "line" (5.0 + 2.0) and "tonight" (8.0), not on top of either.
    assert words[2].end <= words[3].start
    assert words[4].end <= words[5].start
    assert words[5].start == 8.0
    assert result.report.interpolated_words == 2
    assert result.report.mean_displacement > 0.0
    assert result.report.longest_unaligned_run == 2
    _assert_monotone(result)


def test_a_misheard_word_still_donates_its_timing_as_a_near_match() -> None:
    reference = "the shining river"
    hypothesis = _sung("the shinin river", start=3.0)
    result = align_reference_text(reference, hypothesis)
    assert [word.origin for word in result.lines[0].words] == ["matched", "near", "matched"]
    assert result.lines[0].words[1].start == 3.5
    assert result.report.exact_words == 2
    assert result.report.near_words == 1
    assert result.report.matched_fraction == 1.0


def test_extra_hypothesis_words_do_not_disturb_the_reference() -> None:
    reference = "one last dance"
    hypothesis = _sung("yeah yeah one last dance oh oh oh", start=2.0)
    result = align_reference_text(reference, hypothesis)
    assert [word.origin for word in result.lines[0].words] == ["matched"] * 3
    assert result.lines[0].start == 3.0
    assert result.report.matched_fraction == 1.0
    assert result.report.hypothesis_used_fraction < 0.5
    _assert_monotone(result)


def test_a_repeated_chorus_takes_each_occurrences_own_timing() -> None:
    """The hard case: five identical lines, five different correct answers."""

    verses = ["walking down an empty street", "counting every broken window"]
    lines: list[str] = []
    spoken: list[str] = []
    expected: list[float] = []
    cursor = 0.0
    for index in range(5):
        verse = f"{verses[index % 2]} number {index}"
        lines.append(verse)
        spoken.append(verse)
        cursor += 0.5 * len(verse.split())
        lines.append("hold on tight")
        expected.append(cursor)
        spoken.append("hold on tight")
        cursor += 0.5 * 3

    hypothesis = _sung(" ".join(spoken))
    result = align_reference_text("\n".join(lines), hypothesis)

    chorus_starts = [line.start for line in result.lines if line.text == "hold on tight"]
    assert len(chorus_starts) == 5
    assert chorus_starts == pytest.approx(expected)
    assert result.report.matched_fraction == 1.0
    _assert_monotone(result)


def test_lines_sung_out_of_order_lose_timing_rather_than_scrambling_it() -> None:
    reference = "first verse alpha bravo charlie\nsecond verse delta echo foxtrot"
    hypothesis = _sung("second verse delta echo foxtrot first verse alpha bravo charlie")
    result = align_reference_text(reference, hypothesis)

    # A global alignment cannot reorder, so one block keeps real timings and the
    # other is interpolated -- but the output is still monotone and honest.
    assert result.report.matched_fraction < 1.0
    assert result.report.interpolated_words > 0
    _assert_monotone(result)


# --------------------------------------------------------------------------- #
# Hypotheses that are no use at all
# --------------------------------------------------------------------------- #


def test_a_wrong_language_hypothesis_is_reported_as_unusable() -> None:
    reference = "hold the line tonight\nnothing else matters now"
    hypothesis = _sung("nous sommes perdus dans la nuit obscure et froide", start=1.0)
    result = align_reference_text(reference, hypothesis, audio_duration=30.0)

    assert result.report.matched_fraction < 0.2
    assert not result.report.usable
    assert result.report.grade == "poor"
    _assert_monotone(result)


def test_an_empty_hypothesis_falls_back_to_a_spread_and_says_so() -> None:
    reference = "line one here\nline two here\nline three here"
    result = align_reference_text(reference, [], audio_duration=60.0)

    assert result.report.matched_fraction == 0.0
    assert result.report.interpolated_words == 9
    assert not result.report.usable
    assert result.lines[0].start == 0.0
    assert result.lines[-1].end == pytest.approx(60.0)
    assert all(word.origin == "interpolated" for line in result.lines for word in line.words)
    _assert_monotone(result)


def test_a_reference_far_longer_than_the_audio_stays_ordered_and_unusable() -> None:
    reference = "\n".join(f"line number {index} of the sheet" for index in range(60))
    hypothesis = _sung("line number 0 of the sheet line number 1 of the sheet", step=0.4)
    result = align_reference_text(reference, hypothesis, audio_duration=6.0)

    assert len(result.lines) == 60
    assert result.report.longest_unaligned_run > 24
    assert not result.report.usable
    _assert_monotone(result)


def test_hypothesis_words_with_broken_timings_are_ignored_not_trusted() -> None:
    hypothesis: list[LyricWord] = [
        {"text": "hold", "start": 1.0, "end": 1.4},
        {"text": "the", "start": float("nan"), "end": 2.0},
        {"text": "line", "start": -3.0, "end": 2.5},
        {"text": "   ", "start": 3.0, "end": 3.5},
        {"text": "tonight", "start": 4.0, "end": 4.5},
    ]
    result = align_reference_text("hold the line tonight", hypothesis)
    assert result.report.hypothesis_words_ignored == 3
    assert result.report.hypothesis_words == 2
    assert result.lines[0].words[0].start == 1.0
    assert result.lines[0].words[-1].start == 4.0
    _assert_monotone(result)


def test_out_of_order_hypothesis_words_are_sorted_before_alignment() -> None:
    hypothesis: list[LyricWord] = [
        {"text": "tonight", "start": 4.0, "end": 4.5},
        {"text": "hold", "start": 1.0, "end": 1.4},
    ]
    result = align_reference_text("hold tonight", hypothesis)
    assert [word.start for word in result.lines[0].words] == [1.0, 4.0]
    _assert_monotone(result)


# --------------------------------------------------------------------------- #
# Structure of the user's sheet
# --------------------------------------------------------------------------- #


def test_section_markers_are_dropped_but_sung_parentheses_are_kept() -> None:
    reference = "[Chorus]\n(Verse 2)\nhold on tight\n(I love you)"
    result = align_reference_text(reference, _sung("hold on tight i love you"))
    assert [line.text for line in result.lines] == ["hold on tight", "(I love you)"]
    assert result.report.marker_lines_dropped == 2


def test_cues_follow_the_reference_line_breaks_not_the_hypothesis_segments() -> None:
    reference = "one two\nthree four\nfive six"
    hypothesis = _sung("one two three four five six")
    result = align_reference_text(reference, hypothesis)
    assert [len(line.words) for line in result.lines] == [2, 2, 2]
    assert [line.start for line in result.lines] == [0.0, 1.0, 2.0]


def test_align_lines_accepts_the_line_tuple_the_lyrics_module_hands_over() -> None:
    result = align_lines(("hold on tight", "for me"), _sung("hold on tight for me"))
    assert [line.text for line in result.lines] == ["hold on tight", "for me"]


def test_cues_and_annotated_cues_render_the_same_timings() -> None:
    result = align_reference_text("hold the line", _sung("hold line", step=1.0))
    cues = result.cues()
    annotated = result.annotated_cues()
    assert cues[0]["text"] == "hold the line"
    assert [word["text"] for word in cues[0]["words"]] == ["hold", "the", "line"]
    annotated_words = annotated[0]["words"]
    assert isinstance(annotated_words, list)
    assert [word["origin"] for word in annotated_words] == [
        "matched",
        "interpolated",
        "matched",
    ]
    assert annotated[0]["start"] == cues[0]["start"]
    assert annotated[0]["interpolated_words"] == 1


def test_the_report_is_safe_to_write_into_the_manifest_and_the_progress_line() -> None:
    """Both of the report's consumers take it whole and neither redacts it."""

    reference = "supercalifragilistic zzyzxian flibbertigibbet qwertyuiopal"
    result = align_reference_text(reference, _sung("zzyzxian flibbertigibbet"))
    summary = result.report.summary()
    for word in reference.split():
        assert word not in summary
    payload = result.report.as_json()
    assert payload["matched_fraction"] == result.report.matched_fraction
    assert set(payload) >= {"matched_fraction", "mean_displacement", "longest_unaligned_run"}


# --------------------------------------------------------------------------- #
# Bounds
# --------------------------------------------------------------------------- #


def test_an_empty_reference_is_rejected() -> None:
    with pytest.raises(InvalidInputError, match="no words"):
        align_reference_text("\n \n", _sung("anything at all"))
    with pytest.raises(InvalidInputError, match="no words"):
        align_reference_text("--- ...", _sung("anything at all"))


def test_an_oversized_reference_is_refused_with_its_limit_and_no_lyric_text() -> None:
    reference = " ".join(f"secretword{index}" for index in range(MAX_TOKENS + 1))
    with pytest.raises(InvalidInputError) as caught:
        align_reference_text(reference, _sung("hold on"))
    message = str(caught.value)
    assert str(MAX_TOKENS) in message
    assert "secretword" not in message


def test_an_oversized_alignment_is_refused_rather_than_quietly_degraded() -> None:
    columns = MAX_ALIGNMENT_CELLS // MAX_TOKENS + 20
    reference = " ".join(["alpha"] * MAX_TOKENS)
    hypothesis = _sung(" ".join(["beta"] * columns), step=0.2)
    with pytest.raises(InvalidInputError) as caught:
        align_reference_text(reference, hypothesis)
    message = str(caught.value)
    assert str(MAX_ALIGNMENT_CELLS) in message
    assert "alpha" not in message and "beta" not in message


def test_a_bad_duration_is_rejected() -> None:
    with pytest.raises(InvalidInputError, match="duration"):
        align_reference_text("hold on", _sung("hold on"), audio_duration=0.0)


# --------------------------------------------------------------------------- #
# A hypothesis built from something coarser than words
# --------------------------------------------------------------------------- #


def test_hypothesis_from_cues_uses_word_timings_when_they_exist() -> None:
    cues: list[LyricCue] = [
        {
            "start": 1.0,
            "end": 2.0,
            "text": "hold on",
            "words": [
                {"start": 1.0, "end": 1.4, "text": "hold"},
                {"start": 1.5, "end": 2.0, "text": "on"},
            ],
        },
        _cue(3.0, 4.0, "tight tonight"),
    ]
    words = hypothesis_from_cues(cues)
    assert [word["text"] for word in words] == ["hold", "on", "tight", "tonight"]
    assert words[0]["start"] == 1.0
    # The wordless cue is spread inside its own span, never outside it.
    assert words[2]["start"] >= 3.0
    assert words[3]["end"] <= 4.0


def test_alignment_against_line_level_captions_still_beats_a_spread() -> None:
    captions = [_cue(30.0, 33.0, "here comes the sun"), _cue(34.0, 36.0, "little darling")]
    result = align_reference_text(
        "Here comes the sun\nLittle darling",
        hypothesis_from_cues(captions),
        audio_duration=120.0,
    )
    assert result.lines[0].start == pytest.approx(30.0, abs=0.1)
    assert result.lines[1].start == pytest.approx(34.0, abs=0.5)
    assert result.report.matched_fraction == 1.0


def test_overlapping_hypothesis_spans_come_out_ordered_and_barely_moved() -> None:
    """Slurred singing gives overlapping word spans. Cues may not overlap."""

    hypothesis: list[LyricWord] = [
        {"text": "hold", "start": 1.0, "end": 1.8},
        {"text": "on", "start": 1.5, "end": 2.4},
        {"text": "tight", "start": 2.0, "end": 3.0},
    ]
    result = align_reference_text("hold on\ntight", hypothesis)
    words = [word for line in result.lines for word in line.words]
    assert [word.origin for word in words] == ["matched", "matched", "matched"]
    # Starts are pushed off the word in front of them, never pulled earlier, and
    # never by more than the overlap the transcriber reported.
    assert words[0].start == 1.0
    assert words[1].start == 1.8
    assert words[2].start == 2.4
    _assert_monotone(result)


def test_words_before_the_first_match_do_not_run_over_it() -> None:
    reference = " ".join(f"word{index}" for index in range(20)) + "\nfinal line here"
    hypothesis = _sung("final line here", start=0.5, step=0.3)
    result = align_reference_text(reference, hypothesis)
    assert result.lines[0].start == 0.0
    assert result.lines[0].end <= result.lines[1].start
    assert result.lines[1].words[0].origin == "matched"
    _assert_monotone(result)


# --------------------------------------------------------------------------- #
# Property: whatever goes in, the timings come out ordered
# --------------------------------------------------------------------------- #

_POOL = [
    "light",
    "river",
    "hold",
    "tonight",
    "never",
    "again",
    "shadow",
    "burning",
    "waiting",
    "home",
    "don't",
    "I'm",
    "seventeen",
    "1985",
    "café",
    "running",
    "away",
    "under",
    "golden",
    "rain",
    "nothing",
    "else",
    "matters",
]


def _generated_pair(seed: int) -> tuple[str, list[LyricWord]]:
    generator = random.Random(seed)
    lines = [
        " ".join(generator.choice(_POOL) for _ in range(generator.randint(1, 8)))
        for _ in range(generator.randint(1, 8))
    ]
    hypothesis: list[LyricWord] = []
    cursor = generator.uniform(0.0, 5.0)
    for line in lines:
        for word in line.split():
            roll = generator.random()
            if roll < 0.15:
                cursor += generator.uniform(0.2, 0.6)
                continue
            text = word[:-1] + generator.choice("aeiou") if roll < 0.3 else word
            duration = generator.uniform(0.05, 0.5)
            # Sung words slur into each other and faster-whisper reports that as
            # spans that overlap the next word; the output still may not.
            span = duration * (3.0 if generator.random() < 0.25 else 1.0)
            hypothesis.append(
                {"text": text, "start": round(cursor, 3), "end": round(cursor + span, 3)}
            )
            cursor += duration + generator.uniform(0.0, 0.4)
            if roll > 0.95:
                hypothesis.append(
                    {
                        "text": generator.choice(_POOL),
                        "start": round(cursor, 3),
                        "end": round(cursor + 0.2, 3),
                    }
                )
                cursor += 0.3
    return "\n".join(lines), hypothesis


@pytest.mark.parametrize("seed", range(120))
def test_generated_pairs_never_produce_overlapping_or_reversed_timings(seed: int) -> None:
    reference, hypothesis = _generated_pair(seed)
    duration = 300.0 if seed % 3 == 0 else None
    result = align_reference_text(reference, hypothesis, audio_duration=duration)

    _assert_monotone(result)
    assert [word.text for line in result.lines for word in line.words] == reference.split()
    report = result.report
    assert 0.0 <= report.matched_fraction <= 1.0
    assert 0.0 <= report.exact_fraction <= report.matched_fraction
    assert report.mean_displacement >= 0.0
    assert report.longest_unaligned_run >= 0
    assert report.exact_words + report.near_words + report.interpolated_words == sum(
        len(line.words) for line in result.lines
    )
    assert report.usable == (report.grade != "poor")
    if report.matched_fraction == 1.0:
        # Nothing was guessed, so the only uncertainty left is what the monotone
        # pass had to move: these transcripts overlap their spans on purpose, and
        # a word cannot be laid out before the one in front of it. No word can be
        # pushed further than the overlaps ahead of it add up to, and with no
        # overlap at all nothing moves and nothing is uncertain.
        overlaps = [
            max(0.0, float(first["end"]) - float(second["start"]))
            for first, second in pairwise(hypothesis)
        ]
        assert report.mean_displacement <= sum(overlaps) + 1e-3
        if not any(overlaps):
            assert report.mean_displacement == 0.0
    words = [word for line in result.lines for word in line.words]
    if (
        report.matched_fraction >= GOOD_MATCHED_FRACTION
        and report.longest_unaligned_run <= 2
        and words[0].origin != "interpolated"
        and words[-1].origin != "interpolated"
    ):
        # Every guess sits between two measured words, so each is bounded by
        # its own small hole. A run off either end is not: it is bounded by the
        # recording, which is the whole song, and that is not a defect.
        assert report.mean_displacement < 5.0


def _levenshtein(left: str, right: str) -> int:
    """Written out here on purpose, so the oracle below owes the module nothing."""

    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, start=1):
        current = [row]
        for column, right_char in enumerate(right, start=1):
            current.append(
                min(
                    previous[column] + 1,
                    current[column - 1] + 1,
                    previous[column - 1] + (0 if left_char == right_char else 1),
                )
            )
        previous = current
    return previous[-1]


def _scoring_similarity(left: str, right: str) -> float:
    """The similarity the module's documented formula implies, with no bounds at all.

    `token_similarity` cannot be its own oracle here: the length-difference
    bound is *inside* it, so tightening that bound also silences any test that
    asks `token_similarity` which pairs to check. This is the formula from the
    module docstring -- 0.75 edit-distance ratio, 0.25 shared prefix -- and
    nothing else. Tokens here are short, so the 32-character comparison window
    never applies.
    """

    if left == right:
        return 1.0
    if not left or not right:
        return 0.0
    longest = max(len(left), len(right))
    ratio = 1.0 - _levenshtein(left, right) / longest
    shared = 0
    for left_char, right_char in zip(left, right, strict=False):
        if left_char != right_char:
            break
        shared += 1
    return max(0.0, 0.75 * ratio + 0.25 * (2 * shared / (len(left) + len(right))))


def _shared_prefix(left: str, right: str) -> int:
    shared = 0
    for left_char, right_char in zip(left, right, strict=False):
        if left_char != right_char:
            break
        shared += 1
    return shared


def _row_builder_rejects(left: str, right: str) -> bool:
    """The row builder's prefilter, written out from the documented formula.

    `_aligned_pairs` skips scoring a pair whose edit distance it can already
    bound above what MATCH_THRESHOLD allows. The formula reaches the threshold
    only while ``3 * distance <= longest * (1 + prefix)``, and the bounds on the
    distance that come for free are the letters one token has that the other
    lacks -- counted each way, since either direction bounds it -- and the
    difference in length. This is those two facts put together, owing the
    module's own arithmetic nothing, so it is able to disagree with it.
    """

    floor = max(
        (_letter_mask(left) & ~_letter_mask(right)).bit_count(),
        (_letter_mask(right) & ~_letter_mask(left)).bit_count(),
        abs(len(left) - len(right)),
    )
    total = len(left) + len(right)
    longest = max(len(left), len(right))
    return 3 * floor * total > longest * (total + 2 * _shared_prefix(left, right))


def test_the_similarity_prefilter_never_costs_a_match_the_scoring_would_make() -> None:
    """The alignment skips most pairs on two cheap bounds before scoring them.

    Both bounds claim they cannot change the answer, so this drives real
    alignments -- not the bounds in isolation -- and asserts that every pair
    `token_similarity` would accept is still aligned and still donates its time.

    The mutations here insert and delete as well as substitute, which is the
    only way the *length* bound is reached at all: a generator that mutates
    characters in place produces `len(left) == len(right)` every time, and a
    length-difference bound is unreachable when the difference is always zero.
    Half of what a transcriber does to a word changes its length -- "shinin'"
    for "shining", "burn" for "burning" -- so the uneven pairs are the realistic
    ones, not the exotic ones.
    """

    generator = random.Random(3)
    alphabet = "abcdefghijklmnopqrstuvwxyz"
    checked = 0
    uneven = 0
    for _ in range(1500):
        left = "".join(generator.choice(alphabet) for _ in range(generator.randint(1, 12)))
        mutated = list(left)
        for _ in range(generator.randint(1, 4)):
            roll = generator.random()
            if roll < 0.45:
                mutated[generator.randrange(len(mutated))] = generator.choice(alphabet)
            elif roll < 0.75 or len(mutated) <= 1:
                mutated.insert(generator.randrange(len(mutated) + 1), generator.choice(alphabet))
            else:
                del mutated[generator.randrange(len(mutated))]
        right = "".join(mutated)
        if comparison_tokens(left) != (left,) or comparison_tokens(right) != (right,):
            continue  # a contraction or number expansion; not a single-token pair
        if left == right or _scoring_similarity(left, right) < MATCH_THRESHOLD:
            continue
        checked += 1
        # The scoring says this pair is worth taking, so the module must agree:
        # neither bound is allowed to veto what the formula accepts.
        assert token_similarity(left, right) == pytest.approx(_scoring_similarity(left, right)), (
            left,
            right,
        )
        if len(left) != len(right):
            uneven += 1
        result = align_lines([left], [{"text": right, "start": 4.0, "end": 4.5}])
        word = result.lines[0].words[0]
        assert word.origin == "near", (left, right, word.origin)
        assert word.start == 4.0

        longest = max(len(left), len(right))
        # Neither bound may have rejected this pair. The length one lives in
        # `token_similarity` and returns 0.0; the row builder's lives in
        # `_aligned_pairs` and skips the scoring entirely, so a pair has to
        # clear both.
        assert abs(len(left) - len(right)) * 3 <= longest * 2, (left, right)
        assert not _row_builder_rejects(left, right), (left, right)
    assert checked > 100
    assert uneven > 100, "the length bound is only exercised by pairs of unequal length"


def test_neither_similarity_bound_can_be_tightened_without_losing_real_matches() -> None:
    """Both multipliers are load-bearing, and this says by how much.

    The bounds are `difference * 3 <= longest * 2` on the length gap and on the
    letter-set gap. Each is a lower bound on the edit distance, and each is
    written to be exactly as tight as MATCH_THRESHOLD allows. "As tight as
    allowed" is the kind of claim that rots quietly -- a later tidy-up turns a 2
    into a 1, the suite stays green, and misheard words silently stop being
    timed. So: count the accepted pairs each tightened bound would throw away,
    and require that both counts are real.
    """

    generator = random.Random(3)
    alphabet = "abcdefghijklmnopqrstuvwxyz"
    accepted = 0
    lost_to_tighter_length = 0
    lost_to_tighter_mask = 0
    for _ in range(4000):
        left = "".join(generator.choice(alphabet) for _ in range(generator.randint(1, 12)))
        mutated = list(left)
        for _ in range(generator.randint(1, 4)):
            roll = generator.random()
            if roll < 0.45:
                mutated[generator.randrange(len(mutated))] = generator.choice(alphabet)
            elif roll < 0.75 or len(mutated) <= 1:
                mutated.insert(generator.randrange(len(mutated) + 1), generator.choice(alphabet))
            else:
                del mutated[generator.randrange(len(mutated))]
        right = "".join(mutated)
        if comparison_tokens(left) != (left,) or comparison_tokens(right) != (right,):
            continue
        if left == right or _scoring_similarity(left, right) < MATCH_THRESHOLD:
            continue
        accepted += 1
        longest = max(len(left), len(right))
        if abs(len(left) - len(right)) * 3 > longest * 1:
            lost_to_tighter_length += 1
        if (_letter_mask(left) & ~_letter_mask(right)).bit_count() * 3 > longest * 1:
            lost_to_tighter_mask += 1

    assert accepted > 1000
    assert lost_to_tighter_length > 20, "a tightened length bound must cost real matches"
    assert lost_to_tighter_mask > 20, "a tightened mask bound must cost real matches"

    # The named case, end to end: a dropped suffix is the transcriber's
    # commonest edit, and it is a length gap of three on a seven-letter word.
    assert token_similarity("shining", "shin") >= MATCH_THRESHOLD
    assert abs(len("shining") - len("shin")) * 3 <= len("shining") * 2
    assert abs(len("shining") - len("shin")) * 3 > len("shining") * 1
    timed = align_lines(["shining"], [{"text": "shin", "start": 4.0, "end": 4.5}])
    assert timed.lines[0].words[0].origin == "near"
    assert timed.lines[0].words[0].start == 4.0


def test_the_prefix_is_what_lets_the_row_builders_bound_be_tighter_than_the_other() -> None:
    """The row builder uses the pair's own prefix; `token_similarity` cannot.

    The bound above assumes the prefix term is at its maximum, because a
    function that has not looked at the two tokens has nothing cheaper to
    assume. The row builder has looked: the prefix is exactly zero the moment
    the first characters differ, which is most pairs, and the requirement is
    then half as loose. That is a different bound from the one the test above
    says must not be tightened, and it needs both halves proved separately --
    it must reject nothing the scoring would accept, and it must reject enough
    to be worth the arithmetic, or it is a comment pretending to be an
    optimisation.
    """

    generator = random.Random(11)
    alphabet = "abcdefghijklmnopqrstuvwxyz"
    accepted = 0
    lost = 0
    rejected_by_tightened = 0
    rejected_by_loose = 0
    below = 0
    for _ in range(6000):
        left = "".join(generator.choice(alphabet) for _ in range(generator.randint(1, 12)))
        mutated = list(left)
        for _ in range(generator.randint(1, 6)):
            roll = generator.random()
            if roll < 0.45:
                mutated[generator.randrange(len(mutated))] = generator.choice(alphabet)
            elif roll < 0.75 or len(mutated) <= 1:
                mutated.insert(generator.randrange(len(mutated) + 1), generator.choice(alphabet))
            else:
                del mutated[generator.randrange(len(mutated))]
        right = "".join(mutated)
        if comparison_tokens(left) != (left,) or comparison_tokens(right) != (right,):
            continue
        if left == right:
            continue
        if _scoring_similarity(left, right) >= MATCH_THRESHOLD:
            accepted += 1
            if _row_builder_rejects(left, right):
                lost += 1
            continue
        below += 1
        longest = max(len(left), len(right))
        if _row_builder_rejects(left, right):
            rejected_by_tightened += 1
        if (_letter_mask(left) & ~_letter_mask(right)).bit_count() * 3 > longest * 2:
            rejected_by_loose += 1

    assert accepted > 500
    assert lost == 0, "the tightened bound rejected a pair the scoring accepts"
    assert below > 500
    # Both halves of "worth having": it throws out most of what the scoring
    # would have thrown out anyway, and far more of it than the loose bound did.
    assert rejected_by_tightened > below * 0.6
    assert rejected_by_tightened > rejected_by_loose * 1.5


def test_tokens_longer_than_the_comparison_window_are_near_not_exact() -> None:
    """Similarity compares a bounded window; equality inside it is not equality.

    Two tokens that agree for the first 32 characters score 1.0 and are aligned,
    which is right -- but calling them an exact match would report a guessed
    word as a measured one, which is the distinction this module exists to keep.
    """

    head = "abcdefghijklmnopqrstuvwxyzabcdef"
    result = align_lines(
        [head + "zzzzzzzz"], [{"text": head + "qqqqwwww", "start": 2.0, "end": 2.5}]
    )
    assert result.lines[0].words[0].origin == "near"
    assert result.lines[0].words[0].start == 2.0


def test_alignment_does_not_mutate_the_caller_s_hypothesis() -> None:
    hypothesis = _sung("hold the line tonight", start=4.0)
    before = [dict(word) for word in hypothesis]
    align_reference_text("hold the line\ntonight", hypothesis)
    assert [dict(word) for word in hypothesis] == before


def test_a_pair_below_the_threshold_is_never_taken_even_where_the_arithmetic_ties() -> None:
    """The floor under a rejected pair is what makes MATCH_THRESHOLD exact.

    `_similarity` can land one ulp below the threshold: five characters, one
    shared and two edits apart gives 0.49999999999999994, and `_pair_score` puts
    that one ulp of its own below the two gaps the pair would replace. In exact
    arithmetic that settles it -- the pair is worth less than the gaps it
    replaces, so it can never be on an optimal path, and its score could be
    anything. In floating point it does not settle it: added to a corner an
    ordinary alignment reaches, the pair's score and two gaps come out as the
    *same double*, and the fill's `>=` tie-break takes the diagonal.

    The alignment below is one where that happens -- "slate" against "shade" at
    row 10, column 15. `score_row` floors the pair to _UNRELATED_SCORE, far
    enough below two gaps that no rounding reaches the tie, and the fill skips
    the diagonal for it; take both away and this traceback gains a pair the
    threshold rejects. The floored answer is the correct one, which is why the
    floor is not merely a saving.
    """

    similarity = token_similarity("slate", "shade")
    assert similarity < MATCH_THRESHOLD
    assert _pair_score(similarity) < 2 * GAP_PENALTY
    # ...but not by enough to survive being added to that cell's corner.
    corner = -9.559999999999995
    assert corner + _pair_score(similarity) == corner + 2 * GAP_PENALTY

    reference = "blade water away brave away shine blade away night grave slate shine"
    hypothesis = _sung(
        "gold burning crave glade glade home shade shave "
        "burning water water home slate shine shade shine"
    )
    words = align_lines([reference], hypothesis).lines[0].words
    assert words[10].text == "slate"
    assert words[10].origin == "interpolated", "the sub-threshold pair the rounding ties on"
    # The whole traceback, so that a change anywhere in it is visible here.
    measured = [index for index, word in enumerate(words) if word.origin != "interpolated"]
    assert measured == [0, 1, 5, 11]


# --------------------------------------------------------------------------- #
# What the confidence report is worth
#
# `mean_displacement` is what a caller reads to decide whether to use this
# alignment at all, so these are about the number being *right*, not merely
# present. The shape they exist for: the first word of every unmatched run used
# to be measured against the anchor it was placed on top of, and so reported
# exactly zero uncertainty -- perfect confidence on the single commonest guess
# there is.
# --------------------------------------------------------------------------- #


def test_a_word_guessed_inside_a_long_hole_does_not_report_zero_uncertainty() -> None:
    """One word missing between two anchors nineteen seconds apart.

    This is the commonest transcriber error there is, and the report used to
    call it 0.00s of uncertainty because it measured to the *nearest* anchor --
    which, for the first word of a run, is the one it was placed on top of.
    """

    hypothesis: list[LyricWord] = [
        {"text": "alpha", "start": 0.0, "end": 0.4},
        {"text": "bravo", "start": 1.0, "end": 1.4},
        {"text": "delta", "start": 21.0, "end": 21.4},
    ]
    result = align_reference_text("alpha bravo charlie delta", hypothesis, audio_duration=30.0)

    guessed = result.lines[0].words[2]
    assert guessed.text == "charlie"
    assert guessed.origin == "interpolated"
    # It was placed at 1.4 in a hole running to 21.0, so it can be 19.6s out.
    assert guessed.start == pytest.approx(1.4)
    assert result.report.mean_displacement == pytest.approx(19.6 / 4, abs=0.05)
    assert "0.00s" not in result.report.summary()


def test_the_first_word_of_a_run_is_measured_like_every_other_word_in_it() -> None:
    """Every word of a run is bounded by the hole, not by its distance travelled.

    A run's first word sits on the left anchor and its last sits on the right
    one, so measuring to the nearer anchor reports zero at both ends and hides
    the hole entirely. Runs of one, two and three words must all report it.
    """

    for missing in (1, 2, 3):
        words = ["start"] + [f"gap{index}" for index in range(missing)] + ["finish"]
        hypothesis: list[LyricWord] = [
            {"text": "start", "start": 0.0, "end": 0.5},
            {"text": "finish", "start": 40.0, "end": 40.5},
        ]
        result = align_reference_text(" ".join(words), hypothesis, audio_duration=60.0)
        guessed = [word for word in result.lines[0].words if word.origin == "interpolated"]
        assert len(guessed) == missing
        # The hole is 39.5s wide, so the mean over the whole line cannot be
        # small however the run is spread inside it.
        assert result.report.mean_displacement > 39.5 * missing / (missing + 2) * 0.5, missing


def test_the_reported_uncertainty_is_an_upper_bound_on_the_error_it_stands_for() -> None:
    """The claim the field makes, checked against recorded ground truth.

    A synthetic song with known word times, transcribed with a quarter of the
    words missing. `mean_displacement` says it is an upper bound on the mean
    placement error, so compute the real error and check that it is one. The old
    measure reported far less than the error it claimed to bound.

    Every word here is distinct, which is what makes the check exact: the bound
    holds while the measured words either side of a guess are the right ones,
    and a sheet with no repeated token gives the alignment no wrong occurrence
    to pair with. The repeated-token case is a documented limit with its own
    test below.
    """

    generator = random.Random(11)
    trials = 0
    for _ in range(12):
        sheet: list[str] = []
        truth: list[float] = []
        serial = 0
        cursor = generator.uniform(2.0, 20.0)
        for _ in range(25):
            line: list[str] = []
            for _ in range(generator.randint(4, 8)):
                line.append(f"{generator.choice(_POOL)}{serial}")
                serial += 1
            sheet.append(" ".join(line))
            rate = generator.uniform(0.25, 0.55)
            for _ in line:
                truth.append(cursor)
                cursor += rate
            cursor += generator.uniform(0.3, 1.0)
            if generator.random() < 0.2:
                cursor += generator.uniform(5.0, 30.0)  # a solo, or a breakdown
        duration = cursor + 5.0

        flat = [word for line in sheet for word in line.split()]
        assert len(set(flat)) == len(flat)
        hypothesis: list[LyricWord] = [
            {"text": word, "start": round(start, 3), "end": round(start + 0.2, 3)}
            for word, start in zip(flat, truth, strict=True)
            if generator.random() >= 0.25  # the transcriber never heard the rest
        ]

        result = align_lines(sheet, hypothesis, audio_duration=duration)
        placed = [word.start for line in result.lines for word in line.words]
        error = sum(abs(got - want) for got, want in zip(placed, truth, strict=True)) / len(truth)
        assert error > 0.0, "a trial with no error to bound proves nothing"
        # 1e-3 of slack: the output is rounded to milliseconds.
        assert result.report.mean_displacement + 1e-3 >= error, (
            result.report.mean_displacement,
            error,
        )
        trials += 1
    assert trials == 12, "every trial must be checked, or the property is untested"


def test_a_run_off_the_end_is_bounded_by_the_audio_it_could_still_fill() -> None:
    """No anchor to the right, so the recording's own end is the far edge.

    A transcript that stops early leaves the rest of the sheet placed at the
    singing rate and genuinely unknown: those words could be anywhere between
    the last measured word and the end of the audio, and the report says so.
    """

    reference = "one two three four five six seven eight"
    hypothesis = _sung("one two three", start=1.0, step=0.5)
    result = align_reference_text(reference, hypothesis, audio_duration=240.0)
    assert result.report.longest_unaligned_run == 5
    # The five guessed words sit somewhere in the 238 seconds after "three".
    assert result.report.mean_displacement > 100.0
    assert not result.report.usable

    # Without a duration there is no far edge to measure to, and the module
    # says so rather than inventing one: the run's own extent is all it has.
    blind = align_reference_text(reference, hypothesis)
    assert blind.report.mean_displacement < result.report.mean_displacement
    assert blind.report.mean_displacement > 0.0


def test_words_before_the_first_match_are_bounded_by_the_start_of_the_recording() -> None:
    """A long intro: the first line could have been sung anywhere inside it."""

    result = align_reference_text(
        "quiet opening line\nhold on tight",
        _sung("hold on tight", start=48.0),
        audio_duration=90.0,
    )
    opening = result.lines[0].words
    assert all(word.origin == "interpolated" for word in opening)
    # Nothing is sung before the recording starts, so the stretch is [0, 48].
    assert result.report.mean_displacement > 15.0
    assert result.report.mean_displacement <= 48.0


def test_a_measured_word_the_monotone_pass_moved_is_not_reported_as_certain() -> None:
    """A word the transcript timed, moved by seconds, used to report 0.00s.

    `_whisper_worker` substitutes the *segment's* end when faster-whisper omits
    a word end, so one word inside a 25-second segment can claim to end at 26s.
    Every measured word behind it is then dragged to 26s by the monotone pass --
    the layout cannot put a word before the one in front of it -- and the report
    called all of that "measured, therefore certain": `mean uncertainty 0.00s,
    alignment good`, over a mean placement error of 8.9 seconds.

    A measured word's stretch is the instant the transcript gave it, so the
    number now carries how far the pass had to move it away from that instant.
    """

    words = [f"word{index}" for index in range(40)]
    truth = [round(2.0 + index * 0.6, 3) for index in range(40)]
    segment_end = truth[-1] + 0.6
    hypothesis: list[LyricWord] = [
        {
            "text": word,
            "start": start,
            # Word 5 lost its end, so it inherits the segment's.
            "end": segment_end if index == 5 else round(start + 0.4, 3),
        }
        for index, (word, start) in enumerate(zip(words, truth, strict=True))
    ]

    result = align_lines([" ".join(words)], hypothesis, audio_duration=segment_end + 30.0)
    placed = [word.start for line in result.lines for word in line.words]
    assert result.report.matched_fraction == 1.0, "every word is measured; none is a guess"
    assert placed[6] == pytest.approx(segment_end), "the pass dragged the rest to the segment end"

    error = sum(abs(got - want) for got, want in zip(placed, truth, strict=True)) / len(truth)
    assert error == pytest.approx(8.925, abs=1e-3), error
    assert result.report.mean_displacement == pytest.approx(error, abs=1e-3)
    assert "mean uncertainty 8.93s" in result.report.summary()
    assert result.report.grade == "fair"

    # And a transcript that needed no moving still reports certainty, so the
    # number is not merely always non-zero.
    clean: list[LyricWord] = [
        {"text": word, "start": start, "end": round(start + 0.4, 3)}
        for word, start in zip(words, truth, strict=True)
    ]
    settled = align_lines([" ".join(words)], clean, audio_duration=segment_end + 30.0)
    assert settled.report.mean_displacement == 0.0
    assert settled.report.grade == "good"


def test_the_bound_off_the_end_follows_words_laid_out_past_the_audio() -> None:
    """The far edge of a trailing run is the words' own reach, not the duration.

    A run too long for the audio left is laid out past it -- claiming sixty
    words fit in the last second would be the lie -- so the interval that bounds
    them has to follow them out there. Clamping the edge to `audio_duration`
    would report *less* uncertainty for words placed further from anything
    measured, which is backwards.
    """

    reference = "anchorone anchortwo " + " ".join(f"word{index}" for index in range(60))
    hypothesis: list[LyricWord] = [
        {"text": "anchorone", "start": 0.0, "end": 0.4},
        {"text": "anchortwo", "start": 0.5, "end": 0.9},
    ]
    result = align_lines([reference], hypothesis, audio_duration=2.0)

    words = [word for line in result.lines for word in line.words]
    assert words[-1].end == pytest.approx(18.4, abs=0.1), "the run runs past the audio"
    # Edge 18.4 with the run's left anchor at 0.9: every word is at least 8.7s
    # from one end of that interval, and the mean comes out at 12.79. Clamping
    # the edge to audio_duration=2.0 reports 8.154 for the same words in the
    # same places -- less uncertainty the further they are laid from anything
    # measured, which is backwards. (Measured by making that change; it fails
    # this assertion and nothing else in the file.)
    assert result.report.mean_displacement == pytest.approx(12.79, abs=0.05)
    assert not result.report.usable


def test_a_repeated_token_can_be_matched_to_the_wrong_occurrence_and_hide_the_hole() -> None:
    """The limit the confidence report has and cannot currently see.

    A sheet that repeats a token gives the alignment two pairings that score
    exactly the same, and nothing in the token stream separates them. Pick the
    later one and the words before it collapse into a hole milliseconds wide,
    so the uncertainty bound -- which is only ever a bound *given the anchors
    are right* -- collapses too.

    This pins the current, wrong behaviour on purpose. It is in "Known limits"
    in the module docstring, and a fix should turn these assertions round.
    """

    head = [f"head{index}" for index in range(21)]
    tail = [f"tail{index}" for index in range(21)]
    reference = head + ["echo"] + [f"gap{index}" for index in range(7)] + ["echo"] + tail
    hypothesis: list[LyricWord] = [
        {"text": word, "start": index * 0.4, "end": index * 0.4 + 0.3}
        for index, word in enumerate(head)
    ]
    hypothesis.append({"text": "echo", "start": 8.4, "end": 8.7})
    hypothesis += [
        {"text": word, "start": 70.0 + index * 0.4, "end": 70.3 + index * 0.4}
        for index, word in enumerate(tail)
    ]

    result = align_lines([" ".join(reference)], hypothesis, audio_duration=90.0)
    words = result.lines[0].words
    # The sheet's *second* "echo" took the transcript's only one, so the eight
    # words before it are crammed in behind it instead of spanning to 70s.
    assert words[29].text == "echo" and words[29].origin == "matched"
    assert words[29].start == pytest.approx(8.4)
    assert all(word.origin == "interpolated" for word in words[21:29])
    assert words[28].start < 8.4

    # Every gate passes, and every gate is wrong: eight words sit at 8.4s that
    # were sung somewhere in the sixty seconds after it.
    assert result.report.mean_displacement < 0.1
    assert result.report.matched_fraction > USABLE_MATCHED_FRACTION
    assert result.report.longest_unaligned_run <= USABLE_UNALIGNED_RUN
    assert result.report.grade == "fair"
    assert result.report.usable


def test_the_good_grade_demotes_an_alignment_whose_few_gaps_are_long() -> None:
    """Why GOOD_MEAN_DISPLACEMENT is worth having at all.

    Coverage and run length both say this alignment is excellent -- 99% of words
    measured, never more than two missing in a row. Only the uncertainty bound
    notices that the two it missed sit either side of a three-minute
    instrumental, and are therefore anyone's guess.
    """

    words = [f"word{index}" for index in range(120)]
    hypothesis: list[LyricWord] = []
    cursor = 0.0
    for index, word in enumerate(words):
        if index in (60, 61):
            cursor += 180.0  # the solo the transcriber heard no words in
            continue
        hypothesis.append({"text": word, "start": round(cursor, 3), "end": round(cursor + 0.3, 3)})
        cursor += 0.4
    result = align_lines([" ".join(words)], hypothesis, audio_duration=cursor + 10.0)

    assert result.report.matched_fraction > GOOD_MATCHED_FRACTION
    assert result.report.longest_unaligned_run <= 2
    assert result.report.mean_displacement > GOOD_MEAN_DISPLACEMENT
    assert result.report.grade == "fair", "the bound is the only test that can demote this"
    assert result.report.usable


def test_an_oversized_transcript_is_refused_with_its_limit_and_no_lyric_text() -> None:
    """The mirror of the reference cap: a short sheet against an endless transcript.

    Truncating the transcript would misalign everything after the cut without
    saying so, which is the failure nobody can see. The sheet here is two words,
    so nothing else in the module is anywhere near a limit -- only the
    transcript's own cap can refuse this.
    """

    hypothesis = _sung(" ".join(f"heard{index}" for index in range(MAX_TOKENS + 1)), step=0.1)
    with pytest.raises(InvalidInputError) as caught:
        align_reference_text("hold on", hypothesis)
    message = str(caught.value)
    assert "transcript" in message
    assert str(MAX_TOKENS) in message
    assert "heard" not in message and "hold" not in message


# --------------------------------------------------------------------------- #
# The bound that bounds time
#
# MAX_ALIGNMENT_CELLS bounds the traceback's memory. It does not bound the
# clock: at that cap alone, with the comparison cap lifted, 3000 x 1900
# all-distinct nine-letter tokens take five seconds and 1000 x 6000 distinct
# thirty-two-character ones take two and a half minutes. Scoring the distinct
# token pairs is the half that runs away, so that is what MAX_COMPARISON_CELLS
# bounds -- not by counting the pairs, and not by counting their characters
# either, but by pricing both: a pair costs a call before it costs a character,
# and a cap that charged only the characters let short tokens run the clock up
# while the count stayed small. "Cost" in the module docstring has the sweep
# behind COMPARISON_PAIR_CELLS, the time budget MAX_COMPARISON_CELLS is derived
# from, the measured ceiling, and where the estimate is furthest from the clock.
# --------------------------------------------------------------------------- #

_LOW_LETTERS = "abcdefghijklmnopqrstuv"


def _unshared_reference(count: int) -> list[str]:
    """Distinct 22-character tokens, each containing every letter a-v exactly once."""

    generator = random.Random(1)
    tokens: set[str] = set()
    while len(tokens) < count:
        letters = list(_LOW_LETTERS)
        generator.shuffle(letters)
        tokens.add("".join(letters))
    return sorted(tokens)


def _unshared_hypothesis(count: int, length: int) -> list[LyricWord]:
    """Distinct tokens over w-z, so they share no letter with the reference."""

    generator = random.Random(2)
    tokens: set[str] = set()
    while len(tokens) < count:
        tokens.add("".join(generator.choice("wxyz") for _ in range(length)))
    return [
        {"text": token, "start": round(index * 0.1, 3), "end": round(index * 0.1 + 0.05, 3)}
        for index, token in enumerate(sorted(tokens))
    ]


def _short_reference(count: int, length: int) -> list[str]:
    """`count` distinct short tokens that survive `comparison_tokens` unchanged.

    Short tokens are the shape the cap used to under-charge, so they are what
    the tests below are built out of. Anything the tokeniser would rewrite --
    "its" into "it is", a digit run into words -- is skipped, so that a token
    here is one token there and the arithmetic in the tests is the arithmetic
    the module does.
    """

    generator = random.Random(3)
    tokens: set[str] = set()
    while len(tokens) < count:
        token = "".join(generator.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(length))
        if comparison_tokens(token) == (token,):
            tokens.add(token)
    return sorted(tokens)


def _short_vocabularies(
    reference: int, hypothesis: int, length: int
) -> tuple[list[str], list[str]]:
    """Two disjoint vocabularies of distinct short tokens."""

    tokens = _short_reference(reference + hypothesis, length)
    return tokens[:reference], tokens[reference:]


def _short_hypothesis(tokens: list[str]) -> list[LyricWord]:
    return [
        {"text": token, "start": round(index * 0.1, 3), "end": round(index * 0.1 + 0.05, 3)}
        for index, token in enumerate(tokens)
    ]


def test_a_transcript_too_expensive_to_compare_is_refused_not_ground_through() -> None:
    """The cost is in comparing tokens, and it has a cap of its own.

    100 x 1200 tokens is 120,000 cells -- a fiftieth of MAX_ALIGNMENT_CELLS, and
    nothing like a memory problem. It is refused anyway, because scoring
    22-character tokens against 32-character ones twelve hundred times per row
    is where the time goes, and the memory cap cannot see that at all.
    """

    reference = " ".join(_unshared_reference(100))
    with pytest.raises(InvalidInputError) as caught:
        align_lines([reference], _unshared_hypothesis(1200, 32))
    message = str(caught.value)
    assert str(MAX_COMPARISON_CELLS) in message
    assert "comparison cells" in message
    # Numbers and fixed vocabulary only: a refusal from this module is quoted
    # back to the user through `public_error`, which erases URLs and paths but
    # cannot know a lyric from a filename, so nothing here may carry one.
    assert not any(token in message for token in reference.split())


def test_a_pair_is_charged_for_being_a_pair_before_it_is_charged_for_its_characters() -> None:
    """The half of the cost the character count could not see.

    1500 distinct three-character reference tokens against 1000 distinct ones is
    13,500,000 characters of comparison -- under a fifth of the cap, and a shape
    the cap admitted for as long as it counted characters alone. What it really
    is, is a million and a half calls into the scoring, and a call costs the same
    whether the tokens are three characters or thirty. Charged at
    COMPARISON_PAIR_CELLS a pair that is 75,000,000 cells on its own, so the
    alignment is refused.

    What it is refused for is the shape, not this vocabulary. The same 1500 x 1000
    three-character shape measures 7.61 s over an alphabet the letter mask cannot
    separate and 1.25 s over the a-z draw built here, which the mask prunes --
    and telling those apart means comparing every pair's masks, which is most of
    what the cheap one costs. So the estimate charges for the dearer one. Both
    figures are under "Cost" in the module docstring.
    """

    reference, hypothesis = _short_vocabularies(1500, 1000, 3)
    with pytest.raises(InvalidInputError) as caught:
        align_lines([" ".join(reference)], _short_hypothesis(hypothesis))
    # The refusal carries the limit and no lyric text -- checked on the
    # distinctive tokens of the test above, since three-letter tokens drawn from
    # a-z collide with the message's own words and could only be checked for
    # accidentally.
    assert str(MAX_COMPARISON_CELLS) in str(caught.value)
    # The two halves separately, by pricing the same rows against no hypothesis
    # tokens and then against all thousand of them: the characters are nowhere
    # near the cap, and the pair charge is the whole of what carries it over.
    assert _comparison_cost([3] * 1500, 1000 * 3, 0) < MAX_COMPARISON_CELLS
    assert _comparison_cost([3] * 1500, 1000 * 3, 1000) > MAX_COMPARISON_CELLS


def test_a_refusal_costs_nothing_of_the_work_it_is_refusing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refused before the first token comparison, not after most of them.

    The estimate is the same arithmetic the fill would do, run over the token
    lists first, so an input the cap turns down never reaches `_distance` at
    all. That matters where this runs: the user is in front of an intake screen
    with nothing to cancel, and a refusal that arrives after the work is a
    refusal that cost them the whole budget the cap exists to save.
    """

    def refuse(*arguments: object) -> int:
        raise AssertionError("a refused alignment measured a token distance")

    monkeypatch.setattr(alignment, "_distance", refuse)
    reference, hypothesis = _short_vocabularies(1500, 1000, 3)
    with pytest.raises(InvalidInputError):
        align_lines([" ".join(reference)], _short_hypothesis(hypothesis))


def test_the_cap_admits_the_densest_long_song_it_is_drawn_around() -> None:
    """What the cap is calibrated against, in the shape it was calibrated on.

    The densest long song the two size caps leave room for, rounded up: a
    2500-token sheet whose 900 distinct tokens average five characters, against
    a 2400-token transcript with 950 distinct ones. "Cost" in the module
    docstring measures a song of that shape at 763,020 pairs, 58,500,492
    estimated cells and 2.03 s; the round numbers here come to 855,000 pairs and
    64,125,000 cells, and MAX_COMPARISON_CELLS is derived to leave room above
    that. A change to either number that refuses this is refusing real material,
    which is the one thing this cap must not do.
    """

    song = _comparison_cost([5] * 900, 950 * 5, 950)
    assert song == 900 * 5 * (950 * 5) + COMPARISON_PAIR_CELLS * 950 * 900
    assert song < MAX_COMPARISON_CELLS


def test_the_comparison_cap_counts_characters_as_well_as_pairs() -> None:
    """Why the cap is length-aware too, stated as a difference the test can see.

    These two alignments have exactly the same number of token pairs to score --
    100 reference tokens against 1200 hypothesis ones -- and only the length of
    the hypothesis tokens differs. So the pair charge is identical in the two,
    6,000,000 cells, and everything that separates them is characters: scoring a
    pair costs a pass over the token being measured against, so the same
    120,000 pairs are about four times the work at 32 characters that they are
    at eight, and the character half of the estimate is four times larger for
    exactly that reason. Where that half and the clock part company is the
    *reference* side, which is quadratic in the estimate and linear on the clock
    -- measured under "Cost" in the module docstring.
    """

    reference = [" ".join(_unshared_reference(100))]
    with pytest.raises(InvalidInputError):
        align_lines(reference, _unshared_hypothesis(1200, 32))
    result = align_lines(reference, _unshared_hypothesis(1200, 8))
    assert result.report.alignable_words == 100


def test_a_repeated_vocabulary_costs_nothing_extra_however_long_the_song() -> None:
    """The cap is on distinct comparisons, which is why real lyrics never meet it.

    A chorus sung five times is one row of scoring, not five. This is the same
    60 x 1000 shape as the refusal above, with a vocabulary of two -- and it
    aligns, because there are only two distinct token pairs in it.
    """

    result = align_lines(
        [" ".join(["alpha"] * 60)],
        [
            {"text": "gamma", "start": round(index * 0.1, 3), "end": round(index * 0.1 + 0.05, 3)}
            for index in range(1000)
        ],
    )
    assert result.report.alignable_words == 60
    assert result.report.matched_fraction == 0.0


def test_a_measured_alignment_with_long_gaps_is_still_offered_not_thrown_away() -> None:
    """What `usable` promises, checked against the thing it would fall back to.

    False means "keep the timing you already had", which for an untimed sheet is
    an even spread across the duration. So an alignment that is far closer to
    the truth than that spread must not be marked unusable, however wide the
    holes it honestly reports. Sixteen words missed either side of a
    three-minute instrumental report a mean bound of about five seconds -- which
    the previous threshold of 2.5 s condemned, while the alignment it discarded
    was more than twenty times closer to the truth than the spread replacing it.
    """

    words = [f"word{index}" for index in range(420)]
    hypothesis: list[LyricWord] = []
    cursor = 15.0
    truth: list[float] = []
    for index, word in enumerate(words):
        truth.append(cursor)
        if not 202 <= index < 218:
            hypothesis.append(
                {"text": word, "start": round(cursor, 3), "end": round(cursor + 0.3, 3)}
            )
        cursor += 0.4
        if index == 209:
            cursor += 180.0
    duration = cursor + 10.0
    sheet = [" ".join(words[index : index + 7]) for index in range(0, len(words), 7)]
    result = align_lines(sheet, hypothesis, audio_duration=duration)

    assert result.report.mean_displacement > 2.5, "the holes are honestly reported"
    assert result.report.usable, "and the alignment is kept anyway"

    placed = [word.start for line in result.lines for word in line.words]
    error = sum(abs(got - want) for got, want in zip(placed, truth, strict=True)) / len(truth)
    weights = [max(1, len(word)) for word in words]
    spread_cursor = 0.0
    spread_error = 0.0
    for weight, want in zip(weights, truth, strict=True):
        spread_error += abs(spread_cursor - want)
        spread_cursor += duration * weight / sum(weights)
    spread_error /= len(truth)
    assert error < spread_error / 10, (error, spread_error)
    # And the bound it reported really was one.
    assert result.report.mean_displacement + 1e-3 >= error


def test_a_transcript_that_covers_almost_none_of_the_song_is_still_refused() -> None:
    """The other side of the same threshold: a bound this wide is not a timing."""

    words = [f"word{index}" for index in range(60)]
    hypothesis: list[LyricWord] = [
        {"text": word, "start": round(index * 0.4, 3), "end": round(index * 0.4 + 0.3, 3)}
        for index, word in enumerate(words[:40])
    ]
    result = align_lines([" ".join(words)], hypothesis, audio_duration=600.0)
    assert result.report.matched_fraction > USABLE_MATCHED_FRACTION
    assert result.report.longest_unaligned_run <= USABLE_UNALIGNED_RUN
    assert result.report.mean_displacement > USABLE_MEAN_DISPLACEMENT
    assert result.report.grade == "poor", "the bound is the only test that can refuse this"
    assert not result.report.usable


# --------------------------------------------------------------------------- #
# Live code that had no test of its own
# --------------------------------------------------------------------------- #


def test_a_cue_that_would_last_no_time_is_given_a_floor_but_never_past_the_next_line() -> None:
    """Both halves of the floor, because the comment above it claims both.

    A cue rounded to the same instant at both ends is a point rather than a
    span, so it is given MIN_CUE_SPAN_SECONDS -- but never taken past where the
    next line starts, which would overlap it. That trade means a line whose
    successor begins on the same instant keeps its zero span, and that is the
    documented behaviour rather than a missed case.
    """

    stretched = align_lines(
        ["alpha", "bravo"],
        [
            {"text": "alpha", "start": 5.0, "end": 5.0},
            {"text": "bravo", "start": 20.0, "end": 20.4},
        ],
    )
    assert stretched.lines[0].start == 5.0
    assert stretched.lines[0].end == pytest.approx(5.0 + MIN_CUE_SPAN_SECONDS)
    assert stretched.lines[0].end <= stretched.lines[1].start
    _assert_monotone(stretched)

    # The next line starts on the same instant, so there is nowhere to grow.
    crowded = align_lines(
        ["alpha", "bravo"],
        [
            {"text": "alpha", "start": 5.0, "end": 5.0},
            {"text": "bravo", "start": 5.0, "end": 5.4},
        ],
    )
    assert crowded.lines[0].start == crowded.lines[0].end == 5.0
    _assert_monotone(crowded)


def test_the_singing_rate_is_clamped_before_it_lays_out_a_run() -> None:
    """One freakishly wide anchor pair must not stretch the tail across the song.

    Two matched words 200 seconds apart with ten characters between them imply
    twenty seconds a character. Left unclamped, the two words after them would
    be laid out over four hundred seconds; clamped, they take the couple of
    seconds a sung word actually takes.
    """

    result = align_reference_text(
        "alpha omega tail1 tail2",
        [
            {"text": "alpha", "start": 0.0, "end": 0.4},
            {"text": "omega", "start": 200.0, "end": 200.4},
        ],
    )
    tail = result.lines[0].words[2:]
    assert [word.origin for word in tail] == ["interpolated", "interpolated"]
    for word in tail:
        assert word.end - word.start == pytest.approx(5 * 0.5, abs=0.01)
    assert result.lines[0].end < 210.0
    _assert_monotone(result)


def test_a_reference_of_pasted_prose_is_refused_on_characters_before_tokens() -> None:
    """The character cap, which is a separate limit from the token one.

    A sheet made of very long unbroken runs never reaches MAX_TOKENS -- each run
    is one token -- so the token cap cannot refuse it. The character cap is
    checked line by line as the sheet is read, before anything is tokenised,
    which is what stops a pasted novel becoming the failure mode.
    """

    lines = ["x" * 1000] * 300
    assert len(lines) < MAX_TOKENS, "the token cap must not be what refuses this"
    with pytest.raises(InvalidInputError) as caught:
        align_lines(lines, _sung("hold on"))
    message = str(caught.value)
    assert str(MAX_REFERENCE_CHARS) in message
    assert "character" in message
    assert "x" * 20 not in message
