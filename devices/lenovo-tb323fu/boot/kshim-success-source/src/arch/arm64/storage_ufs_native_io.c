/* SPDX-License-Identifier: GPL-3.0-only */
#include <config.h>
#include <storage_ufs_native_io.h>
#include <storage_probe_io.h>
#include <storage_ufs.h>
#include <kshim_mmu.h>
#include <stdbool.h>

#define UFS_NATIVE_BASE UINT64_C(0x01d84000)
#define UFS_NATIVE_SPAN UINT64_C(0x3000)
#define UFS_NATIVE_PAGE UINT64_C(0x1000)
/* PAR_EL1 address bits [51:12] once the fault bit is clear. */
#define UFS_NATIVE_PAR_ADDR_MASK UINT64_C(0x000ffffffffff000)

extern int arm64_storage_ufs_store32(uint64_t address, uint32_t value);

static bool native_el1_ready(void)
{
    uint64_t current_el, stack_select;

    __asm__ volatile("mrs %0, CurrentEL" : "=r"(current_el));
    __asm__ volatile("mrs %0, SPSel" : "=r"(stack_select));
    return current_el == 4 && stack_select == 1 && kshim_mmu_is_enabled();
}

int storage_ufs_native_read32(void *ctx, uint32_t offset, uint32_t *value)
{
    uint64_t address;

    if (!value || (offset & 3U) || offset >= UFS_NATIVE_SPAN)
        return -2;
    address = UFS_NATIVE_BASE + offset;
    if (address < UFS_NATIVE_BASE)
        return -2;
    return storage_probe_native_read32(ctx, address, value);
}

static bool ufs_write_allowed(uint32_t offset, uint32_t value)
{
    /* The value is only inspected by the UIC pairs below. */
    (void)value;
    switch (offset) {
    case 0x18U: /* AHIT */
    case 0x20U: /* IS */
    case 0x24U: /* IE */
    case 0x50U: /* UTRL base low */
    case 0x54U: /* UTRL base high */
    case 0x58U: /* UTRL doorbell */
    case 0x60U: /* RUN */
        return true;
#if defined(CONFIG_KSHIM_UFS_LINK_WAKE) && CONFIG_KSHIM_UFS_LINK_WAKE
    /* Exact UIC command/argument pairs for the inherited-HIBERN8 exit and
     * restore: DME_GET, HIBERN8_ENTER and HIBERN8_EXIT only, with the MPHY
     * TX FSM selector or a zero argument, and zero ARG2/ARG3. */
    case 0x90U: /* UIC command */
        return value == 0x01U || value == 0x17U || value == 0x18U;
    case 0x94U: /* UIC ARG1 */
        return value == 0U || value == UINT32_C(0x00410000);
    case 0x98U: /* UIC ARG2 */
    case 0x9cU: /* UIC ARG3 */
        return value == 0U;
#endif
    default:
        return false;
    }
}

int storage_ufs_native_write32(void *ctx, uint32_t offset, uint32_t value)
{
    uint64_t masks, vectors;
    int status;

    (void)ctx;
    /* HCE/reset/DMESET/SMMU/clock writes stay out of this primitive entirely;
     * the UIC offset/value pairs above are the only opt-in exception. */
    if (!ufs_write_allowed(offset, value))
        return -2;
    if (!native_el1_ready())
        return -3;
    __asm__ volatile("mrs %0, daif\n\tmsr daifset, #0xf\n\tmrs %1, vbar_el1"
                     : "=r"(masks), "=r"(vectors) : : "memory");
    arm64_install_exception_vector();
    status = arm64_storage_ufs_store32(UFS_NATIVE_BASE + offset, value);
    __asm__ volatile("msr vbar_el1, %0\n\tisb\n\tmsr daif, %1"
                     : : "r"(vectors), "r"(masks) : "memory");
    return status;
}

int storage_ufs_native_dma_address(void *arena, size_t bytes, uint64_t *physical)
{
    uint64_t start, first = 0, saved_par, ctr, line_size;
    int status = 0;

    if (physical)
        *physical = 0;
    if (!arena || !physical || bytes != KSHIM_UFS_DMA_BYTES ||
        ((uintptr_t)arena & (UFS_NATIVE_PAGE - 1)))
        return -2;
    start = (uint64_t)(uintptr_t)arena;
    if (start > UINT64_MAX - (uint64_t)bytes)
        return -2;
    if (!native_el1_ready())
        return -3;
    __asm__ volatile("mrs %0, ctr_el0" : "=r"(ctr));
    line_size = UINT64_C(4) << ((ctr >> 16) & UINT64_C(0xf));
    if (line_size > UFS_NATIVE_PAGE)
        return -4;

    __asm__ volatile("mrs %0, par_el1" : "=r"(saved_par));
    for (uint64_t page = 0; page < (uint64_t)bytes; page += UFS_NATIVE_PAGE) {
        uint64_t pa;

        __asm__ volatile("at s1e1w, %1\n\tisb\n\tmrs %0, par_el1"
                         : "=r"(pa) : "r"(start + page) : "memory");
        if (pa & UINT64_C(1)) {
            status = -5;
            break;
        }
        pa &= UFS_NATIVE_PAR_ADDR_MASK;
        if (page == 0) {
            if (pa == 0) {
                status = -6;
                break;
            }
            first = pa;
        } else if (pa != first + page) {
            status = -7;
            break;
        }
    }
    __asm__ volatile("msr par_el1, %0\n\tisb" : : "r"(saved_par) : "memory");
    if (status == 0)
        *physical = first;
    return status;
}

static uint64_t native_dmin_line(void)
{
    uint64_t ctr;

    __asm__ volatile("mrs %0, ctr_el0" : "=r"(ctr));
    return UINT64_C(4) << ((ctr >> 16) & UINT64_C(0xf));
}

void storage_ufs_native_sync_device(void *ctx, void *buffer, size_t bytes)
{
    uint64_t line, end;

    (void)ctx;
    if (!buffer || bytes == 0)
        return;
    line = native_dmin_line();
    if ((uint64_t)(uintptr_t)buffer > UINT64_MAX - (uint64_t)bytes)
        return;
    end = (uint64_t)(uintptr_t)buffer + (uint64_t)bytes;
    for (uint64_t address = (uint64_t)(uintptr_t)buffer & ~(line - 1);
         address < end; address += line)
        __asm__ volatile("dc civac, %0" : : "r"(address) : "memory");
    __asm__ volatile("dsb sy" ::: "memory");
}

void storage_ufs_native_sync_cpu(void *ctx, void *buffer, size_t bytes)
{
    uint64_t line, end;

    (void)ctx;
    if (!buffer || bytes == 0)
        return;
    line = native_dmin_line();
    if ((uint64_t)(uintptr_t)buffer > UINT64_MAX - (uint64_t)bytes)
        return;
    end = (uint64_t)(uintptr_t)buffer + (uint64_t)bytes;
    __asm__ volatile("dsb sy" ::: "memory");
    for (uint64_t address = (uint64_t)(uintptr_t)buffer & ~(line - 1);
         address < end; address += line)
        __asm__ volatile("dc ivac, %0" : : "r"(address) : "memory");
    __asm__ volatile("dsb sy" ::: "memory");
}
