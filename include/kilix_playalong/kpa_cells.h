#ifndef KILIX_PLAYALONG_KPA_CELLS_H
#define KILIX_PLAYALONG_KPA_CELLS_H

/*
 * UTF-8 terminal-cell overlay.
 *
 * kitty-framebuffer places its image at a negative z-index, which leaves the
 * terminal's own foreground cells visible on top.  That is the only honest way
 * to put song lyrics on this surface: the raster font is ASCII bitmaps, and a
 * lyric line is whatever the song is written in.
 *
 * This module validates UTF-8, measures width the way the terminal will
 * (East Asian wide characters count two columns, combining marks count zero),
 * truncates on a character boundary rather than mid-sequence, clears the stale
 * remainder of a row so a shorter line cannot leave the tail of a longer one,
 * and brackets the whole update in a synchronised-update sequence so a frame
 * is never seen half drawn.
 */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Columns the string occupies, or -1 when it is not valid UTF-8. */
int kpa_cells_width(const char *text, size_t length);

/*
 * Longest prefix of `text` that fits in `columns`, in bytes.  Never splits a
 * multi-byte sequence and never strands a combining mark from its base.
 */
size_t kpa_cells_fit(const char *text, size_t length, int columns);

/* True when every byte is a valid, non-overlong, non-surrogate sequence. */
bool kpa_cells_valid_utf8(const char *text, size_t length);

typedef struct kpa_cells_writer kpa_cells_writer;

kpa_cells_writer *kpa_cells_create(int output_fd);
void kpa_cells_destroy(kpa_cells_writer *writer);

void kpa_cells_begin(kpa_cells_writer *writer);
/*
 * Draw one row.  `row` and `column` are 1-based terminal coordinates.  The
 * remainder of the row is cleared, which is what keeps a wide glyph from a
 * previous frame from leaving half of itself behind.
 */
void kpa_cells_row(kpa_cells_writer *writer, int row, int column, int columns,
                   const char *text, size_t length, uint32_t rgb);
void kpa_cells_clear_row(kpa_cells_writer *writer, int row, int columns);
void kpa_cells_end(kpa_cells_writer *writer);

#ifdef __cplusplus
}
#endif

#endif
