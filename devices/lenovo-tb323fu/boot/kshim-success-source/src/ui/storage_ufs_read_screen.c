/* SPDX-License-Identifier: GPL-3.0-only */
#include <storage_ufs_read_screen.h>
#include <stdio.h>

/* Fixed 5x7 cell, scale 4: 20x28 pixels with a 4 pixel gap. Up to ten lines
 * of 40 columns fit the 24 pixel advance inside the 328 row link strip; the
 * legacy NULL-link profile keeps its six lines inside 200 rows. A tiny local
 * glyph table is enough for this fixed diagnostic vocabulary; the repository
 * has no reusable small ASCII font and this must not grow a font subsystem. */
enum {
    GLYPH_WIDTH = 5,
    GLYPH_HEIGHT = 7,
    GLYPH_SCALE = 4,
    GLYPH_ADVANCE = 24,
    LEGACY_LINE_COUNT = 6,
    LINE_COUNT = 10,
    LINE_BYTES = 48,
    LINE_COLUMNS = 40,
    LINE_ORIGIN_X = 8,
    LINE_ORIGIN_Y = 8,
    LINE_HEIGHT = 32,
};

typedef struct {
    char code;
    uint8_t rows[GLYPH_HEIGHT];
} glyph_t;

/* Bit 4 of each row is the leftmost column. Only the characters the fixed
 * lines need are present; anything else renders as '?'. */
static const glyph_t Glyphs[] = {
    {' ', {0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00}},
    {'-', {0x00, 0x00, 0x00, 0x1f, 0x00, 0x00, 0x00}},
    {'0', {0x0e, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0e}},
    {'1', {0x04, 0x0c, 0x04, 0x04, 0x04, 0x04, 0x0e}},
    {'2', {0x0e, 0x11, 0x01, 0x02, 0x04, 0x08, 0x1f}},
    {'3', {0x1f, 0x02, 0x04, 0x02, 0x01, 0x11, 0x0e}},
    {'4', {0x02, 0x06, 0x0a, 0x12, 0x1f, 0x02, 0x02}},
    {'5', {0x1f, 0x10, 0x1e, 0x01, 0x01, 0x11, 0x0e}},
    {'6', {0x06, 0x08, 0x10, 0x1e, 0x11, 0x11, 0x0e}},
    {'7', {0x1f, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08}},
    {'8', {0x0e, 0x11, 0x11, 0x0e, 0x11, 0x11, 0x0e}},
    {'9', {0x0e, 0x11, 0x11, 0x0f, 0x01, 0x02, 0x0c}},
    {'A', {0x0e, 0x11, 0x11, 0x1f, 0x11, 0x11, 0x11}},
    {'B', {0x1e, 0x11, 0x11, 0x1e, 0x11, 0x11, 0x1e}},
    {'C', {0x0e, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0e}},
    {'D', {0x1c, 0x12, 0x11, 0x11, 0x11, 0x12, 0x1c}},
    {'E', {0x1f, 0x10, 0x10, 0x1e, 0x10, 0x10, 0x1f}},
    {'F', {0x1f, 0x10, 0x10, 0x1e, 0x10, 0x10, 0x10}},
    {'G', {0x0e, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0f}},
    {'H', {0x11, 0x11, 0x11, 0x1f, 0x11, 0x11, 0x11}},
    {'I', {0x0e, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0e}},
    {'K', {0x11, 0x12, 0x14, 0x18, 0x14, 0x12, 0x11}},
    {'L', {0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1f}},
    {'M', {0x11, 0x1b, 0x15, 0x15, 0x11, 0x11, 0x11}},
    {'N', {0x11, 0x19, 0x19, 0x15, 0x13, 0x13, 0x11}},
    {'O', {0x0e, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0e}},
    {'P', {0x1e, 0x11, 0x11, 0x1e, 0x10, 0x10, 0x10}},
    {'R', {0x1e, 0x11, 0x11, 0x1e, 0x14, 0x12, 0x11}},
    {'S', {0x0f, 0x10, 0x10, 0x0e, 0x01, 0x01, 0x1e}},
    {'T', {0x1f, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04}},
    {'U', {0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0e}},
    {'V', {0x11, 0x11, 0x11, 0x11, 0x11, 0x0a, 0x04}},
    {'W', {0x11, 0x11, 0x11, 0x15, 0x15, 0x15, 0x0a}},
    {'X', {0x11, 0x11, 0x0a, 0x04, 0x0a, 0x11, 0x11}},
    {'?', {0x0e, 0x11, 0x01, 0x02, 0x04, 0x00, 0x04}},
};

static const uint8_t *glyph_rows(char code)
{
    for (size_t index = 0; index < sizeof(Glyphs) / sizeof(Glyphs[0]); ++index)
        if (Glyphs[index].code == code) return Glyphs[index].rows;
    return Glyphs[sizeof(Glyphs) / sizeof(Glyphs[0]) - 1].rows; /* '?' */
}

/* Rows expand to horizontal runs, so a glyph costs a handful of clipped
 * rectangles instead of 35 single-pixel stores. */
static void draw_glyph(kshim_framebuffer_t *framebuffer, int32_t x, int32_t y,
                       const uint8_t *rows)
{
    for (uint32_t row = 0; row < GLYPH_HEIGHT; ++row) {
        uint8_t bits = (uint8_t)(rows[row] & 0x1fu);
        uint32_t column = 0;
        while (column < GLYPH_WIDTH) {
            uint32_t start;
            if (((bits >> (GLYPH_WIDTH - 1u - column)) & 1u) == 0u) {
                ++column;
                continue;
            }
            start = column;
            while (column < GLYPH_WIDTH &&
                   ((bits >> (GLYPH_WIDTH - 1u - column)) & 1u) != 0u)
                ++column;
            if (kshim_fb_fill_rect(framebuffer,
                                   x + (int32_t)(start * GLYPH_SCALE),
                                   y + (int32_t)(row * GLYPH_SCALE),
                                   (column - start) * GLYPH_SCALE,
                                   GLYPH_SCALE, true) != KSHIM_FB_OK)
                return;
        }
    }
}

static void draw_text(kshim_framebuffer_t *framebuffer, int32_t x, int32_t y,
                      const char *text)
{
    for (uint32_t column = 0; column < LINE_COLUMNS && text[column] != '\0';
         ++column)
        draw_glyph(framebuffer, x + (int32_t)(column * GLYPH_ADVANCE), y,
                   glyph_rows(text[column]));
}

static void format_lines(char lines[LINE_COUNT][LINE_BYTES],
                         const struct kshim_ufs_read_report *report,
                         uint32_t event, uint32_t offset, uint32_t value,
                         const struct kshim_ufs_link_state *link)
{
    (void)snprintf(lines[0], LINE_BYTES, "UFS%s ST %02X OP %02X REG %08X",
                   link ? "10" : "5",
                   (unsigned)report->stage, (unsigned)event,
                   (unsigned)offset);
    (void)snprintf(lines[1], LINE_BYTES, "VAL %08X RC %08X", (unsigned)value,
                   (unsigned)report->status);
    (void)snprintf(lines[2], LINE_BYTES, "REL %08X SAFE %u REST %u",
                   (unsigned)report->release_status,
                   (unsigned)(report->handoff_safe ? 1u : 0u),
                   (unsigned)(report->restore_verified ? 1u : 0u));
    (void)snprintf(lines[3], LINE_BYTES, "IS %08X DB %08X HCS %08X",
                   (unsigned)report->is, (unsigned)report->doorbell,
                   (unsigned)report->hcs);
    (void)snprintf(lines[4], LINE_BYTES, "OCS %08X VALID %08X",
                   (unsigned)report->ocs, (unsigned)report->debug_valid);
    (void)snprintf(lines[5], LINE_BYTES, "PA %08X %08X",
                   (unsigned)(uint32_t)(report->dma_base >> 32),
                   (unsigned)(uint32_t)report->dma_base);
    if (!link)
        return;
    (void)snprintf(lines[6], LINE_BYTES, "LNK %02X CMD %02X UIC %08X",
                   (unsigned)(link->phase & 0xffU),
                   (unsigned)(link->last_command & 0xffU),
                   (unsigned)link->command_result);
    (void)snprintf(lines[7], LINE_BYTES, "TX %08X %08X PWR %X UNC %u",
                   (unsigned)link->initial_tx, (unsigned)link->current_tx,
                   (unsigned)(link->power_status & 0x7U),
                   (unsigned)(link->uncertain ? 1u : 0u));
    const struct kshim_ufs_power_state *p = report->status ?
        &report->power_before_release : &report->power_after_release;
    const struct kshim_ufs_scsi_state *d = report->status ?
        &report->scsi_before_release : &report->scsi_after_release;
    (void)snprintf(lines[8], LINE_BYTES, "DEV I %02X C %02X P %X R %u U %u",
                   (unsigned)(p->initial_mode & 255), (unsigned)(p->current_mode & 255),
                   (unsigned)(p->phase & 15), (unsigned)(p->restore_mode ? 1 : 0),
                   (unsigned)(p->uncertain ? 1 : 0));
    (void)snprintf(lines[9], LINE_BYTES, "SCSI %02X L %02X ST %02X K %X A %02X %02X V %X",
                   (unsigned)(d->opcode & 255), (unsigned)(d->lun & 255),
                   (unsigned)(d->status & 255), (unsigned)(d->sense_key & 15),
                   (unsigned)(d->asc & 255), (unsigned)(d->ascq & 255),
                   (unsigned)(d->sense_valid & 3));
}

int kshim_ufs_read_screen_render(kshim_framebuffer_t *framebuffer,
                                 const struct kshim_ufs_read_report *report,
                                 uint32_t event, uint32_t offset, uint32_t value,
                                 const struct kshim_ufs_link_state *link)
{
    char lines[LINE_COUNT][LINE_BYTES];
    uint32_t strip_height = link ? KSHIM_UFS_READ_SCREEN_STRIP_HEIGHT_MAX
                                 : KSHIM_UFS_READ_SCREEN_STRIP_HEIGHT;
    uint32_t line_count = link ? LINE_COUNT : LEGACY_LINE_COUNT;
    kshim_fb_status_t status;

    if (framebuffer == NULL || report == NULL)
        return KSHIM_FB_ERR_INVALID_ARGUMENT;
    /* The strip clear doubles as the validation gate: an unusable framebuffer
     * returns its status here, before a single pixel is stored. */
    status = kshim_fb_fill_rect(framebuffer, 0, 0, framebuffer->width,
                                strip_height, false);
    if (status != KSHIM_FB_OK) return (int)status;

    format_lines(lines, report, event, offset, value, link);
    for (uint32_t line = 0; line < line_count; ++line)
        draw_text(framebuffer, LINE_ORIGIN_X,
                  LINE_ORIGIN_Y + (int32_t)(line * LINE_HEIGHT), lines[line]);
    /* Only this strip was written, but the display still needs the barrier. */
    kshim_fb_sync();
    return KSHIM_FB_OK;
}
