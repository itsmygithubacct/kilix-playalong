/*
 * The native Kilix surface for kilix-playalong.
 *
 * Five decisions shape this file.
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
 * invert_string_axis() for the row a string is drawn on, used by both the
 * names and the notes, and player_string_number() for the number a player is
 * told.  The string-name gutter is drawn at a constant x outside the lane's
 * clip, so it cannot translate with the notes the way the browser's did.
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

#include "kitty_terminal_session.h"
#include "soft_raster.h"

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
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
 * The lane's row order and the API's string order are inverses of each other,
 * and it is the same inversion read either way: display row 0 is the top of
 * the lane and holds the highest string, and the highest string is index
 * string_count - 1.  One function, used for the row a note lands on and for
 * the name printed beside it, because two copies of this arithmetic are two
 * things that can drift - which is how the browser ended up drawing its
 * labels in one order and its notes in another.
 */
static uint32_t invert_string_axis(uint32_t index, uint32_t string_count)
{
    if (string_count == 0u || index >= string_count) return 0u;
    return string_count - 1u - index;
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

    /* Strings and their names, high to low. */
    for (row = 0u; row < strings; ++row) {
        const uint32_t api = invert_string_axis(row, strings);
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
                /* The same inversion the rows above were laid out with:
                 * api index 0 is the low E and belongs at the bottom. */
                note_row = invert_string_axis((uint32_t)note->string_index,
                                              strings);
                y = caption + (int)note_row * row_h + row_h / 2;
                colour = model->position >= item->start &&
                         model->position <= end ? KPA_UI_ACCENT : KPA_UI_LOOP;
                sr_fill_rect(canvas, x0, (float)(y - row_h / 2 + 2),
                             x1 - x0, (float)(row_h - 4), colour, 0.85f);
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
    const bool notice = model->notice[0] != '\0';
    /* The notice gets its own line rather than being drawn over the lane. */
    const int floor_y = notice && bottom - KPA_UI_LINE > top
        ? bottom - KPA_UI_LINE : bottom;
    int y = top + 2;

    y = draw_transport(canvas, model, y, floor_y);
    y = draw_timeline(canvas, model, y, floor_y);
    y = draw_mixer(canvas, model, y, floor_y);
    if (model->tab_visible) {
        draw_tab_lane(canvas, model, y, floor_y);
    } else if (y + KPA_UI_LINE <= floor_y) {
        draw_text_fit(canvas, KPA_UI_MARGIN, y + 2,
                      "tab hidden - t shows it", KPA_UI_DIM,
                      canvas->w - 2 * KPA_UI_MARGIN);
    }
    if (floor_y < bottom) {
        draw_text_fit(canvas, KPA_UI_MARGIN, floor_y + 2, model->notice,
                      KPA_UI_WARN, canvas->w - 2 * KPA_UI_MARGIN);
    }
}

/* ----------------------------------------------------------- help view */

static const char *const help_lines[] = {
    "space      play / pause",
    "left right seek 5s, with shift 30s",
    "[ ]        set loop start / end      backspace clears the loop",
    "1 .. 6     select a stem             m mute      s solo",
    "v          mute or unmute vocals     + - selected stem gain",
    ", .        practice rate down / up   (when a rate engine is present)",
    "l          show or hide lyrics       t show or hide the tab lane",
    "tab        move focus                shift-tab moves it back",
    "escape     leave this view; from the library it leaves the player",
    "q          quit                      ? this page",
    "",
    "lyrics and vocals are separate: hiding the words never mutes the",
    "singer, and muting the singer never hides the words.  the same holds",
    "for the tab lane and the guitar stem."
};

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
                      index < 10u ? KPA_UI_TEXT : KPA_UI_DIM, width);
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

static void write_lyric_band(kpa_ui_runtime *rt, const kpa_ui_grid *grid,
                             int first, int count)
{
    char line[KPA_UI_LINE_CAPACITY];
    const kpa_lyrics *lyrics = rt->model.lyrics;
    const int centre = count / 2;
    int offset;

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
    const uint32_t api = invert_string_axis(display_row, strings);
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
                  index < 10u ? KPA_UI_TEXT : KPA_UI_DIM);
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
    cell_transport_line(model, line, sizeof line);
    write_row(rt, grid, row++, line, KPA_UI_TEXT);
    if (row <= last) write_row(rt, grid, row++, "", KPA_UI_DIM);
    for (index = 0u; index < model->track_count && row <= last; ++index) {
        cell_mixer_line(model, index, line, sizeof line);
        write_row(rt, grid, row++, line,
                  index == model->selected_track ? KPA_UI_TEXT : KPA_UI_DIM);
    }
    if (model->tab_visible) {
        uint32_t strings = KPA_STRING_COUNT;
        uint32_t display;

        if (model->tab != NULL && model->tab->string_count > 0u &&
            model->tab->string_count <= KPA_STRING_COUNT) {
            strings = model->tab->string_count;
        }
        if (row <= last) write_row(rt, grid, row++, "", KPA_UI_DIM);
        for (display = 0u; display < strings && row <= last; ++display) {
            cell_tab_line(model, display, strings, grid->columns, line,
                          sizeof line);
            write_row(rt, grid, row++, line, KPA_UI_DIM);
        }
    }
    if (model->lyrics_visible && model->lyrics != NULL && row < last) {
        int available = last - row;

        if (available > KPA_UI_MAX_LYRIC_ROWS) {
            available = KPA_UI_MAX_LYRIC_ROWS;
        }
        write_row(rt, grid, row++, "", KPA_UI_DIM);
        write_lyric_band(rt, grid, row, available);
        row += available;
    }
    return row;
}

/* --------------------------------------------------------------- frames */

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
            write_row(rt, &grid, layout.status_row,
                      "space play   arrows seek   [ ] loop   1-6 stem   "
                      "m mute   s solo   v vocals   l lyrics   t tab   "
                      "? help   q quit", KPA_UI_DIM);
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
