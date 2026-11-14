/* SPDX-License-Identifier: GPL-3.0-only */
#include <config.h>
#include <kshim_mmu.h>
#include <qcom_dt.h>
#include <storage_ufs_read_native.h>
#include <storage_ufs_read_screen.h>

/* One statically owned framebuffer description. The pixels are the live
 * scanout that the existing read-only splash parser reports; nothing here
 * allocates, probes a new display or reprograms the controller. */
static kshim_framebuffer_t Screen;
static int ScreenReady;

int storage_ufs_read_screen_init(const void *fdt)
{
    kshim_framebuffer_config_t config;
    kshim_mmu_region_t mmio;
    uint64_t bytes;

    ScreenReady = 0;
    if (!fdt) return -1;
    /* kshim_dt_splash() only reads SDE state. The register window it also
     * returns stays unused: the running controller is never reinitialised. */
    if (kshim_dt_splash(fdt, CrIoDefault(), &config, &mmio)) return -1;
    (void)mmio;
    if (kshim_fb_init(&Screen, &config) != KSHIM_FB_OK) return -1;
    /* kshim_dt_splash() already selects white on black for every advertised
     * format, and the renderer only uses those two colours. */
    bytes = config.buffer_size ? (uint64_t)config.buffer_size
                               : (uint64_t)Screen.frame_size;
    if (bytes == 0) return -1;
    /* The reservation is declared no-map, so the exact scanout span must be
     * non-cacheable before the first store; a rejected mapping disables the
     * screen instead of writing through a write-back alias. */
    if (kshim_mmu_is_enabled() != 0 &&
        kshim_mmu_add_region((uint64_t)Screen.render_address, bytes,
                             KSHIM_MMU_NORMAL_NC,
                             KSHIM_MMU_READ | KSHIM_MMU_WRITE))
        return -1;
    ScreenReady = 1;
    return 0;
}

int storage_ufs_read_screen_ready(void)
{
    return ScreenReady;
}

void storage_ufs_read_screen_show(const struct kshim_ufs_read_report *report,
                                  uint32_t event, uint32_t offset,
                                  uint32_t value,
                                  const struct kshim_ufs_link_state *link)
{
    if (!ScreenReady || !report) return;
    (void)kshim_ufs_read_screen_render(&Screen, report, event, offset, value,
                                       link);
}
