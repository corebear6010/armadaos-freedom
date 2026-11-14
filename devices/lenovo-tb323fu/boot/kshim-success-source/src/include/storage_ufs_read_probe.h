/* SPDX-License-Identifier: GPL-3.0-only */
#ifndef KSHIM_STORAGE_UFS_READ_PROBE_H
#define KSHIM_STORAGE_UFS_READ_PROBE_H
#include <storage_ufs.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Bounded read-only UFS diagnostic sequence over one initially-unowned host.
 *
 * Preconditions the caller must prove before the first use:
 *   - the board adapter established clocks/link, owns the controller and holds
 *     a valid live bus mapping for the whole arena; cache maintenance for that
 *     mapping is supplied through ops, so hardware coherency is not required,
 *     and `physical` is that mapping, never an assumed CPU physical address;
 *   - the host was zero-initialized before its first claim and is not owned;
 *   - the report was zero-initialized and its version/board identity fields
 *     were filled in by the native wrapper; this pure sequence preserves them;
 *   - host, arena, scratch and report stay resident until the call returns,
 *     and the arena/mapping stays resident until kshim_ufs_release succeeds.
 *
 * No disk write is ever issued: the LUN is opened with allow_writes=false, only
 * the generic claim/release path touches registers, and the sequence performs
 * no reset, LUN scan or SMMU write. The optional DEVICE_WAKE profile queries
 * device power, wakes only confirmed SLEEP/POWERDOWN, and restores the exact
 * entry mode before handing off. It adds no disk write or cache flush.
 * The return value is report->handoff_safe. */
#define KSHIM_UFS_READ_REPORT_VERSION 1u
/* status and release_status hold this sentinel until the matching result
 * exists; real results are the zero/negative kshim_storage_result codes. */
#define KSHIM_UFS_READ_UNRUN 1
enum kshim_ufs_read_stage {
    KSHIM_UFS_READ_STAGE_NOT_RUN = 0,
    KSHIM_UFS_READ_STAGE_CPU = 1,
    KSHIM_UFS_READ_STAGE_SMMU = 2,
    KSHIM_UFS_READ_STAGE_CLAIM = 3,
    KSHIM_UFS_READ_STAGE_NOP = 4,
    KSHIM_UFS_READ_STAGE_LUN = 5,
    KSHIM_UFS_READ_STAGE_GPT = 6,
    KSHIM_UFS_READ_STAGE_HEADER = 7,
    KSHIM_UFS_READ_STAGE_TAIL = 8,
    KSHIM_UFS_READ_STAGE_DONE = 9,
    KSHIM_UFS_READ_STAGE_DEVICE = 10
};
/* Explicit field types and natural alignment (no packing) so the native
 * wrapper and the FDT export's v1 record can consume the same prefix layout.
 * debug_valid: bit0 HCS captured, bit1 IS captured, bit2 doorbell captured,
 * bit3 arena debug captured (ocs and response_prefix); a bit means the value
 * below was produced, not that it proves anything healthy. */
struct kshim_ufs_scsi_state {
    uint32_t opcode, lun, status, sense_key, asc, ascq, sense_valid;
};
struct kshim_ufs_read_report {
    uint32_t version, board, stage;
    int32_t status, release_status;
    uint32_t restore_verified, handoff_safe, block_size;
    uint64_t block_count, dma_base, first_lba, last_lba;
    uint8_t partition_uuid[16];
    uint32_t header_crc, tail_crc;
    uint8_t header_prefix[32];
    uint32_t hcs, is, doorbell, last_scsi_status, last_sense_key, ocs;
    uint8_t response_prefix[16];
    uint32_t debug_valid;
    /* Link evidence around cleanup, for a stage that fails before the host's
     * own link record can be read back. link_before_release is copied after
     * the last transfer and before kshim_ufs_release, so it retains the FSM
     * the probe actually observed even when release then rewrites host->link;
     * link_after_release is copied once release returned and records the state
     * the cleanup left behind. Both stay zero when no link work ran. These are
     * host-memory fields only: the 164-byte big-endian wire record of version
     * 1 is unchanged, and any export that publishes this evidence is a
     * separate auxiliary artifact. */
    struct kshim_ufs_link_state link_before_release, link_after_release;
    struct kshim_ufs_power_state power_before_release, power_after_release;
    struct kshim_ufs_scsi_state scsi_before_release, scsi_after_release;
};
bool kshim_ufs_read_probe(struct kshim_ufs_host *host,
                          const struct kshim_ufs_ops *ops, void *arena,
                          uint64_t physical, void *scratch,
                          size_t scratch_bytes,
                          struct kshim_ufs_read_report *report);
#endif
