/*
 * The fretboard model.
 *
 * Pure by construction: no static mutable state, no allocation, no I/O, no
 * rendering.  The only tables here are const, and every function's answer
 * depends on its arguments alone, which is what lets tests/native/test_fret.c
 * assert the whole of docs/FRETBOARD.md without opening a terminal or a song.
 *
 * Three implementation notes worth having in the file rather than the header.
 *
 * Chord candidates are compared on a three-part key written out in full, even
 * though the template table is already in rank order and the roots are already
 * walked in ascending order.  Leaning on either of those would make the result
 * depend on an iteration order rather than on a rule the browser can also
 * implement, and the contract's whole point is that the two agree.
 *
 * The hand-position median is taken from a fret histogram rather than by
 * sorting a copy of the caller's array.  A histogram needs no scratch bound on
 * how many notes a window may hold, cannot be defeated by an unsorted input,
 * and leaves the caller's array const.
 *
 * Nothing in this file reads `pitch` to decide where something is drawn.
 * kpa_fret_point_of takes a string and a fret and never sees a pitch;
 * chord naming takes pitches and never sees a string.
 */

#include "kilix_playalong/kpa_fret.h"

#include <math.h>
#include <stdint.h>
#include <string.h>

/* The longest name the spelling table can produce is "A#madd9/G#": two for
 * the root, five for the suffix, one slash, two for a bass that is not the
 * root, and a NUL. */
_Static_assert(KPA_FRET_CHORD_NAME_CAPACITY >= 11u,
               "chord name buffer must hold A#madd9/G#");

#define PITCH_CLASS_COUNT 12
#define MIDI_PITCH_MAX 127

/* ------------------------------------------------------------- geometry */

double kpa_fret_position(int32_t fret)
{
    if (fret < 0) return -1.0;
    if (fret == 0) return 0.0;
    return 1.0 - pow(2.0, -(double)fret / 12.0);
}

double kpa_fret_cell_centre(int32_t fret)
{
    if (fret < 1) return -1.0;
    return (kpa_fret_position(fret - 1) + kpa_fret_position(fret)) / 2.0;
}

double kpa_fret_display_position(int32_t fret, int32_t highest_displayed_fret)
{
    double span;

    if (fret < 0 || highest_displayed_fret < 1) return -1.0;
    span = kpa_fret_position(highest_displayed_fret);
    return kpa_fret_position(fret) / span;
}

/* --------------------------------------------------------------- inlays */

kpa_fret_inlay kpa_fret_inlay_at(int32_t fret)
{
    switch (fret) {
    case 3:
    case 5:
    case 7:
    case 9:
    case 15:
    case 17:
    case 19:
    case 21:
        return KPA_FRET_INLAY_SINGLE;
    case 12:
    case 24:
        return KPA_FRET_INLAY_DOUBLE;
    default:
        return KPA_FRET_INLAY_NONE;
    }
}

/* -------------------------------------------------------------- strings */

/* A light electric set, low E first, in inches. */
static const double kpa_fret_gauges[KPA_STRING_COUNT] = {
    0.046, 0.036, 0.026, 0.017, 0.013, 0.010
};

double kpa_fret_string_width_ratio(int32_t api_index)
{
    if (api_index < 0 || api_index >= (int32_t)KPA_STRING_COUNT) return -1.0;
    return sqrt(kpa_fret_gauges[api_index] / 0.010);
}

int32_t kpa_fret_string_width(int32_t api_index, int32_t base_width)
{
    double ratio;
    double scaled;

    if (base_width < 1) return 0;
    ratio = kpa_fret_string_width_ratio(api_index);
    if (ratio < 0.0) return 0;
    scaled = round((double)base_width * ratio);
    if (scaled < 1.0) return 1;
    return (int32_t)scaled;
}

/* ---------------------------------------------------- orientation, point */

int32_t kpa_fret_row(int32_t api_index, int32_t string_count,
                     kpa_fret_orientation orientation)
{
    if (string_count < 1 || string_count > KPA_FRET_MAX_STRINGS) return -1;
    if (api_index < 0 || api_index >= string_count) return -1;
    if (orientation == KPA_FRET_LOW_E_TOP) return api_index;
    return string_count - 1 - api_index;
}

bool kpa_fret_point_of(int32_t api_index, int32_t fret, int32_t string_count,
                       int32_t highest_displayed_fret,
                       kpa_fret_orientation orientation,
                       kpa_fret_point *out)
{
    int32_t row;
    double x;

    if (out == NULL || fret < 0 || highest_displayed_fret < 1) return false;
    row = kpa_fret_row(api_index, string_count, orientation);
    if (row < 0) return false;

    if (fret == 0) {
        /* At the nut.  There is no finger, and the nut is what stops the
         * string, so an open note does not sit in the first fret's space. */
        x = 0.0;
    } else {
        x = kpa_fret_cell_centre(fret) /
            kpa_fret_position(highest_displayed_fret);
    }
    out->x = x;
    out->y = ((double)row + 0.5) / (double)string_count;
    return true;
}

bool kpa_fret_point_in_rect(int32_t api_index, int32_t fret,
                            int32_t string_count,
                            int32_t highest_displayed_fret,
                            kpa_fret_orientation orientation,
                            const kpa_fret_rect *rect, kpa_fret_point *out)
{
    kpa_fret_point unit;

    if (rect == NULL || out == NULL) return false;
    if (!isfinite(rect->x) || !isfinite(rect->y) ||
        !isfinite(rect->width) || !isfinite(rect->height)) {
        return false;
    }
    if (!kpa_fret_point_of(api_index, fret, string_count,
                           highest_displayed_fret, orientation, &unit)) {
        return false;
    }
    out->x = rect->x + unit.x * rect->width;
    out->y = rect->y + unit.y * rect->height;
    return true;
}

/* --------------------------------------------------------------- chords */

static const char *const kpa_fret_pitch_names[PITCH_CLASS_COUNT] = {
    "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"
};

const char *kpa_fret_pitch_class_name(int32_t pitch_class)
{
    if (pitch_class < 0 || pitch_class >= PITCH_CLASS_COUNT) return NULL;
    return kpa_fret_pitch_names[pitch_class];
}

typedef struct kpa_fret_template {
    uint16_t intervals;   /* bit n set when interval n is in the chord */
    kpa_fret_chord_quality quality;
    const char *suffix;
} kpa_fret_template;

#define IV(n) ((uint16_t)(1u << (n)))

/*
 * In rank order, which is the order the enumerators are numbered in.  The
 * table tops out at four intervals, which is why five or more distinct pitch
 * classes never match: exact comparison against a four-bit mask cannot
 * succeed for a five-bit interval set.  That refusal is not a special case in
 * the code and does not need to be.
 */
static const kpa_fret_template kpa_fret_templates[] = {
    {(uint16_t)(IV(0) | IV(4) | IV(7)), KPA_FRET_CHORD_MAJOR, ""},
    {(uint16_t)(IV(0) | IV(3) | IV(7)), KPA_FRET_CHORD_MINOR, "m"},
    {(uint16_t)(IV(0) | IV(4) | IV(7) | IV(10)), KPA_FRET_CHORD_DOM7, "7"},
    {(uint16_t)(IV(0) | IV(3) | IV(7) | IV(10)), KPA_FRET_CHORD_MIN7, "m7"},
    {(uint16_t)(IV(0) | IV(4) | IV(7) | IV(11)), KPA_FRET_CHORD_MAJ7, "maj7"},
    {(uint16_t)(IV(0) | IV(4) | IV(7) | IV(9)), KPA_FRET_CHORD_SIX, "6"},
    {(uint16_t)(IV(0) | IV(3) | IV(7) | IV(9)), KPA_FRET_CHORD_MIN6, "m6"},
    {(uint16_t)(IV(0) | IV(5) | IV(7)), KPA_FRET_CHORD_SUS4, "sus4"},
    {(uint16_t)(IV(0) | IV(2) | IV(7)), KPA_FRET_CHORD_SUS2, "sus2"},
    {(uint16_t)(IV(0) | IV(2) | IV(4) | IV(7)), KPA_FRET_CHORD_ADD9, "add9"},
    {(uint16_t)(IV(0) | IV(2) | IV(3) | IV(7)), KPA_FRET_CHORD_MADD9, "madd9"},
    {(uint16_t)(IV(0) | IV(3) | IV(6)), KPA_FRET_CHORD_DIM, "dim"},
    {(uint16_t)(IV(0) | IV(4) | IV(8)), KPA_FRET_CHORD_AUG, "aug"},
    {(uint16_t)(IV(0) | IV(3) | IV(6) | IV(10)), KPA_FRET_CHORD_M7B5, "m7b5"},
    {(uint16_t)(IV(0) | IV(3) | IV(6) | IV(9)), KPA_FRET_CHORD_DIM7, "dim7"}
};

#define TEMPLATE_COUNT \
    (sizeof kpa_fret_templates / sizeof kpa_fret_templates[0])

const char *kpa_fret_chord_quality_suffix(kpa_fret_chord_quality quality)
{
    size_t index;

    if (quality == KPA_FRET_CHORD_POWER) return "5";
    for (index = 0u; index < TEMPLATE_COUNT; ++index) {
        if (kpa_fret_templates[index].quality == quality) {
            return kpa_fret_templates[index].suffix;
        }
    }
    return NULL;
}

static void chord_clear(kpa_fret_chord *out)
{
    out->named = false;
    out->quality = KPA_FRET_CHORD_NONE;
    out->root_pc = -1;
    out->bass_pc = -1;
    out->name[0] = '\0';
}

/* Bounded append; the static assert above is what makes truncation
 * impossible for every name this table can spell. */
static void chord_append(char *text, size_t capacity, size_t *used,
                         const char *piece)
{
    size_t index = 0u;

    while (piece[index] != '\0' && *used + 1u < capacity) {
        text[*used] = piece[index];
        ++*used;
        ++index;
    }
    text[*used] = '\0';
}

static void chord_spell(kpa_fret_chord *out, const char *suffix)
{
    size_t used = 0u;

    out->name[0] = '\0';
    chord_append(out->name, sizeof out->name, &used,
                 kpa_fret_pitch_names[out->root_pc]);
    chord_append(out->name, sizeof out->name, &used, suffix);
    if (out->root_pc != out->bass_pc) {
        chord_append(out->name, sizeof out->name, &used, "/");
        chord_append(out->name, sizeof out->name, &used,
                     kpa_fret_pitch_names[out->bass_pc]);
    }
}

/* The interval set of the pitch-class set seen from `root`: a rotation. */
static uint16_t chord_intervals(uint16_t classes, int32_t root)
{
    uint32_t wide = (uint32_t)classes;
    uint32_t rotated = (wide >> (uint32_t)root) |
                       (wide << (uint32_t)(PITCH_CLASS_COUNT - root));

    return (uint16_t)(rotated & 0x0fffu);
}

bool kpa_fret_chord_identify(const int32_t *pitches, size_t count,
                             kpa_fret_chord *out)
{
    uint16_t classes = 0u;
    int32_t lowest = 0;
    int32_t class_list[PITCH_CLASS_COUNT];
    int32_t class_count = 0;
    int32_t bass_pc;
    int32_t candidate;
    int32_t best_root = -1;
    int32_t best_on_bass = 2;
    int32_t best_rank = 0;
    const char *best_suffix = NULL;
    kpa_fret_chord_quality best_quality = KPA_FRET_CHORD_NONE;
    size_t index;

    if (out == NULL) return false;
    chord_clear(out);
    if (count == 0u || count > KPA_FRET_MAX_PITCHES) return false;
    if (pitches == NULL) return false;

    for (index = 0u; index < count; ++index) {
        int32_t pitch = pitches[index];

        if (pitch < 0 || pitch > MIDI_PITCH_MAX) return false;
        if (index == 0u || pitch < lowest) lowest = pitch;
        classes = (uint16_t)(classes | IV(pitch % PITCH_CLASS_COUNT));
    }

    /* The one place register matters: the bass is the lowest MIDI pitch,
     * taken before the set reduction and regardless of input order. */
    bass_pc = lowest % PITCH_CLASS_COUNT;
    out->bass_pc = bass_pc;

    for (candidate = 0; candidate < PITCH_CLASS_COUNT; ++candidate) {
        if ((classes & IV(candidate)) != 0u) {
            class_list[class_count] = candidate;
            ++class_count;
        }
    }
    /* One pitch class is a note or a note with its own octave, not a chord. */
    if (class_count < 2) return false;

    if (class_count == 2) {
        int32_t low = class_list[0];
        int32_t high = class_list[1];
        int32_t interval = (high - low + PITCH_CLASS_COUNT) %
                           PITCH_CLASS_COUNT;

        /* A fifth, or a fourth which is a fifth upside down.  Every other
         * dyad is refused: a major third alone belongs to C, Cmaj7, Am, Am7
         * and Fmaj7 equally, and naming it is a guess wearing a label. */
        if (interval == 7) {
            out->root_pc = low;
        } else if (interval == 5) {
            out->root_pc = high;
        } else {
            return false;
        }
        out->quality = KPA_FRET_CHORD_POWER;
        out->named = true;
        chord_spell(out, "5");
        return true;
    }

    for (index = 0u; index < (size_t)class_count; ++index) {
        int32_t root = class_list[index];
        uint16_t intervals = chord_intervals(classes, root);
        size_t which;

        for (which = 0u; which < TEMPLATE_COUNT; ++which) {
            int32_t on_bass;
            int32_t rank;

            /* Equality, not containment.  A triad with a neighbouring note
             * glued on matches nothing at any root, which is right: it is a
             * chord with a passing note in it, not a chord with a name. */
            if (intervals != kpa_fret_templates[which].intervals) continue;

            on_bass = (root == bass_pc) ? 0 : 1;
            rank = (int32_t)kpa_fret_templates[which].quality;

            /* The contract's total order, written out: bass first, then
             * quality rank, then root.  The third element has never decided a
             * winner - only aug and dim7 match at several roots, and in both
             * the bass is one of them - but it is here so the order is total
             * and nothing depends on a language's sort being stable. */
            if (best_root >= 0) {
                if (on_bass > best_on_bass) continue;
                if (on_bass == best_on_bass) {
                    if (rank > best_rank) continue;
                    if (rank == best_rank && root >= best_root) continue;
                }
            }
            best_root = root;
            best_on_bass = on_bass;
            best_rank = rank;
            best_quality = kpa_fret_templates[which].quality;
            best_suffix = kpa_fret_templates[which].suffix;
        }
    }

    if (best_root < 0) return false;
    out->root_pc = best_root;
    out->quality = best_quality;
    out->named = true;
    chord_spell(out, best_suffix);
    return true;
}

/* True when the event's positions lie inside the tab's position array. */
static bool tab_event_is_sound(const kpa_tab *tab, uint32_t event_index)
{
    const kpa_tab_event *event;

    if (tab == NULL || tab->events == NULL) return false;
    if (event_index >= tab->event_count) return false;
    event = &tab->events[event_index];
    if (event->position_count == 0u) return true;
    if (tab->positions == NULL) return false;
    return (uint64_t)event->first_position + (uint64_t)event->position_count <=
           (uint64_t)tab->position_count;
}

bool kpa_fret_chord_of_event(const kpa_tab *tab, uint32_t event_index,
                             kpa_fret_chord *out)
{
    int32_t pitches[KPA_FRET_MAX_PITCHES];
    const kpa_tab_event *event;
    uint32_t index;

    if (out == NULL) return false;
    chord_clear(out);
    if (!tab_event_is_sound(tab, event_index)) return false;
    event = &tab->events[event_index];
    if (event->position_count > KPA_FRET_MAX_PITCHES) return false;

    for (index = 0u; index < event->position_count; ++index) {
        pitches[index] =
            (int32_t)tab->positions[event->first_position + index].pitch;
    }
    return kpa_fret_chord_identify(pitches, (size_t)event->position_count,
                                   out);
}

/* -------------------------------------------------------- hand position */

#define HAND_HISTOGRAM_SIZE (KPA_FRET_HAND_MAX_FRET + 1)

static bool hand_from_histogram(const uint32_t *counts, uint32_t total,
                                int32_t max_fret, kpa_fret_hand *out)
{
    uint32_t wanted;
    uint32_t seen = 0u;
    int32_t centre = -1;
    int32_t low;
    int32_t high;
    int32_t fret;

    if (total == 0u) return false;

    /* Lower median: an integer fret without a rounding rule for both
     * languages to disagree about. */
    wanted = (total - 1u) / 2u;
    for (fret = 1; fret < HAND_HISTOGRAM_SIZE; ++fret) {
        seen += counts[fret];
        if (seen > wanted) {
            centre = fret;
            break;
        }
    }
    if (centre < 0) return false;

    low = centre - (KPA_FRET_HAND_FRETS / 2);
    high = centre + (KPA_FRET_HAND_FRETS / 2);
    /* Slide, never stretch.  A hand covers five frets; a window whose notes
     * span nineteen is a reach or a bad transcription, not a hand there. */
    if (low < 1) {
        high += 1 - low;
        low = 1;
    }
    if (high > max_fret) {
        low -= high - max_fret;
        high = max_fret;
        if (low < 1) low = 1;
    }
    out->low = low;
    out->high = high;
    return true;
}

bool kpa_fret_hand_span(const int32_t *frets, size_t count, int32_t max_fret,
                        kpa_fret_hand *out)
{
    uint32_t counts[HAND_HISTOGRAM_SIZE];
    uint32_t total = 0u;
    size_t index;

    if (out == NULL || max_fret < 1) return false;
    if (count > 0u && frets == NULL) return false;
    memset(counts, 0, sizeof counts);

    for (index = 0u; index < count; ++index) {
        int32_t fret = frets[index];

        /* Fret 0 needs no hand and is not at a position; it is the nut doing
         * the work.  Counting it would report first position for a passage
         * played at the twelfth fret over one open drone. */
        if (fret < 1 || fret > KPA_FRET_HAND_MAX_FRET) continue;
        ++counts[fret];
        ++total;
    }
    return hand_from_histogram(counts, total, max_fret, out);
}

bool kpa_fret_hand_at(const kpa_tab *tab, double seconds, kpa_fret_hand *out)
{
    uint32_t counts[HAND_HISTOGRAM_SIZE];
    uint32_t total = 0u;
    double left;
    double right;
    uint32_t event_index;

    if (out == NULL || tab == NULL) return false;
    if (!isfinite(seconds)) return false;
    if (tab->max_fret < 1u) return false;
    memset(counts, 0, sizeof counts);

    left = seconds - KPA_FRET_WINDOW_BACK_S;
    right = seconds + KPA_FRET_WINDOW_FORWARD_S;

    for (event_index = 0u; event_index < tab->event_count; ++event_index) {
        const kpa_tab_event *event;
        uint32_t position;

        if (!tab_event_is_sound(tab, event_index)) return false;
        event = &tab->events[event_index];
        if (!(event->end > left && event->start < right)) continue;

        for (position = 0u; position < event->position_count; ++position) {
            int32_t fret =
                (int32_t)tab->positions[event->first_position + position].fret;

            if (fret < 1 || fret > KPA_FRET_HAND_MAX_FRET) continue;
            ++counts[fret];
            ++total;
        }
    }
    return hand_from_histogram(counts, total,
                               (int32_t)tab->max_fret, out);
}

/* ----------------------------------------------------------- note state */

static void note_report_clear(kpa_fret_note_report *report)
{
    report->count = 0u;
    report->total = 0u;
    report->sounding = 0u;
    report->approaching = 0u;
    report->out_of_range = 0u;
    report->truncated = false;
}

bool kpa_fret_notes_at(const kpa_tab *tab, double seconds, double lead_in,
                       kpa_fret_note *notes, uint32_t capacity,
                       kpa_fret_note_report *report)
{
    uint32_t event_index;

    if (report == NULL) return false;
    note_report_clear(report);
    if (tab == NULL) return false;
    if (notes == NULL && capacity > 0u) return false;
    if (!isfinite(seconds) || !isfinite(lead_in) || lead_in < 0.0) {
        return false;
    }

    for (event_index = 0u; event_index < tab->event_count; ++event_index) {
        const kpa_tab_event *event;
        kpa_fret_note_state state;
        uint32_t position;

        if (!tab_event_is_sound(tab, event_index)) {
            note_report_clear(report);
            return false;
        }
        event = &tab->events[event_index];

        /* Half open, the same rule kpa_lyrics_cue_at uses: a time exactly on
         * an end belongs to what comes next, not to two notes at once. */
        if (event->start <= seconds && seconds < event->end) {
            state = KPA_FRET_NOTE_SOUNDING;
        } else if (event->start > seconds &&
                   event->start <= seconds + lead_in) {
            state = KPA_FRET_NOTE_APPROACHING;
        } else {
            continue;
        }

        for (position = 0u; position < event->position_count; ++position) {
            uint32_t source = event->first_position + position;
            const kpa_tab_position *point = &tab->positions[source];
            kpa_fret_note *note;
            bool bad_string;
            bool bad_fret;

            ++report->total;
            if (report->count >= capacity) {
                report->truncated = true;
                continue;
            }
            note = &notes[report->count];
            note->event_index = event_index;
            note->position_index = source;
            note->string_index = (int32_t)point->string_index;
            note->fret = (int32_t)point->fret;
            note->pitch = (int32_t)point->pitch;
            note->start = event->start;
            note->end = event->end;
            note->state = state;
            note->time_to_start = event->start - seconds;
            if (state == KPA_FRET_NOTE_SOUNDING && event->end > event->start) {
                double life = (seconds - event->start) /
                              (event->end - event->start);

                if (life < 0.0) life = 0.0;
                if (life > 1.0) life = 1.0;
                note->progress = life;
            } else {
                note->progress = 0.0;
            }

            bad_string = (uint32_t)point->string_index >= tab->string_count;
            bad_fret = (uint32_t)point->fret > tab->max_fret;
            note->out_of_range = bad_string || bad_fret;

            ++report->count;
            if (state == KPA_FRET_NOTE_SOUNDING) {
                ++report->sounding;
            } else {
                ++report->approaching;
            }
            if (note->out_of_range) ++report->out_of_range;
        }
    }
    return true;
}
