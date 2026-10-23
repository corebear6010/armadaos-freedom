#include <kshim_mmu.h>

#include <config.h>
#include <dt.h>
#include <qcom_dt.h>
#include <kshim_console.h>
#include <libfdt.h>

#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define MMU_PAGE_SHIFT 12U
#define MMU_PAGE_SIZE UINT64_C(0x1000)
#define MMU_TABLE_ENTRIES 512U
#define MMU_TABLE_COUNT 64U
#define MMU_MAX_REGIONS 128U
#define MMU_ADDRESS_MASK UINT64_C(0x0000fffffffff000)

#define DESC_INVALID UINT64_C(0)
#define DESC_BLOCK UINT64_C(1)
#define DESC_TABLE UINT64_C(3)
#define DESC_PAGE UINT64_C(3)
#define DESC_TYPE_MASK UINT64_C(3)
#define DESC_ATTR_INDEX(index) ((uint64_t)(index) << 2)
#define DESC_AP_RO (UINT64_C(1) << 7)
#define DESC_SH_OUTER (UINT64_C(2) << 8)
#define DESC_SH_INNER (UINT64_C(3) << 8)
#define DESC_AF (UINT64_C(1) << 10)
#define DESC_PXN (UINT64_C(1) << 53)
#define DESC_UXN (UINT64_C(1) << 54)

#define MAIR_NORMAL_WB UINT64_C(0xff)
#define MAIR_NORMAL_NC UINT64_C(0x44)
#define MAIR_DEVICE_nGnRnE UINT64_C(0x00)
#define MAIR_VALUE (MAIR_NORMAL_WB | (MAIR_NORMAL_NC << 8) | \
                    (MAIR_DEVICE_nGnRnE << 16))

#define SCTLR_M (UINT64_C(1) << 0)
#define SCTLR_C (UINT64_C(1) << 2)
#define SCTLR_SA (UINT64_C(1) << 3)
#define SCTLR_I (UINT64_C(1) << 12)

#define CURRENT_EL1 UINT64_C(4)
#define CURRENT_EL2 UINT64_C(8)
#define HCR_E2H (UINT64_C(1) << 34)

#ifndef CONFIG_KSHIM_FB_RENDER_ADDRESS
#define CONFIG_KSHIM_FB_RENDER_ADDRESS 0U
#endif
#ifndef CONFIG_KSHIM_FB_WIDTH
#define CONFIG_KSHIM_FB_WIDTH 0U
#endif
#ifndef CONFIG_KSHIM_FB_HEIGHT
#define CONFIG_KSHIM_FB_HEIGHT 0U
#endif
#ifndef CONFIG_KSHIM_FB_STRIDE
#define CONFIG_KSHIM_FB_STRIDE 0U
#endif
#ifndef CONFIG_KSHIM_FB_FORMAT
#define CONFIG_KSHIM_FB_FORMAT 0U
#endif
#ifndef CONFIG_KSHIM_UEFI_LOAD_ADDRESS
#define CONFIG_KSHIM_UEFI_LOAD_ADDRESS 0U
#endif
#ifndef CONFIG_KSHIM_UEFI_MAX_SIZE
#define CONFIG_KSHIM_UEFI_MAX_SIZE 0U
#endif
#ifndef CONFIG_KSHIM_QEMU_PLATFORM
#define CONFIG_KSHIM_QEMU_PLATFORM 0
#endif
#ifndef CONFIG_KSHIM_SMP_SELFTEST
#define CONFIG_KSHIM_SMP_SELFTEST 0
#endif
#ifndef CONFIG_KSHIM_UI_SCRATCH_BASE
#define CONFIG_KSHIM_UI_SCRATCH_BASE 0U
#endif
#ifndef CONFIG_KSHIM_UI_SCRATCH_SIZE
#define CONFIG_KSHIM_UI_SCRATCH_SIZE 0U
#endif

typedef uint64_t translation_table_t[MMU_TABLE_ENTRIES];

typedef struct {
    translation_table_t *root;
    size_t tables_used;
    kshim_mmu_region_t regions[MMU_MAX_REGIONS];
    size_t region_count;
    uint64_t entry_el;
    uint8_t configured;
    uint8_t sealed;
} mmu_context_t;

static translation_table_t mTables[MMU_TABLE_COUNT]
    __attribute__((aligned(MMU_PAGE_SIZE), section(".mmu_tables")));
static mmu_context_t mMmu;
#if !defined(__aarch64__)
static uint8_t mHostEnabled;
#endif

extern unsigned char __image_start_address__[];
extern unsigned char __image_end_address__[];

/* Written by _start_kshim in startup.S: the address the image actually runs
 * from.  The attached shim is appended to the base image and entered there,
 * so this is payload_base + shim_offset, not the link address. */
uint64_t __image_runtime_base__;

/* ARM64 image header magic: "ARMd", then four zero bytes. */
#define IMAGE_MAGIC_LO UINT32_C(0x644d5241)
#define IMAGE_MAGIC_OFFSET UINT64_C(0x38)
#define KSHIM_MMU_PERIPHERAL_LIMIT UINT64_C(0x80000000)
#define KSHIM_MMU_DRAM_LIMIT UINT64_C(0x100000000)

/* -1 unknown, 0 the runtime base does not hold our image, 1 it does. */
static int mImageBaseVerified = -1;

static uint64_t image_runtime_base(void)
{
    if (__image_runtime_base__ != 0U)
        return __image_runtime_base__ & ~(MMU_PAGE_SIZE - 1U);
    return (uint64_t)(uintptr_t)__image_start_address__;
}

static uint64_t image_runtime_span(void)
{
    uint64_t start = (uint64_t)(uintptr_t)__image_start_address__;
    uint64_t end = (uint64_t)(uintptr_t)__image_end_address__;
    return end > start ? end - start : 0U;
}

/* The load base can only be believed if the image header is really there.
 * Reading it is a physical access taken before translation is enabled. */
static int image_base_matches(uint64_t base)
{
    const volatile uint32_t *header = (const volatile uint32_t *)(uintptr_t)base;
    volatile const uint8_t *magic =
        (volatile const uint8_t *)(uintptr_t)(base + IMAGE_MAGIC_OFFSET);

    if (base == 0U)
        return 0;
    /* code0 must still be a B instruction, and the header magic intact. */
    if ((header[0] & UINT32_C(0x7c000000)) != UINT32_C(0x14000000))
        return 0;
    if ((uint32_t)(magic[0] | (magic[1] << 8) | (magic[2] << 16) |
                   ((uint32_t)magic[3] << 24)) != IMAGE_MAGIC_LO)
        return 0;
    if (magic[4] != 0U || magic[5] != 0U || magic[6] != 0U || magic[7] != 0U)
        return 0;
    return 1;
}

static uint64_t align_down(uint64_t value, uint64_t alignment)
{
    return value & ~(alignment - 1U);
}

static int align_up(uint64_t value, uint64_t alignment, uint64_t *result)
{
    if (value > UINT64_MAX - (alignment - 1U))
        return -1;
    *result = (value + alignment - 1U) & ~(alignment - 1U);
    return 0;
}

static uint64_t block_size(unsigned level)
{
    return UINT64_C(1) << (MMU_PAGE_SHIFT + 9U * (3U - level));
}

static unsigned table_index(uint64_t address, unsigned level)
{
    return (unsigned)((address >> (MMU_PAGE_SHIFT + 9U * (3U - level))) &
                      UINT64_C(0x1ff));
}

static translation_table_t *allocate_table(void)
{
    translation_table_t *table;

    if (mMmu.tables_used >= MMU_TABLE_COUNT)
        return NULL;
    table = &mTables[mMmu.tables_used++];
    memset(table, 0, sizeof(*table));
    return table;
}

static translation_table_t *next_table(uint64_t *entry, unsigned level)
{
    uint64_t descriptor = *entry;
    translation_table_t *table;

    if ((descriptor & DESC_TYPE_MASK) == DESC_TABLE)
        return (translation_table_t *)(uintptr_t)(descriptor & MMU_ADDRESS_MASK);

    table = allocate_table();
    if (table == NULL)
        return NULL;

    if ((descriptor & DESC_TYPE_MASK) == DESC_BLOCK) {
        uint64_t base = descriptor & MMU_ADDRESS_MASK;
        uint64_t attributes = descriptor &
            ~(MMU_ADDRESS_MASK | DESC_TYPE_MASK);
        uint64_t size = block_size(level + 1U);
        uint64_t type = level + 1U == 3U ? DESC_PAGE : DESC_BLOCK;

        for (unsigned index = 0; index < MMU_TABLE_ENTRIES; index++)
            (*table)[index] = (base + (uint64_t)index * size) |
                              attributes | type;
    }

    *entry = ((uint64_t)(uintptr_t)table & MMU_ADDRESS_MASK) | DESC_TABLE;
    return table;
}

static uint64_t descriptor_attributes(
    kshim_mmu_memory_type_t type, uint32_t permissions)
{
    uint64_t attributes = DESC_AF;

    if (type == KSHIM_MMU_NORMAL_WB)
        attributes |= DESC_ATTR_INDEX(0U) | DESC_SH_INNER;
    else if (type == KSHIM_MMU_NORMAL_NC)
        attributes |= DESC_ATTR_INDEX(1U) | DESC_SH_OUTER;
    else
        attributes |= DESC_ATTR_INDEX(2U) | DESC_SH_OUTER;

    if ((permissions & KSHIM_MMU_WRITE) == 0U)
        attributes |= DESC_AP_RO;
    if ((permissions & KSHIM_MMU_EXECUTE) == 0U)
        attributes |= DESC_PXN | DESC_UXN;
    return attributes;
}

static int map_range(
    uint64_t base, uint64_t size, kshim_mmu_memory_type_t type,
    uint32_t permissions)
{
    uint64_t end;
    uint64_t address;
    uint64_t attributes;

    if (size == 0U || base > UINT64_MAX - size)
        return -1;
    address = align_down(base, MMU_PAGE_SIZE);
    if (align_up(base + size, MMU_PAGE_SIZE, &end) != 0 ||
        end <= address || end > (UINT64_C(1) << 48))
        return -1;
    attributes = descriptor_attributes(type, permissions);

    while (address < end) {
        translation_table_t *table = mMmu.root;
        uint64_t remaining = end - address;

        for (unsigned level = 0; level <= 3U; level++) {
            uint64_t span = block_size(level);
            uint64_t *entry = &(*table)[table_index(address, level)];
            uint64_t entry_type = *entry & DESC_TYPE_MASK;

            if (level >= 1U && (entry_type != DESC_TABLE || level == 3U) &&
                (address & (span - 1U)) == 0U && remaining >= span) {
                uint64_t descriptor_type = level == 3U ? DESC_PAGE : DESC_BLOCK;
                *entry = (address & MMU_ADDRESS_MASK) |
                         attributes | descriptor_type;
                address += span;
                break;
            }
            if (level == 3U)
                return -1;
            table = next_table(entry, level);
            if (table == NULL)
                return -2;
        }
    }
    return 0;
}

static int add_region(const kshim_mmu_region_t *region)
{
    int status;

    if (region == NULL || region->size == 0U)
        return 0;
    if (region->type > KSHIM_MMU_DEVICE ||
        (region->permissions & KSHIM_MMU_READ) == 0U ||
        mMmu.region_count >= MMU_MAX_REGIONS)
        return -1;
    uint64_t end;
    if (region->base > UINT64_MAX - region->size ||
        align_up(region->base + region->size, MMU_PAGE_SIZE, &end)) return -1;
    uint64_t start = align_down(region->base, MMU_PAGE_SIZE);
    for (size_t i = 0; i < mMmu.region_count; ++i) {
        const kshim_mmu_region_t *old = &mMmu.regions[i];
        uint64_t old_end;
        if (align_up(old->base + old->size, MMU_PAGE_SIZE, &old_end)) return -1;
        if (start < old_end && align_down(old->base, MMU_PAGE_SIZE) < end &&
            (old->type != region->type || old->permissions != region->permissions)) return -1;
    }
    status = map_range(
        region->base, region->size, region->type, region->permissions);
    if (status != 0)
        return status;
    mMmu.regions[mMmu.region_count++] = *region;
    return 0;
}

static int range_within(
    uint64_t base, uint64_t size, uint64_t outer_base, uint64_t outer_size)
{
    return size <= outer_size && base >= outer_base &&
        base - outer_base <= outer_size - size;
}

static int ranges_overlap(
    uint64_t first_base, uint64_t first_size,
    uint64_t second_base, uint64_t second_size)
{
    if (first_size == 0U || second_size == 0U ||
        first_base > UINT64_MAX - first_size ||
        second_base > UINT64_MAX - second_size)
        return 0;
    return first_base < second_base + second_size &&
        second_base < first_base + first_size;
}

static uint64_t current_el(void)
{
#if defined(__aarch64__)
    uint64_t value;
    __asm__ volatile("mrs %0, CurrentEL" : "=r"(value));
    return value & UINT64_C(0xc);
#else
    return CURRENT_EL1;
#endif
}

#if defined(__aarch64__)
static int supported_entry(uint64_t exception_level)
{
    uint64_t hcr;

    if (exception_level == CURRENT_EL1)
        return 1;
    if (exception_level != CURRENT_EL2)
        return 0;
    __asm__ volatile("mrs %0, hcr_el2" : "=r"(hcr));
    /* VHE changes TCR/SCTLR interpretation.  Do not switch regimes as part
     * of an optional cache optimization. */
    return (hcr & HCR_E2H) == 0U;
}

static uint64_t physical_size_encoding(void)
{
    uint64_t mmfr0;
    __asm__ volatile("mrs %0, id_aa64mmfr0_el1" : "=r"(mmfr0));
    mmfr0 &= UINT64_C(0xf);
    return mmfr0 > 5U ? 5U : mmfr0;
}

#endif

static int enable_at_el(uint64_t exception_level)
{
#if defined(__aarch64__)
    uint64_t root;
    uint64_t tcr;
    uint64_t sctlr;
    uint64_t ps = physical_size_encoding();

    if (!mMmu.configured || !supported_entry(exception_level))
        return -1;
    if (kshim_mmu_is_enabled())
        return 0;
    if (mMmu.sealed == 0U) {
        mMmu.sealed = 1U;
        __asm__ volatile("dc civac, %0\ndsb sy"
                         : : "r"((uintptr_t)&mMmu.sealed) : "memory");
    }
    root = (uint64_t)(uintptr_t)mMmu.root;
    __asm__ volatile("dsb ishst" ::: "memory");

    if (exception_level == CURRENT_EL1) {
        /* 48-bit TTBR0, WB table walks, inner-shareable, TTBR1 disabled. */
        tcr = UINT64_C(16) | (UINT64_C(1) << 8) |
              (UINT64_C(1) << 10) | (UINT64_C(3) << 12) |
              (UINT64_C(1) << 23) | (UINT64_C(16) << 16) |
              (UINT64_C(2) << 30) | (ps << 32);
        __asm__ volatile(
            "tlbi vmalle1\n"
            "dsb ish\n"
            "isb\n"
            "msr mair_el1, %0\n"
            "msr tcr_el1, %1\n"
            "msr ttbr0_el1, %2\n"
            "isb"
            : : "r"(MAIR_VALUE), "r"(tcr), "r"(root) : "memory");
        __asm__ volatile("mrs %0, sctlr_el1" : "=r"(sctlr));
        sctlr |= SCTLR_M | SCTLR_C | SCTLR_I | SCTLR_SA;
        __asm__ volatile("msr sctlr_el1, %0\nisb" : : "r"(sctlr) : "memory");
    } else {
        /* The non-VHE EL2 regime does not require changing E2H or TGE.
         * Preserve HCR on every CPU, including CPU_OFF and error paths. */
        tcr = UINT64_C(16) | (UINT64_C(1) << 8) |
              (UINT64_C(1) << 10) | (UINT64_C(3) << 12) |
              (ps << 16) | (UINT64_C(1) << 23) |
              (UINT64_C(1) << 31);
        __asm__ volatile(
            "tlbi alle2\n"
            "dsb ish\n"
            "isb\n"
            "msr mair_el2, %0\n"
            "msr tcr_el2, %1\n"
            "msr ttbr0_el2, %2\n"
            "isb"
            : : "r"(MAIR_VALUE), "r"(tcr), "r"(root) : "memory");
        __asm__ volatile("mrs %0, sctlr_el2" : "=r"(sctlr));
        sctlr |= SCTLR_M | SCTLR_C | SCTLR_I | SCTLR_SA;
        __asm__ volatile("msr sctlr_el2, %0\nisb" : : "r"(sctlr) : "memory");
    }
    return 0;
#else
    (void)exception_level;
    if (!mMmu.configured)
        return -1;
    mMmu.sealed = 1U;
    mHostEnabled = 1U;
    return 0;
#endif
}

static void clean_range(uint64_t base, uint64_t size)
{
#if defined(__aarch64__)
    uint64_t ctr;
    uint64_t line_size;
    uint64_t end;

    if (size == 0U || base > UINT64_MAX - size)
        return;
    __asm__ volatile("mrs %0, ctr_el0" : "=r"(ctr));
    line_size = UINT64_C(4) << ((ctr >> 16) & UINT64_C(0xf));
    end = base + size;
    for (uint64_t address = align_down(base, line_size);
         address < end; address += line_size)
        __asm__ volatile("dc civac, %0" : : "r"(address) : "memory");
#else
    (void)base;
    (void)size;
#endif
}

static void disable_at_el(uint64_t exception_level)
{
#if defined(__aarch64__)
    extern void arm64_mmu_clean_disable(uint64_t el,
                                       const kshim_mmu_region_t *regions,
                                       size_t count);
    _Static_assert(sizeof(kshim_mmu_region_t) == 24U, "MMU assembly region stride");
    _Static_assert(offsetof(kshim_mmu_region_t, type) == 16U, "MMU assembly type");
    _Static_assert(offsetof(kshim_mmu_region_t, permissions) == 20U, "MMU assembly permissions");
    arm64_mmu_clean_disable(exception_level, mMmu.regions, mMmu.region_count);
#else
    (void)exception_level;
    mHostEnabled = 0U;
#endif
}

static int region_matches(uint64_t base, uint64_t size,
                           kshim_mmu_memory_type_t type, uint32_t permissions)
{
    uint64_t end;
    if (size == 0U || base > UINT64_MAX - size ||
        align_up(base + size, MMU_PAGE_SIZE, &end) != 0)
        return 0;
    uint64_t attributes = descriptor_attributes(type, permissions);
    for (uint64_t address = align_down(base, MMU_PAGE_SIZE); address < end; ) {
        translation_table_t *table = mMmu.root;
        for (unsigned level = 0; level <= 3U; ++level) {
            uint64_t descriptor = (*table)[table_index(address, level)];
            uint64_t kind = descriptor & DESC_TYPE_MASK;
            if (kind == DESC_INVALID)
                return 0;
            if (kind == DESC_BLOCK || level == 3U) {
                if ((descriptor & ~(MMU_ADDRESS_MASK | DESC_TYPE_MASK)) != attributes)
                    return 0;
                uint64_t span = block_size(level);
                uint64_t next = align_down(address, span) + span;
                address = next < end ? next : end;
                break;
            }
            table = (translation_table_t *)(uintptr_t)(descriptor & MMU_ADDRESS_MASK);
        }
    }
    return 1;
}

int kshim_mmu_region_matches(uint64_t base, uint64_t size,
                             kshim_mmu_memory_type_t type, uint32_t permissions)
{
    return type <= KSHIM_MMU_DEVICE &&
        (permissions & KSHIM_MMU_READ) != 0U &&
        base < (UINT64_C(1) << 48) && size <= (UINT64_C(1) << 48) - base &&
        mMmu.root != NULL && mMmu.configured &&
        region_matches(base, size, type, permissions);
}

int kshim_mmu_configure(const kshim_mmu_config_t *config)
{
    kshim_mmu_region_t region;
    uint64_t image_base = (uint64_t)(uintptr_t)__image_start_address__;
    uint64_t image_end = (uint64_t)(uintptr_t)__image_end_address__;

    if (mMmu.sealed || kshim_mmu_is_enabled() || config == NULL ||
        config->extra_region_count > KSHIM_MMU_MAX_EXTRA_REGIONS ||
        (config->extra_region_count != 0U && config->extra_regions == NULL) ||
        image_end <= image_base)
        return -1;

    memset(&mMmu, 0, sizeof(mMmu));
    memset(mTables, 0, sizeof(mTables));
    mMmu.root = allocate_table();
    if (mMmu.root == NULL)
        return -2;

    /* Map only memory owned by this runtime and explicit device resources.
     * Reservation entries and no-map pools never become an implicit RAM map. */
    if (config->fdt) {
        struct dt_range reservations[128]; bool no_map[128]; size_t count;
        if (dt_validate(config->fdt, fdt_totalsize(config->fdt)) ||
            dt_reservations(config->fdt, reservations, no_map, 128, &count)) return -1;
        for (size_t i = 0; i < count; ++i) {
            for (size_t j = 0; j < config->extra_region_count; ++j) {
                const kshim_mmu_region_t *extra = &config->extra_regions[j];
                if (no_map[i] && extra->type == KSHIM_MMU_NORMAL_WB &&
                    ranges_overlap(reservations[i].base, reservations[i].size, extra->base, extra->size)) return -1;
            }
        }
    }

    region = (kshim_mmu_region_t){
        .base = image_base,
        .size = image_end - image_base,
        .type = KSHIM_MMU_NORMAL_WB,
        .permissions = KSHIM_MMU_READ | KSHIM_MMU_WRITE | KSHIM_MMU_EXECUTE,
    };
    if (add_region(&region) != 0)
        return -2;

    if (config->fdt != NULL && fdt_check_header(config->fdt) == 0) {
        uint64_t fdt_base = (uint64_t)(uintptr_t)config->fdt;
        uint64_t fdt_size = (uint32_t)fdt_totalsize(config->fdt);

        if (!range_within(
                fdt_base, fdt_size, image_base, image_end - image_base)) {
            region = (kshim_mmu_region_t){
                .base = fdt_base,
                .size = fdt_size,
                .type = KSHIM_MMU_NORMAL_WB,
                .permissions = KSHIM_MMU_READ | KSHIM_MMU_WRITE,
            };
            if (add_region(&region) != 0)
                return -2;
        }
    }

    region = (kshim_mmu_region_t){
        .base = config->framebuffer_base,
        .size = config->framebuffer_size,
        .type = KSHIM_MMU_NORMAL_NC,
        .permissions = KSHIM_MMU_READ | KSHIM_MMU_WRITE,
    };
    if (add_region(&region) != 0)
        return -2;

    region = (kshim_mmu_region_t){
        .base = config->uefi_base,
        .size = config->uefi_size,
        .type = KSHIM_MMU_NORMAL_WB,
        .permissions = KSHIM_MMU_READ | KSHIM_MMU_WRITE | KSHIM_MMU_EXECUTE,
    };
    if (add_region(&region) != 0)
        return -2;

    for (size_t index = 0; index < config->extra_region_count; index++) {
        if (add_region(&config->extra_regions[index]) != 0)
            return -2;
    }

    if (config->map_qemu_test_windows) {
        const kshim_mmu_region_t qemu_regions[] = {
            {UINT64_C(0x08000000), UINT64_C(0x00010000), KSHIM_MMU_DEVICE,
             KSHIM_MMU_READ | KSHIM_MMU_WRITE},
            {UINT64_C(0x080a0000), UINT64_C(0x00100000), KSHIM_MMU_DEVICE,
             KSHIM_MMU_READ | KSHIM_MMU_WRITE},
            {UINT64_C(0x44000000), UINT64_C(0x00c00000), KSHIM_MMU_NORMAL_NC,
             KSHIM_MMU_READ | KSHIM_MMU_WRITE},
            {UINT64_C(0x46000000), MMU_PAGE_SIZE, KSHIM_MMU_NORMAL_WB,
             KSHIM_MMU_READ | KSHIM_MMU_WRITE},
        };

        for (size_t index = 0;
             index < sizeof(qemu_regions) / sizeof(qemu_regions[0]); index++) {
            /* The configured scanout mapping supersedes the broad QEMU
             * fallback window. Mapping both creates conflicting leaf entries
             * and prevents SMP initialization in the UI profile. */
            if (index == 2U && ranges_overlap(
                    config->framebuffer_base, config->framebuffer_size,
                    qemu_regions[index].base, qemu_regions[index].size))
                continue;
            if (add_region(&qemu_regions[index]) != 0)
                return -2;
        }
    }

    mMmu.entry_el = current_el();
    mMmu.configured = 1U;
#if defined(__aarch64__)
    clean_range((uint64_t)(uintptr_t)&mMmu, sizeof(mMmu));
    clean_range((uint64_t)(uintptr_t)mTables, sizeof(mTables));
    __asm__ volatile("dsb sy" ::: "memory");
#endif
    return 0;
}

int kshim_mmu_configure_default(const void *fdt)
{
    struct kshim_resources *r = kshim_resources_get();
    kshim_mmu_region_t extras[KSHIM_MMU_MAX_EXTRA_REGIONS];
    size_t count = r->mapping_count;
    if (count + 1 > KSHIM_MMU_MAX_EXTRA_REGIONS) return -1;
    memcpy(extras, r->mappings, count * sizeof(*extras));
    if (kshim_console_base()) extras[count++] = (kshim_mmu_region_t){
        kshim_console_base(), kshim_console_size(), KSHIM_MMU_DEVICE, KSHIM_MMU_READ | KSHIM_MMU_WRITE};
    kshim_mmu_config_t config = {
        .fdt = fdt,
        .framebuffer_base = r->framebuffer.render_address,
        .framebuffer_size = r->framebuffer.buffer_size ? r->framebuffer.buffer_size :
            (uint64_t)(r->framebuffer.stride ? r->framebuffer.stride :
                r->framebuffer.width * r->framebuffer.bpp / 8) * r->framebuffer.height,
        .uefi_base = CONFIG_KSHIM_UEFI_LOAD_ADDRESS, .uefi_size = CONFIG_KSHIM_UEFI_MAX_SIZE,
        .extra_regions = extras, .extra_region_count = count,
        .map_qemu_test_windows = CONFIG_KSHIM_QEMU_PLATFORM || CONFIG_KSHIM_SMP_SELFTEST,
    };
    return kshim_mmu_configure(&config);
}

int kshim_mmu_enable_current_cpu(void)
{
    return enable_at_el(current_el());
}

int kshim_mmu_enable_secondary(uint64_t entry_el)
{
    if (entry_el != mMmu.entry_el)
        return -1;
    return enable_at_el(entry_el);
}

int kshim_mmu_is_enabled(void)
{
#if defined(__aarch64__)
    uint64_t value;
    uint64_t exception_level = current_el();

    if (exception_level == CURRENT_EL1)
        __asm__ volatile("mrs %0, sctlr_el1" : "=r"(value));
    else if (exception_level == CURRENT_EL2)
        __asm__ volatile("mrs %0, sctlr_el2" : "=r"(value));
    else
        return 0;
    return (value & SCTLR_M) != 0U;
#else
    return mHostEnabled != 0U;
#endif
}

uint64_t kshim_mmu_entry_el(void)
{
    return mMmu.entry_el;
}

uint64_t kshim_mmu_root_table(void)
{
    return (uint64_t)(uintptr_t)mMmu.root;
}

int kshim_mmu_prepare_handoff(void)
{
    if (!mMmu.configured || !kshim_mmu_is_enabled())
        return 0;
    /* The assembly leaf performs the last clean without writing a C stack
     * frame afterwards. HCR is never modified by this runtime. */
    disable_at_el(current_el());
    return 0;
}

void kshim_mmu_cpu_off_prepare(void)
{
    (void)kshim_mmu_prepare_handoff();
}

void _arm64_mmu_setup(void)
{
    if (!mMmu.configured && kshim_mmu_configure_default(NULL) != 0)
        return;
    (void)kshim_mmu_enable_current_cpu();
}

uint64_t kshim_mmu_image_base(void)
{
    return image_runtime_base();
}

/* Subtract page-rounded no-map reservations before building any RAM leaves. */
static int map_unreserved_range(uint64_t base, uint64_t size,
                                 const struct dt_range *reserved,
                                 const bool *no_map, size_t count,
                                 kshim_mmu_memory_type_t type)
{
    uint64_t end;

    if (size == 0U)
        return 0;
    if (base > UINT64_MAX - size)
        return -1;
    end = align_down(base + size, MMU_PAGE_SIZE);
    if (align_up(base, MMU_PAGE_SIZE, &base) != 0)
        return -1;
    while (base < end) {
        uint64_t next = end;
        uint64_t skip = base;
        for (size_t index = 0; index < count; ++index) {
            uint64_t r_end;
            if (!no_map[index])
                continue;
            if (align_up(reserved[index].base + reserved[index].size,
                         MMU_PAGE_SIZE, &r_end) != 0)
                return -1;
            uint64_t r_base = align_down(reserved[index].base, MMU_PAGE_SIZE);
            if (r_base <= base && r_end > skip)
                skip = r_end;
            else if (r_base > base && r_base < next)
                next = r_base;
        }
        if (skip > base) {
            base = skip;
            continue;
        }
        if (map_range(base, next - base, type,
                      KSHIM_MMU_READ | KSHIM_MMU_WRITE) != 0)
            return -1;
        base = next;
    }
    return 0;
}

/* Implicit RAM stays within low 4 GiB; explicit runtime-owned mappings may
 * lie outside it. Disabled memory nodes and no-map pools are not RAM aliases. */
static int map_declared_memory(const void *fdt)
{
    struct dt_range reserved[128];
    bool no_map[128];
    size_t count;
    int node;

    if (fdt == NULL || dt_validate(fdt, fdt_totalsize(fdt)) != 0 ||
        dt_reservations(fdt, reserved, no_map, 128, &count) != 0)
        return -1;
    /* The broad peripheral window must not fill the holes left by a no-map
     * RAM reservation below 2 GiB (for example on QEMU). */
    if (map_unreserved_range(0U, KSHIM_MMU_PERIPHERAL_LIMIT, reserved,
                             no_map, count, KSHIM_MMU_DEVICE) != 0)
        return -1;

    for (node = fdt_first_subnode(fdt, 0); node >= 0;
         node = fdt_next_subnode(fdt, node)) {
        const char *type = dt_string(fdt, node, "device_type");
        if (type == NULL || strcmp(type, "memory") != 0 || !dt_available(fdt, node))
            continue;
        int length;
        int ac = fdt_address_cells(fdt, 0);
        int sc = fdt_size_cells(fdt, 0);
        if (ac < 1 || ac > 2 || sc < 1 || sc > 2 ||
            fdt_getprop(fdt, node, "reg", &length) == NULL || length <= 0 ||
            length % (4 * (ac + sc)) != 0)
            return -1;
        unsigned tuples = (unsigned)length / (4U * (unsigned)(ac + sc));
        for (unsigned index = 0; index < tuples; ++index) {
            uint64_t base, size;
            if (dt_raw_reg(fdt, node, index, &base, &size) != 0)
                return -1;
            if (size == 0U || base >= KSHIM_MMU_DRAM_LIMIT)
                continue;
            if (size > KSHIM_MMU_DRAM_LIMIT - base)
                size = KSHIM_MMU_DRAM_LIMIT - base;
            if (map_unreserved_range(base, size, reserved, no_map, count,
                                     KSHIM_MMU_NORMAL_WB) != 0)
                return -1;
        }
    }
    return node == -FDT_ERR_NOTFOUND ? 0 : -1;
}

static int map_early_region(uint64_t base, uint64_t size,
                            kshim_mmu_memory_type_t type, uint32_t permissions)
{
    const kshim_mmu_region_t region = {base, size, type, permissions};
    return add_region(&region);
}

static int map_early_payload(const kshim_mmu_early_config_t *config)
{
    struct dt_range reserved[128];
    bool no_map[128];
    size_t count;
    uint64_t end;

    if (config->payload_base == 0U || config->payload_end <= config->payload_base)
        return 0;
    if (dt_reservations(config->fdt, reserved, no_map, 128, &count) != 0)
        return -1;
    if (align_up(config->payload_end, MMU_PAGE_SIZE, &end) != 0)
        return -1;
    uint64_t base = align_down(config->payload_base, MMU_PAGE_SIZE);
    /* The manifest is not validated yet. Do not fill protected holes, or
     * leave them for manifest/DT consumers to fault on after early enable. */
    for (size_t index = 0; index < count; ++index) {
        uint64_t r_end;
        if (!no_map[index])
            continue;
        if (align_up(reserved[index].base + reserved[index].size,
                     MMU_PAGE_SIZE, &r_end) != 0)
            return -1;
        if (base < r_end && align_down(reserved[index].base, MMU_PAGE_SIZE) < end)
            return -1;
    }
    return map_range(base, end - base, KSHIM_MMU_NORMAL_WB,
                      KSHIM_MMU_READ | KSHIM_MMU_WRITE);
}

static int map_early_display(const void *fdt)
{
#if CONFIG_KSHIM_FRAMEBUFFER
    struct dt_range display;
    if (fdt != NULL && kshim_dt_splash_reservation(fdt, &display, NULL) == 0 &&
        map_early_region(display.base, display.size, KSHIM_MMU_NORMAL_NC,
                         KSHIM_MMU_READ | KSHIM_MMU_WRITE) != 0)
        return -1;
#else
    (void)fdt;
#endif
    if (CONFIG_KSHIM_FB_RENDER_ADDRESS != 0U) {
        uint64_t stride = CONFIG_KSHIM_FB_STRIDE ? CONFIG_KSHIM_FB_STRIDE :
            (uint64_t)CONFIG_KSHIM_FB_WIDTH * kshim_fb_format_bpp(CONFIG_KSHIM_FB_FORMAT) / 8U;
        if (map_early_region(CONFIG_KSHIM_FB_RENDER_ADDRESS,
                             stride * CONFIG_KSHIM_FB_HEIGHT,
                             KSHIM_MMU_NORMAL_NC,
                             KSHIM_MMU_READ | KSHIM_MMU_WRITE) != 0)
            return -1;
    }
    return map_early_region(CONFIG_KSHIM_UI_SCRATCH_BASE,
                             CONFIG_KSHIM_UI_SCRATCH_SIZE, KSHIM_MMU_NORMAL_NC,
                             KSHIM_MMU_READ | KSHIM_MMU_WRITE);
}

int kshim_mmu_configure_early(const kshim_mmu_early_config_t *config)
{
    uint64_t image_base;
    uint64_t image_span;
    int status;

    if (config == NULL)
        return -1;
    /* Somebody already runs with translation on: leave it alone. */
    if (kshim_mmu_is_enabled() != 0)
        return mMmu.configured ? 0 : -12;
    /* Another CPU may still be using the pool after this CPU disables it. */
    if (mMmu.sealed)
        return -16;
#if defined(__aarch64__)
    if (!supported_entry(current_el()))
        return -13;
#endif
#if CONFIG_KSHIM_TOUCH
    /* Touch probing can dereference firmware-provided command-DB pointers
     * inside no-map pools. Keep the established MMU-off resource discovery
     * until those consumers provide their early mapping requirements. */
    return -15;
#endif

    image_base = image_runtime_base();
    image_span = image_runtime_span();
    if (image_span == 0U)
        return -2;
    if (mImageBaseVerified < 0)
        mImageBaseVerified = image_base_matches(image_base) ? 1 : 0;
    if (mImageBaseVerified == 0)
        return -3;

    memset(&mMmu, 0, sizeof(mMmu));
    memset(mTables, 0, sizeof(mTables));
    mMmu.root = allocate_table();
    if (mMmu.root == NULL)
        return -4;

    /* Peripheral space and declared RAM, both minus no-map reservations. */
    if (map_declared_memory(config->fdt) < 0) {
        status = -6;
        goto failed;
    }
    /* 3. The packed payload: base image, shim slot, DTBs and manifest. */
    if (map_early_payload(config) != 0) {
        status = -7;
        goto failed;
    }
    /* 4. The image itself, executable.  Splits whatever step 2 or 3 laid down. */
    if (map_early_region(image_base, image_span, KSHIM_MMU_NORMAL_WB,
                          KSHIM_MMU_READ | KSHIM_MMU_WRITE |
                          KSHIM_MMU_EXECUTE) != 0) {
        status = -8;
        goto failed;
    }
    /* 5. The runtime DT, in case it sits outside every declared RAM range. */
    if (config->fdt != NULL && fdt_check_header(config->fdt) == 0) {
        uint64_t dt_base = (uint64_t)(uintptr_t)config->fdt;
        uint64_t dt_size = (uint32_t)fdt_totalsize(config->fdt);
        if (dt_size != 0U &&
            !range_within(dt_base, dt_size, image_base, image_span) &&
            map_early_region(dt_base, dt_size, KSHIM_MMU_NORMAL_WB,
                              KSHIM_MMU_READ | KSHIM_MMU_WRITE) != 0) {
            status = -9;
            goto failed;
        }
    }
    /* Display and scratch have their final NC type before the first enable. */
    if (map_early_display(config->fdt) != 0) {
        status = -14;
        goto failed;
    }

    mMmu.entry_el = current_el();
    mMmu.configured = 1U;
    clean_range((uint64_t)(uintptr_t)&mMmu, sizeof(mMmu));
    clean_range((uint64_t)(uintptr_t)mTables, sizeof(mTables));
#if defined(__aarch64__)
    __asm__ volatile("dsb sy" ::: "memory");
#endif
    if (enable_at_el(mMmu.entry_el) != 0) {
        status = -10;
        goto failed;
    }
    if (kshim_mmu_is_enabled() == 0) {
        status = -11;
        goto failed;
    }
    return 0;

failed:
    /* Never clear a table pool while it is still in use. */
    (void)kshim_mmu_prepare_handoff();
    memset(&mMmu, 0, sizeof(mMmu));
    memset(mTables, 0, sizeof(mTables));
    return status;
}

int kshim_mmu_add_region(uint64_t base, uint64_t size,
                         kshim_mmu_memory_type_t type, uint32_t permissions)
{
    if (base == 0U || size == 0U || type > KSHIM_MMU_DEVICE ||
        (permissions & KSHIM_MMU_READ) == 0U)
        return -1;
    if (mMmu.root == NULL || mMmu.configured == 0U)
        return -1;
    /* Tables become immutable after their first enable, including while a
     * different CPU may still be using them. No online BBM API is exposed. */
    if (mMmu.sealed || kshim_mmu_is_enabled())
        return -2;
    const kshim_mmu_region_t region = {base, size, type, permissions};
    if (add_region(&region) != 0)
        return -3;
    clean_range((uint64_t)(uintptr_t)mTables, sizeof(mTables));
#if defined(__aarch64__)
    __asm__ volatile("dsb sy" ::: "memory");
#endif
    return 0;
}
