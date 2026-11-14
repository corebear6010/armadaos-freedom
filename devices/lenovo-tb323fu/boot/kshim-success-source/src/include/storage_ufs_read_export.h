/* SPDX-License-Identifier: GPL-3.0-only */
#ifndef KSHIM_STORAGE_UFS_READ_EXPORT_H
#define KSHIM_STORAGE_UFS_READ_EXPORT_H
#include <storage_ufs_read_probe.h>

#define KSHIM_UFS_READ_FDT_PROPERTY "kshim,ufs-read-v1"
#define KSHIM_UFS_READ_EXPORT_BYTES 164U
#define KSHIM_UFS_READ_EXPORT_VERSION 1U

/* Export the one-shot native read report into the existing /chosen node of a
 * writable FDT. The function never grows its buffer, never creates /chosen and
 * reads no MMIO. It rejects a NULL tree or report and a report whose version is
 * not KSHIM_UFS_READ_EXPORT_VERSION before it mutates anything, so a stale or
 * foreign report cannot republish itself. Returns 0 or a negative libfdt error;
 * a single fdt_setprop publishes the complete property, and a failed insertion
 * (for example NOSPACE) leaves any previous property byte-identical rather
 * than exposing a partial record.
 *
 * Wire format: 164 bytes, big-endian, in this exact order (no C padding):
 *   0 version, 4 board, 8 stage,
 *   12 status, 16 release_status (signed int32, two's-complement bits kept),
 *   20 restore_verified, 24 handoff_safe, 28 block_size,
 *   32 block_count, 40 dma_base, 48 first_lba, 56 last_lba (u64),
 *   64 partition_uuid (16 raw bytes from the GPT entry),
 *   80 header_crc, 84 tail_crc,
 *   88 header_prefix (32 raw block bytes),
 *   120 hcs, 124 is, 128 doorbell, 132 last_scsi_status,
 *   136 last_sense_key, 140 ocs,
 *   144 response_prefix (16 raw descriptor bytes),
 *   160 debug_valid.
 * Every numeric field is the corresponding struct kshim_ufs_read_report member;
 * debug_valid says which captured registers/descriptor bytes are meaningful.
 * A zero/absent field is not evidence of a healthy value. */
int storage_ufs_read_export(void *fdt,
                            const struct kshim_ufs_read_report *report);

/* Auxiliary link evidence, published as its own property because the 164-byte
 * read record above is frozen: a reader of kshim,ufs-read-v1 keeps seeing
 * exactly the same bytes whether or not this record is present.
 *
 * Wire format: 76 bytes, big-endian, in this exact order (no C padding):
 *   0 version (KSHIM_UFS_LINK_EXPORT_VERSION),
 *   4 link_before_release, nine u32 at 4, 8, 12, 16, 20, 24, 28, 32, 36,
 *   40 link_after_release, the same nine u32 at 40, 44, 48, 52, 56, 60, 64,
 *     68, 72.
 * The field order inside each state is initial_tx, current_tx, last_command,
 * command_result, last_is, power_status, phase, uncertain, restore_hibern8.
 * uncertain and restore_hibern8 are uint8 in struct kshim_ufs_link_state but
 * are encoded as u32 here, so the record carries no padding and no implicit
 * gap. These are the observations the probe saved around its release, not a
 * live view of host->link: link_before_release holds the FSM the probe saw
 * before cleanup rewrote the record, and link_after_release is what cleanup
 * left behind. Both remain zero, including phase 0, when no link work ran. */
#define KSHIM_UFS_LINK_FDT_PROPERTY "kshim,ufs-link-v1"
#define KSHIM_UFS_LINK_EXPORT_BYTES 76U
#define KSHIM_UFS_LINK_EXPORT_VERSION 1U

/* Same contract as storage_ufs_read_export: it rejects a NULL tree or report
 * and a report whose version is not KSHIM_UFS_READ_EXPORT_VERSION before it
 * mutates anything, then checks the header and the existing /chosen. One
 * fdt_setprop publishes the complete 76-byte property, so a failed insertion
 * leaves any previous value byte-identical and the read property untouched.
 * It reads no MMIO, allocates nothing and never grows the FDT. */
int storage_ufs_link_export(void *fdt,
                            const struct kshim_ufs_read_report *report);
/* Separate 100-byte BE record: version@0, power before@4/after@24
 * (initial,current,phase,uncertain,restore_mode), SCSI before@44/after@72
 * (opcode,lun,status,key,ASC,ASCQ,valid). Existing wire records stay frozen. */
#define KSHIM_UFS_POWER_FDT_PROPERTY "kshim,ufs-power-v1"
#define KSHIM_UFS_POWER_EXPORT_BYTES 100U
#define KSHIM_UFS_POWER_EXPORT_VERSION 1U
int storage_ufs_power_export(void *fdt,
                             const struct kshim_ufs_read_report *report);
#endif
