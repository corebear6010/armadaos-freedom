/* SPDX-License-Identifier: GPL-3.0-only */
#pragma once
#include <storage_ufs_settings.h>
#define KSHIM_SETTINGS_FDT_PROPERTY "kshim,settings-v1"
#define KSHIM_SETTINGS_EXPORT_BYTES 64
int storage_settings_export(void *fdt,const struct kshim_ufs_settings *,
                             uint32_t test_performed,int test_status);
