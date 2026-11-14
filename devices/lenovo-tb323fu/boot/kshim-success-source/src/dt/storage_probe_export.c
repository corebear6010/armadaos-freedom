/* SPDX-License-Identifier: GPL-3.0-only */
#include "storage_probe_export.h"
#include <libfdt.h>

static void put_be32(uint8_t *p, uint32_t value)
{
    p[0] = (uint8_t)(value >> 24);
    p[1] = (uint8_t)(value >> 16);
    p[2] = (uint8_t)(value >> 8);
    p[3] = (uint8_t)value;
}
static void put_be64(uint8_t *p, uint64_t value)
{
    put_be32(p, (uint32_t)(value >> 32));
    put_be32(p + 4, (uint32_t)value);
}
int storage_probe_export(void *fdt, const struct storage_probe_snapshot *snapshot)
{
    if (!fdt || !snapshot || snapshot->entry_count > STORAGE_PROBE_MAX_ENTRIES)
        return -FDT_ERR_BADVALUE;
    int rc = fdt_check_header(fdt);
    if (rc) return rc;
    int node = fdt_path_offset(fdt, "/chosen");
    if (node < 0) return node;

    uint8_t report[STORAGE_PROBE_EXPORT_HEADER_BYTES +
                   STORAGE_PROBE_MAX_ENTRIES * STORAGE_PROBE_EXPORT_ENTRY_BYTES];
    put_be32(report + 0, snapshot->version);
    put_be32(report + 4, snapshot->valid);
    put_be32(report + 8, (uint32_t)snapshot->board);
    put_be32(report + 12, (uint32_t)snapshot->status);
    put_be32(report + 16, snapshot->entry_count);
    put_be32(report + 20, snapshot->read_attempts);
    put_be32(report + 24, snapshot->stream_count);
    put_be32(report + 28, snapshot->streams_scanned);
    put_be32(report + 32, snapshot->matching_streams);
    put_be32(report + 36, snapshot->contexts_captured);
    put_be32(report + 40, (uint32_t)snapshot->read_error);
    put_be64(report + 44, snapshot->failed_address);
    for (uint32_t i = 0; i < snapshot->entry_count; ++i) {
        uint8_t *entry = report + STORAGE_PROBE_EXPORT_HEADER_BYTES +
                         i * STORAGE_PROBE_EXPORT_ENTRY_BYTES;
        put_be64(entry, snapshot->entries[i].address);
        put_be32(entry + 8, snapshot->entries[i].value);
    }
    /* One libfdt insertion/resize reserves the complete property before the
     * payload copy. NOSPACE preserves an existing report; it cannot publish
     * only the header or a prefix of the entries. */
    return fdt_setprop(fdt, node, STORAGE_PROBE_FDT_PROPERTY, report,
                      (int)(STORAGE_PROBE_EXPORT_HEADER_BYTES +
                            snapshot->entry_count * STORAGE_PROBE_EXPORT_ENTRY_BYTES));
}
