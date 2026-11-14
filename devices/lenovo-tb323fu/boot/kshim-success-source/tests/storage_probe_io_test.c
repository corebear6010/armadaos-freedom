/* SPDX-License-Identifier: GPL-3.0-only */
#include <storage_probe_io.h>
#include <assert.h>
#include <string.h>
#include <stdio.h>
const unsigned char arm64_storage_probe_read_instruction[4] = {1};
const unsigned char arm64_storage_probe_read_failed[4] = {2};
const unsigned char arm64_storage_ufs_write_instruction[4] = {3};
const unsigned char arm64_storage_ufs_write_failed[4] = {4};

int main(void)
{
    ARM64_EXCEPTION_CONTEXT c = {0}, before;
    c.elr = (uintptr_t)arm64_storage_probe_read_instruction;
    c.esr = UINT64_C(0x25) << 26;
    c.x[0] = 0x1d84000;
    c.x[1] = 0x80000000;
    before = c;
    assert(!arm64_storage_probe_fixup(NULL, 0));
    for (unsigned type = 1; type < 4; ++type) {
        assert(!arm64_storage_probe_fixup(&c, type));
        assert(memcmp(&c, &before, sizeof(c)) == 0);
    }
    c.elr += 4;
    before = c;
    assert(!arm64_storage_probe_fixup(&c, 0));
    assert(memcmp(&c, &before, sizeof(c)) == 0);
    c.elr -= 4;
    c.esr |= UINT64_C(1) << 6; /* A write fault must never be swallowed. */
    before = c;
    assert(!arm64_storage_probe_fixup(&c, 0));
    assert(memcmp(&c, &before, sizeof(c)) == 0);
    c.esr = UINT64_C(0x24) << 26; /* Lower-EL abort is unrelated. */
    before = c;
    assert(!arm64_storage_probe_fixup(&c, 0));
    assert(memcmp(&c, &before, sizeof(c)) == 0);
    c.esr = UINT64_C(0x25) << 26;
    before = c;
    assert(arm64_storage_probe_fixup(&c, 0));
    assert(c.elr == (uintptr_t)arm64_storage_probe_read_failed);
    before.elr = c.elr;
    assert(memcmp(&c, &before, sizeof(c)) == 0);

    /* A write fault is recovered only for the exact native STR helper. */
    c.elr = (uintptr_t)arm64_storage_ufs_write_instruction;
    c.esr = (UINT64_C(0x25) << 26) | (UINT64_C(1) << 6);
    before = c;
    assert(!arm64_storage_probe_fixup(&c, 1));
    assert(memcmp(&c, &before, sizeof(c)) == 0);
    assert(arm64_storage_probe_fixup(&c, 0));
    assert(c.elr == (uintptr_t)arm64_storage_ufs_write_failed);
    before.elr = c.elr;
    assert(memcmp(&c, &before, sizeof(c)) == 0);

    c.elr = (uintptr_t)arm64_storage_ufs_write_instruction + 4;
    before = c;
    assert(!arm64_storage_probe_fixup(&c, 0));
    assert(memcmp(&c, &before, sizeof(c)) == 0);
    c.elr -= 4;

    c.esr &= ~(UINT64_C(1) << 6); /* A read fault at the STR PC is unrelated. */
    before = c;
    assert(!arm64_storage_probe_fixup(&c, 0));
    assert(memcmp(&c, &before, sizeof(c)) == 0);
    c.esr |= UINT64_C(1) << 6;

    c.esr = (UINT64_C(0x24) << 26) | (UINT64_C(1) << 6);
    before = c;
    assert(!arm64_storage_probe_fixup(&c, 0));
    assert(memcmp(&c, &before, sizeof(c)) == 0);

    c.esr = (UINT64_C(0x25) << 26) | (UINT64_C(1) << 6);
    c.elr = (uintptr_t)arm64_storage_probe_read_instruction;
    before = c;
    assert(!arm64_storage_probe_fixup(&c, 0));
    assert(memcmp(&c, &before, sizeof(c)) == 0);
    puts("storage probe exact-instruction abort fixup: PASS");
    return 0;
}
