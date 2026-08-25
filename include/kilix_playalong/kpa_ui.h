#ifndef KILIX_PLAYALONG_KPA_UI_H
#define KILIX_PLAYALONG_KPA_UI_H

/*
 * The native Kilix surface.
 *
 * Rendering is split so that composition is a pure function of the view model
 * and can be asserted in a test that never opens a terminal: kpa_ui_compose
 * draws into a canvas, kpa_ui_run drives it.  Lyrics are the exception and
 * deliberately so - song text is UTF-8 and the embedded raster font is ASCII
 * bitmaps, so lyrics are written as terminal foreground cells over the
 * framebuffer's negative z-index rather than pretending the font can shape
 * them.  See kpa_cells.h.
 *
 * Audio state and display state are independent.  Vocals are a track that can
 * be muted; lyrics are a layer that can be hidden.  Hiding lyrics never mutes
 * vocals and muting vocals never hides lyrics; the same holds for the guitar
 * track and the tab layer.
 *
 * The practice view draws a fretboard as well as the rolling tab lane.  Both
 * are pictures of the same six strings and they are ordered by one
 * preference, low_string_on_top, so that two representations of one
 * instrument on one screen can never disagree about which string is which.
 */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "kilix_playalong/kpa_audio.h"
#include "kilix_playalong/kpa_project.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum kpa_view {
    KPA_VIEW_LIBRARY = 0,
    KPA_VIEW_PRACTICE = 1,
    KPA_VIEW_HELP = 2
} kpa_view;

/*
 * How much of the fretboard block is drawn.  The default is all of it: the
 * point of this screen is a guitar being played, so it opens showing one.
 */
typedef enum kpa_fretboard_mode {
    KPA_FB_NECK_AND_RAMP = 0,
    KPA_FB_NECK = 1,
    KPA_FB_OFF = 2
} kpa_fretboard_mode;

/* What is printed inside a sounding note: its fret, its note name, or both. */
typedef enum kpa_note_label {
    KPA_LABEL_FRET = 0,
    KPA_LABEL_NOTE = 1,
    KPA_LABEL_BOTH = 2
} kpa_note_label;

/* Whether the timeline carries the song's note density behind the playhead. */
typedef enum kpa_overview {
    KPA_OVERVIEW_PLAIN = 0,
    KPA_OVERVIEW_DENSITY = 1
} kpa_overview;

typedef struct kpa_ui_track {
    char label[KPA_TEXT_CAPACITY];
    char kind[KPA_ID_CAPACITY];
    float gain;
    bool muted;
    bool soloed;
} kpa_ui_track;

/*
 * Everything the surface draws, and nothing it does not.  A widget never
 * reaches past this into the audio session or the project, which is what
 * keeps the audio callback and the renderer from sharing state.
 */
typedef struct kpa_ui_model {
    kpa_view view;

    /* Library */
    const kpa_project_summary *summaries;
    uint32_t summary_count;
    uint32_t selected_project;

    /* Practice */
    const char *title;
    const char *artist;
    double position;          /* seconds, from the audible clock */
    double duration;
    bool playing;
    bool underrun;
    bool device_lost;
    double rate;
    bool rate_available;
    bool loop_active;
    double loop_start;
    double loop_end;

    kpa_ui_track tracks[KPA_MAX_TRACKS];
    uint32_t track_count;
    uint32_t selected_track;

    bool lyrics_visible;
    bool tab_visible;
    const kpa_lyrics *lyrics;
    int32_t active_cue;
    int32_t active_word;
    const kpa_tab *tab;

    /* A single bounded line; never a URL, a path or a capability. */
    char notice[KPA_TEXT_CAPACITY];

    /* True when the terminal has no graphics path and the surface is drawing
     * the cell-only fallback: transport, mixer and lyric/tab text, no pixels. */
    bool cell_only;

    /*
     * The fretboard and the practice aids around it.
     *
     * Every field's zero value is the intended default, which is what lets
     * all three of this program's model paths - calloc in kpa_ui_run, memset
     * in main.c's build_model, calloc in the test fixture - keep working
     * unchanged: a zeroed model opens on the neck with the approach ramp,
     * fret numbers, high e on top, a right-handed neck, no capo and a
     * two-second look-ahead.
     */
    kpa_fretboard_mode fretboard;
    kpa_note_label note_label;
    kpa_overview overview;
    bool low_string_on_top;   /* false is the tablature order, high e on top */
    bool left_handed;         /* false puts the nut on the left */
    float ramp_seconds;       /* 0.0f reads as the 2.0f default */
    uint8_t capo;             /* 0 = none; frets below it cannot be played */
    uint8_t lead_in;          /* 0 = off, else seconds rewound before playing */
    uint8_t speed_ramp;       /* 0 = off, else the percent a loop starts at */
    /*
     * The chord label the callout shows, latched by the surface's own
     * refresh so it does not flicker at the rate the notes change.  Empty
     * means the caller kept no history and composition should name the
     * chord it can see, which is the honest answer for a single still.
     */
    char chord[24];
    /* Non-zero when that label is a chord symbol rather than the list of
     * note names the surface falls back to, which is drawn dimmer. */
    uint8_t chord_kind;
} kpa_ui_model;

typedef struct sr_canvas sr_canvas;

/*
 * Pure composition.  Draws the pixel layer for `model` into `canvas`, which
 * the caller sized.  Lyric text is NOT drawn here: it is a cell overlay, and
 * kpa_ui_compose leaves its rows clear so foreground cells show through.
 */
void kpa_ui_compose(sr_canvas *canvas, const kpa_ui_model *model);

/*
 * Rows the cell overlay owns for this model and canvas height, in terminal
 * cells from the top.  Composition and the overlay must agree or lyrics will
 * be drawn over a filled panel.
 */
typedef struct kpa_ui_cell_layout {
    int title_row;
    int lyric_row;
    int lyric_row_count;
    int status_row;
    int columns;
    int rows;
} kpa_ui_cell_layout;

void kpa_ui_cell_layout_get(const kpa_ui_model *model, int columns, int rows,
                            int cell_height, kpa_ui_cell_layout *out);

/* Interactive surface.  Returns 0 on a clean exit. */
int kpa_ui_run(const char *project_id);

/*
 * Headless render of one model to a PPM, for acceptance evidence and for the
 * screenshot the release gate wants.  A picture of an empty player proves only
 * that the program starts, so callers pass a model with a real position.
 */
int kpa_ui_render_ppm(const kpa_ui_model *model, int width, int height,
                      const char *path);

#ifdef __cplusplus
}
#endif

#endif
