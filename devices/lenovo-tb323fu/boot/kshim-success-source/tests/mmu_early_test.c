/* SPDX-License-Identifier: MIT */
#include <assert.h>
#include <stdio.h>
#include <string.h>

/* Inspect the handoff registry as well as descriptors. Register transitions
 * run separately in QEMU; dirty-cache visibility still requires hardware. */
#include "../src/arch/arm64/mmu.c"

__asm__(".pushsection .test_image,\"aw\",@progbits\n.balign 4096\n"
        ".global __image_start_address__\n__image_start_address__:\n"
        ".long 0x14000001\n.space 0x34\n.long 0x644d5241\n.long 0\n"
        ".space 0x1fc0\n"
        ".global __image_end_address__\n__image_end_address__:\n.popsection\n");

static uint64_t tree[2048];

static void reg(int node, uint64_t base, uint64_t size)
{
    fdt64_t cells[] = {cpu_to_fdt64(base), cpu_to_fdt64(size)};
    assert(fdt_setprop(tree, node, "reg", cells, sizeof(cells)) == 0);
}

static void fixture(void)
{
    assert(fdt_create_empty_tree(tree, sizeof(tree)) == 0);
    assert(fdt_setprop_u32(tree, 0, "#address-cells", 2) == 0);
    assert(fdt_setprop_u32(tree, 0, "#size-cells", 2) == 0);
    int memory = fdt_add_subnode(tree, 0, "memory@80000000");
    assert(memory >= 0);
    assert(fdt_setprop_string(tree, memory, "device_type", "memory") == 0);
    reg(memory, UINT64_C(0x80000000), UINT64_C(0x180000000));
    int disabled = fdt_add_subnode(tree, 0, "memory@70000000");
    assert(disabled >= 0);
    assert(fdt_setprop_string(tree, disabled, "device_type", "memory") == 0);
    assert(fdt_setprop_string(tree, disabled, "status", "disabled") == 0);
    reg(disabled, UINT64_C(0x70000000), UINT64_C(0x100000));
    int parent = fdt_add_subnode(tree, 0, "reserved-memory");
    assert(parent >= 0);
    assert(fdt_setprop_u32(tree, parent, "#address-cells", 2) == 0);
    assert(fdt_setprop_u32(tree, parent, "#size-cells", 2) == 0);
    assert(fdt_setprop(tree, parent, "ranges", NULL, 0) == 0);
    const struct { const char *name; uint64_t base, size; int no_map; } ranges[] = {
        {"firmware@90000000", 0x90000000, 0x200000, 1},
        {"overlap@90100000", 0x90100000, 0x200000, 1},
        {"unaligned@91000001", 0x91000001, 1, 1},
        {"cont_splash_region@92000000", 0x92000000, 0x400000, 1},
        {"ordinary@93000000", 0x93000000, 0x200000, 0},
        {"low@48000000", 0x48000000, 0x1000, 1},
        {"placeholder@0", 0, 0, 1},
    };
    for (size_t i = 0; i < sizeof(ranges) / sizeof(ranges[0]); ++i) {
        parent = fdt_path_offset(tree, "/reserved-memory");
        int n = fdt_add_subnode(tree, parent, ranges[i].name);
        assert(n >= 0);
        reg(n, ranges[i].base, ranges[i].size);
        if (ranges[i].no_map)
            assert(fdt_setprop(tree, n, "no-map", NULL, 0) == 0);
    }
    assert(fdt_add_mem_rsv(tree, 0x94000000, 0x1000) == 0);
    assert(dt_validate(tree, sizeof(tree)) == 0);
}

static uint64_t descriptor(uint64_t address)
{
    translation_table_t *table = mMmu.root;
    for (unsigned level = 0; level <= 3U; ++level) {
        uint64_t d = (*table)[table_index(address, level)];
        if ((d & DESC_TYPE_MASK) != DESC_TABLE || level == 3U)
            return d;
        table = (void *)(uintptr_t)(d & MMU_ADDRESS_MASK);
    }
    return 0;
}

static int cleaned_region(uint64_t base, uint64_t size)
{
    for (size_t i = 0; i < mMmu.region_count; ++i) {
        const kshim_mmu_region_t *r = &mMmu.regions[i];
        if (r->type == KSHIM_MMU_NORMAL_WB && (r->permissions & KSHIM_MMU_WRITE) &&
            range_within(base, size, r->base, r->size))
            return 1;
    }
    return 0;
}

static void rejected(const kshim_mmu_early_config_t *config, int status)
{
    assert(kshim_mmu_configure_early(config) == status);
    assert(!kshim_mmu_is_enabled());
    assert(!kshim_mmu_root_table());
    assert(!mMmu.configured && !mMmu.tables_used && !mMmu.region_count);
    for (size_t i = 0; i < MMU_TABLE_COUNT; ++i)
        for (size_t j = 0; j < MMU_TABLE_ENTRIES; ++j)
            assert(mTables[i][j] == 0U);
}

static void failure_cases(void)
{
    kshim_mmu_early_config_t config = {.fdt = tree};
    fixture();
    int memory = fdt_path_offset(tree, "/memory@80000000");
    fdt64_t bad_reg[] = {cpu_to_fdt64(0x80000000), cpu_to_fdt64(0x100000),
                        cpu_to_fdt64(UINT64_MAX - 0x1000), cpu_to_fdt64(0x2000)};
    assert(fdt_setprop(tree, memory, "reg", bad_reg, sizeof(bad_reg)) == 0);
    rejected(&config, -6);

    fixture();
    memory = fdt_path_offset(tree, "/memory@80000000");
    assert(fdt_setprop(tree, memory, "reg", bad_reg, sizeof(bad_reg) - 4) == 0);
    rejected(&config, -6);

    fixture();
    int display = fdt_path_offset(tree, "/reserved-memory/cont_splash_region@92000000");
    reg(display, (uint64_t)(uintptr_t)tree, sizeof(tree));
    rejected(&config, -14); /* NC display must not replace the WB external FDT. */

    fixture();
    config.payload_base = 0x80000000;
    config.payload_end = 0xa0400000;
    rejected(&config, -7); /* Unvalidated payload bounds cross protected holes. */
    config.payload_base = config.payload_end = 0;
    config.fdt = NULL;
    rejected(&config, -6);
}

int main(void)
{
    failure_cases();
    fixture();
    int parent = fdt_path_offset(tree, "/reserved-memory");
    int owned_dt = fdt_add_subnode(tree, parent, "runtime-dt");
    assert(owned_dt >= 0);
    reg(owned_dt, (uint64_t)(uintptr_t)tree, sizeof(tree));
    assert(fdt_setprop(tree, owned_dt, "no-map", NULL, 0) == 0);
    const kshim_mmu_early_config_t config = {
        .fdt = tree, .payload_base = 0xa0000000, .payload_end = 0xa0400000,
    };
    assert(kshim_mmu_configure_early(&config) == 0);
    assert(kshim_mmu_is_enabled());
    assert(descriptor(0x8ffff000) & 1);
    assert(!descriptor(0x90000000));
    assert(!descriptor(0x902ff000));
    assert(descriptor(0x90300000) & 1);
    assert(!descriptor(0x91000000));
    assert(descriptor(0x91001000) & 1);
    assert(!descriptor(0x48000000));
    assert(!descriptor(0x94000000));
    assert(kshim_mmu_region_matches(0x93000000, 0x1000,
        KSHIM_MMU_NORMAL_WB, KSHIM_MMU_READ | KSHIM_MMU_WRITE));
    assert(kshim_mmu_region_matches(0x92000000, 0x400000,
        KSHIM_MMU_NORMAL_NC, KSHIM_MMU_READ | KSHIM_MMU_WRITE));
    assert(((descriptor(0x70000000) >> 2) & 7) == KSHIM_MMU_DEVICE);
    assert(descriptor(0xfffff000) & 1);
    assert(!descriptor(UINT64_C(0x100000000)));
    assert(!descriptor(UINT64_C(0x180000000)));
    assert(descriptor(0xa0000000) & DESC_UXN);
    assert(cleaned_region((uint64_t)(uintptr_t)tree, fdt_totalsize(tree)));
    assert(kshim_mmu_region_matches((uint64_t)(uintptr_t)tree, fdt_totalsize(tree),
        KSHIM_MMU_NORMAL_WB, KSHIM_MMU_READ | KSHIM_MMU_WRITE));
    assert(cleaned_region(image_runtime_base(), image_runtime_span()));
    assert(!kshim_mmu_region_matches(0x90000000, 0x1000,
        KSHIM_MMU_NORMAL_WB, KSHIM_MMU_READ | KSHIM_MMU_WRITE));
    assert(!kshim_mmu_region_matches(0x92000000, 0,
        KSHIM_MMU_NORMAL_NC, KSHIM_MMU_READ | KSHIM_MMU_WRITE));
    assert(!kshim_mmu_region_matches(UINT64_MAX - 0x1000, 0x2000,
        KSHIM_MMU_NORMAL_NC, KSHIM_MMU_READ | KSHIM_MMU_WRITE));
    uint64_t before = descriptor(0x92000000);
    uint64_t root = kshim_mmu_root_table();
    const kshim_mmu_config_t late = {0};
    size_t tables = mMmu.tables_used, regions = mMmu.region_count;
    assert(kshim_mmu_configure(&late) == -1);
    assert(kshim_mmu_add_region(0x92000000, 0x1000, KSHIM_MMU_NORMAL_WB,
        KSHIM_MMU_READ | KSHIM_MMU_WRITE) == -2);
    assert(before == descriptor(0x92000000));
    assert(mMmu.tables_used == tables && mMmu.region_count == regions);
    assert(kshim_mmu_prepare_handoff() == 0);
    assert(!kshim_mmu_is_enabled());
    assert(kshim_mmu_prepare_handoff() == 0);
    assert(kshim_mmu_add_region(0x92000000, 0x1000, KSHIM_MMU_NORMAL_WB,
        KSHIM_MMU_READ | KSHIM_MMU_WRITE) == -2);
    assert(kshim_mmu_configure(&late) == -1);
    assert(kshim_mmu_configure_early(&config) == -16);
    assert(root == kshim_mmu_root_table());
    assert(before == descriptor(0x92000000));
    assert(mMmu.tables_used == tables && mMmu.region_count == regions);
    puts("Early MMU reservations, display, clamp, cache registry, failures and sealing passed");
    return 0;
}
