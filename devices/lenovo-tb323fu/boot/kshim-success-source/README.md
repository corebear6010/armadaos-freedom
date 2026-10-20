# kShimC

kShimC is one implementation of the generic Shim runtime protocol used by
MultiBootKernelPatcher.
The packer enters the attached runtime with BL. kShimC preserves bootloader
arguments and derives the Linux base in `x4`, Manifest address in `x5`, and
size in `x6`. It validates the Manifest and presents its executable entries
through LVGL when a supported continuous splash is verified, with DT-selected
GENI or PL011 serial selection when no usable splash exists.
The default Manifest runtime remains a fixed 4 MiB slot.

This source snapshot is shared by ArmadaOS Freedom's Y700 Gen 4 and Gen 5
profiles. Its directory location under `lenovo-tb323fu` does not restrict it to
Gen 5. See the [Gen 4 boot guide](../../../lenovo-tb322fc/boot/README.md) and
[Gen 5 boot guide](../star-chart-build/README.md) for device-specific packaging.

The runtime uses picolibc 1.8.11 and LVGL 9.2.2. Hardware libraries come from
`lib/CrDK`, pinned at `b6c3335136b7f6b28271917de11e8f11982975fc`.
`ports/crdk/prepare.py` verifies and applies the recorded patches into each
build directory. It never modifies the submodule or a mu_aloha workspace.
See [the CrDK port contract and provenance](ports/crdk/README.md).

## Build

Install an AArch64 GCC toolchain, CMake, Ninja, Python 3.12+ and kconfiglib
(`genconfig` on PATH). Restore the pinned dependencies before choosing a profile.
This bundled snapshot omits the top-level dependency directories and Git metadata;
follow the [dependency record](../../docs/SOURCES.md). In a full kShimC checkout,
submodule initialization is one of the dependency preparation steps:

```sh
git submodule update --init --recursive
python3 tools/build_runtime.py --config configs/hdk8250_defconfig --out build/manifest
```

This produces `build/manifest/kShimC.bin`, verifies the fixed 4 MiB Manifest
slot and rejects undefined symbols. The same command accepts the Andromeda,
HDK8150 and HDK8450 profiles. No firmware payload or mu_aloha path is needed
for a Manifest runtime. CMake evaluates Kconfig at configure time and selects
CrDK and UI sources by capability; `cmake --build` regenerates after config
changes. The firmware uses the boot DT to obtain device addresses.

### Y700 device profiles

| Device | Runtime profile | Boot packaging |
|---|---|---|
| Y700 Gen 4 / TB322FC | `configs/tb322fc_android_defconfig` | [Android or Linux layouts](../../../lenovo-tb322fc/boot/README.md) |
| Y700 Gen 5 / TB323FU | `configs/tb323fu_unified_defconfig` | [Unified-kernel external initramfs](../star-chart-build/README.md) |

Both device profiles enable STAR_CHART, Simplified Chinese, physical keys,
advanced actions, device information and session settings. Gen 4 disables the
debug console and boot timing; Gen 5 enables boot timing. Settings apply only
to the current boot. Linux and Android menu entries come from the selected
device's manifest and payloads.

### Gen 5 external-initrd profile

`configs/tb323fu_unified_defconfig` is the TB323FU profile for the external
initrd Manifest work. It carries the shared launcher menu: the session-only
settings page, the read-only Device info row, and automatic/manual default-boot
behaviour. Session-only settings still apply to the current boot only and are
never written to storage. Every other choice is retained: STAR_CHART style,
270-degree counter-clockwise UI rotation, 243712-byte LVGL buffers, zero
entrance animation, Simplified Chinese with entry details, base-Android
wording, power and advanced actions with QCOM reboot targets, boot timing, and
SPMI keys with two debounce samples; touch, SMP, the UEFI test menu and the
QEMU platform remain off.

```sh
python3 tools/build_runtime.py --config configs/tb323fu_unified_defconfig --out build/tb323fu-unified
```

See [Gen 4 status](../../../lenovo-tb322fc/README.md) and
[Gen 5 status](../../README.md).

## Pack multiple kernels

Build the parent `MultiBootKernelPatcher` repository, then create an INI file:

```ini
[Pack]
Shim=../../kShimC/build/manifest/kShimC.bin
Output=build/FakeKernel
Default=Image-A
Timeout=5000

[Image-A]
Name=Android Linux
Type=Linux
Path=Image
BaseImage=true
Align=21

[Image-B]
Name=Recovery Linux
Type=Linux
Path=/path/to/recovery-Image
BaseImage=false
Align=21

[Image-UEFI]
Name=UEFI
Type=FreeExec
Path=/path/to/UEFI.fd
BaseImage=false
CopyTo=0x9FC00000
CopySizeMax=0x00300000

[Manifest]
Type=Manifest
```

Run the packer with that configuration:

```sh
../../build/MultiBootKernelPatcher ../../Config/Shim.Sample.cfg
```

Additional images are optional. Paths are resolved relative to the
configuration file. Exactly one image must set `BaseImage=true`; `Default`
references an image section. An entry without `CopyTo` is validated as an
ARM64 Linux Image when read; `Align` is optional and its `text_offset`
placement constraint is always applied. An entry with `CopyTo` must also set
`CopySizeMax`. The packer rejects the entry
if its file exceeds that limit; otherwise it is copied to the given address
and booted from there with the original boot arguments. Copy entries default
to 4-byte packed-image alignment, so UEFI does not need an explicit `Align`.
Align is a base-2 alignment exponent from 2 through 21, and assumes the
packed Base Image is loaded at a 2 MiB-aligned address. `Timeout=0` waits for
serial input indefinitely.
The resulting layout follows the generic Shim protocol:

```text
[Base Image slot][KShim 4 MiB slot][aligned images ...][manifest]
```

The Linux slot is at least the ARM64 Image header's effective `image_size`.
The first instruction becomes a BL to the attached runtime; the original
Linux branch is preserved as the second instruction. Header offsets `0x20`, `0x28`, and `0x30` hold the manifest
offset, manifest size, and base image slot size. Additional Linux Images honor
their ARM64 `text_offset` relative to a 2 MiB-aligned base. Copy entries use
their exact file size and KShim synchronizes the data and instruction caches
after copying. The variable-length manifest is appended at the end of the
packed image. At boot, enter a displayed number over PL011 and press Enter;
Escape or the timeout selects the default.

## QEMU

```sh
qemu-system-aarch64 \
	-machine virt,gic-version=3 \
	-cpu cortex-a57 -smp 1 -m 1024M \
	-nographic -no-reboot \
	-kernel build/FakeKernel \
	-append 'console=ttyAMA0,115200 earlycon=pl011,mmio32,0x09000000'
```


## Board menus and input

`configs/` contains capability profiles. The DT splash reservation and stable
SDE source/mixer registers supply the framebuffer address, dimensions, stride
and format. Supported layouts are linear RGB565, 32-bit RGB/BGR and verified
shared-framebuffer split scans. An absent or ambiguous splash falls back to
serial selection. kShimC does not initialize DSI hardware. Volume Up moves up,
Volume Down moves down, and Power confirms. Touch selects a card; the separate
Boot button confirms on profiles with supported touch input. The Y700 profiles
disable launcher touch and use physical volume/power keys; this is separate
from touchscreen support inside Linux.

Menu styles are compile-time choices in `src/Kconfig` and `src/ui/styles/`.
Both Y700 profiles select STAR_CHART, with landscape navigation, entry details,
language selection and physical-key hints. See the [Y700 menu guide](docs/tb323fu-boot-menu.md)
for current behavior. The other theme documents describe the earlier preset
families and their rendering tools; they do not select the installed theme.

STAR_CHART uses `src/ui/fonts/lxgw_wenkai_menu.ksf`, an LXGW WenKai menu subset.
Other styles use the Noto Sans CJK SC subset. CMake selects the matching blob;
LVGL's TinyTTF renders the text. Font licenses and source records remain in
`src/ui/fonts/`. When changing menu text, regenerate the corresponding subset
with `tools/gen_ui_font.py` and update the
[required menu characters](../star-chart-build/menu-required-text.txt).

The MIX 3 5G profile is `andromeda`: ST FTM5 (not FocalTech), GENI I2C at
0x49, 1080 x 2340. Its bus, clocks, GPIOs, CmdDB and RPMh setup are independent
of HDK8150. The protocol also has an SPI transport, but no SPI touch variant
of this phone has been established. This boot driver does not flash touch
firmware. Source revisions and register provenance are recorded under
`ports/crdk` and `configs/andromeda-provenance.json`.
The FTM5 driver sources fetched from GitHub are pinned with hashes in
`third_party/st-ftm5/UPSTREAM.json`. IRQ/RESET GPIO122/54 use the vendor's
8 mA pull-up configuration; the power sequence enables GPIO37, waits 4 ms,
enables the fixed 1.8 V RPMh rail, then pulses reset for 10 ms.

The touch input path delivers every FIFO contact transition to LVGL, so a tap
whose press and release arrive in one read still selects a card or confirms
the Boot button. A controller reset, bus error or shutdown cancels the contact
without generating a click. FTM5 also handles a moving finger whose ENTER
was missed and treats hovering as released. The serial console prints the
board/controller name and initialization or input error code for bringup.

Before payload handoff, the runtime stops touch scanning, restores claimed bus
resources, joins drawing work, confirms secondary CPU_OFF using PSCI, and
cleans writable runtime-owned WB mappings before disabling its MMU. It then
quiesces GENI UART and restores inherited settings. Reserved/no-map memory is
never mapped as general RAM; explicit framebuffer/CmdDB consumers use NC mappings. A failed shutdown prevents handoff.
Unavailable PSCI falls back to rendering on the boot CPU.

## Board identification

`src/dt/device_info.c` uses exact root matches for explicit board and SoC identities. Root
`compatible` entries must match exactly. A stock board that carries no Lenovo identity is accepted
from one measured active-panel reference, never from the model text, a packed
payload, the SoC alone, a numeric phandle or any standby panel: the exact
`/soc/qcom,dsi-display-primary` node (single `qcom,dsi-display` compatible,
`primary` label, itself and its ancestors available) must resolve its
single-cell `qcom,dsi-default-panel` to one unique enabled panel whose node name
and `qcom,mdss-dsi-panel-name` match exactly. That fingerprint is used for the
Elden CPHY panel with an exact `qcom,sunp` root and for the Baldur DPHY panel
with an exact `qcom,canoep` root and no SM8750-family entry. Any other layout,
including a near match of case, prefix, suffix, primary or panel, stays UNKNOWN:
only these measured shapes have evidence.

## Validation

```sh
cmake -S tests -B build/host -G Ninja -DCMAKE_C_COMPILER=clang
cmake --build build/host -j8
ctest --test-dir build/host --output-on-failure
python3 tools/test_qemu_smp.py
python3 tools/test_crdk_upstream.py --edk2 /path/to/edk2
python3 tests/manifest_qemu.py --runtime build/manifest/kShimC.bin
python3 tests/manifest_ui_qemu.py --build build/qemu-manifest-ui
python3 tools/test_qemu_lvgl_failstop.py --build build/qemu-lvgl-failstop
```

The host suite uses ASan and UBSan. QEMU executes the real AArch64 PSCI path
under EL1/HVC and EL2/SMC at 1, 2, 4 and 8 CPUs. The Manifest regression uses
the sibling packer, then boots its default Linux image, another Linux image,
and a copied FreeExec image. It also verifies a separate payload DT and
rejects a DTB exceeding its Manifest entry length or with corrupt structure. `manifest_ui_qemu.py` additionally runs the real
LVGL framebuffer menu with four PSCI CPUs and verifies that the selected
payload observes a varied framebuffer, disabled MMU, and CPU_OFF on all
secondary CPUs. The fail-stop check injects a draw-worker join failure and
verifies that no freed worker is reused. These checks are not hardware
validation of the phone's touch bus, display, or production firmware's PSCI
permissions.

## Build board test images

The standalone tests reserve 40 MiB in the board's Kernel region and embed
one board-matched UEFI FD. Both `UEFI A` and `UEFI B` boot that same FD.
Each build checks
the embedded FD against its saved build report, target device, DEBUG setting,
GENI serial library and nonzero debug PCDs.

```sh
python3 tools/build_hdk8150.py --aloha /path/to/mu_aloha_platforms --out build/hdk8150 \
  --uefi-boot build/uefi-hdk8150/source-uefi-boot.img \
  --uefi-build-dir build/uefi-hdk8150 --require-debug-uart
python3 tools/build_hdk8450.py --aloha /path/to/mu_aloha_platforms --out build/hdk8450 \
  --uefi-boot build/uefi-hdk8450/source-uefi-boot.img \
  --uefi-build-dir build/uefi-hdk8450 --require-debug-uart
python3 tools/build_andromeda_dt.py
python3 tools/build_andromeda.py --aloha /path/to/mu_aloha_platforms --out build/andromeda \
  --uefi-boot build/uefi-andromeda/corrected/source-uefi-boot.img \
  --uefi-build-dir build/uefi-andromeda --require-debug-uart \
  --dtb-bundle build/andromeda-dt/android-andromeda-dtbs.bin
```

The saved Andromeda FD is verified DEBUG+UART, but its original source boot
image carried an incorrect 8450-sized BootShim header. `corrected/` holds a
wrapper rebuilt for 0x9fc00000 / 3 MiB, using the byte-identical FD; its
`provenance.json` records both images. The original snapshot is preserved.
The four Andromeda base trees retain the phone's existing DTBO selection;
32 base/overlay combinations are replayed before packaging. No DTBO is flashed.

HDK8250 bindings and hardware tables use the pinned OnePlus
`oneplus/SM8250_Q_10.0` source at `cad09e061ef6cf689a4a1e54d27562e0f042236a`.
The HDK8250 ST FTM4 touch controller is unsupported; its driver and vendor
reference snapshots have been removed. The menu remains controllable through
physical volume and power keys. Touch input supports FTM5 controllers selected
by their DT property family and independently verified chip ID.

```sh
python3 tools/build_hdk8250.py --out build/hdk8250
```

This produces a 4 MiB Manifest runtime without bundling UEFI. Display layout
is discovered at boot. The standalone packaging tools above require an
explicit `--aloha` only to read board memory reservations and validate the
user-supplied UEFI package. They do not rebuild or edit that workspace.

Standalone copy-in validates that its scratch page does not overlap the
incoming payload, and rejects an unsafe loader placement before writing.
`tests/boot_emulation.py` checks overlapping and disjoint copies, original
arguments, stack, BSS, relocations, both UEFI choices and that refusal path.

## License / 许可证

Unless a file carries a different license notice, kShimC's original source code
is licensed under the **GNU General Public License, version 3 only**
(`GPL-3.0-only`). See [LICENSE](LICENSE) for the full text.

Third-party code, submodules, and font assets retain their own licenses and
copyright notices. Existing per-file licenses remain in effect, including MIT,
libfdt's dual license and the fonts' SIL Open Font License. This project license
does not replace those notices or relicense those components.

除文件中另有许可声明的部分外，kShimC 原创源码采用 **GNU 通用公共许可证第 3 版**
（`GPL-3.0-only`），许可全文见 [LICENSE](LICENSE)。

第三方代码、子模块和字库保留各自的许可证及版权声明。文件中已有的 MIT、libfdt 双重许可、
字体 SIL Open Font License 等授权继续有效，本项目许可证不替代这些声明，
也不改变这些组件原有的授权。
