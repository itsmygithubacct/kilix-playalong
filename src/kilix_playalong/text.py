"""One answer each to "what may this text look like" and "what does it compare as".

Two rules live here, and they are here because during this release each of them
was written more than once, in modules built in parallel, and each set of copies
had drifted by the time they were read side by side:

* **Display.** Nothing a file, a tag or a third-party tool says about itself is
  shown as-is. ``printable_line`` and ``printable_block`` are the whole of that
  policy. Four of their five callers -- ``source._clean_text``,
  ``source._clean_lyrics``, ``pipeline._display_text`` and ``lyrics._plain_text``
  -- each carried a spelling of it, and the spellings disagreed: the two that
  clean a title differed on whether an unprintable was folded at all, and the one
  that cleans a lyric tag *dropped* them where the one that cleans a cue folded
  them, so a sheet carrying an ESC came out of intake as ``a[31mb`` through a
  container's tag and ``a [31mb`` through every other route into the same
  ``lyrics.json``. Two of them also spelled the 200 separately. The fifth caller
  is ``util.public_error``, which had the collapse and the cap and not the fold,
  on the least trusted string in the package: a provider subprocess's stderr.
* **Comparison.** ``fold_accents`` is what "the same words, spelled differently"
  means to this package. Its two callers -- ``alignment.comparison_tokens`` and
  ``_whisper_worker.fold`` -- each used to carry a copy of it *and a copy of the
  apostrophe class*, and that second copy is what made U+02BC a hole in the
  phantom-phrase filter: five spellings of "Don't forget to subscribe" were
  filtered and the sixth shipped as a lyric.

Both rules are policy rather than mechanism, which is why they are one module
and not two helpers dropped into ``util``: what counts as an apostrophe and what
an unprintable character becomes are decisions, and a decision restated in a
second file is a decision that will eventually be made twice.

Deliberately a leaf: ``re`` and ``unicodedata`` and nothing else, no package
import at all. ``alignment`` is otherwise a leaf itself and ``_whisper_worker``
runs in a locked-down subprocess, so a module either of them imports has to be
one that cannot pull anything heavy, anything with a filesystem side effect, or
anything that imports them back.
"""

from __future__ import annotations

import re
import unicodedata

#: Ceiling on one piece of text that a person reads on one line: a title, an
#: artist, or a container tag *name*. Longer than the longest real song title
#: anyone has shipped ("...Learned to Stop Worrying and Love the Bomb" is 96) and
#: short enough that ``source._MAX_IGNORED_TAGS`` of them still fit in a document
#: a browser page and a terminal line both have to render -- that pairing is what
#: bounds ``MediaMetadata.as_json``, and its own docstring works the ceiling out.
#: One number rather than two because the two source arms end in one field: a
#: local file's tags are cleaned in ``source.read_metadata`` and a download's
#: reported metadata in ``pipeline._display_text``, and both write
#: ``manifest["title"]``. ``util.public_error`` deliberately does not use this
#: one; a diagnosis has to survive being read, so it caps at 500.
MAX_DISPLAY_TEXT = 200

#: Every character this package will treat as an apostrophe: the two curly
#: quotes, MODIFIER LETTER APOSTROPHE, and the two accents a keyboard makes easy
#: to type by mistake. Spelled in escapes rather than literally, because four of
#: the five are indistinguishable from each other in most fonts and the point of
#: the class is which codepoints are in it.
#:
#: U+02BC is the one that has to be enumerated rather than inferred: it is
#: category Lm, a *letter*, so ``[^\w\s]`` leaves it alone and a fold written in
#: terms of punctuation keeps that spelling of "don't" as one token while turning
#: every other spelling into two. Not hypothetical -- it is the bug this class was
#: extracted after, and it decided whether a caption-farm hallucination was
#: filtered out of a transcript or shipped as a lyric. ASCII ``'`` is absent
#: because it is the target, not a candidate.
_APOSTROPHE = re.compile("[\u2018\u2019\u02bc\u00b4\u0060]")


def printable_line(value: str, *, limit: int | None = None) -> str:
    r"""One line, printable, optionally bounded: text that is safe to show.

    Unprintables become spaces rather than being dropped. Dropping is the
    tempting spelling and it is wrong: ``a\x1b[31mb`` closes up into ``a[31mb``,
    a shorter string that reads as something the source never said, while folding
    to a space leaves ``a [31mb``, which reads as the damage it is. Everything
    this cleans is bytes somebody else chose -- a container's tag values *and its
    tag names*, a title yt-dlp reports for a video it did not write, a caption
    track -- and all of it reaches a terminal that would act on an ESC, a
    ``.txt`` a user will ``cat``, and a manifest two surfaces render.

    ``limit=None`` is for a caller that bounds the text some other way and would
    otherwise truncate a legitimately long line; ``lyrics`` is that caller,
    bounding the document rather than any line in it.

    The ``isprintable`` guard is a C-level scan standing in front of a
    Python-level loop over every character of a value whose only bound may be
    megabytes -- ``runner.run_command``'s ceiling on ffprobe's output, say. The
    fold still runs, unchanged, on every value that needs it; what changes is
    that ordinary text stops paying for it.

    Every figure below was taken on one machine and another will differ: an
    i7-9850H running CPython 3.10, with unrelated work on the other cores
    throughout. Each is the minimum of repeated rounds run alternately against
    the same function with the guard removed -- the minimum because every sample
    is the real cost plus interference that is never negative, alternately so
    that a machine drifting under load drifts through both arms alike. The order
    of each number is the claim; its digits are not.

    On the tag path, a 255-tag document of 16 KiB prose-shaped values took
    ``source.read_metadata`` from ~320 ms to ~35 ms. The second number is the one
    that moves with the text, because skipping the fold leaves ``split`` and
    ``join`` as the cost: the same document written as one unbroken 16 KiB run
    with no whitespace in it went ~290 ms to ~9 ms instead. One short printable
    value went 1.6 us to 0.36 us at 15 characters and 5.2 us to 0.69 us at 60 --
    the longer the value, the more loop the guard skips.

    A value that really does carry control characters pays the scan for nothing
    and folds anyway. The scan stops at the first unprintable, so its cost is
    where that character sits rather than how long the value is: ~0.07 us with it
    at the front, ~0.4 us with it at the end of a 250-character value. That was
    measured on its own rather than differenced against the fold, which put it
    under this machine's noise. As a share of the fold it ran from a tenth of a
    percent to low double digits, and it is largest exactly where it is smallest
    in absolute terms -- on a two-character value the scan is one method call set
    against almost no loop at all. Either way it is a slice of a fold that value
    was always going to pay.
    """
    if not value.isprintable():
        value = "".join(character if character.isprintable() else " " for character in value)
    collapsed = " ".join(value.split())
    return collapsed if limit is None else collapsed[:limit]


def printable_block(value: str) -> str:
    r"""``printable_line``'s rule for text whose line breaks are its meaning.

    Line breaks are the only formatting a lyric sheet has, and an LRC transcript
    stuffed into a USLT frame is one line per stamp, so reflowing here would
    destroy the very thing ``lyrics`` parses. Everything else that is not
    printable becomes a space, exactly as in ``printable_line`` and for the same
    reason -- this used to drop them instead, which meant an embedded tag and a
    caption track carrying the same ESC came out of intake as two different
    strings.

    Tabs survive as tabs: a sheet that lines its chords up with them is using
    them as layout, and a tab is not something a terminal acts on.
    """
    normalised = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [
        "".join(
            character if character.isprintable() or character == "\t" else " " for character in line
        )
        for line in normalised.split("\n")
    ]
    return "\n".join(line.rstrip() for line in lines).strip("\n")


def fold_accents(value: str) -> str:
    """Case, accents and apostrophe spelling folded away. Never what is displayed.

    The shared half of the package's two text folds, and *exactly* their shared
    half: ``_whisper_worker.fold`` is this followed by collapsing punctuation to
    spaces, and ``alignment.comparison_tokens`` is this followed by expanding
    digits and contractions into the words a singer sings. Those two tails are
    different jobs and are deliberately not merged -- the worker counts a
    repetition loop's period in surface tokens, so expanding "1985 ain't" from
    three tokens to five would push a real loop past ``MAX_PHRASE_TOKENS`` and
    blind the only measured layer of its filter.

    The head is one job, though, and it did not stay one implementation: the two
    copies kept separate apostrophe vocabularies and one of them was missing
    U+02BC. Sharing it is what makes "the worker filters on the same basis the
    aligner matches on" a fact about the code rather than a claim about it.

    NFKD before stripping combining marks, so a precomposed "é" and an "e" with a
    combining acute reach the same string; ``casefold`` rather than ``lower``,
    because it is the one that folds "ß" to "ss" and the Turkish dotted forms.

    The class runs *after* the decomposition, because that is where most of its
    work is: U+0149 decomposes to U+02BC plus "n", and U+1FEF and U+FF40 both
    decompose to U+0060, so three spellings only become apostrophes once NFKD has
    finished with them. U+00B4 ACUTE ACCENT is the exception in the other
    direction and is why the line above it exists -- it decomposes to a space plus
    a combining acute, so by the time the strip is done it is an ordinary space and
    a substitution here can no longer tell it from one. Measured, it was the one
    member of the class that did nothing: that spelling of "Don't" tokenised in
    ``alignment.comparison_tokens`` as ("don", "t") while all four others reached
    the contraction table and gave ("do", "not").

    So it is handled ahead of the decomposition, alone, and guarded by a C-level
    membership test so that the ordinary string pays a scan and not a second regex.
    Alone is exact rather than lazy: it is the only one of the five whose NFKD
    output is a *different* character, which is what makes it the only one the
    substitution below cannot reach.
    """
    if "\u00b4" in value:
        value = value.replace("\u00b4", "'")
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return _APOSTROPHE.sub("'", stripped).casefold()
