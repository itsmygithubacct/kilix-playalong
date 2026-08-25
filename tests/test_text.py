"""The two text policies, and the callers that must not restate them.

`text.py` exists because both rules in it were written more than once by modules
built in parallel and both sets of copies had drifted. So the interesting checks
here are not "does `printable_line` collapse spaces" but "do the four callers of
the display rule still give one answer" and "do the two folds still share a head":
the module can only earn its place while nothing has quietly grown a second copy
again.
"""

from __future__ import annotations

import random
import unicodedata

from kilix_playalong import _whisper_worker as worker
from kilix_playalong import alignment, lyrics, pipeline, source
from kilix_playalong.text import (
    MAX_DISPLAY_TEXT,
    fold_accents,
    printable_block,
    printable_line,
)
from kilix_playalong.util import public_error

#: One ESC-and-CSI sequence, the shape that matters: a terminal acts on it, and
#: dropping it rather than folding it closes ``Alpha`` up onto ``[31m``.
ESCAPED = "Alpha\x1b[31m beta"
NEUTERED = "Alpha [31m beta"


def test_an_unprintable_becomes_a_space_and_never_nothing() -> None:
    """The one decision in `printable_line`, and the reason it is not `str.strip`.

    Dropping is the shorter spelling and it is wrong: it produces ``Alpha[31m
    beta``, a string the source never said, which reads as a word rather than as
    the damage it is. Every surface downstream renders what comes out of here.
    """

    assert printable_line(ESCAPED) == NEUTERED
    assert printable_line("a\x00\x07b") == "a b"
    # Whitespace of every kind collapses to one space, and the ends come off.
    assert printable_line("  a\t\n\u2028 b\xa0\xa0c  ") == "a b c"
    assert printable_line("") == ""


def test_the_cap_is_optional_because_one_caller_bounds_its_text_elsewhere() -> None:
    """`lyrics` passes no limit, and that is a decision rather than an omission.

    A sung line is as long as it is; what that module bounds is the document.
    Capping here would truncate a long caption line to no purpose.
    """

    long_line = "x" * (MAX_DISPLAY_TEXT * 3)
    assert len(printable_line(long_line, limit=MAX_DISPLAY_TEXT)) == MAX_DISPLAY_TEXT
    assert len(printable_line(long_line)) == MAX_DISPLAY_TEXT * 3
    assert printable_line(long_line) == long_line


def test_a_lyric_block_keeps_the_only_formatting_a_lyric_sheet_has() -> None:
    """Line breaks and tabs survive; everything else unprintable is neutered.

    An LRC transcript stuffed into a USLT frame is one line per stamp, so a
    reflow here would destroy what `lyrics` is about to parse. A tab is layout
    and is not something a terminal acts on, so it stays as itself.
    """

    block = f"[00:01.00]{ESCAPED}\r\nsecond\tline  \r\n\r\n"
    # Trailing blank lines come off; the ones between stamps do not.
    assert printable_block(block) == f"[00:01.00]{NEUTERED}\nsecond\tline"
    assert printable_block("a\n\nb") == "a\n\nb"
    # No cap: this one is bounded in bytes by its caller, not in characters here.
    assert printable_block("y" * 5000) == "y" * 5000


def test_every_arm_that_shows_a_user_text_gives_the_same_answer() -> None:
    """The coherence claim, over the four callers that each used to have a copy.

    Two of them are the two source arms writing one manifest field -- a local
    file's tags and a download's reported metadata -- and their disagreement was
    the live one: the download arm did not fold at all, so a yt-dlp title carrying
    an ESC reached a terminal intact. The third is the lyric-tag cleaner, which
    dropped where the others folded. The fourth is a provider's own stderr, which
    is the least trusted string in the package.
    """

    assert source._clean_text(ESCAPED) == NEUTERED
    assert pipeline._display_text(ESCAPED) == NEUTERED
    assert source._clean_lyrics(ESCAPED) == NEUTERED
    assert lyrics._plain_text(ESCAPED) == NEUTERED
    assert public_error(ESCAPED) == NEUTERED


def test_both_title_arms_stop_at_one_ceiling() -> None:
    """A tag title and a reported title land in the same field, so they share a cap.

    Two spellings of 200 is one policy that can drift by half a release. Asserted
    against the constant rather than against 200, so the number can move without
    this becoming a second place that has to be edited.
    """

    long_title = "t" * (MAX_DISPLAY_TEXT + 50)
    assert len(source._clean_text(long_title)) == MAX_DISPLAY_TEXT
    assert len(pipeline._display_text(long_title)) == MAX_DISPLAY_TEXT
    # And the diagnosis ceiling is deliberately looser: an error has to be read.
    assert len(public_error("e" * 4000)) == 500


def test_the_fold_reaches_one_string_from_every_spelling_of_it() -> None:
    """NFKD, combining marks off, apostrophes respelled, casefold. In that order.

    The order is load-bearing at one point only, and it is the apostrophe: NFKD
    can introduce a combining mark, so the strip has to follow it, and casefold
    has to follow the strip or a folded character can recompose.
    """

    assert fold_accents("CAFÉ") == fold_accents("café") == "cafe"
    assert fold_accents("Straße") == "strasse"
    # Every apostrophe spelling reaches the same string -- including the two that
    # only become one under NFKD (U+1FEF, U+FF40) and the one that is destroyed by
    # it unless it is respelled first (U+00B4).
    for mark in "\u2018\u2019\u02bc\u00b4\u0060\u1fef\uff40'":
        assert fold_accents(f"Don{mark}t") == "don't", f"U+{ord(mark):04X}"
    assert fold_accents("") == ""


def test_the_fold_is_the_whole_of_what_the_two_folds_share() -> None:
    """Not "they look alike": the two really are this function plus a tail.

    `_whisper_worker.fold` is this plus a punctuation collapse and
    `alignment.comparison_tokens` is this plus digit and contraction expansion, so
    the filter and the aligner cannot come to disagree about case, an accent or an
    apostrophe without this test failing. They did disagree, about U+02BC, and the
    cost was a caption-farm hallucination shipped as a lyric under one spelling in
    six. The corpus is random rather than chosen so it cannot be tuned to pass.
    """

    random.seed(20260824)
    pool = [chr(code) for code in range(0x20, 0x2500)]
    pool += [*"\u2018\u2019\u02bc\u00b4\u0060'&", "\u0301", "\t", " ", "1985"]
    folded_differently = 0
    for _ in range(20000):
        value = "".join(random.choice(pool) for _ in range(random.randrange(0, 20)))
        collapsed = " ".join(worker._PUNCTUATION.sub(" ", fold_accents(value)).split())
        assert worker.fold(value) == collapsed, repr(value)
        assert alignment.comparison_tokens(value) == alignment.comparison_tokens(
            fold_accents(value)
        ), repr(value)
        if fold_accents(value) != value:
            folded_differently += 1
    # Not vacuous: most of the corpus really does need folding.
    assert folded_differently > 10000, folded_differently


def test_the_fold_never_leaves_a_combining_mark_behind() -> None:
    """The property the strip exists for, over every mark NFKD can produce.

    Scanned rather than sampled: a decomposition that left a mark standing would
    make one spelling of a word compare unequal to the other, which is exactly
    what alignment is trying to see through.
    """

    for code in range(0x0300, 0x0370):
        composed = unicodedata.normalize("NFC", "e" + chr(code))
        assert not any(unicodedata.combining(char) for char in fold_accents(composed))
