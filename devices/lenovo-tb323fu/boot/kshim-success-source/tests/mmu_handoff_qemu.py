#!/usr/bin/env python3
"""Exercise production MMU objects under QEMU, without accessing a device.

Requires a non-touch ARM64 build with compile_commands.json, for example a
tb322fc_android_defconfig build produced by tools/build_runtime.py. Tests EL1,
non-VHE EL2 with TGE clear/set, and rejection of a VHE entry. Checks external
FDT writes, no-map exclusions, immutable tables and both teardown entry points.
QEMU does not model hardware dirty-cache visibility or prove BBM correctness.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess


LINKER = """
ENTRY(_start)
SECTIONS {
    . = 0x40200000;
    __image_start_address__ = .;
    .text : { *(.text.boot) *(.text .text.*) }
    .rodata : { *(.rodata .rodata.*) }
    .data : { *(.data .data.*) }
    .bss ALIGN(4096) (NOLOAD) : {
        __bss_start__ = .;
        *(.bss .bss.*) *(COMMON)
        . = ALIGN(4096);
        *(.mmu_tables)
        . = ALIGN(16);
        . += 0x10000;
        test_stack_top = .;
        __bss_end__ = .;
    }
    __image_end_address__ = .;
    /DISCARD/ : { *(.note*) *(.eh_frame*) }
}
"""

START = r"""
    .section .text.boot, "ax"
    .global _start
_start:
    b start
    nop
    .quad 0
    .quad __image_end_address__ - _start
    .quad 0, 0, 0, 0
    .long 0x644d5241, 0
start:
    msr daifset, #0xf
    adrp x9, vectors
    add x9, x9, :lo12:vectors
    mrs x10, CurrentEL
    cmp x10, #4
    b.ne 1f
    msr vbar_el1, x9
    mrs x10, cpacr_el1
    orr x10, x10, #(3 << 20)
    msr cpacr_el1, x10
    b 2f
1:
    msr vbar_el2, x9
    mov x10, #0x80000000
#if TEST_MODE >= 2
    orr x10, x10, #(1 << 27)
#endif
#if TEST_MODE == 3
    orr x10, x10, #(1 << 34)
#endif
    msr hcr_el2, x10
    isb
    mov x10, #0
#if TEST_MODE == 3
    mov x10, #(3 << 20)
#endif
    msr cptr_el2, x10
2:
    isb
    adrp x9, __bss_start__
    add x9, x9, :lo12:__bss_start__
    adrp x10, __bss_end__
    add x10, x10, :lo12:__bss_end__
3:
    cmp x9, x10
    b.hs 4f
    str xzr, [x9], #8
    b 3b
4:
    adrp x9, test_stack_top
    add x9, x9, :lo12:test_stack_top
    mov sp, x9
    mov x0, #TEST_MODE
    bl test_main
    b .
    .balign 2048
vectors:
    .rept 16
    b exception
    .space 124
    .endr
exception:
    mov x0, #4
    adr x1, exception_message
    hlt #0xf000
    mov x0, #0x20
    adr x1, exception_exit
    hlt #0xf000
    b .
exception_message: .asciz "MMU_HANDOFF_FAIL: exception\n"
    .balign 8
exception_exit: .quad 0x20026, 1
"""

TEST = r"""
#include <kshim_mmu.h>
#include <libfdt.h>
#include <stdint.h>

static void message(const char *text)
{
    register uint64_t op __asm__("x0") = 4;
    register const char *arg __asm__("x1") = text;
    __asm__ volatile("hlt #0xf000" : "+r"(op), "+r"(arg) : : "memory");
}
static _Noreturn void finish(int code)
{
    uint64_t block[] = {0x20026, (uint64_t)code};
    register uint64_t op __asm__("x0") = 0x20;
    register const void *arg __asm__("x1") = block;
    __asm__ volatile("hlt #0xf000" : "+r"(op), "+r"(arg) : : "memory");
    for (;;) __asm__ volatile("wfe");
}
#define CHECK(c) do { if (!(c)) { message("MMU_HANDOFF_FAIL: " #c "\n"); finish(1); } } while (0)
#define RW (KSHIM_MMU_READ | KSHIM_MMU_WRITE)
#define E2H (UINT64_C(1) << 34)
#define TGE (UINT64_C(1) << 27)
static uint64_t hcr(unsigned mode)
{
    uint64_t value = 0;
    if (mode) __asm__ volatile("mrs %0, hcr_el2" : "=r"(value));
    return value;
}
static uint64_t sctlr(unsigned mode)
{
    uint64_t value;
    if (mode) __asm__ volatile("mrs %0, sctlr_el2" : "=r"(value));
    else __asm__ volatile("mrs %0, sctlr_el1" : "=r"(value));
    return value;
}
static void range(void *fdt, int node, uint64_t base, uint64_t size)
{
    fdt64_t cells[] = {cpu_to_fdt64(base), cpu_to_fdt64(size)};
    CHECK(fdt_setprop(fdt, node, "reg", cells, sizeof(cells)) == 0);
}
static void reservation(void *fdt, const char *name, uint64_t base, uint64_t size)
{
    int parent = fdt_path_offset(fdt, "/reserved-memory");
    int node = fdt_add_subnode(fdt, parent, name);
    CHECK(node >= 0);
    range(fdt, node, base, size);
    CHECK(fdt_setprop(fdt, node, "no-map", NULL, 0) == 0);
}
void test_main(unsigned mode)
{
    uint64_t entry_hcr = hcr(mode);
    uint64_t el;
    __asm__ volatile("mrs %0, CurrentEL" : "=r"(el));
    CHECK(el == (mode ? 8U : 4U));
    CHECK((entry_hcr & (E2H | TGE)) ==
          (mode == 3 ? E2H | TGE : mode == 2 ? TGE : 0));
    CHECK((sctlr(mode) & 1U) == 0);

    void *fdt = (void *)(uintptr_t)0x48000000;
    kshim_mmu_early_config_t early = {
        .fdt = fdt, .payload_base = 0x46000000, .payload_end = 0x49400000,
    };
    /* Invalid DT: no partial table pool or changed HCR survives rejection. */
    *(volatile uint64_t *)fdt = 0;
    CHECK(kshim_mmu_configure_early(&early) == (mode == 3 ? -13 : -6));
    CHECK(!kshim_mmu_root_table() && !kshim_mmu_is_enabled());
    CHECK(hcr(mode) == entry_hcr);

    CHECK(fdt_create_empty_tree(fdt, 0x10000) == 0);
    CHECK(fdt_setprop_u32(fdt, 0, "#address-cells", 2) == 0);
    CHECK(fdt_setprop_u32(fdt, 0, "#size-cells", 2) == 0);
    CHECK(fdt_setprop_u64(fdt, 0, "test-cookie", 0) == 0);
    int node = fdt_add_subnode(fdt, 0, "memory@40000000");
    CHECK(node >= 0);
    CHECK(fdt_setprop_string(fdt, node, "device_type", "memory") == 0);
    range(fdt, node, 0x40000000, 0x10000000);
    node = fdt_add_subnode(fdt, 0, "reserved-memory");
    CHECK(node >= 0);
    CHECK(fdt_setprop_u32(fdt, node, "#address-cells", 2) == 0);
    CHECK(fdt_setprop_u32(fdt, node, "#size-cells", 2) == 0);
    CHECK(fdt_setprop(fdt, node, "ranges", NULL, 0) == 0);
    reservation(fdt, "firmware@47000000", 0x47000000, 0x1000);
    reservation(fdt, "runtime-dt@48000000", 0x48000000, 0x10000);
    reservation(fdt, "cont_splash_region@49000000", 0x49000000, 0x400000);

    CHECK(kshim_mmu_configure_early(&early) == (mode == 3 ? -13 : -7));
    CHECK(!kshim_mmu_root_table() && !kshim_mmu_is_enabled());
    CHECK(hcr(mode) == entry_hcr);
    early.payload_end = 0x46100000;
    int status = kshim_mmu_configure_early(&early);
    if (mode == 3) {
        CHECK(status == -13);
        CHECK(!kshim_mmu_root_table() && !kshim_mmu_is_enabled());
        /* The fallback builder must not enable a non-VHE regime either. */
        kshim_mmu_config_t fallback = {.fdt = fdt};
        CHECK(kshim_mmu_configure(&fallback) == 0);
        CHECK(kshim_mmu_enable_current_cpu() != 0);
        CHECK(kshim_mmu_prepare_handoff() == 0);
        CHECK(!kshim_mmu_is_enabled() && hcr(mode) == entry_hcr);
    } else {
        CHECK(status == 0 && kshim_mmu_is_enabled());
        CHECK(hcr(mode) == entry_hcr);
        CHECK(!kshim_mmu_region_matches(0x47000000, 0x1000, KSHIM_MMU_NORMAL_WB, RW));
        CHECK(!kshim_mmu_region_matches(0x47000000, 0x1000, KSHIM_MMU_DEVICE, RW));
        CHECK(kshim_mmu_region_matches(0x49000000, 0x400000, KSHIM_MMU_NORMAL_NC, RW));
        CHECK(kshim_mmu_region_matches(0x46000000, 0x1000, KSHIM_MMU_NORMAL_WB, RW));
        CHECK(!kshim_mmu_region_matches(0x46000000, 0x1000, KSHIM_MMU_NORMAL_WB,
                                        RW | KSHIM_MMU_EXECUTE));
        for (unsigned round = 0; round < 2; ++round) {
            const uint64_t cookie = UINT64_C(0x123456789abcdef0) + round;
            CHECK(fdt_setprop_inplace_u64(fdt, 0, "test-cookie", cookie) == 0);
            if (round) kshim_mmu_cpu_off_prepare();
            else CHECK(kshim_mmu_prepare_handoff() == 0);
            CHECK((sctlr(mode) & 0x1005) == 0 && hcr(mode) == entry_hcr);
            CHECK(kshim_mmu_prepare_handoff() == 0);
            int length;
            const fdt64_t *value = fdt_getprop(fdt, 0, "test-cookie", &length);
            CHECK(value && length == 8 && fdt64_to_cpu(*value) == cookie);
            CHECK(kshim_mmu_add_region(0x49000000, 0x1000, KSHIM_MMU_NORMAL_WB, RW) == -2);
            CHECK(kshim_mmu_configure_early(&early) == -16);
            if (!round) CHECK(kshim_mmu_enable_current_cpu() == 0);
        }
    }
    message("MMU_HANDOFF_PASS\n");
    finish(0);
}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cpu", default="max", help="QEMU CPU with VHE support")
    args = parser.parse_args()
    build, out = args.build.resolve(), args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    config = (build / ".config").read_text()
    if "CONFIG_KSHIM_TOUCH=y" in config:
        parser.error("use a non-touch build to exercise early MMU enable")
    if "CONFIG_KSHIM_FRAMEBUFFER=y" not in config:
        parser.error("use a framebuffer build to test the display reservation")
    commands = json.loads((build / "compile_commands.json").read_text())
    mmu = next(c for c in commands if c["file"].endswith("/arch/arm64/mmu.c"))
    flags = shlex.split(mmu["command"])
    mmu_object = build / flags[flags.index("-o") + 1]
    flags = flags[:flags.index("-o")]
    sources = ["src/dt/dt.c", "src/dt/splash.c", "src/ui/framebuffer.c",
               "src/arch/arm64/arm64.S"]
    sources += ["lib/libfdt/" + n + ".c" for n in
                ("fdt", "fdt_ro", "fdt_rw", "fdt_sw", "fdt_wip",
                 "fdt_check", "fdt_empty_tree", "fdt_addresses")]
    objects = [mmu_object] + [build / "CMakeFiles/kShimC.elf.dir" / (s + ".obj")
                              for s in sources]
    cc = flags[0]
    objcopy = shutil.which("aarch64-linux-gnu-objcopy")
    if not objcopy:
        parser.error("aarch64-linux-gnu-objcopy is required")
    (out / "fixture.ld").write_text(LINKER)
    (out / "fixture.c").write_text(TEST)
    (out / "fixture.S").write_text(START)
    build_steps = [flags + ["-c", str(out / "fixture.c"), "-o", str(out / "fixture.o")]]
    results = []
    for mode, name in enumerate(("el1", "el2-tge0", "el2-tge1", "el2-vhe-rejected")):
        elf, binary = out / (name + ".elf"), out / (name + ".bin")
        steps = build_steps + [
            [cc, "-c", "-DTEST_MODE=" + str(mode), str(out / "fixture.S"),
             "-o", str(out / (name + ".o"))],
            [cc, "-nostdlib", "-static", "-Wl,--build-id=none", "-Wl,--gc-sections",
             "-T", str(out / "fixture.ld"), "-o", str(elf),
             str(out / (name + ".o")), str(out / "fixture.o"),
             *map(str, objects), str(build / "lib/picolibc/libc.a"), "-lgcc"],
            [objcopy, "-O", "binary", str(elf), str(binary)],
        ]
        with (out / (name + ".build.log")).open("w") as log:
            for step in steps:
                subprocess.run(step, cwd=build, stdout=log, stderr=subprocess.STDOUT, check=True)
        command = ["qemu-system-aarch64", "-machine",
                   "virt,virtualization=" + ("on" if mode else "off"),
                   "-cpu", args.cpu, "-accel", "tcg", "-m", "256M", "-smp", "1",
                   "-display", "none", "-monitor", "none", "-serial", "none", "-nic", "none",
                   "-semihosting-config", "enable=on,target=native", "-no-reboot",
                   "-kernel", str(binary)]
        try:
            run = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15)
            text = run.stdout.decode(errors="replace")
            passed = run.returncode == 0 and text.count("MMU_HANDOFF_PASS") == 1 and "FAIL" not in text
        except subprocess.TimeoutExpired as exc:
            text = (exc.stdout or b"").decode(errors="replace") + "\ntimeout\n"
            passed = False
        (out / (name + ".log")).write_text(text)
        results.append({"name": name, "passed": passed, "command": command})
        print(name + (": PASS" if passed else ": FAIL\n" + text), flush=True)
    report = {"scope": "synthetic QEMU only; no hardware cache or power validation",
              "mmu_object_sha256": hashlib.sha256(mmu_object.read_bytes()).hexdigest(),
              "tests": results, "passed": all(r["passed"] for r in results)}
    (out / "validation.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
