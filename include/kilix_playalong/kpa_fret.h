#ifndef KILIX_PLAYALONG_KPA_FRET_H
#define KILIX_PLAYALONG_KPA_FRET_H

/*
 * The fretboard model: geometry, chord naming, hand position and note state.
 *
 * This module is pure.  It opens nothing, draws nothing, allocates nothing and
 * holds no state between calls; every function is a function of its arguments
 * alone.  That is the whole point of it existing as its own file.  The native
 * surface and the browser player draw the same guitar from the same artifacts
 * and share no code, so every number they both compute is a place they can
 * drift apart - and they already have, once, when the tab lane numbered
 * strings from the low E and the tooltip numbered them from the high e.
 *
 * The definition both surfaces implement is docs/FRETBOARD.md, and its
 * machine-checkable form is tests/fixtures/fretboard_vectors.json.  Where this
 * header states a number, the fixture is what decides whether the number is
 * right; tests/native/test_fret.c reads that file and asserts every vector in
 * it rather than restating any value here.
 *
 * Two things this module deliberately does not have:
 *
 *   - A string-number conversion.  The number shown to a player counts from
 *     the high e, and each surface already has exactly one function for it
 *     (player_string_number() in src/native/kpa_ui.c, stringNumber() in the
 *     browser).  A third copy is a third thing that can drift, so the
 *     fretboard uses the row order below and the surface's existing function
 *     for the printed number.  They agree by construction: under
 *     KPA_FRET_HIGH_E_TOP, kpa_fret_row() + 1 is that player number.
 *
 *   - Anything that reads `pitch` to place a note.  `pitch` is redundant with
 *     {string, fret} and is carried for naming only.  kpa_fret_point_of()
 *     takes a string and a fret and cannot see a pitch at all.
 */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "kilix_playalong/kpa_project.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------- geometry */

/*
 * Frets 0..24 are what the contract tabulates and what a guitar neck carries.
 * Nothing here refuses a higher fret - d(n) is defined for every n >= 0 - but
 * a surface drawing more neck than this is drawing an instrument the fixture
 * says nothing about.
 */
#define KPA_FRET_MAX_FRET 24

/*
 * d(n) = 1 - 2^(-n/12): the distance of fret n from the nut as a fraction of
 * scale length.  Geometric, never linear: each fret takes the same ratio of
 * what is left of the string, which is why frets crowd together going up the
 * neck.  Evenly spaced wires are the single thing that makes a drawn
 * fretboard look fake.
 *
 * Returns -1.0 for a negative fret.  Every real value is in [0, 1), so a
 * negative return is unambiguous.
 */
double kpa_fret_position(int32_t fret);

/*
 * cell(n) = (d(n-1) + d(n)) / 2, the centre of the space behind fret n - the
 * place a finger goes and the place an inlay goes, never the wire itself.
 * Defined for n >= 1; returns -1.0 otherwise, fret 0 included, because the
 * nut has no space behind it.
 */
double kpa_fret_cell_centre(int32_t fret);

/*
 * u(n, N) = d(n) / d(N): d(n) renormalised so that the highest displayed fret
 * lands at 1.0 and the drawing fills its box whatever length of neck a
 * surface shows.  N is a per-surface rendering choice, but for a given N both
 * surfaces must produce the same u.  Returns -1.0 when fret < 0 or N < 1.
 */
double kpa_fret_display_position(int32_t fret, int32_t highest_displayed_fret);

/* --------------------------------------------------------------- inlays */

/*
 * The enumerator's value is the number of dots, which is the only thing a
 * renderer needs from it.  Where the two dots of a double marker sit relative
 * to the centre line is a rendering choice and is not pinned.
 */
typedef enum kpa_fret_inlay {
    KPA_FRET_INLAY_NONE = 0,
    KPA_FRET_INLAY_SINGLE = 1,
    KPA_FRET_INLAY_DOUBLE = 2
} kpa_fret_inlay;

/*
 * Single at 3, 5, 7, 9, 15, 17, 19, 21; double at 12 and 24.  The marker for
 * fret n is centred at kpa_fret_cell_centre(n), not at kpa_fret_position(n):
 * putting the dot on the wire shifts every marker toward the bridge and makes
 * a correctly spaced neck read as mis-spaced.
 */
kpa_fret_inlay kpa_fret_inlay_at(int32_t fret);

/* -------------------------------------------------------------- strings */

/*
 * api_index 0 is the low E, ascending in pitch, exactly as guitar-tab.json
 * indexes it and as kpa_tab stores it.  It is never redefined anywhere.
 */

/*
 * width_ratio(s) = sqrt(gauge(s) / 0.010) over a light electric set.  The
 * square root is deliberate: the true 4.6:1 gauge ratio either makes the high
 * e sub-pixel or makes the low E a slab, and 2.14:1 keeps all six visibly
 * graded at the sizes both surfaces draw.  Returns -1.0 outside 0..5, because
 * the contract pins six gauges and guessing a seventh is worse than refusing.
 */
double kpa_fret_string_width_ratio(int32_t api_index);

/*
 * max(1, round(base_width * width_ratio(s))).  base_width is per-surface -
 * raster pixels inside terminal cells here, CSS pixels in the browser - so it
 * is not pinned; the ratio is.  Returns 0 for an out-of-range string or a
 * base_width below 1.
 */
int32_t kpa_fret_string_width(int32_t api_index, int32_t base_width);

/* ---------------------------------------------------- orientation, point */

/*
 * A user preference, and one that governs the tab lane and the fretboard
 * together.  Two pictures of one instrument disagreeing about string order in
 * the same window is a worse defect than either convention is a flaw, so a
 * surface that applies this to only one of the two is broken.
 *
 * KPA_FRET_HIGH_E_TOP is 0 so that a zeroed struct holds the default, which
 * is what both surfaces already draw in the tab lane.
 */
typedef enum kpa_fret_orientation {
    KPA_FRET_HIGH_E_TOP = 0,
    KPA_FRET_LOW_E_TOP = 1
} kpa_fret_orientation;

/* Largest string_count this module will lay out.  Six is what the artifacts
 * carry (KPA_STRING_COUNT); the extra room costs nothing and keeps a seven-
 * or eight-string tab from being refused by an arbitrary limit. */
#define KPA_FRET_MAX_STRINGS 12

/*
 * Display row, 0 at the top:
 *   high-e-top: row = string_count - 1 - api_index
 *   low-e-top:  row = api_index
 * Returns -1 when api_index is outside [0, string_count) or string_count is
 * outside [1, KPA_FRET_MAX_STRINGS].
 */
int32_t kpa_fret_row(int32_t api_index, int32_t string_count,
                     kpa_fret_orientation orientation);

/* A point in the normalised neck box: x 0 at the nut, y 0 at the top edge. */
typedef struct kpa_fret_point {
    double x;
    double y;
} kpa_fret_point;

/* A neck box in whatever units the caller draws in. */
typedef struct kpa_fret_rect {
    double x;
    double y;
    double width;
    double height;
} kpa_fret_rect;

/*
 * Where a {string, fret} lands in the normalised box:
 *
 *   x = 0                    when fret == 0
 *   x = cell(fret) / d(N)    when fret >= 1
 *   y = (row + 0.5) / string_count
 *
 * An open string is marked AT THE NUT, not in the space behind the first
 * fret: there is no finger, and the nut is the thing stopping the string.
 * The + 0.5 puts the string on its row's centre line rather than on the
 * boundary between two rows.
 *
 * x may exceed 1.0.  A fret above highest_displayed_fret is off the drawn end
 * of the neck, and saying so with a coordinate the caller can clip is more
 * use than refusing to answer.  Returns false - leaving *out untouched - only
 * for input that has no point at all: a null out, fret < 0, a string outside
 * [0, string_count), string_count outside [1, KPA_FRET_MAX_STRINGS], or
 * highest_displayed_fret < 1.
 */
bool kpa_fret_point_of(int32_t api_index, int32_t fret, int32_t string_count,
                       int32_t highest_displayed_fret,
                       kpa_fret_orientation orientation,
                       kpa_fret_point *out);

/*
 * The same point mapped into a rectangle: x = rect->x + u * rect->width, and
 * y likewise.  This is the call a renderer actually makes, and having it here
 * means neither surface writes the multiply itself.  Refuses what
 * kpa_fret_point_of refuses, plus a null or non-finite rect.
 */
bool kpa_fret_point_in_rect(int32_t api_index, int32_t fret,
                            int32_t string_count,
                            int32_t highest_displayed_fret,
                            kpa_fret_orientation orientation,
                            const kpa_fret_rect *rect, kpa_fret_point *out);

/* --------------------------------------------------------------- chords */

/* Six strings, so six simultaneous pitches at most. */
#define KPA_FRET_MAX_PITCHES 6u

/* Longest name the table can spell is "A#madd9/G#": ten bytes and a NUL.
 * A root that is also the bass takes no slash, so the ten-byte worst case is
 * always a slash chord. */
#define KPA_FRET_CHORD_NAME_CAPACITY 16u

/*
 * Values 1..15 are the quality ranks from the contract's template table, in
 * that order, so the enumerator doubles as the tie-break key.
 *
 * KPA_FRET_CHORD_POWER sits outside that range on purpose: a dyad is reached
 * from a branch that no template can reach, it never competes with a
 * template, and giving it a rank would imply a comparison that never happens.
 */
typedef enum kpa_fret_chord_quality {
    KPA_FRET_CHORD_NONE = 0,
    KPA_FRET_CHORD_MAJOR = 1,
    KPA_FRET_CHORD_MINOR = 2,
    KPA_FRET_CHORD_DOM7 = 3,
    KPA_FRET_CHORD_MIN7 = 4,
    KPA_FRET_CHORD_MAJ7 = 5,
    KPA_FRET_CHORD_SIX = 6,
    KPA_FRET_CHORD_MIN6 = 7,
    KPA_FRET_CHORD_SUS4 = 8,
    KPA_FRET_CHORD_SUS2 = 9,
    KPA_FRET_CHORD_ADD9 = 10,
    KPA_FRET_CHORD_MADD9 = 11,
    KPA_FRET_CHORD_DIM = 12,
    KPA_FRET_CHORD_AUG = 13,
    KPA_FRET_CHORD_M7B5 = 14,
    KPA_FRET_CHORD_DIM7 = 15,
    KPA_FRET_CHORD_POWER = 100
} kpa_fret_chord_quality;

typedef struct kpa_fret_chord {
    bool named;
    kpa_fret_chord_quality quality;
    int32_t root_pc;    /* 0..11, or -1 when unnamed */
    int32_t bass_pc;    /* 0..11 whenever there was at least one usable pitch */
    /* Always NUL terminated; empty when unnamed, so a caller that ignores the
     * return value prints nothing rather than stale bytes. */
    char name[KPA_FRET_CHORD_NAME_CAPACITY];
} kpa_fret_chord;

/*
 * Name the pitch class: C C# D D# E F F# G G# A A# B.  Sharps only, from the
 * same table tablature.py spells tuning labels with - correct enharmonics need
 * a key signature and nothing in this pipeline produces one, so being
 * uniformly sharp beats inventing flats for some chords and not others.
 * Returns NULL outside 0..11.
 */
const char *kpa_fret_pitch_class_name(int32_t pitch_class);

/* Suffix for a quality: "" for major, "m", "7", ... and "5" for a dyad.
 * Returns NULL for KPA_FRET_CHORD_NONE and for anything not in the table. */
const char *kpa_fret_chord_quality_suffix(kpa_fret_chord_quality quality);

/*
 * Name the chord sounding at one instant, from the MIDI pitches of one
 * event's positions in any order.  Returns true and fills *out when there is
 * a name; returns false and writes an unnamed *out otherwise.
 *
 * A wrong name is worse than no name, and this refuses far more than it
 * accepts by design: on the reference song it refuses 143 of the 438
 * multi-pitch-class events.  Two-note fragments that are not a fifth, a note
 * with its own octave, a triad with a melody note glued on, five distinct
 * pitch classes from an automatic transcription - all of those return
 * nothing, because none of them determines a chord.
 *
 * `pitches` may be NULL only when count is 0.  count above
 * KPA_FRET_MAX_PITCHES, or any pitch outside 0..127, is refused outright.
 */
bool kpa_fret_chord_identify(const int32_t *pitches, size_t count,
                             kpa_fret_chord *out);

/*
 * The same for one event of a loaded tab, reading the pitch the artifact
 * carries rather than deriving one.  Refuses an event index past the end, an
 * event whose position span runs off the position array, and an event with
 * more than KPA_FRET_MAX_PITCHES positions.
 */
bool kpa_fret_chord_of_event(const kpa_tab *tab, uint32_t event_index,
                             kpa_fret_chord *out);

/* -------------------------------------------------------- hand position */

/* The window is asymmetric on purpose: the marker has to arrive before the
 * player does, or it is telling them where they already were. */
#define KPA_FRET_WINDOW_BACK_S 0.5
#define KPA_FRET_WINDOW_FORWARD_S 1.5

/* A hand covers about five frets.  The box is that wide always, and is placed
 * by the median rather than stretched to reach an outlier. */
#define KPA_FRET_HAND_FRETS 5

/* Highest fret this counts.  Every fret in an artifact is a uint8_t, so no
 * real position is above this and the histogram inside costs 1 KiB. */
#define KPA_FRET_HAND_MAX_FRET 255

typedef struct kpa_fret_hand {
    int32_t low;    /* both inclusive, both >= 1: the box never covers the nut */
    int32_t high;
} kpa_fret_hand;

/*
 * Place the five-fret box over a set of frets - whatever a caller collected
 * from its own window.
 *
 *   discard every fret below 1, take the lower median c of what is left,
 *   box = [c - 2, c + 2], then slide (never stretch) it inside [1, max_fret].
 *
 * Open strings are excluded because fret 0 needs no hand and is not at a
 * position; including it would report first position for a passage played at
 * the twelfth fret over one open drone.  Frets above KPA_FRET_HAND_MAX_FRET
 * are discarded the same way - they are not a fret on any instrument this
 * reads.
 *
 * Returns false when nothing is left to place a hand on: that means hide the
 * marker, not move it.  `frets` may be NULL only when count is 0.  max_fret
 * must be at least 1.
 */
bool kpa_fret_hand_span(const int32_t *frets, size_t count, int32_t max_fret,
                        kpa_fret_hand *out);

/*
 * The same, over the events of a tab that overlap
 * [seconds - KPA_FRET_WINDOW_BACK_S, seconds + KPA_FRET_WINDOW_FORWARD_S).
 * An event is in the window when end > left && start < right, and the neck's
 * length comes from tab->max_fret.  The scan is linear for the same reason
 * kpa_fret_notes_at's is: a tab whose events are out of order must still give
 * the right answer.
 *
 * False here means the same thing it means above - hide the marker - and it
 * is also what a caller gets for a null argument, a non-finite time, a tab
 * with no frets at all (max_fret 0), or a tab whose event spans run off its
 * position array.  Nothing about those cases is a hand position either.
 */
bool kpa_fret_hand_at(const kpa_tab *tab, double seconds,
                      kpa_fret_hand *out);

/* ----------------------------------------------------------- note state */

typedef enum kpa_fret_note_state {
    KPA_FRET_NOTE_SOUNDING = 0,     /* start <= t < end */
    KPA_FRET_NOTE_APPROACHING = 1   /* t < start <= t + lead_in */
} kpa_fret_note_state;

typedef struct kpa_fret_note {
    uint32_t event_index;
    uint32_t position_index;   /* index into tab->positions */
    int32_t string_index;      /* api order: 0 is the low E */
    int32_t fret;
    int32_t pitch;             /* as the artifact carries it; never placed by */
    double start;
    double end;
    kpa_fret_note_state state;
    /* Sounding: (t - start) / (end - start), clamped to [0, 1] - how far
     * through its life the note is, which is what a decaying note or a
     * shrinking sustain bar is drawn from.  Approaching: 0.0. */
    double progress;
    /* Approaching: seconds until it starts, in (0, lead_in].  Sounding: <= 0,
     * being how long ago it started. */
    double time_to_start;
    /* True when this position names a string the tab does not have or a fret
     * above tab->max_fret.  It is reported rather than dropped: the query's
     * job is to say what is in the artifact, and geometry refuses to place
     * what cannot be placed. */
    bool out_of_range;
} kpa_fret_note;

typedef struct kpa_fret_note_report {
    uint32_t count;         /* entries written */
    uint32_t total;         /* entries that matched; above count when cut off */
    uint32_t sounding;      /* of the entries written */
    uint32_t approaching;   /* of the entries written */
    uint32_t out_of_range;  /* of the entries written */
    bool truncated;         /* total > count */
} kpa_fret_note_report;

/*
 * Every note sounding at `seconds`, plus every note starting within
 * `lead_in` seconds after it, in the order the artifact stores them.
 *
 * The span is half open - start <= t < end - so a position exactly on an end
 * belongs to what comes next rather than to two notes at once, which is the
 * same rule kpa_lyrics_cue_at uses for cues.
 *
 * The scan is linear over every event, not a binary search from
 * kpa_tab_first_after, because that search assumes events sorted by start and
 * this must give the right answer for a tab whose events are out of order.
 *
 * When more notes match than fit, the first `capacity` in artifact order are
 * written, report->truncated is set, and report->total says how many there
 * were.  Returns false - with a zeroed report - for a null tab or report, a
 * negative or non-finite lead_in, a non-finite time, a null note array with a
 * non-zero capacity, or a tab whose event spans run off its position array.
 */
bool kpa_fret_notes_at(const kpa_tab *tab, double seconds, double lead_in,
                       kpa_fret_note *notes, uint32_t capacity,
                       kpa_fret_note_report *report);

#ifdef __cplusplus
}
#endif

#endif
