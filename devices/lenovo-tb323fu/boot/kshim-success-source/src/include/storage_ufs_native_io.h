/* SPDX-License-Identifier: GPL-3.0-only */
#pragma once
#include <stddef.h>
#include <stdint.h>

/* Bounded native UFS primitives for a diagnostic IO profile.
 *
 * These helpers alone neither authorize DMA nor own any mapping. The actual
 * board/live SMMU proof, the exclusive HBA claim and the lifetime of the arena
 * belong to the caller. The bare-metal boot contract includes no EL2 stage-2
 * translation. The caller passes an isolated, aligned arena; no allocator or
 * page table is modified here. */

/* Offset must be 4-byte aligned and below 0x3000 from the TB323FU UFS base. */
int storage_ufs_native_read32(void *ctx, uint32_t offset, uint32_t *value);
/* Only AHIT/IS/IE/UTRL base/doorbell/RUN offsets are accepted. With
 * CONFIG_KSHIM_UFS_LINK_WAKE the exact UIC command/argument pairs of the
 * inherited-HIBERN8 query/exit/restore are accepted as well. */
int storage_ufs_native_write32(void *ctx, uint32_t offset, uint32_t value);
/* Proves the caller's arena is CPU-mapped and physically contiguous. Returns
 * the CPU mapping's physical base; a fresh SMMU identity decoder must still
 * prove bus addressing separately. */
int storage_ufs_native_dma_address(void *arena, size_t bytes, uint64_t *physical);
/* Cache maintenance for the isolated arena only; never shared with another
 * CPU writer. */
void storage_ufs_native_sync_device(void *ctx, void *buffer, size_t bytes);
void storage_ufs_native_sync_cpu(void *ctx, void *buffer, size_t bytes);
