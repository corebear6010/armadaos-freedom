/* SPDX-License-Identifier: MIT */
#pragma once
#include <stddef.h>
/* Trial only. Patches one existing cell in the bootloader-selected Android
 * tree, retaining all chosen/memory/runtime data and the original pointer.
 * Returns 1 if changed, 0 if already 4.8 MHz, negative on mismatch (no change). */
int tb323fu_android_touch_spi_trial(void *fdt, size_t available);
