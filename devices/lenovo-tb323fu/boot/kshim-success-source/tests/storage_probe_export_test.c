/* SPDX-License-Identifier: GPL-3.0-only */
#include "storage_probe_export.h"
#include <assert.h>
#include <libfdt.h>
#include <stdio.h>
#include <string.h>

static union { uint64_t align; unsigned char bytes[16384]; } blob, before;
static struct storage_probe_snapshot snapshot;
static int chosen(void) { return fdt_path_offset(blob.bytes, "/chosen"); }
static const unsigned char *report(int *length)
{
    return fdt_getprop(blob.bytes, chosen(), STORAGE_PROBE_FDT_PROPERTY, length);
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
    memset(&snapshot, 0, sizeof(snapshot));
    snapshot.version = 1;
    snapshot.board = DEVICE_BOARD_TB323FU;
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
static void test_wire_fields_and_partial_evidence(void)
{
    /* Reordering words, little-endian copies, truncating addresses/error bits,
     * discarding failed snapshots or exporting struct padding must fail. */
    fixture(1);
    snapshot.valid = 0x01020304;
    snapshot.status = STORAGE_PROBE_READ_FAILED;
    snapshot.entry_count = 2;
    snapshot.read_attempts = 0x10203040;
    snapshot.stream_count = 0x11223344;
    snapshot.streams_scanned = 0x55667788;
    snapshot.matching_streams = 0x12345678;
    snapshot.contexts_captured = 0x23456789;
    snapshot.read_error = -23;
    snapshot.failed_address = UINT64_C(0x1023456789abcdef);
    snapshot.entries[0].address = UINT64_C(0x1122334455667788);
    snapshot.entries[0].value = 0x99aabbcc;
    snapshot.entries[1].address = UINT64_C(0xfedcba9876543210);
    snapshot.entries[1].value = 0x10203040;
    snapshot.entries[2].address = UINT64_MAX; /* outside the exported prefix */
    snapshot.entries[2].value = UINT32_MAX;
    static const unsigned char expected[] = {
        0,0,0,1, 1,2,3,4, 0,0,0,2, 0,0,0,4, 0,0,0,2,
        0x10,0x20,0x30,0x40, 0x11,0x22,0x33,0x44,
        0x55,0x66,0x77,0x88, 0x12,0x34,0x56,0x78,
        0x23,0x45,0x67,0x89, 0xff,0xff,0xff,0xe9,
        0x10,0x23,0x45,0x67, 0x89,0xab,0xcd,0xef,
        0x11,0x22,0x33,0x44, 0x55,0x66,0x77,0x88, 0x99,0xaa,0xbb,0xcc,
        0xfe,0xdc,0xba,0x98, 0x76,0x54,0x32,0x10, 0x10,0x20,0x30,0x40
    };
    int total = fdt_totalsize(blob.bytes), length;
    assert(storage_probe_export(blob.bytes, &snapshot) == 0);
    const unsigned char *p = report(&length);
    assert(p && length == 76 && sizeof(expected) == 76 && memcmp(p, expected, sizeof(expected)) == 0);
    assert(fdt_totalsize(blob.bytes) == (unsigned)total);
    unrelated_unchanged();
    /* Replacing a longer report must not retain old entries as trailing data. */
    snapshot.entry_count = 0; snapshot.board = DEVICE_BOARD_UNKNOWN;
    snapshot.status = STORAGE_PROBE_BOARD_UNSUPPORTED; snapshot.valid = 0;
    assert(storage_probe_export(blob.bytes, &snapshot) == 0);
    p = report(&length); assert(p && length == 52 && p[11] == 0 && p[15] == 2 && p[19] == 0);
    unrelated_unchanged();
}
static void test_entry_bound(void)
{
    fixture(1); snapshot.entry_count = 256;
    for (unsigned i = 0; i < 256; ++i) {
        snapshot.entries[i].address = UINT64_C(0x1020304000000000) + i;
        snapshot.entries[i].value = 0x12340000 + i;
    }
    assert(storage_probe_export(blob.bytes, &snapshot) == 0);
    int length; const unsigned char *p = report(&length);
    assert(p && length == 3124);
    static const unsigned char last[] = {0x10,0x20,0x30,0x40,0,0,0,0xff,0x12,0x34,0,0xff};
    assert(memcmp(p + 52 + 255*12, last, sizeof(last)) == 0);
    unrelated_unchanged();
}
static void test_invalid_unchanged(void)
{
    fixture(1); before = blob;
    assert(storage_probe_export(NULL, &snapshot) < 0);
    assert(storage_probe_export(blob.bytes, NULL) < 0);
    assert(memcmp(&blob, &before, sizeof(blob)) == 0);
    snapshot.entry_count = 257;
    assert(storage_probe_export(blob.bytes, &snapshot) < 0);
    assert(memcmp(&blob, &before, sizeof(blob)) == 0);
    fixture(0); before = blob;
    assert(storage_probe_export(blob.bytes, &snapshot) == -FDT_ERR_NOTFOUND);
    assert(memcmp(&blob, &before, sizeof(blob)) == 0);
    fixture(1); blob.bytes[0] = 0; before = blob;
    assert(storage_probe_export(blob.bytes, &snapshot) == -FDT_ERR_BADMAGIC);
    assert(memcmp(&blob, &before, sizeof(blob)) == 0);
}
static void test_no_space_is_not_a_partial_report(void)
{
    /* Enough space for a name but not its property exercises libfdt rollback;
     * a full tree also tests that replacing an existing report is atomic. */
    fixture(1); assert(fdt_pack(blob.bytes) == 0);
    int small_size = fdt_totalsize(blob.bytes) + 32;
    assert(fdt_open_into(blob.bytes, blob.bytes, small_size) == 0);
    unsigned strings_before = fdt_size_dt_strings(blob.bytes);
    assert(storage_probe_export(blob.bytes, &snapshot) == -FDT_ERR_NOSPACE);
    int length; assert(report(&length) == NULL && length == -FDT_ERR_NOTFOUND);
    assert(fdt_size_dt_strings(blob.bytes) == strings_before);
    assert(fdt_totalsize(blob.bytes) == (unsigned)small_size); unrelated_unchanged();
    fixture(1); snapshot.status = STORAGE_PROBE_BOARD_UNSUPPORTED;
    assert(storage_probe_export(blob.bytes, &snapshot) == 0);
    assert(fdt_pack(blob.bytes) == 0); before = blob;
    snapshot.entry_count = 256;
    assert(storage_probe_export(blob.bytes, &snapshot) == -FDT_ERR_NOSPACE);
    assert(memcmp(&blob, &before, sizeof(blob)) == 0);
    const unsigned char *p = report(&length);
    assert(p && length == 52 && p[15] == 2 && p[19] == 0); unrelated_unchanged();
}
int main(void)
{
    test_wire_fields_and_partial_evidence(); test_entry_bound();
    test_invalid_unchanged(); test_no_space_is_not_a_partial_report();
    puts("PASS storage_probe_export: 52-byte BE header, partial/unknown snapshots, 256-entry bound, atomic no-space failures, unrelated FDT properties");
    return 0;
}
