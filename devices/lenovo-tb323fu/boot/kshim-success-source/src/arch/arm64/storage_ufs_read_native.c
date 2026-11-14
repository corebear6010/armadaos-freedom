/* SPDX-License-Identifier: GPL-3.0-only */
#include <config.h>
#include <storage_ufs_read_native.h>
#include <storage_probe_io.h>
#include <storage_ufs_dma.h>
#include <storage_ufs_native_io.h>
#include <timer.h>
#include <string.h>
#if defined(CONFIG_KSHIM_UFS_READ_SCREEN) && CONFIG_KSHIM_UFS_READ_SCREEN
#include <storage_ufs_read_screen.h>
#define KSHIM_UFS_SCREEN 1
#else
#define KSHIM_UFS_SCREEN 0
#endif

/* Everything below is single-shot static storage: the arena and scratch are
 * page-aligned because the CPU mapping proof checks that alignment, and the
 * host is zero-initialized by the loader and never reset after its one claim. */
static _Alignas(4096) uint8_t ReadArena[KSHIM_UFS_DMA_BYTES];
static _Alignas(4096) uint8_t ReadScratch[KSHIM_UFS_DMA_BYTES];
static struct kshim_ufs_host ReadHost;
static struct kshim_ufs_read_report ReadReport;

#if KSHIM_UFS_SCREEN
/* Last painted frame. A busy-wait that re-issues the same read must not add a
 * draw, and the late stages (GPT reads many blocks) redraw once per stage so
 * the observation stays coarse instead of perturbing every transaction. */
static uint32_t ScreenStage, ScreenEvent, ScreenOffset, ScreenValue, ScreenPhase;
static uint32_t ScreenPowerPhase;

/* The link state is plain static memory already maintained by the transport;
 * showing it never adds an MMIO access. Only the opt-in profile exposes it. */
static const struct kshim_ufs_link_state *screen_link(void)
{
#if defined(CONFIG_KSHIM_UFS_LINK_WAKE) && CONFIG_KSHIM_UFS_LINK_WAKE
    return &ReadHost.link;
#else
    return NULL;
#endif
}

static uint32_t screen_link_phase(void)
{
    const struct kshim_ufs_link_state *link = screen_link();

    return link ? link->phase : 0U;
}

static void screen_show(uint32_t event, uint32_t offset, uint32_t value)
{
    uint32_t phase = screen_link_phase();
    uint32_t power_phase = ReadHost.power.phase;

    if (!storage_ufs_read_screen_ready()) return;
    /* A phase change during the restore is a new observation even for the
     * coarse late stages, so it must repaint instead of being deduplicated. */
    if (ReadReport.stage >= KSHIM_UFS_READ_STAGE_LUN) {
        if (ScreenStage == ReadReport.stage && ScreenPhase == phase &&
            ScreenPowerPhase == power_phase) return;
    } else if (ScreenStage == ReadReport.stage && ScreenEvent == event &&
               ScreenOffset == offset && ScreenValue == value &&
               ScreenPhase == phase && ScreenPowerPhase == power_phase) {
        return;
    }
    ScreenStage = ReadReport.stage;
    ScreenEvent = event;
    ScreenOffset = offset;
    ScreenValue = value;
    ScreenPhase = phase;ScreenPowerPhase = power_phase;
    /* A local view keeps live progress separate from the saved fault report. */
    struct kshim_ufs_read_report live = ReadReport;
    live.power_before_release = live.power_after_release = ReadHost.power;
    live.scsi_before_release = live.scsi_after_release =
        (struct kshim_ufs_scsi_state){ReadHost.last_scsi_opcode,ReadHost.last_scsi_lun,
            ReadHost.last_scsi_status,ReadHost.last_sense_key,ReadHost.last_sense_asc,
            ReadHost.last_sense_ascq,ReadHost.last_sense_valid};
    storage_ufs_read_screen_show(&live, event, offset, value, screen_link());
}

/* Paints the final report unconditionally, including an unsafe one: the screen
 * never decides anything and the caller's existing halt keeps it visible. */
static void screen_final(void)
{
    const struct kshim_ufs_link_state *link;

    if (!storage_ufs_read_screen_ready()) return;
    /* The live record cannot explain a failed read: release rewrites
     * host->link, so it would show the cleanup's own phase 6 (or its failure)
     * instead of the state the read failed in. The probe saved a before-release
     * copy, which is the one that retains that cause; after a successful read
     * the after-release copy records what cleanup left behind, and a zero phase
     * in either copy means no link work ran, so the live view stays in use. */
    link = screen_link();
    if (link != NULL) {
        if (ReadReport.status != 0) {
            if (ReadReport.link_before_release.phase != 0)
                link = &ReadReport.link_before_release;
        } else if (ReadReport.link_after_release.phase != 0) {
            link = &ReadReport.link_after_release;
        }
    }
    ScreenStage = ReadReport.stage;
    ScreenPhase = screen_link_phase();
    storage_ufs_read_screen_show(&ReadReport, ScreenEvent, ScreenOffset,
                                 ScreenValue, link);
}
#endif

static int read32(void *ctx, uint32_t offset, uint32_t *value)
{
    int status;
#if KSHIM_UFS_SCREEN
    /* Shown before the access, so a stall here is visible as READ with no
     * following event. A successful read is implied by the next operation. */
    screen_show(KSHIM_UFS_READ_SCREEN_READ, offset, 0);
#endif
    status = storage_ufs_native_read32(ctx, offset, value);
#if KSHIM_UFS_SCREEN
    if (status)
        screen_show(KSHIM_UFS_READ_SCREEN_READ_ERR, offset, (uint32_t)status);
#endif
    return status;
}
static int write32(void *ctx, uint32_t offset, uint32_t value)
{
    int status;
#if KSHIM_UFS_SCREEN
    screen_show(KSHIM_UFS_READ_SCREEN_WRITE, offset, value);
#endif
    status = storage_ufs_native_write32(ctx, offset, value);
#if KSHIM_UFS_SCREEN
    /* A completed write is its own event, so a stall at the actual STR shows
     * as WRITE without the matching WRITE_DONE. */
    if (status)
        screen_show(KSHIM_UFS_READ_SCREEN_WRITE_ERR, offset, (uint32_t)status);
    else
        screen_show(KSHIM_UFS_READ_SCREEN_WRITE_DONE, offset, value);
#endif
    return status;
}
static void sync_device(void *ctx, void *buffer, size_t bytes)
{
#if KSHIM_UFS_SCREEN
    screen_show(KSHIM_UFS_READ_SCREEN_SYNC_DEVICE, 0, (uint32_t)bytes);
#endif
    storage_ufs_native_sync_device(ctx, buffer, bytes);
#if KSHIM_UFS_SCREEN
    screen_show(KSHIM_UFS_READ_SCREEN_SYNC_DEVICE_DONE, 0, (uint32_t)bytes);
#endif
}
static void sync_cpu(void *ctx, void *buffer, size_t bytes)
{
#if KSHIM_UFS_SCREEN
    screen_show(KSHIM_UFS_READ_SCREEN_SYNC_CPU, 0, (uint32_t)bytes);
#endif
    storage_ufs_native_sync_cpu(ctx, buffer, bytes);
#if KSHIM_UFS_SCREEN
    screen_show(KSHIM_UFS_READ_SCREEN_SYNC_CPU_DONE, 0, (uint32_t)bytes);
#endif
}
/* get_current_time() is milliseconds, the unit the transport expects. */
static uint64_t now_ms(void *ctx)
{
    (void)ctx;
    return get_current_time();
}
/* delay() only spins; no interrupt or arch-timer initialization happens. */
static void delay_us(void *ctx, uint32_t micro_seconds)
{
    (void)ctx;
    (void)delay((uint64_t)micro_seconds);
}
static const struct kshim_ufs_ops ReadOps = {
    .ctx = NULL,
    .read32 = read32,
    .write32 = write32,
    .sync_device = sync_device,
    .sync_cpu = sync_cpu,
    .now_ms = now_ms,
    .delay_us = delay_us,
#if defined(CONFIG_KSHIM_UFS_LINK_WAKE) && CONFIG_KSHIM_UFS_LINK_WAKE
    .manage_link = true,
#endif
};

void storage_ufs_read_native_capture(enum device_board actual_board,
                                     struct storage_probe_snapshot *fresh)
{
    uint64_t physical = 0;
    int status;

    /* Pre-IO state: nothing has been claimed, so a handoff would be safe and
     * the unrun sentinel plus a real release result are still consistent. */
    memset(&ReadReport, 0, sizeof(ReadReport));
    ReadReport.version = KSHIM_UFS_READ_REPORT_VERSION;
    ReadReport.board = (uint32_t)actual_board;
    ReadReport.stage = KSHIM_UFS_READ_STAGE_CPU;
    ReadReport.status = KSHIM_UFS_READ_UNRUN;
    ReadReport.release_status = KSHIM_STORAGE_OK;
    ReadReport.restore_verified = 1;
    ReadReport.handoff_safe = 1;
#if KSHIM_UFS_SCREEN
    ScreenStage = ScreenEvent = ScreenOffset = ScreenValue = ScreenPhase = ScreenPowerPhase = 0;
    screen_show(KSHIM_UFS_READ_SCREEN_ENTRY, 0, 0);
#endif
    if (!fresh) {
        ReadReport.status = KSHIM_STORAGE_ARG;
#if KSHIM_UFS_SCREEN
        screen_final();
#endif
        return;
    }

    memset(fresh, 0, sizeof(*fresh));
    fresh->version = STORAGE_PROBE_VERSION;
    fresh->board = actual_board;
    fresh->status = STORAGE_PROBE_ENV_UNAVAILABLE;

    if (actual_board != DEVICE_BOARD_TB322FC &&
        actual_board != DEVICE_BOARD_TB323FU) {
        ReadReport.status = KSHIM_UFS_UNSUPPORTED;
        fresh->status = STORAGE_PROBE_BOARD_UNSUPPORTED;
#if KSHIM_UFS_SCREEN
        screen_final();
#endif
        return;
    }
    /* A missing counter frequency makes every bounded wait meaningless; treat
     * it as unsupported instead of starting the claim with a broken clock. */
    if (arm64_read_cntfrq() == 0) {
        ReadReport.status = KSHIM_UFS_UNSUPPORTED;
#if KSHIM_UFS_SCREEN
        screen_final();
#endif
        return;
    }
    /* CPU VA->PA and contiguity must be proved before the first HBA write. A
     * failure stops at the CPU stage with the helper's code and no DMA. */
#if KSHIM_UFS_SCREEN
    screen_show(KSHIM_UFS_READ_SCREEN_CPU, 0, 0);
#endif
    status = storage_ufs_native_dma_address(ReadArena, KSHIM_UFS_DMA_BYTES,
                                            &physical);
#if KSHIM_UFS_SCREEN
    screen_show(KSHIM_UFS_READ_SCREEN_CPU_DONE, 0, (uint32_t)status);
#endif
    if (status) {
        ReadReport.status = status;
#if KSHIM_UFS_SCREEN
        screen_final();
#endif
        return;
    }
    ReadReport.stage = KSHIM_UFS_READ_STAGE_SMMU;
#if KSHIM_UFS_SCREEN
    screen_show(KSHIM_UFS_READ_SCREEN_SMMU, 0, 0);
#endif
    /* A fresh snapshot, taken here and not an older capture or a menu choice,
     * is the only evidence the identity decoder accepts. */
    storage_probe_native_capture(actual_board, fresh);
    if (!kshim_ufs_dma_is_identity(fresh, physical, KSHIM_UFS_DMA_BYTES)) {
        ReadReport.status = KSHIM_UFS_UNSUPPORTED;
#if KSHIM_UFS_SCREEN
        screen_show(KSHIM_UFS_READ_SCREEN_SMMU_DONE, 0,
                    (uint32_t)KSHIM_UFS_UNSUPPORTED);
        screen_final();
#endif
        return;
    }
#if KSHIM_UFS_SCREEN
    screen_show(KSHIM_UFS_READ_SCREEN_SMMU_DONE, 0, 0);
#endif
    (void)kshim_ufs_read_probe(&ReadHost, &ReadOps, ReadArena, physical,
                              ReadScratch, sizeof(ReadScratch), &ReadReport);
#if KSHIM_UFS_SCREEN
    /* Final report, including an unsafe release: the caller's DAIF+WFE halt
     * below then leaves it on screen. */
    screen_final();
#endif
}

const struct kshim_ufs_read_report *storage_ufs_read_entry_report(void)
{
    return &ReadReport;
}
