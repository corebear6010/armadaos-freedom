/* SPDX-License-Identifier: GPL-3.0-only */
#include <storage_probe.h>

#include <assert.h>
#include <stdio.h>
#include <string.h>

#define UFS UINT64_C(0x01d84000)
#define SMMU UINT64_C(0x15000000)
#define FIXTURE_CAPACITY 1024U

struct reg_value { uint64_t address; uint32_t value; };
struct fixture {
    struct reg_value regs[FIXTURE_CAPACITY];
    uint64_t reads[FIXTURE_CAPACITY];
    size_t count;
    size_t read_count;
    uint64_t fail_address;
};

/* Hardware accesses are the only substitute. The collector, address bounds,
 * capability decoding, matching, deduplication and evidence format are real.
 * These independent addresses describe the allowed HCI snapshot contract. */
static const uint32_t hci_offsets[] = {
    0x00U, 0x04U, 0x08U, 0x0cU, 0x18U, 0x20U, 0x24U,
    0x30U, 0x34U, 0x38U, 0x3cU, 0x40U, 0x44U, 0x48U, 0x4cU,
    0x50U, 0x54U, 0x58U, 0x60U, 0x70U, 0x74U, 0x78U, 0x80U,
    0x90U, 0x94U, 0x98U, 0x9cU, 0xd8U, 0xdcU, 0xe0U,
    0xe4U, 0xfcU, 0x300U, 0x284cU,
};

static void set_reg(struct fixture *f, uint64_t address, uint32_t value)
{
    for (size_t i = 0U; i < f->count; ++i) {
        if (f->regs[i].address == address) {
            f->regs[i].value = value;
            return;
        }
    }
    assert(f->count < FIXTURE_CAPACITY);
    f->regs[f->count++] = (struct reg_value){address, value};
}

static int fixture_read(void *ctx, uint64_t address, uint32_t *value)
{
    struct fixture *f = ctx;
    assert(f->read_count < FIXTURE_CAPACITY);
    f->reads[f->read_count++] = address;
    if (address == f->fail_address) {
        *value = 0xdeadbeefU; /* Failure must never export this poisoned value. */
        return -71;
    }
    for (size_t i = 0U; i < f->count; ++i) {
        if (f->regs[i].address == address) {
            *value = f->regs[i].value;
            return 0;
        }
    }
    fprintf(stderr, "Unexpected register read: 0x%llx\n",
            (unsigned long long)address);
    return -99;
}

static void init_fixture(struct fixture *f, uint32_t streams)
{
    memset(f, 0, sizeof(*f));
    for (size_t i = 0U; i < sizeof(hci_offsets) / sizeof(hci_offsets[0]); ++i)
        set_reg(f, UFS + hci_offsets[i], 0xa5000000U + hci_offsets[i]);
    set_reg(f, UFS, 0x4000001fU); /* MCQ capable, Legacy supported. */
    set_reg(f, UFS + 0x08U, 0x00000400U);
    set_reg(f, UFS + 0x18U, 0U);
    set_reg(f, UFS + 0x30U, 0x0fU);
    set_reg(f, UFS + 0x34U, 1U);
    set_reg(f, UFS + 0x58U, 0U);
    set_reg(f, UFS + 0x78U, 0U);
    set_reg(f, UFS + 0x300U, 0U);
    set_reg(f, SMMU, 0U);
    set_reg(f, SMMU + 0x20U, 0x48000000U | streams);
    set_reg(f, SMMU + 0x24U, 0x10000004U); /* 4K MMIO pages, four CBs. */
    set_reg(f, SMMU + 0x28U, 0x1000U);
    for (uint32_t i = 0U; i < streams; ++i)
        set_reg(f, SMMU + 0x800U + (uint64_t)i * 4U, 0U);
}

static void set_stream(struct fixture *f, uint32_t index, uint32_t smr,
                       uint32_t s2cr)
{
    set_reg(f, SMMU + 0x800U + (uint64_t)index * 4U, smr);
    set_reg(f, SMMU + 0xc00U + (uint64_t)index * 4U, s2cr);
}

static void set_context(struct fixture *f, uint32_t bank, uint64_t page_size,
                        uint64_t context_base)
{
    static const uint32_t offsets[] = {
        0x00U, 0x10U, 0x20U, 0x24U, 0x28U, 0x2cU, 0x30U,
        0x38U, 0x3cU, 0x58U, 0x60U, 0x64U, 0x68U,
    };
    uint64_t base = context_base + (uint64_t)bank * page_size;
    set_reg(f, SMMU + page_size + (uint64_t)bank * 4U, 0x0001f300U);
    set_reg(f, SMMU + page_size + 0x800U + (uint64_t)bank * 4U, 1U);
    for (size_t i = 0U; i < sizeof(offsets) / sizeof(offsets[0]); ++i)
        set_reg(f, base + offsets[i], 0x12340000U + offsets[i]);
}

static const struct storage_probe_entry *find_entry(
    const struct storage_probe_snapshot *s, uint64_t address)
{
    for (uint32_t i = 0U; i < s->entry_count; ++i)
        if (s->entries[i].address == address)
            return &s->entries[i];
    return NULL;
}

static size_t reads_at(const struct fixture *f, uint64_t address)
{
    size_t count = 0U;
    for (size_t i = 0U; i < f->read_count; ++i)
        if (f->reads[i] == address)
            ++count;
    return count;
}

static void check_value(const struct storage_probe_snapshot *s,
                        uint64_t address, uint32_t expected)
{
    const struct storage_probe_entry *entry = find_entry(s, address);
    assert(entry != NULL);
    assert(entry->value == expected);
}

/* Catches treating a boot-menu override/related SoC as hardware identity. */
static void test_board_gate(void)
{
    const enum device_board boards[] = {
        DEVICE_BOARD_UNKNOWN, (enum device_board)99,
    };
    for (size_t i = 0U; i < sizeof(boards) / sizeof(boards[0]); ++i) {
        struct fixture f;
        struct storage_probe_snapshot s;
        init_fixture(&f, 1U);
        memset(&s, 0x5a, sizeof(s));
        assert(storage_probe_collect(boards[i], fixture_read, &f, &s) ==
               STORAGE_PROBE_BOARD_UNSUPPORTED);
        assert(s.version == STORAGE_PROBE_VERSION && s.board == boards[i]);
        assert(s.status == STORAGE_PROBE_BOARD_UNSUPPORTED);
        assert(s.entry_count == 0U && s.read_attempts == 0U && s.valid == 0U);
        assert(f.read_count == 0U);
    }
}

/* Catches invalid callback/output use before the first MMIO. */
static void test_gen4_profile(void)
{
    struct fixture f;
    struct storage_probe_snapshot s;
    init_fixture(&f, 1U);
    set_stream(&f, 0U, 0x80000060U, 0x00010000U);
    assert(storage_probe_collect(DEVICE_BOARD_TB322FC, fixture_read, &f, &s) ==
           STORAGE_PROBE_OK);
    assert(s.board == DEVICE_BOARD_TB322FC && s.matching_streams == 1U);
    check_value(&s, UFS, 0x4000001fU);
    check_value(&s, SMMU + 0xc00U, 0x00010000U);
}

/* Gen4 entry snapshot: ID0=0x4c017e84 reports 132 SMRs, independently
 * of ID1's 83 context banks. NUMSMRG is an eight-bit field, not a 128 limit. */
static void test_gen4_live_smmu_geometry(void)
{
    const uint32_t counts[] = {132U, 255U};
    for (size_t n = 0; n < sizeof(counts) / sizeof(counts[0]); ++n) {
        struct fixture f;
        struct storage_probe_snapshot s;
        init_fixture(&f, counts[n]);
        set_reg(&f, SMMU, 0x002f0406U);
        set_reg(&f, SMMU + 0x20U, 0x4c017e00U | counts[n]);
        set_reg(&f, SMMU + 0x24U, 0x60000053U);
        set_reg(&f, SMMU + 0x28U, 0x00005111U);
        set_stream(&f, counts[n] - 1U, 0x80000060U, 82U);
        set_context(&f, 82U, 0x1000U, SMMU + 0x80000U);
        assert(storage_probe_collect(DEVICE_BOARD_TB322FC, fixture_read, &f, &s) == STORAGE_PROBE_OK);
        assert(s.stream_count == counts[n] && s.streams_scanned == counts[n]);
        assert(s.matching_streams == 1U && s.contexts_captured == 1U);
        check_value(&s, SMMU + 0xd2020U, 0x12340020U);
        assert(reads_at(&f, SMMU + 0xc00U + (counts[n] - 1U) * 4U) == 1U);
        for (size_t i = 0; i < f.read_count; ++i)
            assert(f.reads[i] < SMMU + 0x100000U);
    }
}

static void test_arguments(void)
{
    struct fixture f;
    struct storage_probe_snapshot s;
    init_fixture(&f, 1U);
    assert(storage_probe_collect(DEVICE_BOARD_TB323FU, NULL, &f, &s) ==
           STORAGE_PROBE_INVALID_ARGUMENT);
    assert(s.entry_count == 0U && f.read_count == 0U);
    assert(storage_probe_collect(DEVICE_BOARD_TB323FU, fixture_read, &f, NULL) ==
           STORAGE_PROBE_INVALID_ARGUMENT);
    assert(f.read_count == 0U);
}

/* Catches losing zero values, normalizing queue mode, or guessing idle state. */
static void test_hci_evidence(void)
{
    for (uint32_t mode = 0U; mode <= 1U; ++mode) {
        struct fixture f;
        struct storage_probe_snapshot s;
        struct reg_value before[FIXTURE_CAPACITY];
        init_fixture(&f, 2U);
        set_reg(&f, UFS + 0x300U, mode);
        set_reg(&f, UFS + 0x58U, 0x20U); /* A busy controller is still evidence. */
        memcpy(before, f.regs, sizeof(before));
        assert(storage_probe_collect(DEVICE_BOARD_TB323FU, fixture_read, &f, &s) ==
               STORAGE_PROBE_OK);
        assert(s.status == STORAGE_PROBE_OK);
        assert(s.valid == (STORAGE_PROBE_VALID_UFS | STORAGE_PROBE_VALID_SMMU_IDS |
                           STORAGE_PROBE_VALID_SMMU_LAYOUT));
        check_value(&s, UFS, 0x4000001fU);
        check_value(&s, UFS + 0x300U, mode);
        check_value(&s, UFS + 0x18U, 0U);
        check_value(&s, UFS + 0x58U, 0x20U);
        check_value(&s, UFS + 0xe4U, 0xa50000e4U);
        check_value(&s, UFS + 0x284cU, 0xa500284cU);
        assert(s.streams_scanned == 2U && s.matching_streams == 0U);
        assert(s.contexts_captured == 0U);
        assert(s.entry_count == 38U && s.read_attempts == 40U);
        assert(memcmp(before, f.regs, sizeof(before)) == 0);
    }
}

/* Catches exact-only SID matching, duplicate CB reads and unrelated CB access. */
static void test_masked_multiple_matches(void)
{
    struct fixture f;
    struct storage_probe_snapshot s;
    init_fixture(&f, 5U);
    set_stream(&f, 0U, 0x80000070U, 2U); /* Other SID; no CB2 data provided. */
    set_stream(&f, 1U, 0x80000060U, 0x00010000U); /* Exact bypass. */
    set_stream(&f, 2U, 0x80030063U, 1U); /* Mask low two SID bits. */
    set_stream(&f, 3U, 0x80000060U, 1U); /* Same translated bank. */
    set_reg(&f, SMMU + 0x810U, 0x00000060U); /* Invalid SMR is not a match. */
    set_context(&f, 1U, 0x1000U, SMMU + 0x4000U);
    assert(storage_probe_collect(DEVICE_BOARD_TB323FU, fixture_read, &f, &s) ==
           STORAGE_PROBE_OK);
    assert(s.streams_scanned == 5U && s.matching_streams == 3U);
    assert(s.contexts_captured == 1U);
    check_value(&s, SMMU + 0x808U, 0x80030063U);
    check_value(&s, SMMU + 0xc08U, 1U);
    check_value(&s, SMMU + 0x5020U, 0x12340020U);
    assert(find_entry(&s, SMMU + 0x800U) == NULL);
    assert(reads_at(&f, SMMU + 0xc00U) == 0U);
    assert(reads_at(&f, SMMU + 0x6000U) == 0U);
    assert(reads_at(&f, SMMU + 0x5020U) == 1U);
}

/* Catches reading translated-bank registers for bypass/fault domains. */
static void test_disabled_and_nontranslated(void)
{
    struct fixture f;
    struct storage_probe_snapshot s;
    init_fixture(&f, 2U);
    set_reg(&f, SMMU, 1U); /* Disabled; collection must not initialize it. */
    set_stream(&f, 0U, 0x80000060U, 0x000100ffU);
    set_stream(&f, 1U, 0x80000060U, 0x000200ffU);
    assert(storage_probe_collect(DEVICE_BOARD_TB323FU, fixture_read, &f, &s) ==
           STORAGE_PROBE_OK);
    check_value(&s, SMMU, 1U);
    assert(s.contexts_captured == 0U && s.matching_streams == 2U);
    assert(s.entry_count == 42U);
}

/* Catches decoding unsupported SID/register formats after preserving raw IDs. */
static void test_unsupported_capabilities(void)
{
    const uint32_t cr0[] = {8U, 0U, 0U, 0U};
    const uint32_t id0[] = {0x48000001U, 0x40000001U, 0x08000001U, 0x48000001U};
    const uint32_t id2[] = {0x1000U, 0x1000U, 0x1000U, 0U};
    for (size_t i = 0U; i < 4U; ++i) {
        struct fixture f;
        struct storage_probe_snapshot s;
        init_fixture(&f, 1U);
        set_reg(&f, SMMU, cr0[i]);
        set_reg(&f, SMMU + 0x20U, id0[i]);
        set_reg(&f, SMMU + 0x28U, id2[i]);
        assert(storage_probe_collect(DEVICE_BOARD_TB323FU, fixture_read, &f, &s) ==
               STORAGE_PROBE_SMMU_UNSUPPORTED);
        check_value(&s, SMMU, cr0[i]);
        check_value(&s, SMMU + 0x20U, id0[i]);
        assert((s.valid & STORAGE_PROBE_VALID_SMMU_IDS) != 0U);
        assert((s.valid & STORAGE_PROBE_VALID_SMMU_LAYOUT) == 0U);
        assert(s.streams_scanned == 0U && reads_at(&f, SMMU + 0x800U) == 0U);
    }
}

/* Catches inventing a valid zero for a failed read or continuing after error. */
static void test_read_failure(void)
{
    const uint64_t failures[] = {UFS + 0x20U, SMMU + 0x24U, SMMU + 0x5024U};
    for (size_t i = 0U; i < 3U; ++i) {
        struct fixture f;
        struct storage_probe_snapshot s;
        init_fixture(&f, 2U);
        set_stream(&f, 0U, 0x80000060U, 1U);
        set_context(&f, 1U, 0x1000U, SMMU + 0x4000U);
        f.fail_address = failures[i];
        assert(storage_probe_collect(DEVICE_BOARD_TB323FU, fixture_read, &f, &s) ==
               STORAGE_PROBE_READ_FAILED);
        assert(s.failed_address == failures[i] && s.read_error == -71);
        assert(f.reads[f.read_count - 1U] == failures[i]);
        assert(s.read_attempts == f.read_count);
        assert(find_entry(&s, failures[i]) == NULL);
        assert(s.entry_count > 0U && s.contexts_captured == 0U);
        if (i == 2U)
            check_value(&s, SMMU + 0x5020U, 0x12340020U);
    }
}

/* Catches trusting oversized hardware counts or an out-of-range CB index. */
static void test_bounds(void)
{
    const uint32_t streams[] = {1U, 1U, 0U, 1U};
    const uint32_t ids[] = {0x10000005U, 0xf0000004U, 0x10000004U, 0x10050004U};
    for (size_t i = 0U; i < 4U; ++i) {
        struct fixture f;
        struct storage_probe_snapshot s;
        init_fixture(&f, streams[i]);
        set_reg(&f, SMMU + 0x24U, ids[i]);
        assert(storage_probe_collect(DEVICE_BOARD_TB323FU, fixture_read, &f, &s) ==
               STORAGE_PROBE_BOUNDS);
        assert(s.entry_count == 38U && s.streams_scanned == 0U);
    }
    {
        struct fixture f;
        struct storage_probe_snapshot s;
        init_fixture(&f, 1U);
        set_stream(&f, 0U, 0x80000060U, 4U);
        assert(storage_probe_collect(DEVICE_BOARD_TB323FU, fixture_read, &f, &s) ==
               STORAGE_PROBE_BOUNDS);
        assert(s.contexts_captured == 0U && s.entry_count == 40U);
        assert(reads_at(&f, SMMU + 0x8000U) == 0U);
    }
}

/* Catches a fixed 4K MMIO stride or context base unrelated to ID1 geometry. */
static void test_large_mmio_pages(void)
{
    struct fixture f;
    struct storage_probe_snapshot s;
    init_fixture(&f, 1U);
    set_reg(&f, SMMU + 0x24U, 0x80000002U);
    set_stream(&f, 0U, 0x80000060U, 1U);
    set_context(&f, 1U, 0x10000U, SMMU + 0x20000U);
    assert(storage_probe_collect(DEVICE_BOARD_TB323FU, fixture_read, &f, &s) ==
           STORAGE_PROBE_OK);
    check_value(&s, SMMU + 0x30020U, 0x12340020U);
    assert(s.contexts_captured == 1U);
    for (size_t i = 0U; i < f.read_count; ++i)
        assert(f.reads[i] < SMMU + 0x100000U);
}

/* Catches writing beyond the fixed export, or continuing MMIO when full. */
static void test_record_limit(void)
{
    struct fixture f;
    struct storage_probe_snapshot s;
    init_fixture(&f, 128U);
    for (uint32_t i = 0U; i < 128U; ++i)
        set_stream(&f, i, 0x80000060U, 0x00010000U);
    assert(storage_probe_collect(DEVICE_BOARD_TB323FU, fixture_read, &f, &s) ==
           STORAGE_PROBE_FULL);
    assert(s.entry_count == STORAGE_PROBE_MAX_ENTRIES);
    assert(s.read_attempts == STORAGE_PROBE_MAX_ENTRIES);
    assert(s.streams_scanned < 128U && s.read_error == 0);
    assert(s.failed_address != 0U);
}

/* Catches exporting a reserved S2CR type as a known translated context. */
static void test_reserved_s2cr_type(void)
{
    struct fixture f;
    struct storage_probe_snapshot s;
    init_fixture(&f, 1U);
    set_stream(&f, 0U, 0x80000060U, 0x00030000U);
    assert(storage_probe_collect(DEVICE_BOARD_TB323FU, fixture_read, &f, &s) ==
           STORAGE_PROBE_SMMU_UNSUPPORTED);
    check_value(&s, SMMU + 0xc00U, 0x00030000U);
    assert(s.contexts_captured == 0U);
}

int main(void)
{
    test_board_gate();
    test_gen4_profile();
    test_gen4_live_smmu_geometry();
    test_arguments();
    test_hci_evidence();
    test_masked_multiple_matches();
    test_disabled_and_nontranslated();
    test_unsupported_capabilities();
    test_read_failure();
    test_bounds();
    test_large_mmio_pages();
    test_record_limit();
    test_reserved_s2cr_type();
    puts("storage_probe: all collection, decoding and failure tests passed");
    return 0;
}
