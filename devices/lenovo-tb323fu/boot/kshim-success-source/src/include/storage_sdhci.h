/* SPDX-License-Identifier: GPL-3.0-only */
#pragma once
#include <storage_block.h>
#include <stdbool.h>
/* Board code supplies powered SD-slot MMIO, pinmux, voltage and the actual
 * parent clock. All card data uses PIO; there are no DMA addresses.
 * Callback width is1/2/4 bytes. Do not emulate partial writes with unsafe RMW
 * of status/command registers. The host object must initially be zeroed. */
struct kshim_sdhci_ops {
    void *ctx;
    int (*read)(void *,uint32_t offset,unsigned width,uint32_t *);
    int (*write)(void *,uint32_t offset,unsigned width,uint32_t);
    uint64_t (*now_ms)(void *);
    void (*delay_us)(void *,uint32_t);
    int (*card_present)(void *); /* 1 present,0 absent,negative IO */
};
enum kshim_sd_result {
    KSHIM_SD_TIMEOUT=-30, KSHIM_SD_CARD=-31, KSHIM_SD_PROTOCOL=-32,
    KSHIM_SD_UNSUPPORTED=-33, KSHIM_SD_FAILED=-34
};
struct kshim_sdhci {
    struct kshim_blockdev block;
    struct kshim_sdhci_ops ops;
    uint32_t parent_clock_hz,ocr_mask;
    uint16_t rca;
    uint8_t high_capacity,owned,failed;
};
/* voltage is SDHCI power encoding0x0e(3.3V) or0x0c(3.0V);
 * ocr_mask must describe the board supply's actual supported voltage range.
 * Initializes1-bit400kHz SD/SDHC/SDXC. No voltage switch, high speed or tuning.
 * SDUC/eMMC/SDIO are not silently interpreted as an SD card. */
int kshim_sdhci_open(struct kshim_sdhci *,const struct kshim_sdhci_ops *,
                     uint32_t parent_clock_hz,uint8_t voltage,uint32_t ocr_mask,
                     bool allow_writes);
int kshim_sdhci_release(struct kshim_sdhci *);
