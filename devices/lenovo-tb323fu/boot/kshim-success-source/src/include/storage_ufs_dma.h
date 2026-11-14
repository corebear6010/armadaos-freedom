/* SPDX-License-Identifier: GPL-3.0-only */
#pragma once

#include <storage_probe.h>

#include <stdbool.h>
#include <stdint.h>

/* Checks only a fresh, complete snapshot of the inherited firmware mapping.
 * Nothing here performs MMIO or changes a mapping. A true result is not
 * permission to start DMA: the caller must independently prove the CPU
 * VA->PA relationship and revalidate the snapshot immediately before making
 * an identity-addressing claim. No general IOMMU takeover or page-table code
 * is involved. */
bool kshim_ufs_dma_is_identity(const struct storage_probe_snapshot *snapshot,
                               uint64_t physical_base, uint64_t bytes);
