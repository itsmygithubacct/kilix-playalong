/*
 * The native Kilix surface for kilix-playalong.
 *
 * Six decisions shape this file.
 *
 * Composition is a pure function of the view model.  kpa_ui_compose reads
 * nothing but its arguments and this file's constant tables - no clock, no
 * audio session, no mutable state - so the headless test can assert that the
 * same model draws the same bytes twice and that a model drawn after another
 * leaves nothing of it behind.  Everything that has to look at the world
 * lives in kpa_ui_run.
 *
 * The rows the cell overlay owns are protected by a clip rectangle rather
 * than by discipline.  kpa_ui_compose sets the canvas clip to the band
 * between the reserved pixel bands before it draws anything, so no widget can
 * reach a lyric row even if it miscalculates its own rectangle, and those
 * rows stay at 0x00000000 - fully transparent, which is what lets the
 * terminal's own background show behind the foreground cells that carry the
 * song text.  kpa_ui_cell_layout_get claims only whole cell rows that lie
 * inside those bands, so the two cannot disagree.
 *
 * The tab lane is drawn high string at the top.  kpa_tab indexes strings from
 * the low E at 0, players count the high e as string 1, and the browser
 * surface shipped the identity mapping between the two before it was fixed.
 * The display needs two conversions and each exists exactly once:
 * string_display_row() for the row a string is drawn on, used by the neck,
 * the ramp, the lane's names and notes and the cell-only tab alike, and
 * player_string_number() for the number a player is told.  The string-name
 * gutter is drawn at a constant x outside the lane's clip, so it cannot
 * translate with the notes the way the browser's did.
 *
 * The fretboard is drawn to the physics rather than to a grid, and the
 * physics is not this file's.  kpa_fret.h holds the geometry, the chord
 * table, the hand position and the note state, and the browser player draws
 * the same guitar from the same definitions; nothing here recomputes any of
 * them.  Fret n sits at 1 - 2^(-n/12) of the scale length, so the frets
 * crowd together as they climb - drawing them evenly spaced is the single
 * thing that makes a fretboard look fake, and tests/native/test_ui.c asserts
 * that every fret cell is narrower than the one before it.  Two functions,
 * neck_wire_x() and neck_finger_x(), are the only things that turn a fret
 * into an x on this screen, so the neck, the approach ramp, the position box
 * and the fret ruler cannot drift apart, and one reflection in neck_mirror()
 * is the whole of left-handed.
 *
 * Audio state and display state never touch.  kpa_ui_internal_apply_key is
 * the whole key table and is a pure function of the model; the event loop
 * turns the difference it made into audio calls.  That is what makes "l hides
 * lyrics and does not mute vocals" a property something can assert.
 *
 * Nothing here builds a project path.  Stems are opened through
 * kpa_project_open_artifact, which resolves the manifest's relative path
 * beneath the held project-directory descriptor; this file never joins a root
 * to a name and never opens a project byte by any other route.  The one
 * absolute path in the file is /dev/tty, and it is opened for output only.
 */

#include "kilix_playalong/kpa_ui.h"

#include "kilix_playalong/kpa_cells.h"
#include "kilix_playalong/kpa_fret.h"

#include "kitty_terminal_session.h"
#include "soft_raster.h"

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <math.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

/* ------------------------------------------------------------- palette */

#define KPA_UI_BACKDROP 0x00101014u
#define KPA_UI_PANEL 0x00181820u
#define KPA_UI_RAISED 0x00242430u
#define KPA_UI_FAINT 0x00383848u
#define KPA_UI_TEXT 0x00C8C8D4u
#define KPA_UI_DIM 0x00707884u
#define KPA_UI_ACCENT 0x0050B0FFu
#define KPA_UI_GOOD 0x0060FF80u
#define KPA_UI_WARN 0x00FF9040u
#define KPA_UI_ALERT 0x00FF6060u
#define KPA_UI_LOOP 0x00B070FFu
/*
 * The hand box gets its own ink rather than borrowing the loop's.  Two
 * different meanings sharing one colour on one screen is how a player comes
 * to read "where your hand goes" as "the range you are looping", and the
 * value is the browser's own #2eaa75 so that the two surfaces do not draw
 * the same instrument in two different tempers.
 */
#define KPA_UI_HAND 0x002EAA75u

/* ------------------------------------------------------------ geometry */

#define KPA_UI_MARGIN 8
#define KPA_UI_LINE 18            /* one line of the 8x16 font plus lead */
#define KPA_UI_ROW 20             /* one mixer or library row, pixels */
#define KPA_UI_GUTTER 56          /* pinned string-name gutter, pixels */
#define KPA_UI_LANE_PPS 90.0      /* tab lane pixels per second */
#define KPA_UI_LANE_MIN 48        /* below this the lane is not drawn */
#define KPA_UI_LANE_EVENTS 4096u  /* bound on one frame's lane walk */
#define KPA_UI_LINE_CAPACITY 640u
#define KPA_UI_MAX_LYRIC_ROWS 6

/* -------------------------------------------------------- the fretboard */

/* Board wood, dark and warm, so the instrument reads as an instrument
 * against the cool backdrop.  Nut bone, fret nickel, inlay pearl. */
#define KPA_FB_WOOD 0x00201814u
#define KPA_FB_BONE 0x00E8E0D0u
#define KPA_FB_NICKEL 0x00808890u
#define KPA_FB_PEARL 0x00C8C0B0u
#define KPA_FB_WOUND 0x00B0B4BCu
#define KPA_FB_PLAIN 0x00D8DCE4u

/*
 * How long a note is drawn for at the least, in seconds, measured from its
 * onset.  A string does not fall silent the instant a transcription's event
 * ends, and the shortest event in the audited song is 0.094 s - about two
 * frames at the event loop's 40 ms poll, which is not long enough to see.
 * A note whose written life is shorter than this decays over the remainder
 * instead of vanishing.
 */
#define KPA_FB_MIN_LIFE 0.22f
#define KPA_FB_RAMP_DEFAULT 2.0f
#define KPA_FB_RAMP_MAX 4.0f
/*
 * Notes one frame may hold.  The load was measured on the audited song: a
 * 2.0 s look-ahead carries a median of 17 and a maximum of 32 sounding-plus-
 * approaching notes, and the longest window offered here, 4.0 s, about 60.
 * 96 is slack rather than a real limit, and kpa_fret_notes_at reports when
 * it had to truncate, so an overrun is visible instead of silent.
 */
#define KPA_FB_MAX_NOTES 96u
/* A chord label has to hold this long before it replaces the one on screen.
 * Measured: 0.37 s between changes unlatched, 1.23 s with this rule.  The
 * lag it costs is 0.25 s, which is one median-length event (0.348 s). */
#define KPA_FB_CHORD_HOLD 0.25
/* Fretboard block heights, in pixels.  All chosen. */
#define KPA_FB_CALLOUT_H 18
#define KPA_FB_RULER_H 14
#define KPA_FB_BLOCK_MIN 96       /* below this no fretboard is drawn */
#define KPA_FB_BLOCK_FULL 150     /* below this the ruler, then the ramp, go */
#define KPA_FB_BLOCK_MAX 260
#define KPA_FB_PITCH_MIN 13       /* six strings still readable: 78 px */
#define KPA_FB_NUT_MARGIN 14.0f   /* room for the nut bar and the open marks */
#define KPA_FB_RAIL 56            /* the left rail: chord box and string names */

/* --------------------------------------------------------------- input */

#define KPA_UI_SEEK_STEP 5.0
#define KPA_UI_SEEK_STEP_LARGE 30.0
#define KPA_UI_GAIN_STEP 0.05f
#define KPA_UI_GAIN_MAX 2.0f
#define KPA_UI_RATE_STEP 0.05
#define KPA_UI_RATE_MIN 0.50
#define KPA_UI_RATE_MAX 1.50
/* Frames the display holds a seek target while the decoders catch up. */
#define KPA_UI_SEEK_HOLD_FRAMES 12

/*
 * What the loop must do about a key, returned by the seam below.  The values
 * are mirrored by tests/native/test_ui.c, which asserts them.
 */
#define KPA_UI_KEY_HANDLED 0
#define KPA_UI_KEY_QUIT 1
#define KPA_UI_KEY_OPEN 2
#define KPA_UI_KEY_CLOSE 3

/*
 * Key handling, exported for tests/native/test_ui.c rather than declared in
 * the frozen kpa_ui.h.  The event loop must not be the only thing that knows
 * the key table: "l hides lyrics without muting vocals" is a property of this
 * function, and a test that drove it through a terminal would be a test of
 * the terminal.  It is a pure function of the model - no audio session, no
 * descriptors, no clock - and kpa_ui_run turns the difference it made into
 * audio calls by comparing the model before and after.
 */
int kpa_ui_internal_apply_key(kpa_ui_model *model, uint32_t key,
                              uint32_t modifiers);

/*
 * The caption the lyrics band carries about where its times came from,
 * exported for tests/native/test_ui.c on the same terms as the key table
 * above: it is a pure function of the lyrics, and asserting what a player is
 * told through a terminal would be asserting the terminal.  Writes the line
 * and the ink it is drawn in and returns true, or writes an empty line and
 * returns false when the document made no claim.
 */
bool kpa_ui_internal_lyrics_caption(const kpa_lyrics *lyrics, char *out,
                                    size_t size, uint32_t *rgb);

/*
 * The two pieces of the fretboard this surface owns, exported for
 * tests/native/test_ui.c on the same terms as the pair above.  Neither is
 * the fretboard model: kpa_fret.h holds the geometry, the chord table, the
 * hand position and the note state, and both surfaces draw from it.  What is
 * left here is what only a drawn neck has to decide.
 *
 * kpa_ui_internal_neck_x turns a fret into the two x coordinates this screen
 * draws at - the wire, and the finger behind it - with handedness applied.
 * Asserting the neck really is a neck (every fret cell narrower than the one
 * before it) belongs on this rather than on a canvas read back pixel by
 * pixel.
 *
 * kpa_ui_internal_chord_label is what the callout says.  The name comes from
 * kpa_fret_chord_identify, which refuses far more than it accepts; the only
 * thing added here is what to show when there is no name, which is a display
 * decision and not a naming one.
 */
bool kpa_ui_internal_neck_x(uint32_t max_fret, float x, float width,
                            bool left_handed, uint32_t fret,
                            float *out_wire, float *out_finger);
void kpa_ui_internal_chord_label(const int32_t *pitches, uint32_t count,
                                 char *out, size_t size, bool *out_named);

/* --------------------------------------------------------- small maths */

static double clamp_double(double value, double low, double high)
{
    /* NaN fails both comparisons and falls through to `low`, which is the
     * answer a position or a loop point wants: somewhere on the timeline. */
    if (value >= high) return high;
    if (value > low) return value;
    return low;
}

static float clamp_float(float value, float low, float high)
{
    if (value >= high) return high;
    if (value > low) return value;
    return low;
}

/* Duration as m:ss, or h:mm:ss past an hour.  Never a negative clock. */
static void format_clock(double seconds, char *out, size_t size)
{
    long total;
    long hours;

    if (!(seconds > 0.0)) seconds = 0.0;   /* also catches NaN */
    if (seconds > 359999.0) seconds = 359999.0;
    total = (long)seconds;
    hours = total / 3600L;
    if (hours > 0L) {
        (void)snprintf(out, size, "%ld:%02ld:%02ld", hours,
                       (total / 60L) % 60L, total % 60L);
    } else {
        (void)snprintf(out, size, "%ld:%02ld", total / 60L, total % 60L);
    }
}

/*
 * The one place the API's string order becomes the player's.  kpa_tab indexes
 * from the low E at 0; a guitarist calls the high e string 1.  Everything the
 * player is shown goes through here, and the display row order below is the
 * same inversion applied to the y axis.
 */
static uint32_t player_string_number(uint32_t api_index, uint32_t string_count)
{
    if (string_count == 0u || api_index >= string_count) return 0u;
    return string_count - api_index;
}

/*
 * The display row an API string index lands on, for every widget that draws
 * strings: the neck, the approach ramp, the tab lane's names and notes, and
 * the cell-only tab.  Two copies of this arithmetic are two things that can
 * drift - which is how the browser ended up drawing its labels in one order
 * and its notes in another - so there is one.
 *
 * Both orders are real.  Tablature puts the high e on top and that is what
 * this surface has always drawn; a player looking down at their own
 * instrument sees the low E nearest them, i.e. on top.  Rather than pick a
 * side, low_on_top is a preference (the `o` key) and it moves the neck and
 * the lane together, because two pictures of one instrument disagreeing
 * about string order on one screen is worse than either convention.  The
 * default is false - the tablature order, unchanged.
 *
 * The order itself is kpa_fret_row's, so the neck the browser draws and the
 * neck this draws cannot disagree about it.  This wrapper exists for the
 * unsigned types the surface counts rows in and for nothing else.
 */
static uint32_t string_display_row(uint32_t api_index, uint32_t string_count,
                                   bool low_on_top)
{
    const int32_t row =
        kpa_fret_row((int32_t)api_index, (int32_t)string_count,
                     low_on_top ? KPA_FRET_LOW_E_TOP : KPA_FRET_HIGH_E_TOP);

    if (row < 0) return 0u;
    return (uint32_t)row;
}

/* ------------------------------------------------ the fretboard model */

/*
 * The geometry, the chord table, the hand position and the note state all
 * live in kpa_fret.h and are shared with the browser player, because every
 * number the two surfaces both compute is a place they can drift apart -
 * and they already did once, over which end of the neck string 1 is.
 * Nothing below recomputes any of it.  What is here is what only a drawn
 * neck has to decide: where the box is, which way round it faces, how a
 * struck string looks while it rings, and what to say when the shared
 * namer declines to name a chord.
 */

/*
 * A drawn neck.  `board` is the fret span in screen pixels with the nut at
 * its left edge, always, and `wood` is the wider rectangle the board is
 * painted on - wider because the nut needs a margin to sit in.  left_handed
 * reflects every x about the WOOD's centre on the way out, so that margin
 * moves to the other side with the nut; nut_x and far_x are the reflected
 * ends, for the widgets that need to know which way the strings run.
 */
typedef struct kpa_fb_geom {
    kpa_fret_rect board;      /* nut at board.x, always, before mirroring */
    float wood_x0;            /* the drawn wood, in screen order */
    float wood_x1;
    float mirror_sum;         /* wood_x0 + wood_x1: the left-handed flip */
    float nut_x;              /* fret 0, after mirroring */
    float far_x;              /* the last drawn fret, after mirroring */
    float string_pitch;
    int y0;
    int y1;
    uint32_t strings;
    uint32_t max_fret;
    bool low_on_top;
    bool left_handed;
} kpa_fb_geom;

/*
 * A left-handed neck is the same neck seen from the other side, so it is one
 * reflection on the way out rather than a second set of geometry.  The axis
 * is the wood's centre and not the board's, so the margin the nut sits in
 * moves with the nut.
 */
static float neck_mirror(const kpa_fb_geom *geometry, float x)
{
    if (!geometry->left_handed) return x;
    return geometry->mirror_sum - x;
}

/* The wire itself: d(n) / d(N) across the board. */
static float neck_wire_x(const kpa_fb_geom *geometry, uint32_t fret)
{
    double unit;

    if (fret > geometry->max_fret) fret = geometry->max_fret;
    unit = kpa_fret_display_position((int32_t)fret,
                                     (int32_t)geometry->max_fret);
    if (unit < 0.0) unit = 0.0;
    return neck_mirror(geometry,
                       (float)(geometry->board.x +
                               unit * geometry->board.width));
}

/*
 * Where the finger goes, which is not where the wire is: a player presses
 * into the space behind the fret that names the note, so the mark sits at
 * the centre of that space.  Fret 0 is an open string and is marked at the
 * nut - there is no finger, and the nut is the thing stopping the string.
 * kpa_fret_point_of makes both of those decisions; this only places them.
 */
static float neck_finger_x(const kpa_fb_geom *geometry, uint32_t fret)
{
    kpa_fret_point point;

    if (fret > geometry->max_fret) fret = geometry->max_fret;
    if (!kpa_fret_point_in_rect(0, (int32_t)fret, (int32_t)geometry->strings,
                                (int32_t)geometry->max_fret,
                                KPA_FRET_HIGH_E_TOP, &geometry->board,
                                &point)) {
        return geometry->nut_x;
    }
    return neck_mirror(geometry, (float)point.x);
}

static float neck_string_y(const kpa_fb_geom *geometry, uint32_t api_index)
{
    const uint32_t row = string_display_row(api_index, geometry->strings,
                                            geometry->low_on_top);

    return (float)geometry->y0 + ((float)row + 0.5f) * geometry->string_pitch;
}

bool kpa_ui_internal_neck_x(uint32_t max_fret, float x, float width,
                            bool left_handed, uint32_t fret,
                            float *out_wire, float *out_finger)
{
    kpa_fb_geom geometry;

    if (max_fret < 1u || max_fret > (uint32_t)KPA_FRET_MAX_FRET) return false;
    if (!(width > 0.0f) || fret > max_fret) return false;
    (void)memset(&geometry, 0, sizeof geometry);
    geometry.board.x = (double)x;
    geometry.board.y = 0.0;
    geometry.board.width = (double)width;
    geometry.board.height = 1.0;
    geometry.strings = KPA_STRING_COUNT;
    geometry.max_fret = max_fret;
    geometry.left_handed = left_handed;
    geometry.wood_x0 = x;
    geometry.wood_x1 = x + width;
    geometry.mirror_sum = geometry.wood_x0 + geometry.wood_x1;
    geometry.nut_x = neck_mirror(&geometry, x);
    if (out_wire != NULL) *out_wire = neck_wire_x(&geometry, fret);
    if (out_finger != NULL) *out_finger = neck_finger_x(&geometry, fret);
    return true;
}

/*
 * String ink, warm at the low E and cool at the high e.  Colour here is
 * redundant: every string already has its own row on both the neck and the
 * lane, so nothing at all is carried by hue alone and a reader who cannot
 * separate these six loses nothing.  That is a description of this layout,
 * not a claim that the palette was checked for contrast between the pairs.
 */
static const uint32_t kpa_string_rgb[KPA_STRING_COUNT] = {
    0x00FF7A5Cu, 0x00FFB84Du, 0x00E8E24Du,
    0x0060E88Au, 0x0050B0FFu, 0x00C08CFFu
};

/* Thickness, from the shared gauge ratios: the low E is 2.14 times the high
 * e, which is the square root of the true gauge ratio and is what keeps all
 * six visibly graded instead of making the high e sub-pixel. */
static float fb_string_width(uint32_t api_index, float base)
{
    const double ratio = kpa_fret_string_width_ratio((int32_t)api_index);

    if (ratio < 0.0) return base;
    return base * (float)ratio;
}

static uint32_t fb_string_count(const kpa_tab *tab)
{
    if (tab != NULL && tab->string_count > 0u &&
        tab->string_count <= KPA_STRING_COUNT) {
        return tab->string_count;
    }
    return KPA_STRING_COUNT;
}

/*
 * The board's last fret.  The audited transcription declares 20 and uses
 * 0..19; the clamp keeps the neck inside the range the shared contract
 * covers, which is 24 frets.
 */
static uint32_t fb_max_fret(const kpa_tab *tab)
{
    uint32_t max = 20u;

    if (tab != NULL && tab->max_fret > 0u) max = tab->max_fret;
    if (max < 5u) max = 5u;
    if (max > (uint32_t)KPA_FRET_MAX_FRET) max = (uint32_t)KPA_FRET_MAX_FRET;
    return max;
}

static float model_ramp_seconds(const kpa_ui_model *model)
{
    /* One accessor rather than a special case at every use, so a memset
     * model behaves like one that asked for the default. */
    if (!(model->ramp_seconds > 0.05f)) return KPA_FB_RAMP_DEFAULT;
    if (model->ramp_seconds > KPA_FB_RAMP_MAX) return KPA_FB_RAMP_MAX;
    return model->ramp_seconds;
}

static float fb_visual_life(const kpa_fret_note *note)
{
    const float life = (float)(note->end - note->start);

    return life > KPA_FB_MIN_LIFE ? life : KPA_FB_MIN_LIFE;
}

/*
 * Sounding and arriving, as the SCREEN means them rather than as the
 * artifact does.  A note is drawn from its onset until its visual life is
 * spent, which is at or after the end the transcription wrote; everything
 * that has not started yet is arriving.  kpa_fret_note.state is relative to
 * the instant the query was made, and fb_collect deliberately queries an
 * earlier one, so these two are what classify a note here.
 */
static bool fb_note_sounding(const kpa_fret_note *note, double when)
{
    return note->start <= when &&
           when < note->start + (double)fb_visual_life(note);
}

static bool fb_note_arriving(const kpa_fret_note *note, double when)
{
    return note->start > when;
}

/*
 * One frame's notes: everything sounding now and everything arriving inside
 * the look-ahead window, from one call into the shared model.
 */
typedef struct kpa_fb_frame {
    kpa_fret_note notes[KPA_FB_MAX_NOTES];
    kpa_fret_note_report report;
    /* The instant being drawn, which is NOT the instant the notes were
     * queried at: see fb_collect. */
    double when;
    /* The newest note on each string, or NULL.  See fb_ringing. */
    const kpa_fret_note *ring[KPA_STRING_COUNT];
    uint32_t ringing;
} kpa_fb_frame;

/*
 * LATEST ONSET WINS, one note per string.
 *
 * This is not a tidying-up.  Sampled at 20 Hz over the audited song, 46% of
 * instants have between two and four notes claiming a single string at
 * once - a transcription's sustains overlap the next note on the same
 * string - and the most any one string is asked to play at once is four.  A
 * neck that drew all of them would show shapes no hand can play.  After
 * this rule between none and six strings ring, and 5% of those instants have
 * nothing sounding at all.
 *
 * It is a decision about what to draw, which is why it is here and not in
 * kpa_fret.c: the query's job is to report what the artifact says.
 */
static void fb_ringing(kpa_fb_frame *frame, uint32_t strings)
{
    uint32_t index;

    (void)memset(frame->ring, 0, sizeof frame->ring);
    frame->ringing = 0u;
    for (index = 0u; index < frame->report.count; ++index) {
        const kpa_fret_note *note = &frame->notes[index];
        const uint32_t string = (uint32_t)note->string_index;
        const kpa_fret_note *held;

        if (!fb_note_sounding(note, frame->when)) continue;
        if (note->string_index < 0 || string >= strings) continue;
        held = frame->ring[string];
        if (held != NULL && held->start > note->start) continue;
        frame->ring[string] = note;
    }
    for (index = 0u; index < strings; ++index) {
        if (frame->ring[index] != NULL) ++frame->ringing;
    }
}

/*
 * One query, asked KPA_FB_MIN_LIFE earlier than the instant being drawn with
 * the window widened by the same amount, so that the answer also carries the
 * notes that have just ended - the ones this screen is still drawing the
 * tail of.  Everything is classified against frame->when afterwards.
 */
static bool fb_collect(kpa_fb_frame *frame, const kpa_tab *tab, double when,
                       float look_ahead, uint32_t strings)
{
    const double tail = (double)KPA_FB_MIN_LIFE;

    (void)memset(&frame->report, 0, sizeof frame->report);
    frame->when = when;
    if (!kpa_fret_notes_at(tab, when - tail, (double)look_ahead + tail,
                           frame->notes, KPA_FB_MAX_NOTES, &frame->report)) {
        (void)memset(frame->ring, 0, sizeof frame->ring);
        frame->ringing = 0u;
        return false;
    }
    fb_ringing(frame, strings);
    return true;
}

/*
 * How present a note looks and how hard it was just struck, both from its
 * age alone.  A 0.094 s note - the shortest in the audited song - is held
 * open for 0.055 s, sustains to 0.132 s and decays to 0.22 s; the 6.052 s
 * note attacks for 0.10 s and decays over its last 0.18 s.  Both read.
 */
typedef struct kpa_fb_envelope {
    float level;
    float flash;      /* 1 at the pluck, 0 once the note is just sounding */
} kpa_fb_envelope;

static kpa_fb_envelope fb_envelope(const kpa_fret_note *note, double when)
{
    kpa_fb_envelope out;
    const float age = (float)(when - note->start);
    const float life = fb_visual_life(note);
    float attack;
    float release;

    out.level = 0.0f;
    out.flash = 0.0f;
    attack = life * 0.25f;
    if (attack > 0.10f) attack = 0.10f;
    release = life * 0.40f;
    if (release > 0.18f) release = 0.18f;
    if (!(age > 0.0f)) {
        out.level = 1.0f;
        out.flash = 1.0f;
        return out;
    }
    if (age < attack) {
        out.level = 1.0f;
        out.flash = 1.0f - age / attack;
    } else if (age < life - release) {
        out.level = 1.0f;
    } else if (age < life) {
        out.level = (life - age) / release;
    }
    return out;
}

/* --------------------------------------------------------- chord names */

/*
 * What the callout says about a set of sounding pitches.
 *
 * The name is kpa_fret_chord_identify's and only its.  That namer refuses
 * far more than it accepts - on the reference song it declines 143 of 438
 * multi-pitch-class events - because a wrong chord symbol is worse than no
 * chord symbol.  A screen still has to put something on that line, so what
 * is added here is the fallback and nothing else: the pitch classes that
 * are sounding, spelled with the same table, which is a list of notes and
 * is never dressed up as a chord.  *out_named says which of the two it is,
 * and the callout draws an unnamed line in dimmer ink for exactly that
 * reason.
 */
void kpa_ui_internal_chord_label(const int32_t *pitches, uint32_t count,
                                 char *out, size_t size, bool *out_named)
{
    kpa_fret_chord chord;
    bool seen[12];
    size_t used = 0u;
    uint32_t index;
    int32_t pitch_class;

    if (out == NULL || size == 0u) return;
    out[0] = '\0';
    if (out_named != NULL) *out_named = false;
    if (pitches == NULL || count == 0u) return;
    if (kpa_fret_chord_identify(pitches, (size_t)count, &chord)) {
        (void)snprintf(out, size, "%s", chord.name);
        if (out_named != NULL) *out_named = true;
        return;
    }
    (void)memset(seen, 0, sizeof seen);
    for (index = 0u; index < count; ++index) {
        if (pitches[index] < 0) continue;
        seen[pitches[index] % 12] = true;
    }
    for (pitch_class = 0; pitch_class < 12; ++pitch_class) {
        const char *name;
        int written;

        if (!seen[pitch_class]) continue;
        name = kpa_fret_pitch_class_name(pitch_class);
        if (name == NULL) continue;
        written = snprintf(out + used, size - used, "%s%s",
                           used > 0u ? " " : "", name);
        if (written < 0 || (size_t)written >= size - used) break;
        used += (size_t)written;
    }
}

/* The ringing set as pitches, for the label above. */
static uint32_t fb_ring_pitches(const kpa_fb_frame *frame, uint32_t strings,
                                int32_t *out)
{
    uint32_t index;
    uint32_t count = 0u;

    for (index = 0u; index < strings && count < KPA_FRET_MAX_PITCHES;
         ++index) {
        if (frame->ring[index] != NULL) {
            out[count++] = frame->ring[index]->pitch;
        }
    }
    return count;
}

/* ----------------------------------------------------------- the hand */

/*
 * Where the hand is, from the shared model: a five-fret box placed by the
 * median of the frets played over a window that reaches 0.5 s back and
 * 1.5 s forward.  Forward-weighted on purpose - a marker that arrives with
 * the player is telling them where they already were - and stateless, which
 * is what keeps kpa_ui_compose a pure function of the model.  Open strings
 * are not a hand position and do not pull it to the nut.
 */
static bool fb_hand(const kpa_tab *tab, double when, kpa_fret_hand *out)
{
    return kpa_fret_hand_at(tab, when, out);
}

/* The lowest fretted fret of one event, or 0 when it is all open strings. */
static uint32_t fb_event_anchor(const kpa_tab *tab, uint32_t event,
                                uint32_t strings)
{
    const kpa_tab_event *item;
    uint32_t slot;
    uint32_t anchor = 0u;

    if (tab == NULL || tab->positions == NULL || event >= tab->event_count) {
        return 0u;
    }
    item = &tab->events[event];
    for (slot = 0u; slot < item->position_count; ++slot) {
        const uint32_t at = item->first_position + slot;
        uint32_t fret;

        if (at >= tab->position_count) break;
        if ((uint32_t)tab->positions[at].string_index >= strings) continue;
        fret = tab->positions[at].fret;
        if (fret == 0u) continue;
        if (anchor == 0u || fret < anchor) anchor = fret;
    }
    return anchor;
}

/*
 * The next time the hand has to move, and what it lands on.  Measured on
 * the audited song, a move of two frets or more falls within four seconds
 * at 98% of sampled instants, so this line almost always has something to
 * say; when it has nothing it draws nothing rather than a placeholder.
 *
 * The walk is bounded rather than open: at the measured 0.092 s minimum gap
 * between onsets, 256 events is more than twenty seconds of song, and a
 * move further off than that is not the next thing a player needs.
 */
#define KPA_FB_MOVE_EVENTS 256u

typedef struct kpa_fb_move {
    bool found;
    double when;
    uint32_t anchor;
    char chord[KPA_FRET_CHORD_NAME_CAPACITY];
} kpa_fb_move;

static kpa_fb_move fb_next_move(const kpa_tab *tab, double when,
                                uint32_t strings, const kpa_fret_hand *hand)
{
    kpa_fb_move move;
    uint32_t event;
    uint32_t walked;

    (void)memset(&move, 0, sizeof move);
    if (tab == NULL || tab->events == NULL) return move;
    event = kpa_tab_first_after(tab, when);
    for (walked = 0u; event < tab->event_count && walked < KPA_FB_MOVE_EVENTS;
         ++event, ++walked) {
        const kpa_tab_event *item = &tab->events[event];
        kpa_fret_chord chord;
        uint32_t anchor;
        uint32_t distance;

        if (item->start <= when) continue;
        anchor = fb_event_anchor(tab, event, strings);
        if (anchor == 0u) continue;
        distance = anchor > (uint32_t)hand->low
            ? anchor - (uint32_t)hand->low
            : (uint32_t)hand->low - anchor;
        if (distance < 2u) continue;
        if (kpa_fret_chord_of_event(tab, event, &chord)) {
            (void)snprintf(move.chord, sizeof move.chord, "%s", chord.name);
        }
        move.found = true;
        move.when = item->start;
        move.anchor = anchor;
        break;
    }
    return move;
}

/* ------------------------------------------------------ reserved bands */

/*
 * Pixels kpa_ui_compose refuses to draw in, top and bottom, as a function of
 * the canvas height and the model alone.  Both terms are non-decreasing in
 * height, which is what lets kpa_ui_cell_layout_get - which knows the height
 * only as rows * cell_height - reason safely about a canvas that may be a
 * fraction of a cell taller than that.
 */
static void reserved_bands(const kpa_ui_model *model, int height,
                           int *out_top, int *out_bottom)
{
    int top = height / 12;
    int bottom = height / 12;

    /* The caps are in pixels because kpa_ui_compose has no cell height to
     * work from.  They are set at roughly two cell rows on a large terminal
     * font, which is what kpa_ui_cell_layout_get needs before it can claim
     * even one row from the bottom band. */
    if (top > 48) top = 48;
    if (bottom > 72) bottom = 72;
    if (model->view == KPA_VIEW_PRACTICE && model->lyrics_visible) {
        int lyrics = height / 6;

        if (lyrics > 96) lyrics = 96;
        bottom += lyrics;
    }
    *out_top = top;
    *out_bottom = bottom;
}

/* True when this model puts song text in the bottom band. */
static bool model_has_lyric_band(const kpa_ui_model *model)
{
    return model->view == KPA_VIEW_PRACTICE && model->lyrics_visible;
}

/* --------------------------------------------------------- text drawing */

/*
 * Draw one line, truncated to `room` pixels.  Measured with sr_text_width
 * rather than assumed at eight pixels a character: the right-hand end of a
 * line that runs off its panel is the end worth keeping off the panel next
 * to it.
 */
static void draw_text_fit(sr_canvas *canvas, int x, int y, const char *text,
                          uint32_t rgb, int room)
{
    char line[KPA_UI_LINE_CAPACITY];
    size_t length;

    if (text == NULL || room <= 0) return;
    length = strlen(text);
    if (length >= sizeof line) length = sizeof line - 1u;
    (void)memcpy(line, text, length);
    line[length] = '\0';
    while (length > 0u && sr_text_width(line, 1) > room) {
        line[--length] = '\0';
    }
    if (length == 0u) return;
    sr_text(canvas, (float)x, (float)y, line, rgb, 1.0f, 1);
}

/* Right-aligned counterpart; `right` is the exclusive right edge. */
static void draw_text_right(sr_canvas *canvas, int right, int y,
                            const char *text, uint32_t rgb, int room)
{
    int width;

    if (text == NULL || room <= 0) return;
    width = sr_text_width(text, 1);
    if (width > room) {
        draw_text_fit(canvas, right - room, y, text, rgb, room);
        return;
    }
    sr_text(canvas, (float)(right - width), (float)y, text, rgb, 1.0f, 1);
}

/* A filled proportion bar: the shape a gain or a position reads as. */
static void draw_meter(sr_canvas *canvas, int x, int y, int width, int height,
                       float fraction, uint32_t rgb)
{
    if (width <= 0 || height <= 0) return;
    sr_fill_rect(canvas, (float)x, (float)y, (float)width, (float)height,
                 KPA_UI_RAISED, 1.0f);
    fraction = clamp_float(fraction, 0.0f, 1.0f);
    if (fraction > 0.0f) {
        sr_fill_rect(canvas, (float)x, (float)y, (float)width * fraction,
                     (float)height, rgb, 1.0f);
    }
}

/* -------------------------------------------------------- library view */

static void draw_library(sr_canvas *canvas, const kpa_ui_model *model,
                         int top, int bottom)
{
    char line[KPA_UI_LINE_CAPACITY];
    const int left = KPA_UI_MARGIN;
    const int right = canvas->w - KPA_UI_MARGIN;
    const int width = right - left;
    /* Fixed columns, for the same reason the mixer has them: a list whose
     * last column is pinned to the far edge of a wide terminal reads as two
     * unrelated lists. */
    const int title_x = left + 24;
    const int artist_x = left + 300;
    const int time_right = left + 566;
    const int track_right = left + 614;
    const int state_x = left + 630;
    int y;
    int visible;
    uint32_t first = 0u;
    uint32_t index;

    if (width <= 0) return;
    sr_fill_rect(canvas, 0.0f, (float)top, (float)canvas->w,
                 (float)KPA_UI_ROW, KPA_UI_PANEL, 1.0f);
    (void)snprintf(line, sizeof line, "library   %u project%s",
                   (unsigned)model->summary_count,
                   model->summary_count == 1u ? "" : "s");
    draw_text_fit(canvas, left, top + 3, line, KPA_UI_TEXT, width);
    draw_text_right(canvas, right, top + 3,
                    "enter open   tab focus   q quit   ? help", KPA_UI_DIM,
                    width / 2);

    y = top + KPA_UI_ROW + 4;
    if (y + KPA_UI_LINE > bottom) return;
    draw_text_fit(canvas, title_x, y, "TITLE", KPA_UI_DIM, 268);
    draw_text_fit(canvas, artist_x, y, "ARTIST", KPA_UI_DIM, 200);
    draw_text_right(canvas, time_right, y, "TIME", KPA_UI_DIM, 56);
    draw_text_right(canvas, track_right, y, "TRK", KPA_UI_DIM, 40);
    draw_text_fit(canvas, state_x, y, "STATE", KPA_UI_DIM, 64);
    y += KPA_UI_LINE;

    if (model->summaries == NULL || model->summary_count == 0u) {
        draw_text_fit(canvas, left, y + 4,
                      "no projects yet - build one with the pipeline",
                      KPA_UI_DIM, width);
        return;
    }
    visible = (bottom - y) / KPA_UI_ROW;
    if (visible <= 0) return;
    /* Scroll the window rather than the selection: the highlighted row stays
     * on screen no matter how long the list is. */
    if (model->selected_project >= (uint32_t)visible) {
        first = model->selected_project - (uint32_t)visible + 1u;
    }
    for (index = first; index < model->summary_count; ++index) {
        const kpa_project_summary *entry = &model->summaries[index];
        const bool selected = index == model->selected_project;
        const uint32_t colour = selected ? KPA_UI_TEXT : KPA_UI_DIM;
        char cell[32];

        if (y + KPA_UI_ROW > bottom) break;
        if (selected) {
            sr_fill_rect(canvas, (float)left - 4.0f, (float)y,
                         (float)(width + 8), (float)KPA_UI_ROW,
                         KPA_UI_RAISED, 1.0f);
            sr_text(canvas, (float)left, (float)(y + 2), ">", KPA_UI_ACCENT,
                    1.0f, 1);
        }
        /* The embedded font is ASCII bitmaps, so a title in another script
         * shows here as '?'.  The selection's exact title is written to the
         * title cell row, which is UTF-8; see kpa_cells.h. */
        draw_text_fit(canvas, title_x, y + 2, entry->title, colour, 268);
        draw_text_fit(canvas, artist_x, y + 2, entry->artist, KPA_UI_DIM,
                      200);
        format_clock(entry->duration, cell, sizeof cell);
        draw_text_right(canvas, time_right, y + 2, cell, KPA_UI_DIM, 56);
        (void)snprintf(cell, sizeof cell, "%u",
                       (unsigned)entry->track_count);
        draw_text_right(canvas, track_right, y + 2, cell, KPA_UI_DIM, 40);
        draw_text_fit(canvas, state_x, y + 2,
                      entry->ready ? "ready" : "pending",
                      entry->ready ? KPA_UI_GOOD : KPA_UI_WARN, 64);
        y += KPA_UI_ROW;
    }
}

/* ------------------------------------------------------- practice view */

/* Play state as a shape as well as a word: the shape reads at a glance. */
static void draw_play_glyph(sr_canvas *canvas, int x, int y, bool playing)
{
    if (playing) {
        sr_fill_triangle(canvas, (float)x, (float)y, (float)x,
                         (float)(y + 12), (float)(x + 11), (float)(y + 6),
                         KPA_UI_GOOD, 1.0f);
        return;
    }
    sr_fill_rect(canvas, (float)x, (float)y, 4.0f, 12.0f, KPA_UI_WARN, 1.0f);
    sr_fill_rect(canvas, (float)(x + 7), (float)y, 4.0f, 12.0f, KPA_UI_WARN,
                 1.0f);
}

static int draw_transport(sr_canvas *canvas, const kpa_ui_model *model,
                          int y, int bottom)
{
    char line[KPA_UI_LINE_CAPACITY];
    char elapsed[32];
    char total[32];
    const int left = KPA_UI_MARGIN;
    const int right = canvas->w - KPA_UI_MARGIN;
    const int width = right - left;

    if (width <= 0 || y + KPA_UI_ROW > bottom) return y;
    sr_fill_rect(canvas, 0.0f, (float)y, (float)canvas->w, (float)KPA_UI_ROW,
                 KPA_UI_PANEL, 1.0f);
    draw_play_glyph(canvas, left, y + 4, model->playing);
    format_clock(model->position, elapsed, sizeof elapsed);
    format_clock(model->duration, total, sizeof total);
    (void)snprintf(line, sizeof line, "%s   %s / %s",
                   model->playing ? "playing" : "paused", elapsed, total);
    draw_text_fit(canvas, left + 20, y + 2, line, KPA_UI_TEXT, width - 20);

    /* Rate control is real or it is absent; it is never a number that
     * quietly transposed the song. */
    if (model->rate_available) {
        (void)snprintf(line, sizeof line, "%.2fx", model->rate);
        draw_text_right(canvas, right, y + 2, line,
                        model->rate < 0.999 || model->rate > 1.001
                            ? KPA_UI_ACCENT : KPA_UI_DIM, 80);
    } else {
        draw_text_right(canvas, right, y + 2, "rate unavailable", KPA_UI_DIM,
                        160);
    }
    if (model->device_lost) {
        draw_text_right(canvas, right - 176, y + 2, "device lost",
                        KPA_UI_ALERT, 100);
    } else if (model->underrun) {
        draw_text_right(canvas, right - 176, y + 2, "underrun", KPA_UI_WARN,
                        100);
    }
    return y + KPA_UI_ROW + 2;
}

/*
 * The song's shape, drawn inside the timeline: how many notes the guitar
 * part has in each slice of it.  A player looking for the solo, or for the
 * quiet bar before the chorus, can see where it is instead of scrubbing for
 * it.  One pass over the events into a fixed set of buckets, so the cost
 * does not grow with the width of the terminal.
 */
#define KPA_UI_DENSITY_BUCKETS 192

static void draw_density(sr_canvas *canvas, const kpa_tab *tab, double span,
                         int x, int y, int width, int height)
{
    uint16_t bucket[KPA_UI_DENSITY_BUCKETS];
    uint32_t event;
    uint16_t peak = 0u;
    int index;

    if (tab == NULL || tab->events == NULL || width <= 0 || !(span > 0.0)) {
        return;
    }
    (void)memset(bucket, 0, sizeof bucket);
    for (event = 0u; event < tab->event_count; ++event) {
        const double at = tab->events[event].start / span;
        int slot;

        if (!(at >= 0.0) || at >= 1.0) continue;
        slot = (int)(at * (double)KPA_UI_DENSITY_BUCKETS);
        if (slot < 0 || slot >= KPA_UI_DENSITY_BUCKETS) continue;
        if (bucket[slot] < 0xFFFFu) ++bucket[slot];
        if (bucket[slot] > peak) peak = bucket[slot];
    }
    if (peak == 0u) return;
    for (index = 0; index < KPA_UI_DENSITY_BUCKETS; ++index) {
        const float share = (float)bucket[index] / (float)peak;
        const float bar = (float)height * share;
        const float x0 = (float)x + (float)width * (float)index /
                                    (float)KPA_UI_DENSITY_BUCKETS;
        const float x1 = (float)x + (float)width * (float)(index + 1) /
                                    (float)KPA_UI_DENSITY_BUCKETS;

        if (bucket[index] == 0u) continue;
        sr_fill_rect(canvas, x0, (float)y + (float)height - bar,
                     x1 - x0 > 1.0f ? x1 - x0 : 1.0f, bar, KPA_UI_FAINT,
                     0.9f);
    }
}

static int draw_timeline(sr_canvas *canvas, const kpa_ui_model *model,
                         int y, int bottom)
{
    const int left = KPA_UI_MARGIN;
    const int right = canvas->w - KPA_UI_MARGIN;
    const int width = right - left;
    const int height = 14;
    const double span = model->duration > 0.0 ? model->duration : 1.0;
    double fraction;
    int head;

    if (width <= 16 || y + height + 8 > bottom) return y;
    sr_fill_rect(canvas, (float)left, (float)y, (float)width, (float)height,
                 KPA_UI_RAISED, 1.0f);
    if (model->overview == KPA_OVERVIEW_DENSITY) {
        draw_density(canvas, model->tab, span, left, y, width, height);
    }
    if (model->loop_active && model->loop_end > model->loop_start) {
        const double start = clamp_double(model->loop_start / span, 0.0, 1.0);
        const double end = clamp_double(model->loop_end / span, 0.0, 1.0);
        const float x0 = (float)left + (float)width * (float)start;
        const float x1 = (float)left + (float)width * (float)end;

        sr_fill_rect(canvas, x0, (float)y, x1 - x0, (float)height,
                     KPA_UI_LOOP, 0.55f);
        sr_line(canvas, x0, (float)y, x0, (float)(y + height), 2.0f,
                KPA_UI_LOOP, 1.0f, 0, 0);
        sr_line(canvas, x1, (float)y, x1, (float)(y + height), 2.0f,
                KPA_UI_LOOP, 1.0f, 0, 0);
    }
    fraction = clamp_double(model->position / span, 0.0, 1.0);
    sr_fill_rect(canvas, (float)left, (float)y,
                 (float)width * (float)fraction, (float)height,
                 KPA_UI_ACCENT, 0.35f);
    head = left + (int)((double)width * fraction);
    sr_line(canvas, (float)head, (float)y - 2.0f, (float)head,
            (float)(y + height + 2), 2.0f, KPA_UI_TEXT, 1.0f, 0, 0);
    return y + height + 6;
}

static int draw_mixer(sr_canvas *canvas, const kpa_ui_model *model,
                      int y, int bottom)
{
    const int left = KPA_UI_MARGIN;
    /*
     * Fixed columns rather than a row stretched to the canvas: a mixer whose
     * meters drift to the far edge of a wide terminal is a mixer nobody can
     * read against its labels.
     */
    const int label_x = left + 30;
    const int kind_x = left + 186;
    const int meter_x = left + 274;
    const int value_x = left + 386;
    const int flag_x = left + 428;
    const int width = canvas->w - KPA_UI_MARGIN - left;
    bool any_solo = false;
    uint32_t index;

    if (width <= 0 || model->track_count == 0u) return y;
    for (index = 0u; index < model->track_count &&
                     index < KPA_MAX_TRACKS; ++index) {
        if (model->tracks[index].soloed) any_solo = true;
    }
    for (index = 0u; index < model->track_count &&
                     index < KPA_MAX_TRACKS; ++index) {
        const kpa_ui_track *track = &model->tracks[index];
        const bool selected = index == model->selected_track;
        /* A soloed track anywhere silences the ones that are not soloed;
         * showing the mute state alone would call an inaudible track live. */
        const bool audible = !track->muted && (!any_solo || track->soloed);
        char label[64];

        if (y + KPA_UI_ROW > bottom) break;
        if (selected) {
            sr_fill_rect(canvas, (float)left - 4.0f, (float)y,
                         (float)(width + 8), (float)KPA_UI_ROW,
                         KPA_UI_RAISED, 1.0f);
        }
        (void)snprintf(label, sizeof label, "%s%u",
                       selected ? ">" : " ", (unsigned)(index + 1u));
        draw_text_fit(canvas, left, y + 2, label,
                      selected ? KPA_UI_ACCENT : KPA_UI_DIM, 28);
        draw_text_fit(canvas, label_x, y + 2, track->label,
                      audible ? KPA_UI_TEXT : KPA_UI_DIM, 150);
        draw_text_fit(canvas, kind_x, y + 2, track->kind, KPA_UI_DIM, 84);
        draw_meter(canvas, meter_x, y + 5, 100, 10,
                   track->gain / KPA_UI_GAIN_MAX,
                   audible ? KPA_UI_ACCENT : KPA_UI_DIM);
        (void)snprintf(label, sizeof label, "%.2f", (double)track->gain);
        draw_text_fit(canvas, value_x, y + 2, label, KPA_UI_DIM, 36);
        draw_text_fit(canvas, flag_x, y + 2, track->muted ? "M" : "-",
                      track->muted ? KPA_UI_ALERT : KPA_UI_FAINT, 12);
        draw_text_fit(canvas, flag_x + 18, y + 2, track->soloed ? "S" : "-",
                      track->soloed ? KPA_UI_GOOD : KPA_UI_FAINT, 12);
        y += KPA_UI_ROW;
    }
    return y + 4;
}

/* ---------------------------------------------------- practice layout */

#define KPA_UI_TRANSPORT_H 22
#define KPA_UI_TIMELINE_H 20

/*
 * Where each band of the practice view goes, decided in one place so that
 * draw_practice stops doing arithmetic between its widgets and so that what
 * gets given up on a shrinking terminal is a list rather than an accident.
 *
 * A -1 means "not drawn"; heights of 0 mean the same for the bands that
 * carry one.  Drop order, most expendable first: the tab lane, the fret
 * ruler, the approach ramp, the mixer's rows (which become a one-line
 * strip), the fretboard block, the timeline.  The transport is last and is
 * always drawn if anything is.
 *
 * Every proportion below is chosen rather than measured.  What this produces
 * is not: at the 1280x720 --render size, with the audited project's six
 * stems and the lyric band shown, the reserved bands take 48 px off the top
 * and 156 off the bottom, and the 516 px left over come out as transport 22,
 * timeline 20, mixer 124, fretboard 215 (callout 18, ramp 54, neck 129 -
 * 21.5 px a string - and ruler 14), tab lane 133.  The neck's wood in that
 * frame runs from y 288 to y 416, which is where this says it should.
 */
typedef struct kpa_practice_layout {
    int transport_y;
    int timeline_y;
    int mixer_y;
    int mixer_h;
    bool mixer_compact;
    int fb_y;
    int fb_h;               /* the whole fretboard block; 0 when absent */
    int callout_y;
    int ramp_y;
    int ramp_h;
    int neck_y;
    int neck_h;
    int ruler_y;            /* -1 when the ruler was given up */
    int lane_y;
    int lane_h;             /* 0 when the lane is not drawn */
    int notice_y;           /* -1 when there is no line to say anything on */
    bool lane_dropped;      /* asked for and refused: say so */
} kpa_practice_layout;

static void practice_layout(const kpa_ui_model *model, int top, int bottom,
                            int width, kpa_practice_layout *out)
{
    /* Ruler and ramp in the order they are given up: both, then the ramp
     * alone, then neither.  The first that leaves the neck a readable
     * height wins. */
    static const bool candidate_ruler[3] = {true, false, false};
    static const bool candidate_ramp[3] = {true, true, false};
    const uint32_t strings = fb_string_count(model->tab);
    const int min_neck = (int)strings * KPA_FB_PITCH_MIN;
    int y = top + 2;
    int floor_y = bottom;
    int rest;
    int mixer_full;
    size_t candidate;

    (void)memset(out, 0, sizeof *out);
    out->transport_y = -1;
    out->timeline_y = -1;
    out->mixer_y = -1;
    out->callout_y = -1;
    out->ramp_y = -1;
    out->neck_y = -1;
    out->ruler_y = -1;
    out->lane_y = -1;
    out->notice_y = -1;
    if (bottom - top < KPA_UI_ROW) return;

    /* The notice takes its own line off the bottom rather than being drawn
     * over whatever is there, which is what this view has always done. */
    if (model->notice[0] != '\0' && bottom - KPA_UI_LINE > top) {
        floor_y = bottom - KPA_UI_LINE;
        out->notice_y = floor_y + 2;
    }

    if (y + KPA_UI_ROW > floor_y) return;
    out->transport_y = y;
    y += KPA_UI_TRANSPORT_H;
    if (y + KPA_UI_TIMELINE_H + 2 > floor_y) return;
    out->timeline_y = y;
    y += KPA_UI_TIMELINE_H;

    rest = floor_y - y;
    if (rest <= 0) return;

    /*
     * The mixer.  Full rows cost track_count * 20 px of height; the compact
     * strip costs one row of height and about 40 px of width per stem, so
     * five stems fit in 200 px across instead of 100 px down.  That trade is
     * only worth making when something else wants the height, which here
     * means a fretboard that would otherwise be cramped.
     */
    mixer_full = model->track_count > 0u
        ? (int)model->track_count * KPA_UI_ROW + 4 : 0;
    if (mixer_full > 0) {
        const bool wants_neck = model->fretboard != KPA_FB_OFF;
        const bool strip_fits = width >= (int)model->track_count * 40 + 16;

        if (!wants_neck || !strip_fits ||
            rest - mixer_full >= KPA_FB_BLOCK_FULL) {
            out->mixer_h = mixer_full;
        } else {
            out->mixer_h = KPA_UI_ROW;
            out->mixer_compact = true;
        }
        if (out->mixer_h > rest) out->mixer_h = rest;
        if (out->mixer_h >= KPA_UI_ROW) {
            out->mixer_y = y;
            y += out->mixer_h;
        } else {
            out->mixer_h = 0;
        }
        rest = floor_y - y;
    }

    /* The fretboard block. */
    if (model->fretboard != KPA_FB_OFF && rest >= KPA_FB_BLOCK_MIN) {
        int block = (rest * 62) / 100;

        if (block > KPA_FB_BLOCK_MAX) block = KPA_FB_BLOCK_MAX;
        if (block < KPA_FB_BLOCK_FULL) block = KPA_FB_BLOCK_FULL;
        if (block > rest) block = rest;
        for (candidate = 0u; candidate < 3u; ++candidate) {
            const int ruler = candidate_ruler[candidate] ? KPA_FB_RULER_H : 0;
            int ramp = 0;
            int neck;

            if (candidate_ramp[candidate] &&
                model->fretboard == KPA_FB_NECK_AND_RAMP) {
                ramp = ((block - KPA_FB_CALLOUT_H - ruler) * 30) / 100;
                /* A ramp too short to show half a second of look-ahead is
                 * not a ramp; the neck has the height instead. */
                if (ramp < 24) ramp = 0;
            }
            neck = block - KPA_FB_CALLOUT_H - ruler - ramp;
            if (neck < min_neck) continue;
            out->fb_y = y;
            out->fb_h = block;
            out->callout_y = y;
            out->ramp_y = y + KPA_FB_CALLOUT_H;
            out->ramp_h = ramp;
            out->neck_y = out->ramp_y + ramp;
            out->neck_h = neck;
            out->ruler_y = ruler > 0 ? out->neck_y + neck : -1;
            y += block;
            break;
        }
        rest = floor_y - y;
    }

    /* Whatever is left is the tab lane's, if it is enough to read. */
    if (model->tab_visible) {
        if (rest >= KPA_UI_LANE_MIN) {
            out->lane_y = y;
            out->lane_h = rest;
        } else if (out->fb_h > 0) {
            /* The old code dropped the lane in silence.  A player who asked
             * for both and got one is owed the sentence that says which key
             * gives them the other. */
            out->lane_dropped = true;
            if (out->notice_y < 0 && bottom - y >= KPA_UI_LINE) {
                out->notice_y = bottom - KPA_UI_LINE + 2;
            }
        }
    }
}

/*
 * The mixer as one row: four characters of each stem's name, a gain bar and
 * its mute and solo marks.  Everything the rows carry except the numeric
 * gain, in a fifth of the height.
 */
static void draw_mixer_strip(sr_canvas *canvas, const kpa_ui_model *model,
                             int y, int bottom)
{
    const int left = KPA_UI_MARGIN;
    const int right = canvas->w - KPA_UI_MARGIN;
    const int slot = 40;
    uint32_t index;
    bool any_solo = false;

    if (y + KPA_UI_ROW > bottom || model->track_count == 0u) return;
    for (index = 0u; index < model->track_count &&
                     index < KPA_MAX_TRACKS; ++index) {
        if (model->tracks[index].soloed) any_solo = true;
    }
    for (index = 0u; index < model->track_count &&
                     index < KPA_MAX_TRACKS; ++index) {
        const kpa_ui_track *track = &model->tracks[index];
        const bool selected = index == model->selected_track;
        const bool audible = !track->muted && (!any_solo || track->soloed);
        const int x = left + (int)index * slot;
        char label[8];
        size_t at;

        if (x + slot > right) break;
        if (selected) {
            sr_fill_rect(canvas, (float)x - 2.0f, (float)y,
                         (float)slot, (float)KPA_UI_ROW, KPA_UI_RAISED, 1.0f);
        }
        /* Four characters of the label, which is what tells Bass from Both
         * without a column of full names. */
        for (at = 0u; at < 4u; ++at) {
            label[at] = track->label[at];
            if (label[at] == '\0') break;
        }
        label[at < 4u ? at : 4u] = '\0';
        draw_text_fit(canvas, x, y + 1, label,
                      audible ? KPA_UI_TEXT : KPA_UI_DIM, 32);
        draw_meter(canvas, x, y + 14, 24, 3, track->gain / KPA_UI_GAIN_MAX,
                   audible ? KPA_UI_ACCENT : KPA_UI_DIM);
        if (track->muted) {
            draw_text_fit(canvas, x + 26, y + 1, "M", KPA_UI_ALERT, 10);
        }
        if (track->soloed) {
            draw_text_fit(canvas, x + 26, y + 9, "S", KPA_UI_GOOD, 10);
        }
    }
}

/* ------------------------------------------------ drawing the fretboard */

/*
 * The drawn neck, from the block the layout allotted.  The rail on the left
 * carries the chord shape above and the string names beside their own
 * strings, at a constant x - the same pinning the tab lane's gutter has, and
 * for the same reason.
 */
static void fb_geometry(const kpa_ui_model *model,
                        const kpa_practice_layout *layout, int left,
                        int right, kpa_fb_geom *out)
{
    const float wood_x0 = (float)(left + KPA_FB_RAIL);
    const float wood_x1 = (float)right;

    (void)memset(out, 0, sizeof *out);
    out->strings = fb_string_count(model->tab);
    out->max_fret = fb_max_fret(model->tab);
    out->low_on_top = model->low_string_on_top;
    out->left_handed = model->left_handed;
    out->wood_x0 = wood_x0;
    out->wood_x1 = wood_x1;
    /* The nut sits a little inside the wood so its bar and the open-string
     * marks on it have somewhere to be.  Mirroring is about the wood rather
     * than about the board, so a left-handed neck puts that margin on the
     * right where the nut now is. */
    out->board.x = (double)(wood_x0 + KPA_FB_NUT_MARGIN);
    out->board.y = (double)layout->neck_y;
    out->board.width = (double)wood_x1 - out->board.x;
    out->board.height = (double)layout->neck_h;
    out->mirror_sum = wood_x0 + wood_x1;
    out->y0 = layout->neck_y;
    out->y1 = layout->neck_y + layout->neck_h;
    out->string_pitch = (float)layout->neck_h / (float)out->strings;
    out->nut_x = neck_mirror(out, (float)out->board.x);
    out->far_x = neck_mirror(out, (float)(out->board.x + out->board.width));
}

/* The width of the space behind fret n, which is what decides whether a
 * fret number or a note name will fit inside a dot drawn there. */
static float fb_cell_width(const kpa_fb_geom *geometry, uint32_t fret)
{
    float behind;
    float at;

    if (fret == 0u) return KPA_FB_NUT_MARGIN * 2.0f;
    behind = neck_wire_x(geometry, fret - 1u);
    at = neck_wire_x(geometry, fret);
    return at > behind ? at - behind : behind - at;
}

static void fb_note_text(const kpa_ui_model *model, const kpa_fret_note *note,
                         char *out, size_t size)
{
    const char *name = kpa_fret_pitch_class_name(note->pitch % 12);

    out[0] = '\0';
    if (name == NULL) name = "?";
    switch (model->note_label) {
    case KPA_LABEL_NOTE:
        (void)snprintf(out, size, "%s", name);
        break;
    case KPA_LABEL_BOTH:
        (void)snprintf(out, size, "%d%s", (int)note->fret, name);
        break;
    case KPA_LABEL_FRET:
    default:
        (void)snprintf(out, size, "%d", (int)note->fret);
        break;
    }
}

/*
 * The frets, the inlays and the strings: the instrument with nothing played
 * on it yet.
 */
static void draw_neck_furniture(sr_canvas *canvas, const kpa_fb_geom *geometry)
{
    const float board_h = (float)(geometry->y1 - geometry->y0);
    const float inlay_r = geometry->string_pitch * 0.35f < 6.0f
        ? geometry->string_pitch * 0.35f : 6.0f;
    uint32_t fret;
    uint32_t api;

    sr_fill_rect(canvas, geometry->wood_x0, (float)geometry->y0,
                 geometry->wood_x1 - geometry->wood_x0, board_h,
                 KPA_FB_WOOD, 1.0f);
    for (fret = 1u; fret <= geometry->max_fret; ++fret) {
        const float x = neck_wire_x(geometry, fret);
        const kpa_fret_inlay inlay = kpa_fret_inlay_at((int32_t)fret);
        const float dot_x = neck_finger_x(geometry, fret);

        sr_line(canvas, x, (float)geometry->y0, x, (float)geometry->y1, 1.5f,
                KPA_FB_NICKEL, 1.0f, 0, 0);
        /* The marker goes in the space behind the wire, never on it: a dot
         * drawn on the wire shifts every marker toward the bridge and makes
         * a correctly spaced neck read as mis-spaced. */
        if (inlay == KPA_FRET_INLAY_SINGLE) {
            sr_fill_circle(canvas, dot_x,
                           (float)geometry->y0 + board_h * 0.5f, inlay_r,
                           KPA_FB_PEARL, 0.8f);
        } else if (inlay == KPA_FRET_INLAY_DOUBLE) {
            sr_fill_circle(canvas, dot_x,
                           (float)geometry->y0 + board_h / 3.0f, inlay_r,
                           KPA_FB_PEARL, 0.8f);
            sr_fill_circle(canvas, dot_x,
                           (float)geometry->y0 + board_h * 2.0f / 3.0f,
                           inlay_r, KPA_FB_PEARL, 0.8f);
        }
    }
    /* The nut, last of the furniture and thicker than any fret. */
    sr_fill_rect(canvas, geometry->nut_x - 2.0f, (float)geometry->y0, 4.0f,
                 board_h, KPA_FB_BONE, 1.0f);
    for (api = 0u; api < geometry->strings; ++api) {
        const float y = neck_string_y(geometry, api);
        const float base = geometry->string_pitch * 0.06f < 1.6f
            ? geometry->string_pitch * 0.06f : 1.6f;

        sr_line(canvas, geometry->wood_x0, y, geometry->wood_x1, y,
                fb_string_width(api, base > 0.9f ? base : 0.9f),
                api < 3u ? KPA_FB_WOUND : KPA_FB_PLAIN, 1.0f, 0, 0);
    }
}

/*
 * The five-fret box over where the hand is.  Everything about it comes from
 * kpa_fret_hand_at, including the fact that it never covers the nut: an open
 * string needs no hand and does not pull the box down to the first fret.
 */
static void draw_hand_box(sr_canvas *canvas, const kpa_fb_geom *geometry,
                          const kpa_fret_hand *hand)
{
    const float first = neck_wire_x(geometry, (uint32_t)hand->low - 1u);
    const float last = neck_wire_x(geometry, (uint32_t)hand->high);
    const float x0 = first < last ? first : last;
    const float x1 = first < last ? last : first;
    const float y = (float)geometry->y0 - 3.0f;
    const float h = (float)(geometry->y1 - geometry->y0) + 6.0f;

    /* Faint enough that the wood, the inlays and the fret wires under it
     * all still read: it says where the hand is, it does not replace the
     * part of the neck the hand is on.  The fill matches the browser's 0.16;
     * the outline is much softer than the 0.55 it carried when this shared
     * the loop's colour, because at 1280x720 that edge was the loudest thing
     * on the neck and the box read as a selection rather than as a hand.
     *
     * The fill stays at 0.09 and deliberately does NOT copy the browser's
     * 0.16: that alpha is tuned against a lighter board, and rendered over
     * this one it turns the box into a solid green slab that outshouts the
     * strings it sits behind. Same colour, same meaning, different ground --
     * so the number that matches is the one that looks the same, not the one
     * that reads the same in the source. Checked by rendering both. */
    sr_fill_rect(canvas, x0, y, x1 - x0, h, KPA_UI_HAND, 0.09f);
    sr_stroke_rect(canvas, x0, y, x1 - x0, h, 1.0f, KPA_UI_HAND, 0.28f);
}

/*
 * One sounding note.  The dot is where the finger is; the string is redrawn
 * either side of it - dead between the nut and the finger, standing wave
 * from the finger to the far end - which is the thing that makes the picture
 * read as a guitar being played rather than as dots on a diagram.
 *
 * The wave is a function of model->position and of nothing else, so it is
 * frozen while the player is paused.  That is correct: a paused guitar is
 * not vibrating.
 */
static void draw_sounding_note(sr_canvas *canvas, const kpa_ui_model *model,
                               const kpa_fb_geom *geometry,
                               const kpa_fret_note *note, uint32_t api,
                               double when)
{
    const kpa_fb_envelope envelope = fb_envelope(note, when);
    const uint32_t fret = note->fret >= 0 ? (uint32_t)note->fret : 0u;
    const float y = neck_string_y(geometry, api);
    const float x = neck_finger_x(geometry, fret);
    const uint32_t ink = kpa_string_rgb[api < KPA_STRING_COUNT ? api : 0u];
    const float cell = fb_cell_width(geometry, fret);
    const bool muted_by_capo = model->capo > 0u && fret > 0u &&
                               fret < (uint32_t)model->capo;
    float radius;
    float amplitude;
    float phase;
    int segment;
    char label[8];

    if (!(envelope.level > 0.0f)) return;
    /* Bounded by the string spacing as well as by the fret it sits behind,
     * so that a chord barred across one fret reads as one dot per string
     * rather than as a blob.  The spacing is 13 px at its tightest, and 528
     * of the audited song's 937 events sound two or more notes at once. */
    radius = geometry->string_pitch * 0.38f;
    if (radius > cell * 0.40f) radius = cell * 0.40f;
    if (radius > 9.0f) radius = 9.0f;
    if (radius < 2.0f) radius = 2.0f;

    /*
     * The vibrating length: an open string rings from the nut, a fretted one
     * from the finger.  Amplitude is capped at a fifth of the string spacing
     * so a loud note can never smear into the string next to it.
     */
    amplitude = 2.6f * (0.6f * envelope.flash + 0.4f * envelope.level);
    if (amplitude > geometry->string_pitch * 0.2f) {
        amplitude = geometry->string_pitch * 0.2f;
    }
    /* Dead between the nut and the finger.  An open string has no such
     * length: x is the nut for fret 0. */
    if (fret > 0u) {
        sr_line(canvas, geometry->nut_x, y, x, y, 1.0f, KPA_UI_FAINT, 0.45f,
                0, 0);
    }
    /*
     * ...and a standing wave from there to the end of the board, with a node
     * at each end, drawn as twelve segments.  It is a function of
     * model->position and of nothing else, so a paused player sees a still
     * string - which is correct: a paused guitar is not vibrating.
     */
    phase = sinf(6.2831853f * 6.0f * (float)when);
    for (segment = 0; segment < 12; ++segment) {
        const float t0 = (float)segment / 12.0f;
        const float t1 = (float)(segment + 1) / 12.0f;
        const float x0 = x + (geometry->far_x - x) * t0;
        const float x1 = x + (geometry->far_x - x) * t1;
        const float y0 = y + amplitude * sinf(3.14159265f * t0) * phase;
        const float y1 = y + amplitude * sinf(3.14159265f * t1) * phase;

        sr_line(canvas, x0, y0, x1, y1, 1.0f, ink,
                0.35f + 0.65f * envelope.level, 0, 0);
    }

    if (muted_by_capo) {
        /* Never drawn at a negative fret: the note is where the artifact
         * says it is, and the mark says it cannot be played from here. */
        sr_ring(canvas, x, y, radius, 1.5f, KPA_UI_ALERT, 0.9f);
        sr_line(canvas, x - radius * 0.6f, y - radius * 0.6f,
                x + radius * 0.6f, y + radius * 0.6f, 1.5f, KPA_UI_ALERT,
                0.9f, 0, 0);
        sr_line(canvas, x - radius * 0.6f, y + radius * 0.6f,
                x + radius * 0.6f, y - radius * 0.6f, 1.5f, KPA_UI_ALERT,
                0.9f, 0, 0);
        return;
    }
    if (fret == 0u) {
        /* An open string is a ring on the nut, which is what a chord box
         * means by `o`.  Distinguishing open from silent matters: 15% of the
         * audited song's positions are open strings. */
        sr_ring(canvas, x, y, radius * 0.8f, 2.0f, ink,
                0.35f + 0.65f * envelope.level);
        return;
    }
    radius *= (0.72f + 0.28f * envelope.level) *
              (1.0f + 0.40f * envelope.flash);
    if (radius > geometry->string_pitch * 0.46f) {
        radius = geometry->string_pitch * 0.46f;
    }
    sr_fill_circle(canvas, x, y, radius, ink,
                   0.25f + 0.75f * envelope.level);
    if (envelope.flash > 0.0f) {
        /* The pluck: a ring that expands out of the dot and is gone by the
         * time the note is merely sounding. */
        sr_ring(canvas, x, y, radius * (1.0f + 0.6f * envelope.flash), 1.5f,
                ink, 0.7f * envelope.flash);
    }
    fb_note_text(model, note, label, sizeof label);
    if (label[0] != '\0' &&
        radius * 2.0f >= (float)sr_text_width(label, 1) + 2.0f) {
        sr_text(canvas, x - (float)sr_text_width(label, 1) * 0.5f, y - 8.0f,
                label, KPA_UI_BACKDROP, 1.0f, 1);
    }
}

/*
 * The approach ramp: time running downward onto the neck, sharing the neck's
 * x mapping exactly, so a mark reaches the string it belongs to at the
 * instant that note sounds.
 */
static void draw_ramp(sr_canvas *canvas, const kpa_ui_model *model,
                      const kpa_fb_geom *geometry, const kpa_fb_frame *frame,
                      const kpa_practice_layout *layout)
{
    const float window = model_ramp_seconds(model);
    const float top = (float)layout->ramp_y;
    const float height = (float)layout->ramp_h;
    uint32_t index;
    float mark;
    char line[32];

    if (layout->ramp_h <= 0) return;
    /* Half a second per gridline, so the distance to the next chord is
     * readable rather than merely visible. */
    for (mark = 0.5f; mark < window; mark += 0.5f) {
        const float y = top + height * (1.0f - mark / window);

        sr_line(canvas, geometry->wood_x0, y, geometry->wood_x1, y, 1.0f,
                KPA_UI_FAINT, 0.5f, 0, 0);
    }
    /*
     * At the top of the band, and at the end of the neck furthest from the
     * nut: the marks crowd toward the nut because 93% of this song's notes
     * are at fret 8 or below, so the far end is the corner with room in it.
     * Which end that is depends on which hand the player uses.
     */
    (void)snprintf(line, sizeof line, "%.1fs ahead", (double)window);
    if (geometry->left_handed) {
        draw_text_fit(canvas, (int)geometry->wood_x0 + 2, layout->ramp_y + 1,
                      line, KPA_UI_FAINT, 100);
    } else {
        draw_text_right(canvas, (int)geometry->wood_x1, layout->ramp_y + 1,
                        line, KPA_UI_FAINT, 100);
    }
    for (index = 0u; index < frame->report.count; ++index) {
        const kpa_fret_note *note = &frame->notes[index];
        const uint32_t api = (uint32_t)note->string_index;
        float remaining;
        float u;
        float x;
        float y;
        float comb;
        float cell;

        if (!fb_note_arriving(note, frame->when)) continue;
        if (note->string_index < 0 || api >= geometry->strings) continue;
        if (note->out_of_range || note->fret < 0) continue;
        /* Measured from the instant being drawn.  note->time_to_start is
         * measured from the one the query was made at, which is earlier. */
        remaining = (float)(note->start - frame->when);
        u = remaining / window;
        if (u < 0.0f) u = 0.0f;
        if (u > 1.0f) continue;
        cell = fb_cell_width(geometry, (uint32_t)note->fret);
        comb = cell * 0.14f;
        if (comb > 3.0f) comb = 3.0f;
        /* The comb offset: six notes at one fret would otherwise be one dot
         * six deep.  Fanned by string it reads as a strum. */
        x = neck_finger_x(geometry, (uint32_t)note->fret) +
            ((float)string_display_row(api, geometry->strings,
                                       geometry->low_on_top) -
             ((float)geometry->strings - 1.0f) * 0.5f) * comb;
        y = top + height * (1.0f - u);
        if (remaining <= 0.5f) {
            /* Close enough to be the next thing the hand does: a dashed
             * line saying where it goes. */
            sr_line(canvas, x, y, x, top + height, 1.0f,
                    kpa_string_rgb[api < KPA_STRING_COUNT ? api : 0u], 0.18f,
                    3, 3);
        }
        sr_fill_circle(canvas, x, y, 3.0f,
                       kpa_string_rgb[api < KPA_STRING_COUNT ? api : 0u],
                       0.30f + 0.70f * (1.0f - u));
    }
}

/*
 * The chord shape, in the rail, as a guitarist reads one: strings across,
 * frets down, a dot where a finger goes, a ring on an open string and a
 * cross on one that is not sounding.  The strings run in the same order as
 * the neck beside it, so the box and the instrument agree.
 */
static void draw_chord_box(sr_canvas *canvas, const kpa_fb_geom *geometry,
                           const kpa_fb_frame *frame, const kpa_fret_hand *hand,
                           bool has_hand, int x, int y, int width, int height)
{
    const uint32_t strings = geometry->strings;
    const int column = width / (int)(strings + 1u);
    const int mark_h = 7;
    int rows = (height - mark_h - 2) / 8;
    const uint32_t first = has_hand ? (uint32_t)hand->low : 1u;
    uint32_t api;
    int row;
    int grid_x;
    int grid_y;
    int row_h;
    char label[8];

    if (column < 5 || rows < 3) return;
    if (rows > KPA_FRET_HAND_FRETS) rows = KPA_FRET_HAND_FRETS;
    row_h = (height - mark_h - 2) / rows;
    if (row_h > 10) row_h = 10;
    grid_x = x + column;                    /* one column for the fret number */
    grid_y = y + mark_h + 2;

    for (row = 0; row <= rows; ++row) {
        const float line_y = (float)(grid_y + row * row_h);

        sr_line(canvas, (float)grid_x, line_y,
                (float)(grid_x + column * (int)(strings - 1u)), line_y, 1.0f,
                row == 0 ? KPA_FB_BONE : KPA_UI_FAINT, row == 0 ? 0.9f : 0.7f,
                0, 0);
    }
    for (api = 0u; api < strings; ++api) {
        const uint32_t display = string_display_row(api, strings,
                                                    geometry->low_on_top);
        const int sx = grid_x + (int)display * column;
        const kpa_fret_note *note = frame->ring[api];

        sr_line(canvas, (float)sx, (float)grid_y, (float)sx,
                (float)(grid_y + rows * row_h), 1.0f, KPA_UI_FAINT, 0.7f,
                0, 0);
        if (note == NULL) {
            sr_line(canvas, (float)sx - 2.0f, (float)y, (float)sx + 2.0f,
                    (float)(y + 5), 1.0f, KPA_UI_DIM, 0.8f, 0, 0);
            sr_line(canvas, (float)sx - 2.0f, (float)(y + 5), (float)sx + 2.0f,
                    (float)y, 1.0f, KPA_UI_DIM, 0.8f, 0, 0);
            continue;
        }
        if (note->fret == 0) {
            sr_ring(canvas, (float)sx, (float)(y + 3), 2.5f, 1.0f,
                    KPA_UI_TEXT, 0.9f);
            continue;
        }
        if ((uint32_t)note->fret >= first &&
            (uint32_t)note->fret < first + (uint32_t)rows) {
            const int offset = note->fret - (int)first;

            sr_fill_circle(canvas, (float)sx,
                           (float)(grid_y + offset * row_h + row_h / 2), 3.0f,
                           kpa_string_rgb[api < KPA_STRING_COUNT ? api : 0u],
                           1.0f);
        }
    }
    /* The fret the box starts at, or the shape means nothing. */
    if (has_hand) {
        (void)snprintf(label, sizeof label, "%d", (int)first);
        draw_text_fit(canvas, x, grid_y + row_h / 2 - 8, label, KPA_UI_DIM,
                      column + 2);
    }
}

/*
 * The callout: where the hand is, what is sounding, and where it has to go
 * next.  One 8x16 line, which is why the chord name is drawn at scale 1 and
 * carries its weight through ink rather than size - scale 2 is 32 px tall
 * and this band is 18.
 */
static void draw_callout(sr_canvas *canvas, const kpa_ui_model *model,
                         const kpa_fb_frame *frame, const kpa_fret_hand *hand,
                         bool has_hand, double when, int x, int y, int width,
                         uint32_t strings)
{
    char line[KPA_UI_LINE_CAPACITY];
    char chord[32];
    bool named = false;
    int cursor = x;
    int room;

    if (width <= 0) return;
    if (has_hand) {
        (void)snprintf(line, sizeof line, "pos %d", (int)hand->low);
    } else {
        (void)snprintf(line, sizeof line, "open");
    }
    draw_text_fit(canvas, cursor, y + 1, line, KPA_UI_DIM, 64);
    cursor += 64;

    /*
     * The latched label when the caller keeps one, and the label of what is
     * ringing right now when it does not.  A still has no history, so
     * main.c's one-frame render gets the honest unlatched answer with no
     * special case anywhere outside this line.
     */
    if (model->chord[0] != '\0') {
        (void)snprintf(chord, sizeof chord, "%s", model->chord);
        named = model->chord_kind != 0u;
    } else {
        int32_t pitches[KPA_FRET_MAX_PITCHES];
        const uint32_t count = fb_ring_pitches(frame, strings, pitches);

        kpa_ui_internal_chord_label(pitches, count, chord, sizeof chord,
                                    &named);
    }
    if (chord[0] != '\0') {
        draw_text_fit(canvas, cursor, y + 1, chord,
                      named ? KPA_UI_TEXT : KPA_UI_DIM, 120);
    }
    cursor += 128;
    room = x + width - cursor;
    if (room <= 0) return;
    if (has_hand) {
        const kpa_fb_move move = fb_next_move(model->tab, when, strings, hand);

        if (!move.found) return;
        if (move.chord[0] != '\0') {
            (void)snprintf(line, sizeof line, "-> pos %u, %s  in %.1fs",
                           (unsigned)move.anchor, move.chord,
                           move.when - when);
        } else {
            (void)snprintf(line, sizeof line, "-> pos %u  in %.1fs",
                           (unsigned)move.anchor, move.when - when);
        }
        draw_text_fit(canvas, cursor, y + 1, line, KPA_UI_WARN, room);
    }
}

/* The fret numbers under the neck, bright inside the hand's box and dim
 * outside it, which is how the neck itself says where the hand is. */
static void draw_fret_ruler(sr_canvas *canvas, const kpa_fb_geom *geometry,
                            const kpa_fret_hand *hand, bool has_hand, int y)
{
    uint32_t fret;

    for (fret = 1u; fret <= geometry->max_fret; ++fret) {
        const bool marked = kpa_fret_inlay_at((int32_t)fret) !=
                            KPA_FRET_INLAY_NONE;
        const bool inside = has_hand && (int32_t)fret >= hand->low &&
                            (int32_t)fret <= hand->high;
        const float cell = fb_cell_width(geometry, fret);
        char label[8];
        int width;

        /* Every fret while there is room for its number, and only the
         * marked frets once the cells are narrower than that. */
        if (cell < 20.0f && !marked && !inside) continue;
        (void)snprintf(label, sizeof label, "%u", (unsigned)fret);
        width = sr_text_width(label, 1);
        sr_text(canvas, neck_finger_x(geometry, fret) - (float)width * 0.5f,
                (float)y, label, inside ? KPA_UI_TEXT : KPA_UI_DIM, 1.0f, 1);
    }
}

/*
 * The whole fretboard block.  Clipped to its own rectangle for the same
 * reason the tab lane is: the clip, not the arithmetic inside it, is what
 * keeps a widget out of the band next to it.  The four clip values are saved
 * and put back by hand - never sr_canvas_reset_clip, which would throw away
 * the band protection kpa_ui_compose set.
 */
static void draw_fretboard(sr_canvas *canvas, const kpa_ui_model *model,
                           const kpa_practice_layout *layout)
{
    static const char *const default_labels[KPA_STRING_COUNT] = {
        "E", "A", "D", "G", "B", "e"
    };
    const int left = KPA_UI_MARGIN;
    const int right = canvas->w - KPA_UI_MARGIN;
    const int clip_x0 = canvas->clip_x0;
    const int clip_y0 = canvas->clip_y0;
    const int clip_x1 = canvas->clip_x1;
    const int clip_y1 = canvas->clip_y1;
    const double when = model->position;
    kpa_fb_geom geometry;
    kpa_fret_hand hand;
    /* On the stack deliberately: composition is a pure function of the
     * model, and an allocation that can fail is a branch that would make it
     * one of two pictures.  About seven kilobytes at KPA_FB_MAX_NOTES. */
    kpa_fb_frame frame;
    bool has_hand;
    uint32_t api;

    if (layout->fb_h <= 0 || right - left < KPA_FB_RAIL + 80) return;
    /* Zeroed before it is asked for: every reader below is guarded by
     * has_hand, and a struct that is only conditionally written is one
     * refactor away from being read anyway. */
    (void)memset(&hand, 0, sizeof hand);
    fb_geometry(model, layout, left, right, &geometry);
    (void)fb_collect(&frame, model->tab, when, model_ramp_seconds(model),
                     geometry.strings);
    has_hand = fb_hand(model->tab, when, &hand);

    sr_canvas_set_clip(canvas, left, layout->fb_y, right - left,
                       layout->fb_h);
    draw_neck_furniture(canvas, &geometry);
    if (has_hand) draw_hand_box(canvas, &geometry, &hand);
    for (api = 0u; api < geometry.strings; ++api) {
        if (frame.ring[api] == NULL) continue;
        draw_sounding_note(canvas, model, &geometry, frame.ring[api], api,
                           when);
    }
    if (layout->ramp_h > 0) {
        draw_ramp(canvas, model, &geometry, &frame, layout);
    }
    if (layout->ruler_y >= 0) {
        draw_fret_ruler(canvas, &geometry, &hand, has_hand, layout->ruler_y);
    }
    /* The rail: the chord shape above, the string names beside their own
     * strings.  Both at a constant x, outside everything that scrolls. */
    if (layout->neck_y - layout->callout_y >= 40) {
        draw_chord_box(canvas, &geometry, &frame, &hand, has_hand, left,
                       layout->callout_y + KPA_FB_CALLOUT_H, KPA_FB_RAIL - 6,
                       layout->neck_y - layout->callout_y - KPA_FB_CALLOUT_H);
    }
    for (api = 0u; api < geometry.strings; ++api) {
        const char *name = default_labels[api < KPA_STRING_COUNT ? api : 0u];
        char label[32];

        if (model->tab != NULL && api < KPA_STRING_COUNT &&
            model->tab->tuning_labels[api][0] != '\0') {
            name = model->tab->tuning_labels[api];
        }
        /* The number a player is told is the player's, never the index. */
        (void)snprintf(label, sizeof label, "%s %u", name,
                       (unsigned)player_string_number(api, geometry.strings));
        draw_text_fit(canvas, left, (int)neck_string_y(&geometry, api) - 8,
                      label, KPA_UI_DIM, KPA_FB_RAIL - 6);
    }
    draw_callout(canvas, model, &frame, &hand, has_hand, when,
                 left + KPA_FB_RAIL, layout->callout_y,
                 right - left - KPA_FB_RAIL, geometry.strings);
    if (frame.report.truncated) {
        draw_text_right(canvas, right, layout->callout_y + 1,
                        "more notes than this frame draws", KPA_UI_WARN, 260);
    }
    /* Restored to what the caller had, not to the canvas: the notice line
     * below this is still the caller's to draw. */
    sr_canvas_set_clip(canvas, clip_x0, clip_y0, clip_x1 - clip_x0,
                       clip_y1 - clip_y0);
}

/*
 * The rolling tab lane.
 *
 * Row 0 is the top of the lane and carries the highest string, which is what
 * a guitarist reading tab expects and what the API's low-E-first order is
 * not.  The gutter is drawn at a constant x and the note walk is clipped to
 * the lane, so the names cannot slide out of the frame as the song advances.
 */
static void draw_tab_lane(sr_canvas *canvas, const kpa_ui_model *model,
                          int top, int bottom)
{
    static const char *const default_labels[KPA_STRING_COUNT] = {
        "E", "A", "D", "G", "B", "e"
    };
    const kpa_tab *tab = model->tab;
    const int left = KPA_UI_MARGIN;
    const int right = canvas->w - KPA_UI_MARGIN;
    const int lane_x0 = left + KPA_UI_GUTTER;
    const int head_x = lane_x0 + (right - lane_x0) / 4;
    const int caption = top + KPA_UI_LINE;
    const int clip_x0 = canvas->clip_x0;
    const int clip_y0 = canvas->clip_y0;
    const int clip_x1 = canvas->clip_x1;
    const int clip_y1 = canvas->clip_y1;
    uint32_t strings = KPA_STRING_COUNT;
    uint32_t row;
    int row_h;
    char line[KPA_UI_LINE_CAPACITY];

    if (bottom - top < KPA_UI_LANE_MIN || right - lane_x0 < 64) return;
    if (tab != NULL && tab->string_count > 0u &&
        tab->string_count <= KPA_STRING_COUNT) {
        strings = tab->string_count;
    }
    row_h = (bottom - caption) / (int)strings;
    if (row_h < 8) return;

    (void)snprintf(line, sizeof line, "tab   %u events   max fret %u",
                   tab != NULL ? (unsigned)tab->event_count : 0u,
                   tab != NULL ? (unsigned)tab->max_fret : 0u);
    draw_text_fit(canvas, left, top + 1, line, KPA_UI_DIM, right - left);

    /*
     * Strings and their names, in the order the whole screen is using.
     * string_display_row maps an api index to a row and a row back to an
     * api index: both orders it offers are their own inverse, so one
     * function serves the lane's rows and the neck's alike.
     */
    for (row = 0u; row < strings; ++row) {
        const uint32_t api = string_display_row(row, strings,
                                                model->low_string_on_top);
        const int y = caption + (int)row * row_h + row_h / 2;
        const char *name = default_labels[api < KPA_STRING_COUNT ? api : 0u];

        if (tab != NULL && api < KPA_STRING_COUNT &&
            tab->tuning_labels[api][0] != '\0') {
            name = tab->tuning_labels[api];
        }
        sr_line(canvas, (float)lane_x0, (float)y, (float)right, (float)y,
                1.0f, KPA_UI_RAISED, 1.0f, 0, 0);
        /* The number a player is told is the player's, never the index. */
        (void)snprintf(line, sizeof line, "%s %u", name,
                       (unsigned)player_string_number(api, strings));
        draw_text_fit(canvas, left, y - 8, line, KPA_UI_DIM, KPA_UI_GUTTER - 4);
    }

    if (tab != NULL && tab->events != NULL && tab->positions != NULL) {
        const double window_left =
            model->position - (double)(head_x - lane_x0) / KPA_UI_LANE_PPS;
        const double window_right =
            model->position + (double)(right - head_x) / KPA_UI_LANE_PPS;
        uint32_t event = kpa_tab_first_after(tab, window_left);
        uint32_t drawn = 0u;

        /* Clipped to the lane so a note can never reach the gutter; the
         * clip is restored to the band the caller set, not to the canvas. */
        sr_canvas_set_clip(canvas, lane_x0, caption, right - lane_x0,
                           bottom - caption);
        for (; event < tab->event_count && drawn < KPA_UI_LANE_EVENTS;
             ++event, ++drawn) {
            const kpa_tab_event *item = &tab->events[event];
            uint32_t slot;

            if (item->start > window_right) break;
            for (slot = 0u; slot < item->position_count; ++slot) {
                const uint32_t at = item->first_position + slot;
                const kpa_tab_position *note;
                double end;
                float x0;
                float x1;
                uint32_t note_row;
                int y;
                uint32_t colour;
                bool sounding;
                char fret[8];

                if (at >= tab->position_count) break;
                note = &tab->positions[at];
                if ((uint32_t)note->string_index >= strings) continue;
                end = item->end > item->start ? item->end : item->start + 0.18;
                x0 = (float)head_x +
                     (float)((item->start - model->position) *
                             KPA_UI_LANE_PPS);
                x1 = (float)head_x +
                     (float)((end - model->position) * KPA_UI_LANE_PPS);
                if (x1 - x0 < 16.0f) x1 = x0 + 16.0f;
                /* The same mapping the rows above were laid out with, so
                 * the notes cannot end up in one order and the names in
                 * another - which is the bug the browser shipped. */
                note_row = string_display_row((uint32_t)note->string_index,
                                              strings,
                                              model->low_string_on_top);
                y = caption + (int)note_row * row_h + row_h / 2;
                /* The string's own colour, the same one the neck draws it
                 * in, so a note in the lane and the dot it becomes on the
                 * fretboard are visibly the same string.  Sounding now is
                 * the opaque one; what has not arrived yet is washed out. */
                sounding = model->position >= item->start &&
                           model->position <= end;
                colour = kpa_string_rgb[note->string_index <
                                        (int)KPA_STRING_COUNT
                             ? note->string_index : 0u];
                sr_fill_rect(canvas, x0, (float)(y - row_h / 2 + 2),
                             x1 - x0, (float)(row_h - 4), colour,
                             sounding ? 0.95f : 0.45f);
                (void)snprintf(fret, sizeof fret, "%u",
                               (unsigned)note->fret);
                sr_text(canvas, x0 + 3.0f, (float)(y - 8), fret,
                        KPA_UI_BACKDROP, 1.0f, 1);
            }
        }
        /* Restored to what the caller had, not to the band: draw_practice
         * still has a notice line to put below this. */
        sr_canvas_set_clip(canvas, clip_x0, clip_y0, clip_x1 - clip_x0,
                           clip_y1 - clip_y0);
    } else {
        draw_text_fit(canvas, lane_x0 + 8, caption + 2,
                      "no tab for this project", KPA_UI_DIM,
                      right - lane_x0 - 16);
    }
    /* The playhead last, over the notes it is passing. */
    sr_line(canvas, (float)head_x, (float)caption, (float)head_x,
            (float)bottom, 2.0f, KPA_UI_TEXT, 0.9f, 0, 0);
}

static void draw_practice(sr_canvas *canvas, const kpa_ui_model *model,
                          int top, int bottom)
{
    kpa_practice_layout layout;
    const int width = canvas->w - 2 * KPA_UI_MARGIN;

    practice_layout(model, top, bottom, width, &layout);
    if (layout.transport_y < 0) return;
    (void)draw_transport(canvas, model, layout.transport_y, bottom);
    if (layout.timeline_y >= 0) {
        (void)draw_timeline(canvas, model, layout.timeline_y, bottom);
    }
    if (layout.mixer_y >= 0) {
        if (layout.mixer_compact) {
            draw_mixer_strip(canvas, model, layout.mixer_y,
                             layout.mixer_y + layout.mixer_h);
        } else {
            (void)draw_mixer(canvas, model, layout.mixer_y,
                             layout.mixer_y + layout.mixer_h);
        }
    }
    if (layout.fb_h > 0) draw_fretboard(canvas, model, &layout);
    if (layout.lane_h > 0) {
        draw_tab_lane(canvas, model, layout.lane_y,
                      layout.lane_y + layout.lane_h);
    } else if (!model->tab_visible && layout.fb_h == 0 &&
               layout.mixer_y >= 0 &&
               layout.mixer_y + layout.mixer_h + KPA_UI_LINE <= bottom) {
        draw_text_fit(canvas, KPA_UI_MARGIN,
                      layout.mixer_y + layout.mixer_h + 2,
                      "tab hidden - t shows it   f shows the fretboard",
                      KPA_UI_DIM, width);
    }
    if (layout.notice_y >= 0) {
        /* The model's own notice first; a lane that was asked for and did
         * not fit is the surface's own sentence, and it names the key that
         * gives the player back whichever of the two they wanted. */
        if (model->notice[0] != '\0') {
            draw_text_fit(canvas, KPA_UI_MARGIN, layout.notice_y,
                          model->notice, KPA_UI_WARN, width);
        } else if (layout.lane_dropped) {
            draw_text_fit(canvas, KPA_UI_MARGIN, layout.notice_y,
                          "no room for both - f hides the neck, t hides the "
                          "lane", KPA_UI_DIM, width);
        }
    }
}

/* ----------------------------------------------------------- help view */

static const char *const help_lines[] = {
    "space      play / pause             c lead-in on / off   C 2s or 4s",
    "left right seek 5s, with shift 30s",
    "[ ]        set loop start / end      backspace clears the loop",
    "{ }        widen the loop start / end by 0.1s",
    "a A        jump to the loop start / end, or to the lyric cue either side",
    "1 .. 6     select a stem             m mute      s solo",
    "v          mute or unmute vocals     + - selected stem gain",
    ", .        practice rate down / up   (when a rate engine is present)",
    "r R        loop speed ramp 70/80/90% / reset the rate to 1.00",
    "f F        fretboard: neck+ramp, neck, off / look-ahead 1 to 4 seconds",
    "n o        note labels: fret, name, both / swap which string is on top",
    "k K        capo up / down            w note-density overview on the bar",
    "l          show or hide lyrics       t show or hide the tab lane",
    "tab        move focus                shift-tab moves it back",
    "escape     leave this view; from the library it leaves the player",
    "q          quit                      ? this page",
    "",
    "a shifted letter is always the same family as its unshifted one - the",
    "other direction, or the second axis - so shift can never turn a key",
    "into something unrelated.",
    "",
    "lyrics and vocals are separate: hiding the words never mutes the",
    "singer, and muting the singer never hides the words.  the same holds",
    "for the tab lane and the guitar stem."
};

/*
 * Where the key table ends and the prose begins.  It was a bare 10 in two
 * functions, which is a coupling that breaks silently the first time a line
 * is added above it - and this change added six.
 */
#define KPA_UI_HELP_KEY_LINES 16u

static void draw_help(sr_canvas *canvas, const kpa_ui_model *model,
                      int top, int bottom)
{
    const int left = KPA_UI_MARGIN;
    const int width = canvas->w - 2 * KPA_UI_MARGIN;
    const size_t count = sizeof help_lines / sizeof help_lines[0];
    size_t index;
    int y;

    if (width <= 0) return;
    sr_fill_rect(canvas, 0.0f, (float)top, (float)canvas->w,
                 (float)KPA_UI_ROW, KPA_UI_PANEL, 1.0f);
    draw_text_fit(canvas, left, top + 3, "keys", KPA_UI_TEXT, width);
    draw_text_right(canvas, canvas->w - KPA_UI_MARGIN, top + 3,
                    model->rate_available
                        ? "rate control available"
                        : "no pitch-preserving rate engine in this build",
                    KPA_UI_DIM, width / 2);
    y = top + KPA_UI_ROW + 6;
    for (index = 0u; index < count; ++index) {
        if (y + KPA_UI_LINE > bottom) break;
        draw_text_fit(canvas, left, y, help_lines[index],
                      index < KPA_UI_HELP_KEY_LINES ? KPA_UI_TEXT
                                                    : KPA_UI_DIM, width);
        y += KPA_UI_LINE;
    }
}

/* ------------------------------------------------------------- compose */

void kpa_ui_compose(sr_canvas *canvas, const kpa_ui_model *model)
{
    int top;
    int bottom;
    int y0;
    int y1;

    if (canvas == NULL || canvas->px == NULL || canvas->w <= 0 ||
        canvas->h <= 0) {
        return;
    }
    /*
     * Every pixel is written on every call, the reserved bands included, so
     * a canvas reused for a second model cannot show a trace of the first.
     * Transparent black is also the right value for those bands: the
     * framebuffer sits behind the text plane, so a transparent row shows the
     * terminal's own background under the foreground cells.
     */
    (void)memset(canvas->px, 0,
                 (size_t)canvas->w * (size_t)canvas->h * sizeof *canvas->px);
    sr_canvas_reset_clip(canvas);
    if (model == NULL) return;
    /* cell_only draws nothing at all: the transport, the mixer, the tab and
     * the lyrics all reach the player as terminal cells. */
    if (model->cell_only) return;

    reserved_bands(model, canvas->h, &top, &bottom);
    y0 = top;
    y1 = canvas->h - bottom;
    if (y1 <= y0) return;

    /* The clip, not the arithmetic below it, is what keeps every widget out
     * of the rows the cell overlay owns. */
    sr_canvas_set_clip(canvas, 0, y0, canvas->w, y1 - y0);
    sr_fill_rect(canvas, 0.0f, (float)y0, (float)canvas->w, (float)(y1 - y0),
                 KPA_UI_BACKDROP, 1.0f);
    switch (model->view) {
    case KPA_VIEW_LIBRARY:
        draw_library(canvas, model, y0, y1);
        break;
    case KPA_VIEW_PRACTICE:
        draw_practice(canvas, model, y0, y1);
        break;
    case KPA_VIEW_HELP:
        draw_help(canvas, model, y0, y1);
        break;
    default:
        break;
    }
    sr_canvas_reset_clip(canvas);
}

/* -------------------------------------------------------- cell layout */

void kpa_ui_cell_layout_get(const kpa_ui_model *model, int columns, int rows,
                            int cell_height, kpa_ui_cell_layout *out)
{
    int top;
    int bottom;
    int height;
    int claimed;
    int lyric_rows;
    int lowest;

    if (out == NULL) return;
    out->title_row = -1;
    out->lyric_row = -1;
    out->lyric_row_count = 0;
    out->status_row = -1;
    out->columns = columns > 0 ? columns : 0;
    out->rows = rows > 0 ? rows : 0;
    if (model == NULL || columns <= 0 || rows <= 0 || cell_height <= 0) {
        return;
    }
    if (rows > INT_MAX / cell_height) return;

    if (model->cell_only) {
        /* No pixel layer exists, so every row belongs to the overlay. */
        out->title_row = 0;
        out->status_row = rows - 1;
        if (rows > 2) {
            out->lyric_row = 1;
            out->lyric_row_count = rows - 2;
        }
        return;
    }

    /*
     * The canvas is rows * cell_height tall, and may be up to one cell
     * taller when a caller sized it in pixels rather than in cells.  The
     * top band is anchored at y = 0 and both bands are non-decreasing in
     * height, so a row claimed at the top is safe outright; a row claimed
     * from the bottom is measured against a band whose top edge moves down
     * with the extra pixels, so one cell row of slack is dropped there.
     * Without it, a canvas that is not a whole number of cells tall could
     * put a claimed row inside the drawn area.
     */
    height = rows * cell_height;
    reserved_bands(model, height, &top, &bottom);
    if (cell_height <= top) out->title_row = 0;
    if (bottom < cell_height) return;
    claimed = (bottom - cell_height) / cell_height;
    if (claimed < 1) return;
    out->status_row = rows - 1;
    if (!model_has_lyric_band(model)) return;

    lyric_rows = claimed - 1;
    if (lyric_rows > KPA_UI_MAX_LYRIC_ROWS) lyric_rows = KPA_UI_MAX_LYRIC_ROWS;
    lowest = out->title_row >= 0 ? 1 : 0;
    if (rows - 1 - lyric_rows < lowest) lyric_rows = rows - 1 - lowest;
    if (lyric_rows <= 0) return;
    out->lyric_row = rows - 1 - lyric_rows;
    out->lyric_row_count = lyric_rows;
}

/* ------------------------------------------------------------ still ---- */

int kpa_ui_render_ppm(const kpa_ui_model *model, int width, int height,
                      const char *path)
{
    sr_canvas canvas;

    if (model == NULL || path == NULL || width <= 0 || height <= 0) return 1;
    if (!sr_canvas_init(&canvas, width, height)) return 1;
    kpa_ui_compose(&canvas, model);
    /* Alpha is dropped by the PPM, so the rows the cell overlay owns appear
     * black here.  On a terminal they are transparent and the lyric cells
     * are drawn into them; a still of the pixel layer cannot show that. */
    if (!sr_write_ppm(&canvas, path)) {
        sr_canvas_free(&canvas);
        return 1;
    }
    sr_canvas_free(&canvas);
    return 0;
}

/* --------------------------------------------------------- the key table */

/* First track of this kind, or KPA_MAX_TRACKS when the project has none. */
static uint32_t track_of_kind(const kpa_ui_model *model, const char *kind)
{
    uint32_t index;

    for (index = 0u; index < model->track_count &&
                     index < KPA_MAX_TRACKS; ++index) {
        if (strcmp(model->tracks[index].kind, kind) == 0) return index;
    }
    return KPA_MAX_TRACKS;
}

static void set_notice(kpa_ui_model *model, const char *text)
{
    (void)snprintf(model->notice, sizeof model->notice, "%s", text);
}

static int apply_key_library(kpa_ui_model *model, uint32_t key, bool shift)
{
    const uint32_t count = model->summary_count;

    if (count == 0u) {
        if (key == KITTYKB_KEY_ENTER) set_notice(model, "no projects to open");
        return KPA_UI_KEY_HANDLED;
    }
    if (model->selected_project >= count) model->selected_project = 0u;
    switch (key) {
    case KITTYKB_KEY_TAB:
    case KITTYKB_KEY_DOWN:
        if (shift && key == KITTYKB_KEY_TAB) {
            model->selected_project =
                (model->selected_project + count - 1u) % count;
        } else {
            model->selected_project = (model->selected_project + 1u) % count;
        }
        break;
    case KITTYKB_KEY_UP:
        model->selected_project =
            (model->selected_project + count - 1u) % count;
        break;
    case KITTYKB_KEY_ENTER:
        return KPA_UI_KEY_OPEN;
    default:
        break;
    }
    return KPA_UI_KEY_HANDLED;
}

static void adjust_rate(kpa_ui_model *model, double delta)
{
    if (!model->rate_available) {
        /* Said plainly rather than moved silently: a guitar part transposed
         * by a semitone is worse than one that plays at full speed. */
        set_notice(model, "no pitch-preserving rate engine in this build");
        return;
    }
    model->rate = clamp_double(model->rate + delta, KPA_UI_RATE_MIN,
                               KPA_UI_RATE_MAX);
}

static void toggle_mute(kpa_ui_model *model, uint32_t track)
{
    if (track >= model->track_count || track >= KPA_MAX_TRACKS) return;
    model->tracks[track].muted = !model->tracks[track].muted;
}

/*
 * The look-ahead windows the ramp offers.  Measured on the audited song, a
 * 2.0 s window carries a median of 17 marks and at most 32, which is
 * comfortable; 4.0 s reaches about 60 and is visibly busy.  Which of those
 * a player wants is theirs to decide, which is why this is a key and not a
 * constant.
 */
static float next_ramp_seconds(float current)
{
    static const float steps[5] = {1.0f, 1.5f, 2.0f, 3.0f, 4.0f};
    size_t index;

    for (index = 0u; index < 5u; ++index) {
        if (current < steps[index] - 0.01f) return steps[index];
    }
    return steps[0];
}

/* Positions the capo makes unplayable: they are on the neck behind it. */
static uint32_t notes_below_capo(const kpa_tab *tab, uint32_t capo)
{
    uint32_t index;
    uint32_t count = 0u;

    if (tab == NULL || tab->positions == NULL || capo == 0u) return 0u;
    for (index = 0u; index < tab->position_count; ++index) {
        const uint32_t fret = tab->positions[index].fret;

        if (fret > 0u && fret < capo) ++count;
    }
    return count;
}

/*
 * Jump to the cue either side of where the player is.  The step back is
 * measured from a moment slightly before now, so pressing it twice moves
 * two lines back rather than landing on the current line for ever.
 */
static void jump_to_cue(kpa_ui_model *model, bool forward)
{
    const kpa_lyrics *lyrics = model->lyrics;
    uint32_t index;

    if (lyrics == NULL || lyrics->cues == NULL || lyrics->cue_count == 0u) {
        set_notice(model, "this project has no lyric cues to jump between");
        return;
    }
    if (forward) {
        for (index = 0u; index < lyrics->cue_count; ++index) {
            if (lyrics->cues[index].start > model->position + 0.01) {
                model->position = clamp_double(lyrics->cues[index].start, 0.0,
                                               model->duration);
                return;
            }
        }
        set_notice(model, "no cue after this one");
        return;
    }
    for (index = lyrics->cue_count; index > 0u; --index) {
        if (lyrics->cues[index - 1u].start < model->position - 0.25) {
            model->position = clamp_double(lyrics->cues[index - 1u].start,
                                           0.0, model->duration);
            return;
        }
    }
    model->position = 0.0;
}

static int apply_key_practice(kpa_ui_model *model, uint32_t key, bool shift)
{
    const double step = shift ? KPA_UI_SEEK_STEP_LARGE : KPA_UI_SEEK_STEP;
    const uint32_t count = model->track_count;
    kpa_ui_track *track = NULL;

    if (count > 0u) {
        if (model->selected_track >= count) model->selected_track = 0u;
        if (model->selected_track < KPA_MAX_TRACKS) {
            track = &model->tracks[model->selected_track];
        }
    }
    switch (key) {
    case ' ':
        /*
         * The lead-in is a count-in with no click track: starting playback
         * rewinds into the song's own audio so the player hears the bar
         * before the one they are practising.  This build has no metronome
         * and does not pretend to; it never rewinds past the loop start,
         * because the engine would wrap straight back out of there.
         */
        if (!model->playing && model->lead_in > 0u) {
            const double floor_at = model->loop_active ? model->loop_start
                                                       : 0.0;

            model->position = clamp_double(model->position -
                                           (double)model->lead_in,
                                           floor_at, model->duration);
        }
        model->playing = !model->playing;
        break;
    case KITTYKB_KEY_LEFT:
        model->position = clamp_double(model->position - step, 0.0,
                                       model->duration);
        break;
    case KITTYKB_KEY_RIGHT:
        model->position = clamp_double(model->position + step, 0.0,
                                       model->duration);
        break;
    case '[':
        model->loop_start = clamp_double(model->position, 0.0,
                                         model->duration);
        if (model->loop_end <= model->loop_start) {
            model->loop_end = clamp_double(model->loop_start + 1.0, 0.0,
                                           model->duration);
        }
        model->loop_active = model->loop_end > model->loop_start;
        break;
    case ']':
        model->loop_end = clamp_double(model->position, 0.0,
                                       model->duration);
        model->loop_active = model->loop_end > model->loop_start;
        if (!model->loop_active) {
            set_notice(model, "a loop end must come after its start");
        }
        break;
    case KITTYKB_KEY_BACKSPACE:
        model->loop_active = false;
        model->loop_start = 0.0;
        model->loop_end = 0.0;
        break;
    case '1': case '2': case '3': case '4': case '5': case '6':
        if (key - (uint32_t)'1' < count) {
            model->selected_track = key - (uint32_t)'1';
        } else {
            set_notice(model, "this project has no such stem");
        }
        break;
    case KITTYKB_KEY_TAB:
        if (count > 0u) {
            model->selected_track = shift
                ? (model->selected_track + count - 1u) % count
                : (model->selected_track + 1u) % count;
        }
        break;
    case 'm':
        toggle_mute(model, model->selected_track);
        break;
    case 's':
        if (track != NULL) track->soloed = !track->soloed;
        break;
    case 'v': {
        /* An audio change and only an audio change: the lyric layer is not
         * consulted here and is not touched. */
        const uint32_t vocal = track_of_kind(model, "vocals");

        if (vocal >= KPA_MAX_TRACKS) {
            set_notice(model, "this project has no vocal stem");
        } else {
            toggle_mute(model, vocal);
        }
        break;
    }
    case 'l':
        /* A display change and only a display change: no track is touched. */
        model->lyrics_visible = !model->lyrics_visible;
        break;
    case 't':
        model->tab_visible = !model->tab_visible;
        /*
         * As text there are not rows for both pictures of the guitar, so
         * the request that just arrived wins and says what it took.  Only
         * on the press that turns this layer ON: cycling the other one is
         * not a request for this one, and a key that quietly switched
         * something off every time it was pressed would be worse than the
         * crowding it is avoiding.
         */
        if (model->cell_only && model->tab_visible &&
            model->fretboard != KPA_FB_OFF) {
            model->fretboard = KPA_FB_OFF;
            set_notice(model, "the neck is hidden - as text these rows hold "
                              "one of the two");
        }
        break;
    /*
     * From here down, the keys the fretboard added.  One rule holds across
     * all of them, and it is why the file lowercases A-Z rather than
     * treating a shifted letter as a key of its own: a shifted letter is
     * always the SAME FAMILY as its unshifted one - the opposite direction,
     * or the second axis of the same thing - so shift can never silently
     * turn a key into something unrelated.  Tab and Shift-Tab already set
     * that precedent.
     */
    case 'f': {
        const bool was_off = model->fretboard == KPA_FB_OFF;

        if (shift) {
            model->ramp_seconds = next_ramp_seconds(model_ramp_seconds(model));
            break;
        }
        model->fretboard = (kpa_fretboard_mode)((model->fretboard + 1) % 3);
        /* See `t` above: only the press that turns the neck back on takes
         * the lane's rows, never the ones that cycle the ramp off. */
        if (model->cell_only && was_off && model->fretboard != KPA_FB_OFF &&
            model->tab_visible) {
            model->tab_visible = false;
            set_notice(model, "the lane is hidden - as text these rows hold "
                              "one of the two");
        }
        break;
    }
    case 'n':
        model->note_label = (kpa_note_label)((model->note_label + 1) % 3);
        break;
    case 'o':
        /* One key, both pictures: the neck, the lane and the cell-only tab
         * all read the same preference. */
        model->low_string_on_top = !model->low_string_on_top;
        break;
    case 'k': {
        uint32_t behind;

        if (shift) {
            model->capo = model->capo == 0u ? 7u
                                            : (uint8_t)(model->capo - 1u);
        } else {
            model->capo = model->capo >= 7u ? 0u
                                            : (uint8_t)(model->capo + 1u);
        }
        behind = notes_below_capo(model->tab, model->capo);
        if (behind > 0u) {
            char line[KPA_TEXT_CAPACITY];

            (void)snprintf(line, sizeof line,
                           "capo %u - %u note%s in this part sit behind it",
                           (unsigned)model->capo, (unsigned)behind,
                           behind == 1u ? "" : "s");
            set_notice(model, line);
        }
        break;
    }
    case 'c':
        if (shift) {
            model->lead_in = model->lead_in == 4u ? 2u : 4u;
            break;
        }
        model->lead_in = model->lead_in > 0u ? 0u : 2u;
        break;
    case 'r':
        if (!model->rate_available) {
            /* The existing path: it says why and changes nothing. */
            adjust_rate(model, 0.0);
            break;
        }
        if (shift) {
            model->speed_ramp = 0u;
            model->rate = 1.0;
            break;
        }
        /*
         * The ramp starts a loop slow and adds five points of rate every
         * time it comes round, up to full speed.  The wrap is detected by
         * the surface's own refresh, not here: a key table that watched the
         * clock would stop being a pure function of the model.
         */
        if (model->speed_ramp == 0u) {
            model->speed_ramp = 70u;
        } else if (model->speed_ramp < 90u) {
            model->speed_ramp = (uint8_t)(model->speed_ramp + 10u);
        } else {
            model->speed_ramp = 0u;
        }
        model->rate = model->speed_ramp > 0u
            ? (double)model->speed_ramp / 100.0 : 1.0;
        if (!model->loop_active) {
            set_notice(model, "the speed ramp steps up each time a loop comes "
                              "round - set one with [ and ]");
        }
        break;
    case 'a':
        if (model->loop_active) {
            model->position = clamp_double(shift ? model->loop_end
                                                 : model->loop_start,
                                           0.0, model->duration);
            break;
        }
        jump_to_cue(model, shift);
        break;
    case '{':
        model->loop_start = clamp_double(model->loop_start - 0.1, 0.0,
                                         model->duration);
        model->loop_active = model->loop_end > model->loop_start;
        break;
    case '}':
        model->loop_end = clamp_double(model->loop_end + 0.1, 0.0,
                                       model->duration);
        model->loop_active = model->loop_end > model->loop_start;
        break;
    case 'w':
        model->overview = model->overview == KPA_OVERVIEW_DENSITY
            ? KPA_OVERVIEW_PLAIN : KPA_OVERVIEW_DENSITY;
        break;
    case '+': case '=':
        if (track != NULL) {
            track->gain = clamp_float(track->gain + KPA_UI_GAIN_STEP, 0.0f,
                                      KPA_UI_GAIN_MAX);
        }
        break;
    case '-': case '_':
        if (track != NULL) {
            track->gain = clamp_float(track->gain - KPA_UI_GAIN_STEP, 0.0f,
                                      KPA_UI_GAIN_MAX);
        }
        break;
    case ',': case '<':
        adjust_rate(model, -KPA_UI_RATE_STEP);
        break;
    case '.': case '>':
        adjust_rate(model, KPA_UI_RATE_STEP);
        break;
    default:
        break;
    }
    return KPA_UI_KEY_HANDLED;
}

int kpa_ui_internal_apply_key(kpa_ui_model *model, uint32_t key,
                              uint32_t modifiers)
{
    bool shift = (modifiers & (uint32_t)KITTYKB_MOD_SHIFT) != 0u;

    if (model == NULL) return KPA_UI_KEY_HANDLED;
    /* A keystroke is the answer to whatever the last one had to say. */
    model->notice[0] = '\0';
    if (key >= (uint32_t)'A' && key <= (uint32_t)'Z') {
        /* A terminal with no keyboard protocol reports the shifted
         * character rather than the key plus a modifier, so M and m have to
         * be the same key or shift would silently disable half the table. */
        key += (uint32_t)'a' - (uint32_t)'A';
        shift = true;
    }

    if (key == 'q') return KPA_UI_KEY_QUIT;
    if (key == '?' || (key == '/' && shift)) {
        model->view = model->view == KPA_VIEW_HELP
            ? (model->title != NULL ? KPA_VIEW_PRACTICE : KPA_VIEW_LIBRARY)
            : KPA_VIEW_HELP;
        return KPA_UI_KEY_HANDLED;
    }
    if (key == KITTYKB_KEY_ESCAPE) {
        /*
         * Escape leaves a view, and from the outermost view it leaves the
         * player.  It never becomes a key that does nothing: that is what
         * trapping the Kilix escape path would look like from the inside.
         */
        switch (model->view) {
        case KPA_VIEW_HELP:
            model->view = model->title != NULL ? KPA_VIEW_PRACTICE
                                               : KPA_VIEW_LIBRARY;
            return KPA_UI_KEY_HANDLED;
        case KPA_VIEW_PRACTICE:
            model->view = KPA_VIEW_LIBRARY;
            return KPA_UI_KEY_CLOSE;
        default:
            return KPA_UI_KEY_QUIT;
        }
    }
    switch (model->view) {
    case KPA_VIEW_LIBRARY:
        return apply_key_library(model, key, shift);
    case KPA_VIEW_PRACTICE:
        return apply_key_practice(model, key, shift);
    default:
        return KPA_UI_KEY_HANDLED;
    }
}

/* ------------------------------------------------------------- runtime */

/* The terminal grid the overlay writes into, in cells. */
typedef struct kpa_ui_grid {
    int columns;
    int rows;
    int cell_height;
    int origin_row;      /* 1-based terminal row holding canvas row 0 */
    int origin_column;
} kpa_ui_grid;

static bool row_is_claimed(const kpa_ui_cell_layout *layout, int row)
{
    if (row < 0) return false;
    if (row == layout->title_row || row == layout->status_row) return true;
    return layout->lyric_row >= 0 && row >= layout->lyric_row &&
           row < layout->lyric_row + layout->lyric_row_count;
}

static bool same_grid(const kpa_ui_grid *left, const kpa_ui_grid *right)
{
    return left->columns == right->columns && left->rows == right->rows &&
           left->origin_row == right->origin_row &&
           left->origin_column == right->origin_column;
}

typedef struct kpa_ui_mix {
    bool playing;
    double position;
    double rate;
    bool loop_active;
    double loop_start;
    double loop_end;
    float gain[KPA_MAX_TRACKS];
    bool muted[KPA_MAX_TRACKS];
    bool soloed[KPA_MAX_TRACKS];
} kpa_ui_mix;

typedef struct kpa_ui_runtime {
    kpa_project project;
    kpa_lyrics lyrics;
    kpa_tab tab;
    bool project_open;
    bool lyrics_loaded;
    bool tab_loaded;

    kpa_audio_session *audio;
    /*
     * Indexed by the model's track number, not by the order the session
     * accepted them: a stem that failed to decode leaves a hole, and a
     * packed array would slide every stem after it by one and mute the
     * wrong instrument.
     */
    kpa_track_id track_ids[KPA_MAX_TRACKS];
    bool track_live[KPA_MAX_TRACKS];
    uint32_t audio_tracks;
    uint32_t output_rate;

    kittyts_session session;
    bool session_started;
    kpa_cells_writer *cells;
    int cell_fd;
    bool cell_fd_owned;

    sr_canvas canvas;
    bool canvas_live;
    uint8_t *rgba;
    size_t rgba_size;

    kpa_project_summary summaries[64];
    uint32_t summary_count;

    /* What the overlay owned last frame, so a row it gives up is erased.
     * The framebuffer sits behind the text plane, so a stale foreground cell
     * would otherwise stay legible over the pixels that replaced it. */
    kpa_ui_cell_layout drawn;
    kpa_ui_grid drawn_grid;
    bool drawn_valid;

    kpa_ui_model model;
    /* The model the screen is currently showing.  A paused player whose
     * model has not changed does not need the frame sent again, and this is
     * what lets the loop keep polling keys without repainting. */
    kpa_ui_model shown;
    bool shown_valid;
    int seek_hold;      /* frames the display holds a requested position */
    bool signals_installed;

    /*
     * Derived state that is history rather than model: the chord label's
     * latch and the loop's rate ramp.  Both are kept here and not in the
     * model, because everything in the model has to be drawable from itself
     * alone - that is what makes kpa_ui_compose a pure function of it.
     */
    char chord_candidate[24];
    double candidate_since;
    double last_position;
    bool last_position_valid;
} kpa_ui_runtime;

/*
 * The signal path.  The pointer is published before the handlers are
 * installed and the handlers are removed before it is cleared, so the handler
 * never sees a pointer that is being changed underneath it.
 * kittyts_emergency_restore is async-signal-safe and gets the terminal back;
 * the loop then unwinds through the ordinary path, which still calls
 * kittyts_stop.
 */
static kittyts_session *ui_signal_session;
static volatile sig_atomic_t ui_signal_seen;

static void ui_signal_handler(int number)
{
    (void)number;
    ui_signal_seen = 1;
    if (ui_signal_session != NULL) {
        kittyts_emergency_restore(ui_signal_session);
    }
}

static void install_signal_handlers(kittyts_session *session, bool install)
{
    struct sigaction action;
    int signals[2];
    size_t index;

    signals[0] = SIGINT;
    signals[1] = SIGTERM;
    (void)memset(&action, 0, sizeof action);
    if (install) {
        ui_signal_session = session;
        ui_signal_seen = 0;
        action.sa_handler = ui_signal_handler;
    } else {
        action.sa_handler = SIG_DFL;
    }
    (void)sigemptyset(&action.sa_mask);
    /* No SA_RESTART: poll() has to come back so the loop can see the flag. */
    for (index = 0u; index < 2u; ++index) {
        (void)sigaction(signals[index], &action, NULL);
    }
    if (!install) ui_signal_session = NULL;
}

/* ------------------------------------------------------- audio plumbing */

static double frames_to_seconds(uint64_t frames, uint32_t rate)
{
    if (rate == 0u) return 0.0;
    return (double)frames / (double)rate;
}

static uint64_t seconds_to_frames(double seconds, uint32_t rate)
{
    if (rate == 0u || !(seconds > 0.0)) return 0u;
    return (uint64_t)(seconds * (double)rate);
}

static void capture_mix(const kpa_ui_model *model, kpa_ui_mix *out)
{
    uint32_t index;

    (void)memset(out, 0, sizeof *out);
    out->playing = model->playing;
    out->position = model->position;
    out->rate = model->rate;
    out->loop_active = model->loop_active;
    out->loop_start = model->loop_start;
    out->loop_end = model->loop_end;
    for (index = 0u; index < KPA_MAX_TRACKS; ++index) {
        out->gain[index] = model->tracks[index].gain;
        out->muted[index] = model->tracks[index].muted;
        out->soloed[index] = model->tracks[index].soloed;
    }
}

/*
 * Turn the difference a key made into audio calls.  The key table never
 * reaches the session itself, which is what keeps "display state" and "audio
 * state" from becoming one mutable thing that a later change can tangle.
 */
static void apply_mix(kpa_ui_runtime *rt, const kpa_ui_mix *before,
                      const kpa_ui_mix *after)
{
    kpa_ui_model *model = &rt->model;
    uint32_t index;

    if (rt->audio == NULL) return;
    for (index = 0u; index < KPA_MAX_TRACKS; ++index) {
        if (!rt->track_live[index]) continue;
        if (after->gain[index] != before->gain[index]) {
            (void)kpa_audio_set_gain(rt->audio, rt->track_ids[index],
                                     after->gain[index]);
        }
        if (after->muted[index] != before->muted[index]) {
            (void)kpa_audio_set_muted(rt->audio, rt->track_ids[index],
                                      after->muted[index]);
        }
        if (after->soloed[index] != before->soloed[index]) {
            (void)kpa_audio_set_soloed(rt->audio, rt->track_ids[index],
                                       after->soloed[index]);
        }
    }
    if (after->position != before->position) {
        const uint64_t frame = seconds_to_frames(after->position,
                                                 rt->output_rate);

        if (kpa_audio_seek(rt->audio, frame) == KPA_AUDIO_OK) {
            /* Hold the requested position on screen until the audible clock
             * catches up, or the cursor snaps back for a moment. */
            rt->seek_hold = KPA_UI_SEEK_HOLD_FRAMES;
        }
    }
    if (after->loop_active != before->loop_active ||
        after->loop_start != before->loop_start ||
        after->loop_end != before->loop_end) {
        (void)kpa_audio_set_loop(
            rt->audio,
            after->loop_active ? seconds_to_frames(after->loop_start,
                                                   rt->output_rate) : 0u,
            after->loop_active ? seconds_to_frames(after->loop_end,
                                                   rt->output_rate) : 0u);
    }
    if (after->rate != before->rate) {
        const kpa_audio_result result = kpa_audio_set_rate(rt->audio,
                                                           after->rate);

        if (result == KPA_AUDIO_RATE_UNAVAILABLE) {
            /* Believe the engine over the model: the rate did not change. */
            model->rate = before->rate;
            model->rate_available = false;
            set_notice(model, "this build has no pitch-preserving rate engine");
        } else if (result != KPA_AUDIO_OK) {
            model->rate = before->rate;
            set_notice(model, kpa_audio_result_name(result));
        }
    }
    if (after->playing != before->playing) {
        const kpa_audio_result result = after->playing
            ? kpa_audio_play(rt->audio) : kpa_audio_pause(rt->audio);

        if (result != KPA_AUDIO_OK) {
            model->playing = before->playing;
            set_notice(model, kpa_audio_result_name(result));
        }
    }
}

/* ------------------------------------------------ project open and close */

static void runtime_close_project(kpa_ui_runtime *rt)
{
    kpa_audio_destroy(rt->audio);
    rt->audio = NULL;
    rt->audio_tracks = 0u;
    rt->output_rate = 0u;
    rt->seek_hold = 0;
    (void)memset(rt->track_live, 0, sizeof rt->track_live);
    (void)memset(rt->track_ids, 0, sizeof rt->track_ids);
    /* Both loaders zero their struct on failure and both frees are safe on a
     * zeroed struct, so this is the single free path for either outcome. */
    kpa_lyrics_free(&rt->lyrics);
    kpa_tab_free(&rt->tab);
    rt->lyrics_loaded = false;
    rt->tab_loaded = false;
    if (rt->project_open) {
        kpa_project_close(&rt->project);
        rt->project_open = false;
    }
    rt->model.title = NULL;
    rt->model.artist = NULL;
    rt->model.lyrics = NULL;
    rt->model.tab = NULL;
    rt->model.track_count = 0u;
    rt->model.selected_track = 0u;
    rt->model.duration = 0.0;
    rt->model.position = 0.0;
    rt->model.playing = false;
    rt->model.loop_active = false;
    rt->model.loop_start = 0.0;
    rt->model.loop_end = 0.0;
    rt->model.active_cue = -1;
    rt->model.active_word = -1;
    (void)memset(rt->model.tracks, 0, sizeof rt->model.tracks);
}

/*
 * Stems are opened through kpa_project_open_artifact and handed to the audio
 * session as descriptors.  Building a path and opening it here would move the
 * decision about what a manifest may name out of the reader that validates
 * it, which is the whole of the security boundary.
 */
static void runtime_open_audio(kpa_ui_runtime *rt)
{
    kpa_audio_options options;
    uint32_t index;

    (void)memset(rt->track_live, 0, sizeof rt->track_live);
    kpa_audio_options_init(&options);
    if (kpa_audio_create(&rt->audio, &options) != KPA_AUDIO_OK) {
        rt->audio = NULL;
        set_notice(&rt->model, "no audio device; the surface is silent");
        return;
    }
    for (index = 0u; index < rt->project.track_count &&
                     index < KPA_MAX_TRACKS; ++index) {
        const kpa_track *track = &rt->project.tracks[index];
        kpa_result error = KPA_OK;
        kpa_track_id id = 0u;
        int fd = kpa_project_open_artifact(&rt->project, track->path, &error);

        if (fd < 0) {
            set_notice(&rt->model, kpa_result_name(error));
            continue;
        }
        if (kpa_audio_add_track(rt->audio, fd, &id) == KPA_AUDIO_OK) {
            rt->track_ids[index] = id;
            rt->track_live[index] = true;
            rt->audio_tracks++;
        } else {
            set_notice(&rt->model, "a stem could not be decoded");
        }
        (void)close(fd);
    }
    if (rt->audio_tracks == 0u) {
        kpa_audio_destroy(rt->audio);
        rt->audio = NULL;
        (void)memset(rt->track_live, 0, sizeof rt->track_live);
        set_notice(&rt->model, "no playable stems in this project");
    }
}

static int runtime_open_project(kpa_ui_runtime *rt, const char *project_id)
{
    kpa_ui_model *model = &rt->model;
    kpa_result result;
    uint32_t index;

    runtime_close_project(rt);
    if (project_id == NULL || !kpa_project_id_valid(project_id)) {
        set_notice(model, "not a project id");
        return -1;
    }
    result = kpa_project_open(&rt->project, project_id);
    if (result != KPA_OK) {
        set_notice(model, kpa_result_name(result));
        return -1;
    }
    rt->project_open = true;
    if (rt->project.has_lyrics &&
        kpa_lyrics_load(&rt->project, &rt->lyrics) == KPA_OK) {
        rt->lyrics_loaded = true;
    }
    if (rt->project.has_tab &&
        kpa_tab_load(&rt->project, &rt->tab) == KPA_OK) {
        rt->tab_loaded = true;
    }
    runtime_open_audio(rt);

    model->title = rt->project.title;
    model->artist = rt->project.artist;
    model->duration = rt->project.duration;
    model->position = 0.0;
    model->playing = false;
    model->rate = 1.0;
    model->rate_available = kpa_audio_rate_available(rt->audio);
    model->lyrics = rt->lyrics_loaded ? &rt->lyrics : NULL;
    model->tab = rt->tab_loaded ? &rt->tab : NULL;
    model->active_cue = -1;
    model->active_word = -1;
    model->track_count = rt->project.track_count < KPA_MAX_TRACKS
        ? rt->project.track_count : KPA_MAX_TRACKS;
    for (index = 0u; index < model->track_count; ++index) {
        const kpa_track *track = &rt->project.tracks[index];

        (void)snprintf(model->tracks[index].label,
                       sizeof model->tracks[index].label, "%s", track->label);
        (void)snprintf(model->tracks[index].kind,
                       sizeof model->tracks[index].kind, "%s", track->kind);
        model->tracks[index].gain = 1.0f;
        model->tracks[index].muted = track->default_muted;
        model->tracks[index].soloed = false;
        if (rt->audio != NULL && rt->track_live[index] &&
            track->default_muted) {
            (void)kpa_audio_set_muted(rt->audio, rt->track_ids[index], true);
        }
    }
    model->selected_track = 0u;
    model->view = KPA_VIEW_PRACTICE;
    return 0;
}

static void runtime_refresh_library(kpa_ui_runtime *rt)
{
    const uint32_t capacity =
        (uint32_t)(sizeof rt->summaries / sizeof rt->summaries[0]);

    rt->summary_count = 0u;
    (void)kpa_project_list(rt->summaries, capacity, &rt->summary_count);
    rt->model.summaries = rt->summaries;
    rt->model.summary_count = rt->summary_count;
    if (rt->model.selected_project >= rt->summary_count) {
        rt->model.selected_project = 0u;
    }
}

/* ---------------------------------------------------------- the overlay */

static void grid_of(const kpa_ui_runtime *rt, kpa_ui_grid *out)
{
    (void)memset(out, 0, sizeof *out);
    if (rt->model.cell_only) {
        /* Cell-only owns the whole screen, so it measures the terminal
         * rather than borrowing the framebuffer's centred image rectangle. */
        struct winsize window;

        out->cell_height = 1;
        out->origin_row = 1;
        out->origin_column = 1;
        if (ioctl(STDOUT_FILENO, TIOCGWINSZ, &window) == 0) {
            out->columns = window.ws_col;
            out->rows = window.ws_row;
        }
        if (out->columns <= 0) out->columns = 80;
        if (out->rows <= 0) out->rows = 24;
        return;
    }
    {
        const int cell_w = kittyts_cell_width(&rt->session);
        const int cell_h = kittyts_cell_height(&rt->session);

        if (cell_w <= 0 || cell_h <= 0 || !rt->canvas_live) return;
        out->cell_height = cell_h;
        out->columns = rt->canvas.w / cell_w;
        out->rows = rt->canvas.h / cell_h;
        out->origin_row = kittyts_origin_y(&rt->session) / cell_h + 1;
        out->origin_column = kittyts_origin_x(&rt->session) / cell_w + 1;
    }
}

/*
 * Copy at most `columns` columns of UTF-8 into a bounded field.  Cut on a
 * character boundary, because a row assembled from byte-truncated pieces
 * stops being valid UTF-8 in the middle and the writer would then drop
 * everything after the break rather than just the tail of one column.
 */
static void fit_cell_text(char *out, size_t size, const char *text,
                          int columns)
{
    size_t length;
    size_t fitted;

    if (text == NULL) {
        out[0] = '\0';
        return;
    }
    /*
     * Bound the bytes first and the columns second.  Columns do not bound
     * bytes - a run of combining marks is many bytes and no columns at all -
     * so the byte limit goes in as the length, and kpa_cells_fit stops at the
     * last whole character inside it.
     */
    length = strlen(text);
    if (length > size - 1u) length = size - 1u;
    fitted = kpa_cells_fit(text, length, columns);
    if (fitted > length) fitted = length;
    (void)memcpy(out, text, fitted);
    out[fitted] = '\0';
}

static void write_row(kpa_ui_runtime *rt, const kpa_ui_grid *grid, int row,
                      const char *text, uint32_t rgb)
{
    if (row < 0 || row >= grid->rows) return;
    kpa_cells_row(rt->cells, grid->origin_row + row, grid->origin_column,
                  grid->columns, text, strlen(text), rgb);
}

/*
 * One cue as the browser draws it: the words joined by single spaces when the
 * cue has word timings, the cue's own text when it does not.  *sung is the
 * byte offset of the end of the active word, which is where the line is split
 * into what has been heard and what has not.
 */
static void lyric_line(const kpa_lyrics *lyrics, int32_t cue_index,
                       int32_t active_word, char *out, size_t size,
                       size_t *sung)
{
    const kpa_cue *cue;
    size_t used = 0u;
    uint32_t offset;

    out[0] = '\0';
    *sung = 0u;
    if (lyrics == NULL || cue_index < 0 ||
        (uint32_t)cue_index >= lyrics->cue_count) {
        return;
    }
    cue = &lyrics->cues[cue_index];
    if (cue->word_count == 0u || lyrics->words == NULL) {
        if (cue->text != NULL) {
            size_t length = cue->length;

            if (length > size - 1u) length = size - 1u;
            (void)memcpy(out, cue->text, length);
            used = length;
        }
        out[used] = '\0';
        return;
    }
    for (offset = 0u; offset < cue->word_count; ++offset) {
        const uint32_t at = cue->first_word + offset;
        const kpa_word *word;
        size_t length;

        if (at >= lyrics->word_count) break;
        word = &lyrics->words[at];
        if (offset > 0u && used + 1u < size) out[used++] = ' ';
        length = word->length;
        if (length > size - 1u - used) length = size - 1u - used;
        if (word->text != NULL && length > 0u) {
            (void)memcpy(out + used, word->text, length);
            used += length;
        }
        if (active_word >= 0 && (uint32_t)active_word == at) *sung = used;
    }
    out[used] = '\0';
}

/* The active cue, split at the active word so what has been sung reads as
 * sung.  Two calls on one row: kpa_cells_row erases from where it stops. */
static void write_lyric_row(kpa_ui_runtime *rt, const kpa_ui_grid *grid,
                            int row, const char *text, size_t sung,
                            bool active)
{
    const size_t length = strlen(text);
    int width;

    if (row < 0 || row >= grid->rows) return;
    if (!active || sung == 0u || sung > length) {
        write_row(rt, grid, row, text, active ? KPA_UI_TEXT : KPA_UI_DIM);
        return;
    }
    width = kpa_cells_width(text, sung);
    if (width < 0 || width >= grid->columns) {
        write_row(rt, grid, row, text, KPA_UI_TEXT);
        return;
    }
    kpa_cells_row(rt->cells, grid->origin_row + row, grid->origin_column,
                  grid->columns, text, sung, KPA_UI_ACCENT);
    kpa_cells_row(rt->cells, grid->origin_row + row,
                  grid->origin_column + width, grid->columns - width,
                  text + sung, length - sung, KPA_UI_TEXT);
}

/*
 * What the band says about where its times came from.
 *
 * The estimated case is the one a player must see.  Spans invented by
 * spreading a line across the song highlight word by word exactly as
 * confidently as measured ones do, so nothing on the screen distinguishes a
 * measurement from a guess unless it is said in words.  The other kinds are
 * captioned too, which is what leaves no answer from here meaning one thing
 * only: a document written before the field existed, about which this surface
 * has been told nothing.
 */
bool kpa_ui_internal_lyrics_caption(const kpa_lyrics *lyrics, char *out,
                                    size_t size, uint32_t *rgb)
{
    const kpa_lyrics_alignment *report;
    double percent;

    if (out == NULL || size == 0u) return false;
    out[0] = '\0';
    if (rgb != NULL) *rgb = KPA_UI_DIM;
    if (lyrics == NULL) return false;
    report = &lyrics->alignment;
    switch (lyrics->timing) {
    case KPA_TIMING_ESTIMATED:
        if (rgb != NULL) *rgb = KPA_UI_WARN;
        (void)snprintf(out, size,
                       "timing estimated - spread across the song, not "
                       "measured");
        return true;
    case KPA_TIMING_MEASURED:
        /* Measured with no report is all this can honestly say; a percentage
         * the document did not carry is not one to put on the screen. */
        if (!report->present) {
            (void)snprintf(out, size, "timing measured");
            return true;
        }
        /*
         * Floored, not rounded: 99.6% of the words is not all of them, and a
         * caption reading "100% matched, 2 filled in" contradicts itself.
         * The hair added first is what keeps a fraction whose product landed
         * a shade under its own hundredth - 0.29 * 100.0 is
         * 28.999999999999996 - from reading one percent low; a fraction has
         * to be within 1e-11 of 1.0 before that hair can carry it to 100.
         *
         * kpa_project.c refuses a fraction outside 0..1, so the clamp is not
         * for a document: it is for a model some other caller assembled by
         * hand, where the cast below would otherwise be undefined.
         */
        percent = report->matched_fraction * 100.0 + 1e-9;
        if (!(percent >= 0.0)) percent = 0.0;
        if (percent > 100.0) percent = 100.0;
        if (!report->usable && rgb != NULL) *rgb = KPA_UI_WARN;
        (void)snprintf(out, size,
                       "timing measured - %u%% of words matched, %u filled "
                       "in%s", (unsigned)percent,
                       (unsigned)report->interpolated_words,
                       report->usable ? "" : " (the aligner called it poor)");
        return true;
    case KPA_TIMING_AUTHORED:
        (void)snprintf(out, size,
                       "timing authored - the source carried these stamps");
        return true;
    case KPA_TIMING_UNKNOWN:
    default:
        return false;
    }
}

static void write_lyric_band(kpa_ui_runtime *rt, const kpa_ui_grid *grid,
                             int first, int count)
{
    char line[KPA_UI_LINE_CAPACITY];
    const kpa_lyrics *lyrics = rt->model.lyrics;
    uint32_t caption_rgb = KPA_UI_DIM;
    int centre;
    int offset;

    /*
     * The caption takes the band's top row, which carries the line furthest
     * behind the singer and is the cheapest row in the band to give up, and
     * only when there is a row to give: a band one row tall is the line being
     * sung and nothing else, and covering that to explain it would be
     * perverse.
     */
    if (count > 1 &&
        kpa_ui_internal_lyrics_caption(lyrics, line, sizeof line,
                                       &caption_rgb)) {
        write_row(rt, grid, first, line, caption_rgb);
        ++first;
        --count;
    }
    centre = count / 2;
    for (offset = 0; offset < count; ++offset) {
        const int32_t cue = rt->model.active_cue + (int32_t)(offset - centre);
        size_t sung = 0u;

        if (lyrics == NULL || rt->model.active_cue < 0 || cue < 0 ||
            (uint32_t)cue >= lyrics->cue_count) {
            kpa_cells_clear_row(rt->cells, grid->origin_row + first + offset,
                                grid->columns);
            continue;
        }
        lyric_line(lyrics, cue, rt->model.active_word, line, sizeof line,
                   &sung);
        write_lyric_row(rt, grid, first + offset, line, sung,
                        offset == centre);
    }
}

/* ------------------------------------------------- cell-only body rows */

static void cell_transport_line(const kpa_ui_model *model, char *out,
                                size_t size)
{
    char elapsed[32];
    char total[32];
    char rate[48];
    char loop[96];

    format_clock(model->position, elapsed, sizeof elapsed);
    format_clock(model->duration, total, sizeof total);
    if (model->rate_available) {
        (void)snprintf(rate, sizeof rate, "   %.2fx", model->rate);
    } else {
        (void)snprintf(rate, sizeof rate, "   rate unavailable");
    }
    loop[0] = '\0';
    if (model->loop_active) {
        char from[32];
        char to[32];

        format_clock(model->loop_start, from, sizeof from);
        format_clock(model->loop_end, to, sizeof to);
        (void)snprintf(loop, sizeof loop, "   loop %s-%s", from, to);
    }
    (void)snprintf(out, size, "%s  %s / %s%s%s%s",
                   model->playing ? "[>]" : "[||]", elapsed, total, rate,
                   loop,
                   model->device_lost ? "   device lost"
                                      : model->underrun ? "   underrun" : "");
}

static void cell_mixer_line(const kpa_ui_model *model, uint32_t index,
                            char *out, size_t size)
{
    const kpa_ui_track *track = &model->tracks[index];
    char label[80];
    char kind[40];
    char meter[16];
    int filled = (int)(clamp_float(track->gain / KPA_UI_GAIN_MAX, 0.0f, 1.0f) *
                       10.0f + 0.5f);
    int cell;

    for (cell = 0; cell < 10; ++cell) meter[cell] = cell < filled ? '=' : '.';
    meter[10] = '\0';
    fit_cell_text(label, sizeof label, track->label, 14);
    fit_cell_text(kind, sizeof kind, track->kind, 8);
    (void)snprintf(out, size, "%s%u %-14s %-8s [%s] %s%s",
                   index == model->selected_track ? ">" : " ",
                   (unsigned)(index + 1u), label, kind, meter,
                   track->muted ? "M" : "-", track->soloed ? "S" : "-");
}

/*
 * One string of the tab, as text, in the same order and with the same
 * numbering as the pixel lane: display row 0 is the highest string, and the
 * number printed is the player's.
 */
static void cell_tab_line(const kpa_ui_model *model, uint32_t display_row,
                          uint32_t strings, int columns, char *out,
                          size_t size)
{
    static const char *const default_labels[KPA_STRING_COUNT] = {
        "E", "A", "D", "G", "B", "e"
    };
    const kpa_tab *tab = model->tab;
    const uint32_t api = string_display_row(display_row, strings,
                                            model->low_string_on_top);
    const char *name = default_labels[api < KPA_STRING_COUNT ? api : 0u];
    const double per_column = 4.0;         /* columns per second */
    int lane;
    int head;
    int prefix;
    size_t used;
    int cell;

    if (tab != NULL && api < KPA_STRING_COUNT &&
        tab->tuning_labels[api][0] != '\0') {
        name = tab->tuning_labels[api];
    }
    prefix = (int)snprintf(out, size, "%-2s %u |", name,
                           (unsigned)player_string_number(api, strings));
    if (prefix < 0 || (size_t)prefix >= size) {
        out[0] = '\0';
        return;
    }
    used = (size_t)prefix;
    lane = columns - prefix - 1;
    if (lane < 1 || (size_t)lane >= size - used) return;
    head = lane / 4;
    for (cell = 0; cell < lane; ++cell) out[used + (size_t)cell] = '-';
    out[used + (size_t)lane] = '\0';
    if (head < lane) out[used + (size_t)head] = '|';
    if (tab == NULL || tab->events == NULL || tab->positions == NULL) return;
    {
        const double left = model->position - (double)head / per_column;
        uint32_t event = kpa_tab_first_after(tab, left);
        uint32_t drawn = 0u;

        for (; event < tab->event_count && drawn < KPA_UI_LANE_EVENTS;
             ++event, ++drawn) {
            const kpa_tab_event *item = &tab->events[event];
            const double at = (item->start - left) * per_column;
            uint32_t slot;
            char fret[8];
            int column;
            int digit;

            if (at >= (double)lane) break;
            if (at < 0.0) continue;
            column = (int)at;
            for (slot = 0u; slot < item->position_count; ++slot) {
                const uint32_t index = item->first_position + slot;

                if (index >= tab->position_count) break;
                if ((uint32_t)tab->positions[index].string_index != api)
                    continue;
                (void)snprintf(fret, sizeof fret, "%u",
                               (unsigned)tab->positions[index].fret);
                for (digit = 0; fret[digit] != '\0'; ++digit) {
                    if (column + digit >= lane) break;
                    out[used + (size_t)(column + digit)] = fret[digit];
                }
            }
        }
    }
}

/* The mixer in one row: an initial, a short bar and the flags.  Written
 * before the neck is given up, because five stems as one line still say what
 * is loud and what is muted. */
static void cell_mixer_strip(const kpa_ui_model *model, char *out, size_t size)
{
    size_t used = 0u;
    uint32_t index;

    out[0] = '\0';
    for (index = 0u; index < model->track_count && index < KPA_MAX_TRACKS;
         ++index) {
        const kpa_ui_track *track = &model->tracks[index];
        char bar[8];
        int filled = (int)(clamp_float(track->gain / KPA_UI_GAIN_MAX, 0.0f,
                                       1.0f) * 4.0f + 0.5f);
        int cell;
        int written;

        for (cell = 0; cell < filled && cell < 4; ++cell) bar[cell] = '=';
        bar[cell] = '\0';
        written = snprintf(out + used, size - used, "%s%c%s%s%s",
                           index > 0u ? " " : "",
                           track->label[0] != '\0' ? track->label[0] : '?',
                           bar, track->muted ? "M" : "",
                           track->soloed ? "S" : "");
        if (written < 0 || (size_t)written >= size - used) break;
        used += (size_t)written;
    }
}

/*
 * The neck as terminal cells.
 *
 * A cell grid has equal-width columns, so a proportionally spaced fretboard
 * cannot be drawn in one: the frets would come out evenly spaced, which is
 * the single thing that makes a drawn fretboard look fake.  This therefore
 * does not draw a neck.  It draws a WINDOW at the hand - a few frets side by
 * side with their numbers underneath - which is honest about being a grid,
 * and it is what the pixel surface's position box is showing anyway.
 *
 * ASCII only.  The cell layer is UTF-8 and would carry box drawing happily,
 * but those characters are ambiguous-width, and a terminal that renders them
 * double slides the whole grid sideways.  The lyrics stay UTF-8; a fret
 * diagram has nothing to gain from it.
 */
#define KPA_UI_CELL_PREFIX 4
#define KPA_UI_CELL_FRET 5
#define KPA_UI_CELL_FRETS_MIN 4
#define KPA_UI_CELL_FRETS_MAX 8
/* Rows the lyric band is given before anything else is laid out, when there
 * are lyrics to show: the caption and three cues. */
#define KPA_UI_CELL_LYRIC_ROWS 4

static int cell_neck_rows(uint32_t strings)
{
    /* A blank, the callout, one row per string and the fret numbers. */
    return 1 + 1 + (int)strings + 1;
}

static int write_cell_neck(kpa_ui_runtime *rt, const kpa_ui_grid *grid,
                           int row, int last)
{
    static const char *const default_labels[KPA_STRING_COUNT] = {
        "E", "A", "D", "G", "B", "e"
    };
    const kpa_ui_model *model = &rt->model;
    const uint32_t strings = fb_string_count(model->tab);
    kpa_fb_frame frame;
    kpa_fret_hand hand;
    char line[KPA_UI_LINE_CAPACITY];
    char chord[32];
    bool named = false;
    bool has_hand;
    uint32_t first;
    uint32_t display;
    int frets;
    int lane;
    int index;

    frets = (grid->columns - KPA_UI_CELL_PREFIX - 1) / KPA_UI_CELL_FRET;
    if (frets < KPA_UI_CELL_FRETS_MIN) return row;
    if (frets > KPA_UI_CELL_FRETS_MAX) frets = KPA_UI_CELL_FRETS_MAX;
    if (row + cell_neck_rows(strings) - 1 > last) return row;
    lane = 1 + frets * KPA_UI_CELL_FRET;
    if ((size_t)(KPA_UI_CELL_PREFIX + lane) >= sizeof line) return row;

    (void)fb_collect(&frame, model->tab, model->position,
                     model_ramp_seconds(model), strings);
    (void)memset(&hand, 0, sizeof hand);
    has_hand = fb_hand(model->tab, model->position, &hand);
    first = has_hand ? (uint32_t)hand.low : 1u;

    write_row(rt, grid, row++, "", KPA_UI_DIM);

    /* The callout, in the same three fields the pixel surface uses. */
    {
        int32_t pitches[KPA_FRET_MAX_PITCHES];
        const uint32_t count = fb_ring_pitches(&frame, strings, pitches);
        char tail[96];

        if (model->chord[0] != '\0') {
            (void)snprintf(chord, sizeof chord, "%s", model->chord);
            named = model->chord_kind != 0u;
        } else {
            kpa_ui_internal_chord_label(pitches, count, chord, sizeof chord,
                                        &named);
        }
        tail[0] = '\0';
        if (has_hand) {
            const kpa_fb_move move = fb_next_move(model->tab, model->position,
                                                  strings, &hand);

            if (move.found) {
                (void)snprintf(tail, sizeof tail, "   next: pos %u%s%s in %.1fs",
                               (unsigned)move.anchor,
                               move.chord[0] != '\0' ? ", " : "  ",
                               move.chord, move.when - model->position);
            }
        }
        {
            char where[16];

            if (has_hand) {
                (void)snprintf(where, sizeof where, "pos %u",
                               (unsigned)hand.low);
            } else {
                (void)snprintf(where, sizeof where, "open");
            }
            (void)snprintf(line, sizeof line, "%-8s%-10s%s", where, chord,
                           tail);
        }
        write_row(rt, grid, row++, line, named ? KPA_UI_TEXT : KPA_UI_DIM);
    }

    for (display = 0u; display < strings; ++display) {
        const uint32_t api = string_display_row(display, strings,
                                                model->low_string_on_top);
        const kpa_fret_note *sounding = api < KPA_STRING_COUNT
            ? frame.ring[api] : NULL;
        const char *name = default_labels[api < KPA_STRING_COUNT ? api : 0u];
        char cells[KPA_UI_LINE_CAPACITY];
        int at;

        if (model->tab != NULL && api < KPA_STRING_COUNT &&
            model->tab->tuning_labels[api][0] != '\0') {
            name = model->tab->tuning_labels[api];
        }
        cells[0] = '|';
        for (at = 0; at < frets; ++at) {
            const int cell = 1 + at * KPA_UI_CELL_FRET;

            cells[cell] = '-';
            cells[cell + 1] = '-';
            cells[cell + 2] = '-';
            cells[cell + 3] = '-';
            cells[cell + 4] = '|';
        }
        cells[lane] = '\0';

        /* What is arriving, first, so a note that is sounding overwrites it
         * rather than the other way round. */
        for (index = 0; index < (int)frame.report.count; ++index) {
            const kpa_fret_note *note = &frame.notes[index];
            int cell;

            if (!fb_note_arriving(note, frame.when)) continue;
            if (note->string_index < 0 ||
                (uint32_t)note->string_index != api) {
                continue;
            }
            if (note->fret < (int32_t)first ||
                note->fret >= (int32_t)first + frets) {
                continue;
            }
            cell = 1 + (note->fret - (int32_t)first) * KPA_UI_CELL_FRET + 2;
            if (cells[cell] == '-') cells[cell] = 'o';
        }
        if (sounding != NULL) {
            const int fret = sounding->fret;

            if (fret == 0) {
                cells[0] = '0';
                for (at = 1; at < lane; ++at) {
                    if (cells[at] == '-' || cells[at] == 'o') cells[at] = '=';
                }
            } else if (fret >= (int)first && fret < (int)first + frets) {
                const int cell = 1 + (fret - (int)first) * KPA_UI_CELL_FRET;
                /* Wide enough for any int the compiler can prove reaches
                 * here, not just for the two digits a fret really has. */
                char digits[16];

                for (at = cell; at < lane; ++at) {
                    if (cells[at] == '-' || cells[at] == 'o') cells[at] = '=';
                }
                if (model->capo > 0u && fret < (int)model->capo) {
                    /* Behind the capo: unplayable from here, and said so
                     * rather than moved to a fret the artifact never had. */
                    cells[cell + 1] = 'x';
                    cells[cell + 2] = '=';
                } else {
                    (void)snprintf(digits, sizeof digits, "%d", fret);
                    cells[cell + 1] = digits[0];
                    if (digits[1] != '\0') cells[cell + 2] = digits[1];
                }
            }
        }
        /* Four columns of prefix, then the nut: "e 1 |----|...".  The
         * name is truncated to two so a long tuning label cannot shift the
         * grid out from under the fret numbers below it. */
        (void)snprintf(line, sizeof line, "%-2.2s%u %s", name,
                       (unsigned)player_string_number(api, strings), cells);
        write_row(rt, grid, row++, line,
                  sounding != NULL ? KPA_UI_TEXT : KPA_UI_DIM);
    }

    /* The fret numbers, centred under their own cells. */
    for (index = 0; index < KPA_UI_CELL_PREFIX + lane; ++index) {
        line[index] = ' ';
    }
    line[KPA_UI_CELL_PREFIX + lane] = '\0';
    for (index = 0; index < frets; ++index) {
        char number[8];
        const int width = (int)snprintf(number, sizeof number, "%u",
                                        (unsigned)(first + (uint32_t)index));
        /* The column the row puts that fret's first digit in, so a number
         * here sits under the note it names rather than near it. */
        const int centre = KPA_UI_CELL_PREFIX + 2 +
                           index * KPA_UI_CELL_FRET;
        int digit;

        for (digit = 0; digit < width; ++digit) {
            line[centre + digit] = number[digit];
        }
    }
    write_row(rt, grid, row++, line, KPA_UI_DIM);
    return row;
}

/*
 * The library as text.  Better than the pixel list, in fact: a title is
 * whatever the song is written in, and these rows are UTF-8 cells rather
 * than the ASCII bitmap font.
 */
static int write_cell_library(kpa_ui_runtime *rt, const kpa_ui_grid *grid,
                              int row, int last)
{
    char line[KPA_UI_LINE_CAPACITY];
    const kpa_ui_model *model = &rt->model;
    uint32_t index;
    uint32_t first = 0u;
    int room = last - row + 1;

    if (model->summaries == NULL || model->summary_count == 0u) {
        write_row(rt, grid, row, "no projects yet", KPA_UI_DIM);
        return row + 1;
    }
    if (room > 0 && model->selected_project >= (uint32_t)room) {
        first = model->selected_project - (uint32_t)room + 1u;
    }
    for (index = first; index < model->summary_count && row <= last;
         ++index) {
        const kpa_project_summary *entry = &model->summaries[index];
        const bool selected = index == model->selected_project;
        char title[120];
        char artist[88];
        char clock[32];

        format_clock(entry->duration, clock, sizeof clock);
        fit_cell_text(title, sizeof title, entry->title, 28);
        fit_cell_text(artist, sizeof artist, entry->artist, 20);
        (void)snprintf(line, sizeof line, "%s %-28s %-20s %6s %2u %s",
                       selected ? ">" : " ", title, artist, clock,
                       (unsigned)entry->track_count,
                       entry->ready ? "ready" : "pending");
        write_row(rt, grid, row++, line,
                  selected ? KPA_UI_TEXT : KPA_UI_DIM);
    }
    return row;
}

static int write_cell_help(kpa_ui_runtime *rt, const kpa_ui_grid *grid,
                           int row, int last)
{
    const size_t count = sizeof help_lines / sizeof help_lines[0];
    size_t index;

    for (index = 0u; index < count && row <= last; ++index) {
        write_row(rt, grid, row++, help_lines[index],
                  index < KPA_UI_HELP_KEY_LINES ? KPA_UI_TEXT : KPA_UI_DIM);
    }
    return row;
}

static int write_cell_body(kpa_ui_runtime *rt, const kpa_ui_grid *grid,
                           int row, int last)
{
    char line[KPA_UI_LINE_CAPACITY];
    const kpa_ui_model *model = &rt->model;
    uint32_t index;

    if (row > last) return row;
    if (model->view == KPA_VIEW_LIBRARY) {
        return write_cell_library(rt, grid, row, last);
    }
    if (model->view == KPA_VIEW_HELP) {
        return write_cell_help(rt, grid, row, last);
    }
    {
        const uint32_t strings = fb_string_count(model->tab);
        const bool wants_neck = model->fretboard != KPA_FB_OFF &&
                                model->tab != NULL;
        const bool wants_lyrics = model->lyrics_visible &&
                                  model->lyrics != NULL;
        const int neck_rows = wants_neck ? cell_neck_rows(strings) : 0;
        const int lane_rows = model->tab_visible ? 1 + (int)strings : 0;
        /*
         * The lyric band is taken off the top of the budget rather than
         * given whatever is left over.  With no pixel layer this is the
         * surface, and a player who can see the words is the reason it
         * degrades to text at all instead of refusing to start.
         */
        int lyric_rows = 0;
        int body_last;
        int available;

        if (wants_lyrics) {
            /* Four rows: the provenance caption and the line being sung
             * with one either side of it.  Never at the cost of the
             * transport and the mixer having nowhere to go. */
            lyric_rows = last - row + 1 - 6;
            if (lyric_rows > KPA_UI_CELL_LYRIC_ROWS) {
                lyric_rows = KPA_UI_CELL_LYRIC_ROWS;
            }
            if (lyric_rows < 0) lyric_rows = 0;
        }
        body_last = last - (lyric_rows > 0 ? lyric_rows + 1 : 0);

        cell_transport_line(model, line, sizeof line);
        write_row(rt, grid, row++, line, KPA_UI_TEXT);
        if (row <= body_last) write_row(rt, grid, row++, "", KPA_UI_DIM);

        /*
         * What gets given up, in order: the tab lane, then the mixer's
         * rows - five stems as one line still say what is loud and what is
         * muted - and only then the neck.  An 80x24 terminal has room for
         * one of the two pictures of the guitar and not both, which is why
         * `f` and `t` say what they took when a player asks for the other.
         */
        {
            const int room = body_last - row + 1;
            const bool crowded = wants_neck &&
                                 room - (int)model->track_count < neck_rows;

            if (crowded && model->track_count > 0u && row <= body_last) {
                cell_mixer_strip(model, line, sizeof line);
                write_row(rt, grid, row++, line, KPA_UI_DIM);
            } else {
                for (index = 0u; index < model->track_count &&
                                 row <= body_last; ++index) {
                    cell_mixer_line(model, index, line, sizeof line);
                    write_row(rt, grid, row++, line,
                              index == model->selected_track ? KPA_UI_TEXT
                                                             : KPA_UI_DIM);
                }
            }
        }
        if (wants_neck) row = write_cell_neck(rt, grid, row, body_last);
        /*
         * The lane is all six strings or none of them: five rows of a
         * six-string tab is not a tab, it is a tab with the low E missing
         * and nothing saying so.
         */
        if (model->tab_visible && row + lane_rows - 1 <= body_last) {
            uint32_t display;

            write_row(rt, grid, row++, "", KPA_UI_DIM);
            for (display = 0u; display < strings; ++display) {
                cell_tab_line(model, display, strings, grid->columns, line,
                              sizeof line);
                write_row(rt, grid, row++, line, KPA_UI_DIM);
            }
        }
        if (lyric_rows > 0) {
            available = lyric_rows;
            /* Anything the body did not use is cleared, so the band sits at
             * the bottom where it always is rather than sliding up when the
             * lane is dropped. */
            while (row < last - available) {
                kpa_cells_clear_row(rt->cells, grid->origin_row + row,
                                    grid->columns);
                ++row;
            }
            write_row(rt, grid, row++, "", KPA_UI_DIM);
            write_lyric_band(rt, grid, row, available);
            row += available;
        }
    }
    return row;
}

/* --------------------------------------------------------------- frames */

/*
 * The status row names the keys of the view it is under, not every key the
 * player has.  One row could hold the whole table once; it cannot now, and a
 * row that lists half a table without saying so is worse than one that lists
 * the half you are looking at.  `?` still has all of them.
 */
static const char *status_line(const kpa_ui_model *model)
{
    switch (model->view) {
    case KPA_VIEW_LIBRARY:
        return "up down select   enter open   tab focus   ? help   q quit";
    case KPA_VIEW_HELP:
        return "? or escape leaves this page   q quit";
    case KPA_VIEW_PRACTICE:
    default:
        return "space play   arrows seek   [ ] loop   a A jump   f neck   "
               "F ahead   o strings   n labels   k capo   w density   "
               "m mute   s solo   l lyrics   t tab   ? help   q quit";
    }
}

static void draw_overlay(kpa_ui_runtime *rt)
{
    kpa_ui_cell_layout layout;
    kpa_ui_grid grid;
    char line[KPA_UI_LINE_CAPACITY];
    const kpa_ui_model *model = &rt->model;
    int row;

    if (rt->cells == NULL) return;
    grid_of(rt, &grid);
    if (grid.columns <= 0 || grid.rows <= 0) return;
    kpa_ui_cell_layout_get(model, grid.columns, grid.rows, grid.cell_height,
                           &layout);
    kpa_cells_begin(rt->cells);
    if (rt->drawn_valid) {
        const bool moved = !same_grid(&rt->drawn_grid, &grid);

        for (row = 0; row < rt->drawn.rows; ++row) {
            if (!row_is_claimed(&rt->drawn, row)) continue;
            if (!moved && row_is_claimed(&layout, row)) continue;
            kpa_cells_clear_row(rt->cells,
                                rt->drawn_grid.origin_row + row,
                                rt->drawn_grid.origin_column - 1 +
                                    rt->drawn_grid.columns);
        }
    }
    if (layout.title_row >= 0) {
        /* The title is a cell row because a song title is whatever the song
         * is written in, and the raster font is ASCII bitmaps. */
        if (model->title != NULL && model->artist != NULL) {
            char title[320];
            char artist[288];

            fit_cell_text(title, sizeof title, model->title,
                          grid.columns > 8 ? grid.columns - 8 : 1);
            fit_cell_text(artist, sizeof artist, model->artist,
                          grid.columns > 8 ? grid.columns - 8 : 1);
            (void)snprintf(line, sizeof line, "%s - %s", title, artist);
        } else {
            (void)snprintf(line, sizeof line, "kilix-playalong");
        }
        write_row(rt, &grid, layout.title_row, line, KPA_UI_ACCENT);
    }
    if (layout.lyric_row >= 0 && layout.lyric_row_count > 0) {
        if (model->cell_only) {
            row = write_cell_body(rt, &grid, layout.lyric_row,
                                  layout.lyric_row + layout.lyric_row_count
                                      - 1);
            while (row < layout.lyric_row + layout.lyric_row_count) {
                kpa_cells_clear_row(rt->cells, grid.origin_row + row,
                                    grid.columns);
                row++;
            }
        } else {
            write_lyric_band(rt, &grid, layout.lyric_row,
                             layout.lyric_row_count);
        }
    }
    if (layout.status_row >= 0) {
        if (model->notice[0] != '\0') {
            write_row(rt, &grid, layout.status_row, model->notice,
                      KPA_UI_WARN);
        } else {
            write_row(rt, &grid, layout.status_row, status_line(model),
                      KPA_UI_DIM);
        }
    }
    kpa_cells_end(rt->cells);
    rt->drawn = layout;
    rt->drawn_grid = grid;
    rt->drawn_valid = true;
}

static void draw_frame(kpa_ui_runtime *rt, bool force)
{
    /* Byte comparison of the whole model, which is the honest question: the
     * screen is a function of it, so an unchanged model is an unchanged
     * screen.  Both copies move as whole structs, so the padding compares
     * equal too. */
    if (!force && rt->shown_valid &&
        memcmp(&rt->shown, &rt->model, sizeof rt->model) == 0) {
        return;
    }
    if (!rt->model.cell_only && rt->canvas_live && rt->rgba != NULL) {
        kpa_ui_compose(&rt->canvas, &rt->model);
        if (sr_pack_rgba(&rt->canvas, rt->rgba, rt->rgba_size)) {
            (void)kittyts_present(&rt->session, rt->rgba, rt->canvas.w,
                                  rt->canvas.h);
        }
    }
    /*
     * The framebuffer encodes and writes its frames on its own thread and
     * exposes no output lock, so a cell row written here can land inside a
     * graphics packet.  The cost is a glitched frame, not a wedged terminal:
     * both layers are written again on the next redraw.  A real fix needs an
     * output lock in kitty-framebuffer and is still open.
     */
    draw_overlay(rt);
    rt->shown = rt->model;
    rt->shown_valid = true;
}

static bool runtime_resize(kpa_ui_runtime *rt, int width, int height)
{
    uint8_t *rgba;
    size_t bytes;

    if (width <= 0 || height <= 0) return true;
    if (rt->canvas_live && rt->canvas.w == width && rt->canvas.h == height) {
        return true;
    }
    bytes = (size_t)width * (size_t)height * 4u;
    rgba = malloc(bytes);
    if (rgba == NULL) return false;
    if (rt->canvas_live) {
        sr_canvas_free(&rt->canvas);
        rt->canvas_live = false;
    }
    if (!sr_canvas_init(&rt->canvas, width, height)) {
        free(rgba);
        return false;
    }
    rt->canvas_live = true;
    free(rt->rgba);
    rt->rgba = rgba;
    rt->rgba_size = bytes;
    return true;
}

static void refresh_model(kpa_ui_runtime *rt)
{
    kpa_audio_snapshot snapshot;
    kpa_ui_model *model = &rt->model;

    if (rt->audio == NULL) return;
    kpa_audio_snapshot_get(rt->audio, &snapshot);
    if (snapshot.output_rate > 0u) rt->output_rate = snapshot.output_rate;
    model->playing = snapshot.playing;
    model->underrun = snapshot.underrun;
    model->device_lost = snapshot.device_lost;
    model->rate = snapshot.rate;
    model->duration = frames_to_seconds(snapshot.duration_frames,
                                        snapshot.output_rate);
    if (rt->seek_hold > 0) {
        rt->seek_hold--;
    } else {
        model->position = frames_to_seconds(snapshot.audible_frame,
                                            snapshot.output_rate);
    }
    if (model->lyrics != NULL) {
        model->active_cue = kpa_lyrics_cue_at(model->lyrics, model->position);
        model->active_word = kpa_lyrics_word_at(model->lyrics,
                                                model->active_cue,
                                                model->position);
    }
}

/*
 * The chord label's latch.
 *
 * Named from what is ringing every frame, the label changes every 0.37 s on
 * the audited song, which is a flicker rather than a reading.  Holding the
 * last name through the passages the shared namer declines, and making a new
 * name wait until it has been true for a quarter of a second, takes that to
 * 1.23 s.  The lag it costs is 0.25 s - one median-length event - and it
 * buys a line a player can actually read.
 *
 * Silence clears it outright.  A chord symbol left standing over a rest is
 * not a held reading, it is a stale one.
 */
static void refresh_chord(kpa_ui_runtime *rt)
{
    kpa_ui_model *model = &rt->model;
    kpa_fb_frame frame;
    int32_t pitches[KPA_FRET_MAX_PITCHES];
    char label[sizeof model->chord];
    const uint32_t strings = fb_string_count(model->tab);
    uint32_t count;
    bool named = false;

    if (model->tab == NULL) {
        model->chord[0] = '\0';
        model->chord_kind = 0u;
        return;
    }
    (void)fb_collect(&frame, model->tab, model->position, 0.0f, strings);
    count = fb_ring_pitches(&frame, strings, pitches);
    if (count == 0u) {
        model->chord[0] = '\0';
        model->chord_kind = 0u;
        rt->chord_candidate[0] = '\0';
        return;
    }
    kpa_ui_internal_chord_label(pitches, count, label, sizeof label, &named);
    if (!named) {
        /* Hold the last real chord; show the notes only when there has
         * never been one to hold. */
        if (model->chord[0] == '\0') {
            (void)snprintf(model->chord, sizeof model->chord, "%s", label);
            model->chord_kind = 0u;
        }
        return;
    }
    if (strcmp(label, rt->chord_candidate) != 0) {
        (void)snprintf(rt->chord_candidate, sizeof rt->chord_candidate, "%s",
                       label);
        rt->candidate_since = model->position;
        return;
    }
    /* A seek backwards is not a candidate that has been true for a long
     * time; the timer restarts from wherever the player landed. */
    if (model->position < rt->candidate_since) {
        rt->candidate_since = model->position;
        return;
    }
    if (model->position - rt->candidate_since < KPA_FB_CHORD_HOLD) return;
    if (strcmp(label, model->chord) != 0) {
        (void)snprintf(model->chord, sizeof model->chord, "%s", label);
        model->chord_kind = 1u;
    }
}

/*
 * The loop's speed ramp: every time the loop comes round, five points
 * faster, up to full speed.  The wrap is what a loop looks like from here -
 * the audible clock jumping back to near the loop start - and the rate is
 * changed through the engine, which is still the thing that decides whether
 * a rate is available at all.
 */
static void refresh_speed_ramp(kpa_ui_runtime *rt)
{
    kpa_ui_model *model = &rt->model;
    double next;

    if (model->speed_ramp == 0u || rt->audio == NULL) return;
    if (!model->loop_active || !model->rate_available) return;
    if (!rt->last_position_valid) return;
    if (!(model->position < rt->last_position - 0.25)) return;
    if (model->position > model->loop_start + 0.5) return;
    next = model->rate + 0.05;
    if (next > 1.0) next = 1.0;
    if (next <= model->rate) return;
    if (kpa_audio_set_rate(rt->audio, next) == KPA_AUDIO_OK) {
        model->rate = next;
    } else {
        /* Believe the engine: the ramp stops rather than showing a rate
         * the audio never took. */
        model->rate_available = false;
        model->speed_ramp = 0u;
        set_notice(model, "this build has no pitch-preserving rate engine");
    }
}

/*
 * Everything the model carries that is a function of history rather than of
 * this instant.  Called unconditionally after refresh_model, which returns
 * early when there is no audio session - the chord latch still has to run on
 * a surface that is silent.
 */
static void refresh_derived(kpa_ui_runtime *rt)
{
    refresh_chord(rt);
    refresh_speed_ramp(rt);
    rt->last_position = rt->model.position;
    rt->last_position_valid = true;
}

static int runtime_loop(kpa_ui_runtime *rt)
{
    bool running = true;

    while (running && !ui_signal_seen) {
        struct pollfd descriptor;
        kittykb_event event;
        int ready;
        int width = 0;
        int height = 0;

        bool force = false;

        if (!rt->model.cell_only &&
            kittyts_check_resize(&rt->session, &width, &height)) {
            if (!runtime_resize(rt, width, height)) return 1;
            force = true;
        }
        if (rt->model.cell_only && rt->drawn_valid) {
            kpa_ui_grid grid;

            /* Cell-only has no framebuffer to report a resize, so the grid
             * is measured every frame: without this the unchanged-model
             * check below would hold a stale screen after a resize. */
            grid_of(rt, &grid);
            if (!same_grid(&rt->drawn_grid, &grid)) {
                rt->drawn_valid = false;
                force = true;
            }
        }
        if (!rt->model.cell_only && kittyts_failed(&rt->session)) {
            /* The graphics transport gave up.  Text still reaches the
             * player, so this degrades to the cell-only surface instead of
             * leaving them looking at a frame that stopped updating. */
            rt->model.cell_only = true;
            rt->drawn_valid = false;
            force = true;
            set_notice(&rt->model, "graphics stopped; drawing as text");
        }
        refresh_model(rt);
        refresh_derived(rt);
        draw_frame(rt, force);

        descriptor.fd = STDIN_FILENO;
        descriptor.events = POLLIN;
        descriptor.revents = 0;
        ready = poll(&descriptor, 1u, 40);
        if (ui_signal_seen) break;
        /*
         * Read on a timeout as well as on data: a lone escape is held
         * pending until its timeout expires, and that timeout is only
         * examined inside this call, so a loop that read only when
         * something had arrived would make Escape appear to do nothing
         * until the next keystroke.
         *
         * Never after an interrupted poll, and never without re-asserting
         * O_NONBLOCK first.  kitty-input made this descriptor non-blocking,
         * but the async-signal restore puts the terminal's original flags
         * back the instant a signal lands, and a read issued after that
         * blocks with the loop already on its way out.  Still open: a
         * signal delivered in the few instructions between the fcntl and
         * the read leaves that read waiting for a byte.
         */
        if (ready >= 0) {
            const int flags = fcntl(STDIN_FILENO, F_GETFL);

            if (flags >= 0 && (flags & O_NONBLOCK) == 0) {
                (void)fcntl(STDIN_FILENO, F_SETFL, flags | O_NONBLOCK);
            }
            (void)kittyts_read_input(&rt->session);
        }
        while (running && kittyts_next_key_event(&rt->session, &event)) {
            kpa_ui_mix before;
            kpa_ui_mix after;
            int action;

            if (event.action == KITTYKB_ACTION_RELEASE) continue;
            capture_mix(&rt->model, &before);
            action = kpa_ui_internal_apply_key(&rt->model, event.key,
                                               event.modifiers);
            capture_mix(&rt->model, &after);
            apply_mix(rt, &before, &after);
            switch (action) {
            case KPA_UI_KEY_QUIT:
                running = false;
                break;
            case KPA_UI_KEY_OPEN:
                if (rt->model.selected_project < rt->summary_count) {
                    const char *id =
                        rt->summaries[rt->model.selected_project].id;

                    if (runtime_open_project(rt, id) != 0) {
                        rt->model.view = KPA_VIEW_LIBRARY;
                    }
                }
                break;
            case KPA_UI_KEY_CLOSE:
                runtime_close_project(rt);
                runtime_refresh_library(rt);
                break;
            default:
                break;
            }
        }
    }
    return 0;
}

/* ---------------------------------------------------------------- entry */

static void runtime_teardown(kpa_ui_runtime *rt)
{
    if (rt->signals_installed) {
        install_signal_handlers(&rt->session, false);
        rt->signals_installed = false;
    }
    kpa_cells_destroy(rt->cells);
    rt->cells = NULL;
    if (rt->cell_fd_owned && rt->cell_fd >= 0) (void)close(rt->cell_fd);
    rt->cell_fd = -1;
    rt->cell_fd_owned = false;
    free(rt->rgba);
    rt->rgba = NULL;
    if (rt->canvas_live) {
        sr_canvas_free(&rt->canvas);
        rt->canvas_live = false;
    }
    if (rt->session_started) {
        /* Normal teardown even when a signal already ran the emergency
         * restore: each layer's own claim decides what is left to undo. */
        kittyts_stop(&rt->session);
        rt->session_started = false;
    }
    runtime_close_project(rt);
}

static int runtime_start_session(kpa_ui_runtime *rt)
{
    kittyts_options options;

    kittyts_session_init(&rt->session);
    kittyts_options_init(&options);
    if (kittyts_start(&rt->session, STDIN_FILENO, STDOUT_FILENO,
                      &options) == 0) {
        rt->session_started = true;
        rt->model.cell_only = false;
        return 0;
    }
    if (errno != ENOTSUP) {
        (void)fprintf(stderr, "kilix-playalong: %s\n", strerror(errno));
        return -1;
    }
    /*
     * No graphics path.  That is a reason to draw the player as text, not a
     * reason to refuse to play the song: the same session without the
     * graphics probe still gives raw mode, the alternate screen and decoded
     * keys, and every frame goes out as cells.
     */
    options.framebuffer.probe_graphics = false;
    if (kittyts_start(&rt->session, STDIN_FILENO, STDOUT_FILENO,
                      &options) != 0) {
        (void)fprintf(stderr, "kilix-playalong: %s\n", strerror(errno));
        return -1;
    }
    rt->session_started = true;
    rt->model.cell_only = true;
    return 0;
}

int kpa_ui_run(const char *project_id)
{
    kpa_ui_runtime *rt;
    int status;

    rt = calloc(1u, sizeof *rt);
    if (rt == NULL) return 1;
    rt->cell_fd = -1;
    rt->model.view = KPA_VIEW_LIBRARY;
    rt->model.lyrics_visible = true;
    rt->model.tab_visible = true;
    rt->model.rate = 1.0;
    rt->model.active_cue = -1;
    rt->model.active_word = -1;
    runtime_refresh_library(rt);
    /* A NULL id opens the library rather than a song: the list of what this
     * machine has needs no project and no sound card. */
    if (project_id != NULL && runtime_open_project(rt, project_id) != 0) {
        (void)fprintf(stderr, "kilix-playalong: %s\n", rt->model.notice);
        runtime_close_project(rt);
        free(rt);
        return 2;
    }
    if (runtime_start_session(rt) != 0) {
        runtime_teardown(rt);
        free(rt);
        return 1;
    }
    /*
     * The overlay writes to its own descriptor.  kitty-framebuffer puts
     * O_NONBLOCK on the file description behind stdout, and a short write is
     * a hard failure for the cell writer, so a fresh open of the controlling
     * terminal keeps the text plane on a blocking path.  Falling back to
     * stdout is safe: the writer latches and stops touching the terminal.
     */
    rt->cell_fd = open("/dev/tty", O_WRONLY | O_CLOEXEC);
    rt->cell_fd_owned = rt->cell_fd >= 0;
    if (rt->cell_fd < 0) rt->cell_fd = STDOUT_FILENO;
    rt->cells = kpa_cells_create(rt->cell_fd);
    if (!rt->model.cell_only &&
        !runtime_resize(rt, kittyts_width(&rt->session),
                        kittyts_height(&rt->session))) {
        runtime_teardown(rt);
        free(rt);
        return 1;
    }
    install_signal_handlers(&rt->session, true);
    rt->signals_installed = true;
    status = runtime_loop(rt);
    runtime_teardown(rt);
    free(rt);
    return status;
}
