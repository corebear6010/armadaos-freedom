/* SPDX-License-Identifier: GPL-3.0-only */
#ifndef KSHIM_STORAGE_UFS_READ_NATIVE_H
#define KSHIM_STORAGE_UFS_READ_NATIVE_H
#include <device_info.h>
#include <storage_probe.h>
#include <storage_ufs_read_probe.h>

/* One-shot native adapter for the validated read-only UFS read sequence.
 *
 * storage_ufs_read_native_capture() is called exactly once at boot entry with
 * the actual board taken from the validated runtime FDT - never a menu
 * selection. It owns every precondition the pure sequence requires:
 *   - unsupported boards return KSHIM_UFS_UNSUPPORTED with
 *     STORAGE_PROBE_BOARD_UNSUPPORTED in the snapshot and touch no MMIO;
 *   - a zero CNTFRQ leaves the timer unusable and is rejected the same way;
 *   - the CPU mapping of the isolated arena is proved through
 *     storage_ufs_native_dma_address() before the first HBA write;
 *   - a fresh SMMU snapshot is captured and its identity mapping checked
 *     against that physical base immediately before the claim.
 * Only then are the native MMIO/sync/timer ops installed and the pure
 * kshim_ufs_read_probe() called. The arena, scratch buffer, host and report
 * are static, aligned and zero-initialized; nothing is allocated, reset,
 * retried or released a second time. The host, arena and mapping stay
 * resident until the release inside that call completes.
 *
 * report->handoff_safe is the only handoff decision: zero means no payload may
 * start. The snapshot is evidence only and never permission to start DMA. */
void storage_ufs_read_native_capture(enum device_board actual_board,
                                     struct storage_probe_snapshot *fresh);
/* The single static report written by that capture; the menu title and the
 * FDT export read it after the capture returned. */
const struct kshim_ufs_read_report *storage_ufs_read_entry_report(void);
#if defined(CONFIG_KSHIM_UFS_READ_SCREEN) && CONFIG_KSHIM_UFS_READ_SCREEN
/* Native diagnostic screen (SCREEN profile only; absent in every other build).
 *
 * storage_ufs_read_screen_init() reuses the existing read-only
 * kshim_dt_splash() to learn the live scanout, validates it with
 * kshim_fb_init() and maps exactly that span non-cacheable for write. It never
 * reinitialises the display controller, never writes outside the scanout RAM
 * and returns non-zero when the display is unavailable or the mapping is
 * rejected, so the IO behaviour of the sequence is unchanged either way.
 * storage_ufs_read_screen_show() paints one frame and does nothing until the
 * init succeeded; it never reads a register or touches the report. The
 * optional link is only displayed; a NULL link keeps the legacy six lines. */
int storage_ufs_read_screen_init(const void *fdt);
int storage_ufs_read_screen_ready(void);
void storage_ufs_read_screen_show(const struct kshim_ufs_read_report *report,
                                  uint32_t event, uint32_t offset,
                                  uint32_t value,
                                  const struct kshim_ufs_link_state *link);
#endif
#endif
