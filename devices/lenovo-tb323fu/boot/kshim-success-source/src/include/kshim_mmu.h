#pragma once

#include <stddef.h>
#include <stdint.h>

#define KSHIM_MMU_MAX_EXTRA_REGIONS 100U

typedef enum {
    KSHIM_MMU_NORMAL_WB = 0,
    KSHIM_MMU_NORMAL_NC,
    KSHIM_MMU_DEVICE,
} kshim_mmu_memory_type_t;

enum {
    KSHIM_MMU_READ = 1U << 0,
    KSHIM_MMU_WRITE = 1U << 1,
    KSHIM_MMU_EXECUTE = 1U << 2,
};

typedef struct {
    uint64_t base;
    uint64_t size;
    kshim_mmu_memory_type_t type;
    uint32_t permissions;
} kshim_mmu_region_t;

typedef struct {
    const void *fdt;
    uint64_t framebuffer_base;
    uint64_t framebuffer_size;
    uint64_t uefi_base;
    uint64_t uefi_size;
    const kshim_mmu_region_t *extra_regions;
    size_t extra_region_count;
    uint8_t map_qemu_test_windows;
} kshim_mmu_config_t;

/* Builders refuse to replace a table pool after its first enable. */
int kshim_mmu_configure(const kshim_mmu_config_t *config);
int kshim_mmu_configure_default(const void *fdt);

/* Early configuration, called from kshim_main() before the console, the DT
 * snapshot or any resource probing exists.  It maps, in this order:
 *   1. low peripheral space excluding no-map pools (device-nGnRnE, RW),
 *   2. low RAM excluding page-rounded no-map reservations (write-back, RW),
 *   3. the packed payload window, rejecting no-map conflicts (write-back, RW),
 *   4. the running image (write-back, RWX),
 *   5. the external runtime DT (write-back, RW, registered for cache clean),
 *   6. the display reservation and configured test scratch (non-cacheable).
 * Final memory types are chosen before enabling; live tables are immutable.
 * HCR_EL2 is preserved. Unsupported VHE entries decline early enable. */
typedef struct {
    const void *fdt;
    uint64_t payload_base;
    uint64_t payload_end;
} kshim_mmu_early_config_t;

int kshim_mmu_configure_early(const kshim_mmu_early_config_t *config);
/* Add a compatible region before the first enable. Returns -2 once tables
 * have been used by any CPU; online descriptor replacement is unsupported. */
int kshim_mmu_add_region(uint64_t base, uint64_t size,
                         kshim_mmu_memory_type_t type, uint32_t permissions);
/* Inspect existing attributes without modifying a live table. */
int kshim_mmu_region_matches(uint64_t base, uint64_t size,
                             kshim_mmu_memory_type_t type, uint32_t permissions);

int kshim_mmu_enable_current_cpu(void);
int kshim_mmu_enable_secondary(uint64_t entry_el);
int kshim_mmu_is_enabled(void);
uint64_t kshim_mmu_entry_el(void);
uint64_t kshim_mmu_root_table(void);
/* Runtime load base of the image, or its link address when the build is
 * loaded where it was linked (standalone/QEMU). */
uint64_t kshim_mmu_image_base(void);
int kshim_mmu_prepare_handoff(void);
void kshim_mmu_cpu_off_prepare(void);

/* Compatibility entry used by the existing main path. */
void _arm64_mmu_setup(void);
