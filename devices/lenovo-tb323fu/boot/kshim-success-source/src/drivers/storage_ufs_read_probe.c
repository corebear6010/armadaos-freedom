/* SPDX-License-Identifier: GPL-3.0-only */
#ifndef KSHIM_HOST_TEST
#include <config.h>
#endif
#include <storage_ufs_read_probe.h>
#include <string.h>

/* Read-only diagnostic sequence: NOP, capacity, backup-GPT lookup, then one
 * header and one tail block read. The two GiB figure is the required ARMADA
 * partition size, not a device capacity; nothing here hardcodes a UUID or a
 * controller capacity. */
enum {
    REG_AHIT = 0x18, REG_IS = 0x20, REG_IE = 0x24, REG_HCS = 0x30,
    REG_BASE_LO = 0x50, REG_BASE_HI = 0x54, REG_DOORBELL = 0x58,
    REG_RUN = 0x60, REG_TASK_DB = 0x78, ARENA_RSP = 1536
};
#define ARMADA_PARTITION_BYTES UINT64_C(2147483648)
#define DEBUG_HCS 1u
#define DEBUG_IS 2u
#define DEBUG_DOORBELL 4u
#define DEBUG_ARENA 8u

static int reg_read(struct kshim_ufs_host *h, uint32_t offset, uint32_t *value)
{
    if (!h->ops.read32) return KSHIM_STORAGE_IO;
    return h->ops.read32(h->ops.ctx, offset, value) ? KSHIM_STORAGE_IO : 0;
}
/* Clears every result while keeping the wrapper-owned identity fields. */
static void report_reset(struct kshim_ufs_read_report *r)
{
    uint32_t version = r->version, board = r->board;
    memset(r, 0, sizeof(*r));
    r->version = version;
    r->board = board;
    r->stage = KSHIM_UFS_READ_STAGE_NOT_RUN;
    r->status = KSHIM_UFS_READ_UNRUN;
    r->release_status = KSHIM_UFS_READ_UNRUN;
}
/* Best effort register/descriptor snapshot taken before release. Registers are
 * only read here; the descriptor is inspected only when the last transfer
 * completed and the CPU view was synchronized, never while DMA is in flight. */
static struct kshim_ufs_scsi_state scsi_snapshot(const struct kshim_ufs_host *h)
{
    return (struct kshim_ufs_scsi_state){h->last_scsi_opcode,h->last_scsi_lun,
        h->last_scsi_status,h->last_sense_key,h->last_sense_asc,
        h->last_sense_ascq,h->last_sense_valid};
}
static void report_capture(struct kshim_ufs_host *h,
                           struct kshim_ufs_read_report *r)
{
    uint32_t v;
    /* Copy the host's link record first: release rewrites host->link, so this
     * is the only point that keeps the FSM the probe actually observed. */
    r->link_before_release = h->link;
    r->power_before_release = h->power;
    r->scsi_before_release = scsi_snapshot(h);
    if (h->ops.read32) {
        if (!reg_read(h, REG_HCS, &v)) { r->hcs = v; r->debug_valid |= DEBUG_HCS; }
        if (!reg_read(h, REG_IS, &v)) { r->is = v; r->debug_valid |= DEBUG_IS; }
        if (!reg_read(h, REG_DOORBELL, &v)) {
            r->doorbell = v;
            r->debug_valid |= DEBUG_DOORBELL;
        }
    }
    r->last_scsi_status = h->last_scsi_status;
    r->last_sense_key = h->last_sense_key;
    if (!h->poisoned && h->arena) {
        r->ocs = h->arena[8] & 15u;
        memcpy(r->response_prefix, h->arena + ARENA_RSP,
               sizeof(r->response_prefix));
        r->debug_valid |= DEBUG_ARENA;
    }
}
/* Proves the firmware-owned registers came back. Any read failure or mismatch
 * is a release failure for the caller: the arena stays quarantined. */
static int restore_check(struct kshim_ufs_host *h)
{
    uint32_t lo, hi, run, ie, ahit, db, tdb;
    if (reg_read(h, REG_BASE_LO, &lo) || reg_read(h, REG_BASE_HI, &hi) ||
        reg_read(h, REG_RUN, &run) || reg_read(h, REG_IE, &ie) ||
        reg_read(h, REG_AHIT, &ahit) || reg_read(h, REG_DOORBELL, &db) ||
        reg_read(h, REG_TASK_DB, &tdb)) return KSHIM_STORAGE_IO;
    if (lo != h->saved_base[0] || hi != h->saved_base[1] ||
        run != h->saved_run || ie != h->saved_ie || ahit != h->saved_ahit ||
        db || tdb) return KSHIM_STORAGE_IO;
    return 0;
}
/* The backup partition must cover exactly two GiB of whole blocks. All span
 * arithmetic is guarded so a malformed GPT cannot wrap the comparison. */
static int partition_geometry(const struct kshim_partition *p,
                              uint32_t block_size, uint64_t block_count)
{
    if (block_size != 512u && block_size != 4096u) return KSHIM_STORAGE_FORMAT;
    if (!block_count || p->first_lba > p->last_lba || p->last_lba >= block_count)
        return KSHIM_STORAGE_RANGE;
    uint64_t span = p->last_lba - p->first_lba;
    if (span == UINT64_MAX) return KSHIM_STORAGE_OVERFLOW;
    ++span;
    if (span > UINT64_MAX / block_size) return KSHIM_STORAGE_OVERFLOW;
    if (span * block_size != ARMADA_PARTITION_BYTES) return KSHIM_STORAGE_FORMAT;
    return 0;
}
bool kshim_ufs_read_probe(struct kshim_ufs_host *host,
                          const struct kshim_ufs_ops *ops, void *arena,
                          uint64_t physical, void *scratch,
                          size_t scratch_bytes,
                          struct kshim_ufs_read_report *report)
{
    if (!report) return false;
    report_reset(report);
    /* A zero mapping is a broken precondition, not a claim attempt: nothing is
     * probed and ownership is unverified, so handoff is not reported safe. */
    if (!host || !ops || !arena || !physical || !scratch ||
        scratch_bytes < KSHIM_UFS_DMA_BYTES) {
        report->status = KSHIM_STORAGE_ARG;
        return false;
    }
    report->dma_base = physical;
    report->stage = KSHIM_UFS_READ_STAGE_CLAIM;
    if (host->owned) {
        /* Another owner still holds the controller. Never release or erase a
         * state that this call did not create. */
        report->status = KSHIM_UFS_BUSY;
        return false;
    }
    int rc = kshim_ufs_claim(host, ops, arena, KSHIM_UFS_DMA_BYTES, physical);
    if (rc && !host->owned) {
        /* Nothing was taken or written: the state is unchanged and safe. */
        report->status = rc;
        report->release_status = KSHIM_STORAGE_OK;
        report->restore_verified = 1;
        report->handoff_safe = 1;
        return true;
    }
    struct kshim_ufs_lun lun;
    struct kshim_partition part;
    if (!rc) {
        report->stage = KSHIM_UFS_READ_STAGE_NOP;
        if ((rc = kshim_ufs_nop(host))) goto finish;
#if defined(CONFIG_KSHIM_UFS_DEVICE_WAKE) && CONFIG_KSHIM_UFS_DEVICE_WAKE
        report->stage = KSHIM_UFS_READ_STAGE_DEVICE;
        if ((rc = kshim_ufs_prepare_device(host))) goto finish;
#endif
        report->stage = KSHIM_UFS_READ_STAGE_LUN;
        if ((rc = kshim_ufs_open_lun(host, 0, false, &lun))) goto finish;
        report->block_size = lun.block.block_size;
        report->block_count = lun.block.block_count;
        report->stage = KSHIM_UFS_READ_STAGE_GPT;
        if ((rc = kshim_gpt_find_backup(&lun.block, &part, scratch,
                                        scratch_bytes))) goto finish;
        if ((rc = partition_geometry(&part, lun.block.block_size,
                                     lun.block.block_count))) goto finish;
        report->first_lba = part.first_lba;
        report->last_lba = part.last_lba;
        memcpy(report->partition_uuid, part.uuid,
               sizeof(report->partition_uuid));
        report->stage = KSHIM_UFS_READ_STAGE_HEADER;
        if ((rc = kshim_block_read(&lun.block, part.first_lba, 1, scratch)))
            goto finish;
        report->header_crc = kshim_crc32(scratch, lun.block.block_size);
        memcpy(report->header_prefix, scratch, sizeof(report->header_prefix));
        report->stage = KSHIM_UFS_READ_STAGE_TAIL;
        if ((rc = kshim_block_read(&lun.block, part.last_lba, 1, scratch)))
            goto finish;
        report->tail_crc = kshim_crc32(scratch, lun.block.block_size);
        report->stage = KSHIM_UFS_READ_STAGE_DONE;
        rc = KSHIM_STORAGE_OK;
    }
finish:
    report->status = rc;
    report_capture(host, report);
    /* Release is mandatory once owned, even when claim itself failed after
     * taking ownership. There is no retry and no ownership rewrite here; the
     * caller obeys handoff_safe even though release may clear host->owned. */
    int release = kshim_ufs_release(host);
    /* Records what cleanup left behind, including a failed release. */
    report->link_after_release = host->link;
    report->power_after_release = host->power;
    report->scsi_after_release = scsi_snapshot(host);
    report->release_status = release;
    if (release) {
        report->restore_verified = 0;
        report->handoff_safe = 0;
        return false;
    }
    if (restore_check(host)) {
        report->release_status = KSHIM_STORAGE_IO;
        report->restore_verified = 0;
        report->handoff_safe = 0;
        return false;
    }
    report->restore_verified = 1;
    report->handoff_safe = 1;
    return true;
}
