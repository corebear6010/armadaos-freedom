/* SPDX-License-Identifier: GPL-3.0-only */
#ifndef KSHIM_STORAGE_PROBE_EXPORT_H
#define KSHIM_STORAGE_PROBE_EXPORT_H
#include "storage_probe.h"

#define STORAGE_PROBE_FDT_PROPERTY "kshim,storage-entry-v1"
#define STORAGE_PROBE_EXPORT_HEADER_BYTES 52U
#define STORAGE_PROBE_EXPORT_ENTRY_BYTES 12U
/* Export one property in the existing /chosen node. The caller supplies a
 * writable FDT with sufficient space; this function does not grow its buffer
 * or create /chosen. Returns 0 or a negative libfdt error. No MMIO is read.
 *
 * Wire format: 13 big-endian 32-bit words (52 bytes), in this exact order:
 *   0 version, 4 valid, 8 board, 12 status, 16 entry_count,
 *   20 read_attempts, 24 stream_count, 28 streams_scanned,
 *   32 matching_streams, 36 contexts_captured, 40 read_error,
 *   44 failed_address_hi, 48 failed_address_lo.
 * read_error preserves the signed int32 two's-complement bit pattern.
 * Then entry_count records, each 12 bytes: address_hi, address_lo, value,
 * all big-endian u32. The property includes no unused entries or C padding.
 *
 * Partial/error snapshots and unsupported-board zero-entry snapshots remain
 * evidence and are exported as supplied. A missing entry is unknown, not zero;
 * interpret count/layout fields together with the valid bits and status.
 * Rejected input leaves the FDT unchanged. A failed property insertion or
 * resize leaves no partial new report and preserves any previous report.
 */
int storage_probe_export(void *fdt, const struct storage_probe_snapshot *snapshot);
#endif
