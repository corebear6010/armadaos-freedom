/* SPDX-License-Identifier: GPL-3.0-only */
#pragma once

#include <device_info.h>
#include <stdint.h>

#define STORAGE_PROBE_VERSION 1U
#define STORAGE_PROBE_MAX_ENTRIES 256U

/* This API has no MMIO write or DMA operation. The caller must supply the
 * actual validated boot-FDT board, never the user's selected boot target.
 * The reader must return zero only after a successful 32-bit read, and a
 * nonzero error otherwise. Fixed addresses do not make native reads safe:
 * catching an access abort, if supported, belongs to the caller's reader. */
typedef int (*storage_probe_read32_fn)(void *ctx, uint64_t address,
                                      uint32_t *value);

enum storage_probe_status {
    STORAGE_PROBE_OK = 0,             /* Entire supported snapshot collected. */
    STORAGE_PROBE_NOT_RUN = 1,        /* Initialized, collection not finished. */
    STORAGE_PROBE_BOARD_UNSUPPORTED = 2, /* No MMIO was attempted. */
    STORAGE_PROBE_INVALID_ARGUMENT = 3,  /* No MMIO was attempted. */
    STORAGE_PROBE_READ_FAILED = 4,    /* Reader failed; earlier evidence kept. */
    STORAGE_PROBE_SMMU_UNSUPPORTED = 5, /* Raw IDs/config kept; not decoded. */
    STORAGE_PROBE_BOUNDS = 6,         /* Declared layout/index outside limits. */
    STORAGE_PROBE_FULL = 7,           /* Fixed record capacity exhausted. */
    STORAGE_PROBE_ENV_UNAVAILABLE = 8, /* Caller cannot safely perform MMIO;
                                         set by entry wrapper, not collector. */
};

enum storage_probe_valid {
    STORAGE_PROBE_VALID_UFS = 1U << 0, /* Entire UFS whitelist was read. */
    STORAGE_PROBE_VALID_SMMU_IDS = 1U << 1, /* CR0 and ID0/1/2 were read. */
    STORAGE_PROBE_VALID_SMMU_LAYOUT = 1U << 2, /* Supported bounded layout. */
};

struct storage_probe_entry {
    uint64_t address;
    uint32_t value;
};

struct storage_probe_snapshot {
    uint32_t version;
    enum device_board board;
    enum storage_probe_status status;
    uint32_t valid;
    uint32_t entry_count;        /* Only entries [0, entry_count) are valid. */
    uint32_t read_attempts;      /* Includes successful discarded SMR reads. */
    uint32_t stream_count;       /* ID0 count; valid when SMMU_LAYOUT is set. */
    uint32_t streams_scanned;    /* SMRs successfully read, including nonmatches. */
    uint32_t matching_streams;   /* Valid SMRs matching SID 0x60, masks included. */
    uint32_t contexts_captured;  /* Unique translated CBs fully captured. */
    uint64_t failed_address;    /* Failed reader or the next address at a limit;
                                  zero for unsupported/layout-only rejection. */
    int32_t read_error;          /* Reader's nonzero result, READ_FAILED only. */
    struct storage_probe_entry entries[STORAGE_PROBE_MAX_ENTRIES];
};

/* Fully clears out on entry, including on a rejected board. Only TB322FC and TB323FU are
 * allowed. UFS records are followed by SMMU CR0/IDs and SID 0x60 matching
 * SMR/S2CR/CB records. Nonmatching streams are inspected but not exported.
 * Reused context banks are captured once. Collection stops at the first
 * failed read, unsupported layout, invalid index or record limit. Partial
 * entries remain valid even when their enclosing group is incomplete; an
 * absent entry is unknown, not a zero register. No controller initialization,
 * reset, DMA submission, SMMU enabling or firmware mapping change occurs.
 * A successful snapshot is evidence only, never permission to start DMA.
 * Serialize fields explicitly; structure padding is not an export format. */
enum storage_probe_status storage_probe_collect(
    enum device_board actual_board, storage_probe_read32_fn read32,
    void *ctx, struct storage_probe_snapshot *out);

#if defined(CONFIG_KSHIM_STORAGE_ENTRY_PROBE) && CONFIG_KSHIM_STORAGE_ENTRY_PROBE
/* Development profile: expose the captured status to its visible menu title. */
const struct storage_probe_snapshot *storage_probe_entry_snapshot(void);
#endif
