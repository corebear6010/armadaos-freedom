# Early MMU And Handoff Invariants

This change addresses the early-MMU correctness review, not a demonstrated
standby-power root cause. QEMU and host tests do not replace device testing.

## Mapping Lifecycle

- BASE_ONLY, DT_ONLY, RESOURCES_ONLY and RAMOOPS_RECOVERY_ONLY skip optional
  early enable. The explicit resource-diagnostic MMU-cycle remains separate.
- Early implicit RAM is clamped to addresses below 4 GiB. Both RAM and the
  low peripheral window exclude page-rounded `no-map` reservations and FDT
  reservation-map entries. A payload window crossing one of those holes
  rejects optional early enable: manifest bounds are not validated yet, and
  simply leaving holes could cause later manifest/DT reads to fault.
- The executing image and external runtime FDT are explicit WB consumers.
  The complete display reservation, configured framebuffer and test scratch
  receive their final NC attributes before the first enable. Conflicting
  explicit consumers cause early configuration to fail with the MMU off.
- Enabled tables are immutable. `kshim_mmu_add_region()` is an offline-only
  compatibility API and returns `-2` after first enable, including after the
  current CPU disables translation. Both builders also refuse to replace a
  sealed pool: a secondary might still be using it. Re-enabling the same
  tables is supported. Online BBM and cross-CPU remapping are not supported.
- The menu checks existing framebuffer and scratch attributes rather than
  replacing descriptors. An incompatible framebuffer rejects the GUI; an
  incompatible scratch pool is discarded before LVGL can dereference it.

## Register And Cache Handoff

EL1 and non-VHE EL2 are supported. The MMU code never writes HCR_EL2, including
on secondaries and failure paths; E2H=0 with either TGE value is preserved.
An E2H=1 entry returns `-13` from early configuration, and the fallback enable
also refuses that regime. No attempt is made to switch into or out of VHE.

Touch-enabled profiles return `-15` from early configuration. Their resource
discovery can dereference firmware CmdDB pointers inside excluded pools, so
they retain MMU-off probing followed by the existing explicit-resource builder.
The TB322FC and TB323FU profiles have touch disabled and retain early enable.

Writable WB consumers, including an external runtime FDT, are registered for
handoff cleaning. A stackless ARM64 leaf performs the final clean/invalidate
to PoC, instruction-cache invalidation, SCTLR M/C/I clear and local TLBI for
the current EL. There are no stack stores between the final clean and cache
disable. The C wrapper can resume uncached. Teardown is idempotent and does
not clear shared lifecycle state when a secondary prepares for CPU_OFF.

GUI selection already tears down the MMU. A common post-selection teardown
also covers serial-only menus before arbitrary payload copies or DT overlays;
those destinations are not assumed to be in the early clean registry. Final
platform shutdown still runs before the actual payload branch.

## Regression Tests

With the dependencies described in `DEPENDENCIES.md` available, host tests are
built separately from firmware:

```sh
cmake -S tests -B build/host -G Ninja -DKSHIM_TEST_MENU_STYLE=FLAT
cmake --build build/host -j8
ctest --test-dir build/host --output-on-failure
```

`mmu_early_test` inspects the actual MMU implementation's descriptors and clean
registry. It covers overlapping/unaligned reservations, rejection of payloads
spanning holes, disabled memory, the 4 GiB clamp, display NC attributes, explicit FDT
ownership, failed-build rollback and sealing. `mmu_test` also checks the older
builder's writable external FDT and offline additions. The platform menu tests
check refusal of an incompatible framebuffer and rejection of unsafe scratch.

For the register paths, build a non-touch framebuffer profile and run:

```sh
python3 tools/build_runtime.py --config configs/tb322fc_android_defconfig --out build/tb322fc
python3 tests/mmu_handoff_qemu.py --build build/tb322fc --out build/mmu-handoff-qemu
```

The latter links the actual production MMU, ARM64 teardown and DT objects into
a synthetic fixture. QEMU `-cpu max` runs EL1, EL2/TGE=0, EL2/TGE=1 and VHE
rejection. Both teardown APIs, failure paths, external FDT updates and sealing
are exercised. This is a synthetic single-CPU register test, not a hardware
dirty-cache model. `tools/test_qemu_smp.py` separately covers worker CPU_OFF.

The existing `tests/dt_only_qemu.py`, `tests/manifest_ui_qemu.py` and
`tests/manifest_qemu.py` cover diagnostic exits, real UI shutdown and serial
payload/DT dispatch. The manifest tests require the sibling packer or an
equivalent locally supplied binary.

## Validation On 2026-10-22

- FLAT host suite: 55/55 passed with ASan/UBSan enabled. GCC 13's pre-existing
  `tb323fu_menu_test.c:907` format-truncation warning required the local build
  override `-DCMAKE_C_FLAGS=-Wno-error=format-truncation`; source warning policy
  was not relaxed. The final CTest log contained no sanitizer diagnostics.
- TB322FC Android, TB323FU and TB323FU unified runtime builds: exactly 4 MiB,
  no undefined symbols. These are runtime binaries, not packaged or flashed
  boot images. The existing RWX load-segment linker warning remains.
- MMU register fixture: 4/4 passed; SMP self-test: 4/4 passed (EL1/HVC and
  EL2/SMC, one and four CPUs).
- DT_ONLY: 6/6 passed using an isolated diagnostic `main.c` linked with current
  common firmware objects. RESOURCES_ONLY: 6/6 passed with a full diagnostic
  build. Each tests valid/missing/corrupt FDT at EL1 and EL2.
- Four-CPU manifest UI: 2/2 passed (base and copied payload, varied framebuffer,
  MMU off, workers off). Serial manifest dispatch: 10/10 passed, including
  copied payload, custom DTB, malformed DTB fallback and base-only layout.
- Existing local packer binary used for manifest fixtures; QEMU commands use
  `-nic none` because this installation lacks the default NIC ROM.

Still required: device boot/display/input checks, the actual entry EL/HCR,
external FDT cache visibility on hardware, suspend/resume and controlled
standby-current A/B measurements with otherwise identical boot components.
