/* SPDX-License-Identifier: GPL-3.0-only */
#include "storage_ufs_read_export.h"
#include <assert.h>
#include <device_info.h>
#include <libfdt.h>
#include <stdio.h>
#include <string.h>

static union { uint64_t align; unsigned char bytes[16384]; } blob, before;
static struct kshim_ufs_read_report report;
static int chosen(void) { return fdt_path_offset(blob.bytes, "/chosen"); }
static const unsigned char *property(int *length)
{
    return fdt_getprop(blob.bytes, chosen(), KSHIM_UFS_READ_FDT_PROPERTY, length);
}
static const unsigned char *link_property(int *length)
{
    return fdt_getprop(blob.bytes, chosen(), KSHIM_UFS_LINK_FDT_PROPERTY, length);
}
static uint32_t get_be32(const unsigned char *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}
static uint64_t get_be64(const unsigned char *p)
{
    return ((uint64_t)get_be32(p) << 32) | (uint64_t)get_be32(p + 4);
}
static void fixture(int with_chosen)
{
    memset(&blob, 0, sizeof(blob));
    assert(fdt_create_empty_tree(blob.bytes, sizeof(blob.bytes)) == 0);
    assert(fdt_setprop_string(blob.bytes, 0, "model", "keep root model") == 0);
    assert(fdt_add_subnode(blob.bytes, 0, "unrelated") >= 0);
    assert(fdt_setprop_u32(blob.bytes, fdt_path_offset(blob.bytes, "/unrelated"), "keep", 0xaabbccdd) == 0);
    if (with_chosen) {
        assert(fdt_add_subnode(blob.bytes, 0, "chosen") >= 0);
        assert(fdt_setprop_string(blob.bytes, chosen(), "bootargs", "keep bootargs") == 0);
    }
    memset(&report, 0, sizeof(report));
    report.version = KSHIM_UFS_READ_EXPORT_VERSION;
    report.board = DEVICE_BOARD_TB323FU;
}
static void fill_report(void)
{
    for (unsigned i = 0; i < sizeof(report.partition_uuid); ++i)
        report.partition_uuid[i] = (uint8_t)i;
    for (unsigned i = 0; i < sizeof(report.header_prefix); ++i)
        report.header_prefix[i] = (uint8_t)(0x20 + i);
    for (unsigned i = 0; i < sizeof(report.response_prefix); ++i)
        report.response_prefix[i] = (uint8_t)(0x40 + i);
    report.stage = KSHIM_UFS_READ_STAGE_DONE;
    report.status = KSHIM_UFS_SCSI;
    report.release_status = KSHIM_STORAGE_OK;
    report.restore_verified = 1;
    report.handoff_safe = 1;
    report.block_size = 4096;
    report.block_count = UINT64_C(0x1122334455667788);
    report.dma_base = UINT64_C(0x1020304050607080);
    report.first_lba = UINT64_C(0x0000000000000800);
    report.last_lba = UINT64_C(0x00000000004007ff);
    report.header_crc = 0x0a0b0c0d;
    report.tail_crc = 0xf0e0d0c0;
    report.hcs = 0x00000001;
    report.is = 0x00000002;
    report.doorbell = 0;
    report.last_scsi_status = 0;
    report.last_sense_key = 5;
    report.ocs = 3;
    report.debug_valid = 0x0f;
}
static void unrelated_unchanged(void)
{
    int length;
    const char *model = fdt_getprop(blob.bytes, 0, "model", &length);
    assert(model && length == 16 && strcmp(model, "keep root model") == 0);
    const char *args = fdt_getprop(blob.bytes, chosen(), "bootargs", &length);
    assert(args && length == 14 && strcmp(args, "keep bootargs") == 0);
    const unsigned char *keep = fdt_getprop(blob.bytes, fdt_path_offset(blob.bytes, "/unrelated"), "keep", &length);
    static const unsigned char expected[4] = {0xaa, 0xbb, 0xcc, 0xdd};
    assert(keep && length == 4 && memcmp(keep, expected, 4) == 0);
}
static void test_exact_wire_layout(void)
{
    /* Any reordering, little-endian copy, narrowed address/code, expanded UUID
     * or exported C padding changes the size or one of these offsets. */
    fixture(1);
    fill_report();
    int total = fdt_totalsize(blob.bytes), length;
    assert(KSHIM_UFS_READ_EXPORT_BYTES == 164U);
    assert(storage_ufs_read_export(blob.bytes, &report) == 0);
    const unsigned char *p = property(&length);
    assert(p && length == 164);
    assert(fdt_totalsize(blob.bytes) == (unsigned)total);
    assert(get_be32(p + 0) == 1);
    assert(get_be32(p + 4) == (uint32_t)DEVICE_BOARD_TB323FU);
    assert(get_be32(p + 8) == (uint32_t)KSHIM_UFS_READ_STAGE_DONE);
    assert(get_be32(p + 12) == (uint32_t)KSHIM_UFS_SCSI); /* -24 two's-complement */
    assert(get_be32(p + 16) == 0);
    assert(get_be32(p + 20) == 1 && get_be32(p + 24) == 1);
    assert(get_be32(p + 28) == 4096);
    assert(get_be64(p + 32) == UINT64_C(0x1122334455667788));
    assert(get_be64(p + 40) == UINT64_C(0x1020304050607080));
    assert(get_be64(p + 48) == UINT64_C(0x0000000000000800));
    assert(get_be64(p + 56) == UINT64_C(0x00000000004007ff));
    for (unsigned i = 0; i < 16; ++i) assert(p[64 + i] == (unsigned char)i);
    assert(get_be32(p + 80) == 0x0a0b0c0d && get_be32(p + 84) == 0xf0e0d0c0);
    for (unsigned i = 0; i < 32; ++i) assert(p[88 + i] == (unsigned char)(0x20 + i));
    assert(get_be32(p + 120) == 1 && get_be32(p + 124) == 2);
    assert(get_be32(p + 128) == 0 && get_be32(p + 132) == 0);
    assert(get_be32(p + 136) == 5 && get_be32(p + 140) == 3);
    for (unsigned i = 0; i < 16; ++i) assert(p[144 + i] == (unsigned char)(0x40 + i));
    assert(get_be32(p + 160) == 0x0f);
    unrelated_unchanged();
    /* Replacing a signed failure with success must not leave old bits behind. */
    report.status = KSHIM_STORAGE_OK;
    report.release_status = KSHIM_STORAGE_OK;
    report.debug_valid = 0;
    assert(storage_ufs_read_export(blob.bytes, &report) == 0);
    p = property(&length);
    assert(p && length == 164 && get_be32(p + 12) == 0 && get_be32(p + 160) == 0);
    unrelated_unchanged();
}
static void test_invalid_unchanged(void)
{
    fixture(1);
    fill_report();
    assert(storage_ufs_read_export(blob.bytes, &report) == 0);
    before = blob;
    assert(storage_ufs_read_export(NULL, &report) < 0);
    assert(storage_ufs_read_export(blob.bytes, NULL) < 0);
    assert(memcmp(&blob, &before, sizeof(blob)) == 0);
    report.version = 2; /* foreign or stale report: rejected before mutation */
    assert(storage_ufs_read_export(blob.bytes, &report) == -FDT_ERR_BADVALUE);
    assert(memcmp(&blob, &before, sizeof(blob)) == 0);
    int length;
    const unsigned char *p = property(&length);
    assert(p && length == 164 && get_be32(p + 0) == 1);
    fixture(0);
    before = blob;
    assert(storage_ufs_read_export(blob.bytes, &report) == -FDT_ERR_NOTFOUND);
    assert(memcmp(&blob, &before, sizeof(blob)) == 0);
    fixture(1);
    blob.bytes[0] = 0;
    before = blob;
    assert(storage_ufs_read_export(blob.bytes, &report) == -FDT_ERR_BADMAGIC);
    assert(memcmp(&blob, &before, sizeof(blob)) == 0);
}
static void test_no_space_preserves_previous(void)
{
    /* A shorter older property cannot grow in a packed tree, so the failed
     * insertion must leave that previous value and the tree untouched. */
    fixture(1);
    static const unsigned char older[2] = {'a', 'b'};
    assert(fdt_setprop(blob.bytes, chosen(), KSHIM_UFS_READ_FDT_PROPERTY,
                       older, 2) == 0);
    assert(fdt_pack(blob.bytes) == 0);
    int packed = fdt_totalsize(blob.bytes);
    assert(fdt_open_into(blob.bytes, blob.bytes, packed) == 0);
    before = blob;
    fill_report();
    assert(storage_ufs_read_export(blob.bytes, &report) == -FDT_ERR_NOSPACE);
    assert(memcmp(&blob, &before, sizeof(blob)) == 0);
    int length;
    const unsigned char *p = property(&length);
    assert(p && length == 2 && p[0] == 'a' && p[1] == 'b');
    assert(fdt_totalsize(blob.bytes) == (unsigned)packed);
    unrelated_unchanged();
}
/* Distinct high and low bytes per field, so a swapped pair, an off-by-four
 * shift or a little-endian copy changes at least one assertion below. */
static void fill_link(struct kshim_ufs_link_state *link, uint32_t seed,
                      uint8_t uncertain, uint8_t restore)
{
    link->initial_tx = 0x11000000u + seed;
    link->current_tx = 0x22000000u + seed;
    link->last_command = 0x33000000u + seed;
    link->command_result = 0x44000000u + seed;
    link->last_is = 0x55000000u + seed;
    link->power_status = 0x66000000u + seed;
    link->phase = 0x77000000u + seed;
    link->uncertain = uncertain;
    link->restore_hibern8 = restore;
}
/* The auxiliary record is separate, so the read property must survive both its
 * first insertion and its replacement byte for byte. */
static void read_property_unchanged(const unsigned char *snapshot)
{
    int length;
    const unsigned char *p = property(&length);
    assert(p && length == 164 && memcmp(p, snapshot, 164) == 0);
}
static void test_link_exact_wire_layout(void)
{
    /* 76 bytes: version, then nine u32 for the before state at 4 and nine for
     * the after state at 40. Neither narrowing uncertain/restore_hibern8 to
     * one byte nor restoring C padding keeps this size or these offsets. */
    fixture(1);
    fill_report();
    assert(storage_ufs_read_export(blob.bytes, &report) == 0);
    int read_length;
    const unsigned char *read = property(&read_length);
    assert(read && read_length == 164);
    unsigned char read_snapshot[164];
    memcpy(read_snapshot, read, sizeof(read_snapshot));
    int total = fdt_totalsize(blob.bytes);

    fill_link(&report.link_before_release, 0x10u, 1, 1);
    fill_link(&report.link_after_release, 0x20u, 0, 1);
    assert(KSHIM_UFS_LINK_EXPORT_BYTES == 76U);
    assert(storage_ufs_link_export(blob.bytes, &report) == 0);
    int length;
    const unsigned char *p = link_property(&length);
    assert(p && length == 76);
    assert(fdt_totalsize(blob.bytes) == (unsigned)total);
    assert(get_be32(p + 0) == KSHIM_UFS_LINK_EXPORT_VERSION);
    assert(get_be32(p + 4) == report.link_before_release.initial_tx);
    assert(get_be32(p + 8) == report.link_before_release.current_tx);
    assert(get_be32(p + 12) == report.link_before_release.last_command);
    assert(get_be32(p + 16) == report.link_before_release.command_result);
    assert(get_be32(p + 20) == report.link_before_release.last_is);
    assert(get_be32(p + 24) == report.link_before_release.power_status);
    assert(get_be32(p + 28) == report.link_before_release.phase);
    assert(get_be32(p + 32) == 1); /* uncertain widened from uint8 */
    assert(get_be32(p + 36) == 1); /* restore_hibern8 widened from uint8 */
    assert(get_be32(p + 40) == report.link_after_release.initial_tx);
    assert(get_be32(p + 44) == report.link_after_release.current_tx);
    assert(get_be32(p + 48) == report.link_after_release.last_command);
    assert(get_be32(p + 52) == report.link_after_release.command_result);
    assert(get_be32(p + 56) == report.link_after_release.last_is);
    assert(get_be32(p + 60) == report.link_after_release.power_status);
    assert(get_be32(p + 64) == report.link_after_release.phase);
    assert(get_be32(p + 68) == 0);
    assert(get_be32(p + 72) == 1);
    /* The two states must not be interchangeable: swapping them fails here. */
    assert(get_be32(p + 4) != get_be32(p + 40));
    read_property_unchanged(read_snapshot);
    unrelated_unchanged();

    /* Replacing the auxiliary record must not disturb the read property, and
     * no byte of the previous auxiliary value may survive the rewrite. */
    fill_link(&report.link_before_release, 0x30u, 0, 0);
    fill_link(&report.link_after_release, 0x40u, 1, 0);
    assert(storage_ufs_link_export(blob.bytes, &report) == 0);
    p = link_property(&length);
    assert(p && length == 76 && get_be32(p + 0) == KSHIM_UFS_LINK_EXPORT_VERSION);
    assert(get_be32(p + 4) == report.link_before_release.initial_tx);
    assert(get_be32(p + 32) == 0 && get_be32(p + 36) == 0);
    assert(get_be32(p + 40) == report.link_after_release.initial_tx);
    assert(get_be32(p + 68) == 1 && get_be32(p + 72) == 0);
    read_property_unchanged(read_snapshot);
    unrelated_unchanged();
}
static void test_link_invalid_unchanged(void)
{
    fixture(1);
    fill_report();
    fill_link(&report.link_before_release, 0x10u, 1, 1);
    fill_link(&report.link_after_release, 0x20u, 0, 1);
    assert(storage_ufs_read_export(blob.bytes, &report) == 0);
    assert(storage_ufs_link_export(blob.bytes, &report) == 0);
    int read_length;
    const unsigned char *read = property(&read_length);
    assert(read && read_length == 164);
    unsigned char read_snapshot[164];
    memcpy(read_snapshot, read, sizeof(read_snapshot));
    before = blob;
    assert(storage_ufs_link_export(NULL, &report) < 0);
    assert(storage_ufs_link_export(blob.bytes, NULL) < 0);
    assert(memcmp(&blob, &before, sizeof(blob)) == 0);
    report.version = 2; /* rejected through the read version, before mutation */
    assert(storage_ufs_link_export(blob.bytes, &report) == -FDT_ERR_BADVALUE);
    assert(memcmp(&blob, &before, sizeof(blob)) == 0);
    int length;
    const unsigned char *p = link_property(&length);
    assert(p && length == 76 && get_be32(p + 0) == KSHIM_UFS_LINK_EXPORT_VERSION);
    read_property_unchanged(read_snapshot);
    fixture(0);
    before = blob;
    assert(storage_ufs_link_export(blob.bytes, &report) == -FDT_ERR_NOTFOUND);
    assert(memcmp(&blob, &before, sizeof(blob)) == 0);
    fixture(1);
    blob.bytes[0] = 0;
    before = blob;
    assert(storage_ufs_link_export(blob.bytes, &report) == -FDT_ERR_BADMAGIC);
    assert(memcmp(&blob, &before, sizeof(blob)) == 0);
}
static void test_link_no_space_preserves_previous(void)
{
    /* Same packed-tree argument as the read exporter's NOSPACE case: the older
     * two-byte value must survive, and the read property must stay identical
     * whether the auxiliary insertion happened or not. */
    fixture(1);
    fill_report();
    assert(storage_ufs_read_export(blob.bytes, &report) == 0);
    int read_length;
    const unsigned char *read = property(&read_length);
    assert(read && read_length == 164);
    unsigned char read_snapshot[164];
    memcpy(read_snapshot, read, sizeof(read_snapshot));
    static const unsigned char older[2] = {'a', 'b'};
    assert(fdt_setprop(blob.bytes, chosen(), KSHIM_UFS_LINK_FDT_PROPERTY,
                       older, 2) == 0);
    assert(fdt_pack(blob.bytes) == 0);
    int packed = fdt_totalsize(blob.bytes);
    assert(fdt_open_into(blob.bytes, blob.bytes, packed) == 0);
    before = blob;
    fill_link(&report.link_before_release, 0x10u, 1, 1);
    fill_link(&report.link_after_release, 0x20u, 0, 1);
    assert(storage_ufs_link_export(blob.bytes, &report) == -FDT_ERR_NOSPACE);
    assert(memcmp(&blob, &before, sizeof(blob)) == 0);
    int length;
    const unsigned char *p = link_property(&length);
    assert(p && length == 2 && p[0] == 'a' && p[1] == 'b');
    assert(fdt_totalsize(blob.bytes) == (unsigned)packed);
    read_property_unchanged(read_snapshot);
    unrelated_unchanged();
}
static void test_power_export(void)
{
    fixture(1);fill_report();
    report.power_before_release=(struct kshim_ufs_power_state){0x22,0x11,3,0,2};
    report.power_after_release=(struct kshim_ufs_power_state){0x22,0x22,7,0,0};
    report.scsi_before_release=(struct kshim_ufs_scsi_state){0x88,0,2,2,4,2,3};
    report.scsi_after_release=(struct kshim_ufs_scsi_state){0x1b,0xd0,0,0,0,0,0};
    assert(storage_ufs_read_export(blob.bytes,&report)==0);
    assert(storage_ufs_link_export(blob.bytes,&report)==0);
    unsigned char read_copy[164],link_copy[76];int n;
    memcpy(read_copy,property(&n),164);
    memcpy(link_copy,link_property(&n),76);
    assert(storage_ufs_power_export(blob.bytes,&report)==0);
    const unsigned char *p=fdt_getprop(blob.bytes,chosen(),KSHIM_UFS_POWER_FDT_PROPERTY,&n);
    const uint32_t want[25]={1,0x22,0x11,3,0,2,0x22,0x22,7,0,0,0x88,0,2,2,4,2,3,0x1b,0xd0,0,0,0,0,0};
    assert(p && n==100);
    for(unsigned i=0;i<25;++i)assert(get_be32(p+4*i)==want[i]);
    read_property_unchanged(read_copy);
    assert(memcmp(link_property(&n),link_copy,76)==0);
    before=blob;
    assert(storage_ufs_power_export(NULL,&report)<0);
    assert(storage_ufs_power_export(blob.bytes,NULL)<0);
    report.version=2;
    assert(storage_ufs_power_export(blob.bytes,&report)==-FDT_ERR_BADVALUE);
    assert(memcmp(&blob,&before,sizeof(blob))==0);
    fixture(0);fill_report();before=blob;
    assert(storage_ufs_power_export(blob.bytes,&report)==-FDT_ERR_NOTFOUND);
    assert(memcmp(&blob,&before,sizeof(blob))==0);
    fixture(1);fill_report();
    assert(fdt_setprop(blob.bytes,chosen(),KSHIM_UFS_POWER_FDT_PROPERTY,"xy",2)==0);
    assert(fdt_pack(blob.bytes)==0);before=blob;
    assert(storage_ufs_power_export(blob.bytes,&report)==-FDT_ERR_NOSPACE);
    assert(memcmp(&blob,&before,sizeof(blob))==0);
}
int main(void)
{
    test_power_export();
    test_exact_wire_layout();
    test_invalid_unchanged();
    test_no_space_preserves_previous();
    test_link_exact_wire_layout();
    test_link_invalid_unchanged();
    test_link_no_space_preserves_previous();
    puts("PASS storage_ufs_read_export: 164-byte BE layout, signed codes, raw UUID/prefix bytes, 164-byte property, NULL/version/NOSPACE keep the old property");
    puts("PASS storage_ufs_link_export: 76-byte BE version+before/after link states, widened uint8 flags, separate property, read property byte-identical, NULL/version/BADMAGIC/NOTFOUND/NOSPACE keep previous values");
    return 0;
}
