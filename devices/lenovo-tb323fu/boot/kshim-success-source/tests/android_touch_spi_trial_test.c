/* SPDX-License-Identifier: MIT */
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <libfdt.h>
#include "tb323fu_android_touch.h"

#define PARENT "/soc/qcom,qupv3_4_geni_se@1ac0000/spi@1a80000"
#define CHILD PARENT "/novatek@0"
static uint64_t tree[1024 * 1024 / 8], saved[1024 * 1024 / 8];
static unsigned char pad[350 * 1024];

static void fixture(void)
{
    int node;
    assert(fdt_create_empty_tree(tree, sizeof tree) == 0);
    assert(fdt_setprop_string(tree, 0, "model", "TB323FU synthetic trial") == 0);
    node = fdt_add_subnode(tree, 0, "chosen");
    assert(node >= 0);
    assert(fdt_setprop_string(tree, node, "bootargs", "android-original-dynamic-data") == 0);
    assert(fdt_setprop_u64(tree, node, "linux,initrd-start", 0x8877665544332211ULL) == 0);
    node = fdt_add_subnode(tree, 0, "memory");
    assert(node >= 0);
    assert(fdt_setprop_u64(tree, node, "reg", 0x8000000090000000ULL) == 0);
    assert(fdt_setprop(tree, node, "unrelated-large-data", pad, sizeof pad) == 0);
    node = fdt_add_subnode(tree, 0, "soc");
    assert(node >= 0);
    node = fdt_add_subnode(tree, node, "qcom,qupv3_4_geni_se@1ac0000");
    assert(node >= 0);
    node = fdt_add_subnode(tree, node, "spi@1a80000");
    assert(node >= 0);
    assert(fdt_setprop_string(tree, node, "status", "okay") == 0);
    assert(fdt_setprop_u32(tree, node, "spi-max-frequency", 19200000) == 0);
    node = fdt_add_subnode(tree, node, "novatek@0");
    assert(node >= 0);
    assert(fdt_setprop_string(tree, node, "compatible", "novatek,NVT-ts-spi") == 0);
    assert(fdt_setprop_u32(tree, node, "reg", 0) == 0);
    assert(fdt_setprop_string(tree, node, "status", "ok") == 0);
    /* Keep capacity for negative-case edits; real populated data exceeds256KiB. */
    assert(fdt_totalsize(tree) > 256 * 1024);
}

static void reject_unchanged(size_t available)
{
    memcpy(saved, tree, sizeof tree);
    assert(tb323fu_android_touch_spi_trial(tree, available) < 0);
    assert(memcmp(saved, tree, sizeof tree) == 0);
}

int main(void)
{
    fixture();
    int n = fdt_path_offset(tree, PARENT), len;
    const unsigned char *prop = fdt_getprop(tree, n, "spi-max-frequency", &len);
    assert(prop && len == 4);
    size_t offset = prop - (const unsigned char *)tree;
    size_t total = fdt_totalsize(tree);
    memcpy(saved, tree, sizeof tree);
    assert(tb323fu_android_touch_spi_trial(tree, sizeof tree) == 1);
    assert(fdt_totalsize(tree) == total && fdt_check_full(tree, sizeof tree) == 0);
    assert(memcmp(saved, tree, offset) == 0);
    assert(memcmp((unsigned char *)saved + offset + 4,
                  (unsigned char *)tree + offset + 4,
                  sizeof tree - offset - 4) == 0);
    assert(fdt32_to_cpu(*(const fdt32_t *)((const unsigned char *)tree + offset)) == 4800000);
    memcpy(saved, tree, sizeof tree);
    assert(tb323fu_android_touch_spi_trial(tree, sizeof tree) == 0);
    assert(memcmp(saved, tree, sizeof tree) == 0);

    fixture(); n = fdt_path_offset(tree, CHILD);
    assert(fdt_setprop_string(tree, n, "compatible", "another,touch") == 0);
    reject_unchanged(sizeof tree);
    fixture(); n = fdt_path_offset(tree, CHILD);
    assert(fdt_setprop_string(tree, n, "status", "disabled") == 0);
    reject_unchanged(sizeof tree);
    fixture(); n = fdt_path_offset(tree, PARENT);
    assert(fdt_setprop_string(tree, n, "status", "disabled") == 0);
    reject_unchanged(sizeof tree);
    fixture(); n = fdt_path_offset(tree, CHILD);
    assert(fdt_setprop_u32(tree, n, "reg", 1) == 0);
    reject_unchanged(sizeof tree);
    fixture(); n = fdt_path_offset(tree, PARENT);
    assert(fdt_setprop_u32(tree, n, "spi-max-frequency", 9600000) == 0);
    reject_unchanged(sizeof tree);
    fixture(); n = fdt_path_offset(tree, PARENT);
    assert(fdt_setprop_u64(tree, n, "spi-max-frequency", 19200000) == 0);
    reject_unchanged(sizeof tree);
    fixture(); n = fdt_path_offset(tree, PARENT);
    assert(fdt_delprop(tree, n, "spi-max-frequency") == 0);
    reject_unchanged(sizeof tree);
    fixture(); n = fdt_path_offset(tree, CHILD);
    assert(fdt_set_name(tree, n, "other@0") == 0);
    reject_unchanged(sizeof tree);
    fixture(); reject_unchanged(fdt_totalsize(tree) - 1);
    fixture(); ((unsigned char *)tree)[0] ^= 0xff;
    reject_unchanged(sizeof tree);
    assert(tb323fu_android_touch_spi_trial(NULL, 0) < 0);
    puts("PASS: one-cell change; large dynamic tree preserved; repeat unchanged; 11 mismatch cases unchanged");
    return 0;
}
