/* SPDX-License-Identifier: GPL-3.0-only */
#pragma once
#include <arm64_exception.h>
#include <storage_probe.h>
#include <stdbool.h>

/* Only the exact whitelisted LDR below and, when the native IO profile is
 * enabled, the exact STR used by its writer are recoverable. */
bool arm64_storage_probe_fixup(ARM64_EXCEPTION_CONTEXT *context,
                               uint64_t exception_type);
/* Exact whitelisted LDR used by the entry probe. The whitelist, vector and
 * DAIF handling stay unchanged; the caller owns EL1/MMU preconditions. */
int storage_probe_native_read32(void *ctx, uint64_t address, uint32_t *value);
void storage_probe_native_capture(enum device_board actual_board,
                                   struct storage_probe_snapshot *out);
