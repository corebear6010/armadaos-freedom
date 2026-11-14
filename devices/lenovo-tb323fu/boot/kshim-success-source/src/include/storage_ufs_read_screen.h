/* SPDX-License-Identifier: GPL-3.0-only */
#ifndef KSHIM_STORAGE_UFS_READ_SCREEN_H
#define KSHIM_STORAGE_UFS_READ_SCREEN_H
#include <framebuffer.h>
#include <stdint.h>
#include <storage_ufs_read_probe.h>

/* Pure, allocation-free diagnostic renderer for the read-only UFS sequence.
 *
 * It only paints the top strip of an already validated framebuffer and never
 * reads a register, calls the storage layer, mutates the report or decides
 * whether a handoff is safe. The caller owns the pixel memory, its MMU mapping
 * and the display state; an invalid or null framebuffer writes nothing.
 *
 * One frame is six fixed 5x7 glyph lines at scale 4 (20x28 pixels, 24 pixel
 * advance), white on the framebuffer's own foreground/background colours, all
 * inside the first KSHIM_UFS_READ_SCREEN_STRIP_HEIGHT rows. With a link state
 * it grows to ten lines inside KSHIM_UFS_READ_SCREEN_STRIP_HEIGHT_MAX rows:
 * the legacy six lines plus link state, device power and SCSI error details.
 * A NULL link keeps the exact legacy six-line profile. Everything below the
 * painted strip is left untouched, so a later full-screen UI simply overwrites
 * it. The stage/release fields are printed as raw 32-bit two's-complement hex:
 * a value like FFFFFFF6 is the signed kshim_storage_result -10, and
 * KSHIM_UFS_READ_UNRUN (1) marks a field whose result does not exist yet.
 *
 * The event number is only a label. It is never stored in the report and
 * never changes the sequence. */
#define KSHIM_UFS_READ_SCREEN_STRIP_HEIGHT 200U
#define KSHIM_UFS_READ_SCREEN_STRIP_HEIGHT_MAX 328U
enum kshim_ufs_read_screen_event {
    KSHIM_UFS_READ_SCREEN_ENTRY = 1,
    KSHIM_UFS_READ_SCREEN_CPU = 2,
    KSHIM_UFS_READ_SCREEN_CPU_DONE = 3,
    KSHIM_UFS_READ_SCREEN_SMMU = 4,
    KSHIM_UFS_READ_SCREEN_SMMU_DONE = 5,
    KSHIM_UFS_READ_SCREEN_READ = 6,
    KSHIM_UFS_READ_SCREEN_READ_ERR = 7,
    KSHIM_UFS_READ_SCREEN_WRITE = 8,
    KSHIM_UFS_READ_SCREEN_WRITE_DONE = 9,
    KSHIM_UFS_READ_SCREEN_WRITE_ERR = 10,
    KSHIM_UFS_READ_SCREEN_SYNC_DEVICE = 11,
    KSHIM_UFS_READ_SCREEN_SYNC_DEVICE_DONE = 12,
    KSHIM_UFS_READ_SCREEN_SYNC_CPU = 13,
    KSHIM_UFS_READ_SCREEN_SYNC_CPU_DONE = 14,
};

/* Returns KSHIM_FB_OK after painting and syncing, or the framebuffer helper's
 * negative status when the framebuffer or the report is unusable. Uses a small
 * fixed stack frame only: no static surface, no heap, no font blob. */
int kshim_ufs_read_screen_render(kshim_framebuffer_t *framebuffer,
                                 const struct kshim_ufs_read_report *report,
                                 uint32_t event, uint32_t offset, uint32_t value,
                                 const struct kshim_ufs_link_state *link);
#endif
