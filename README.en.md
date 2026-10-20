<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/assets/armada-mark-white.svg">
    <img src=".github/assets/armada-mark-black.svg" alt="Armada" width="112">
  </picture>
</p>

<h1 align="center">ArmadaOS Freedom</h1>

<p align="center"><a href="README.md">简体中文</a> · <strong>English</strong></p>

<p align="center"><strong>SteamOS-like Linux for ARM tablets</strong></p>

<p align="center">
  A tablet-focused fork of <a href="https://github.com/armada-os/armada">ArmadaOS</a>,
  bringing Steam Gaming Mode, FEX, Proton, and KDE Plasma to ARM64 tablets,
  with kShimC multi-boot.
</p>

<p align="center">
  <a href="https://github.com/armada-os/armada"><img alt="Based on ArmadaOS" src="https://img.shields.io/badge/based_on-ArmadaOS-18181a?style=flat"></a>
  <a href="#device-support"><img alt="Y700 Gen 4 and Gen 5 tablet adaptation" src="https://img.shields.io/badge/devices-Y700_Gen_4_%26_5-285580?style=flat"></a>
  <a href="LICENSE"><img alt="GPL-2.0-or-later license" src="https://img.shields.io/badge/license-GPL--2.0--or--later-18181a?style=flat"></a>
</p>

<p align="center">
  <a href="#device-support"><strong>Device support &amp; status</strong></a>
  ·
  <a href="#kshimc-multi-boot">Boot menu</a>
  ·
  <a href="#documentation">Documentation</a>
  ·
  <a href="https://armadaos.dev/">Upstream documentation</a>
</p>

## About ArmadaOS Freedom

ArmadaOS Freedom is a community-maintained tablet branch based on
[ArmadaOS](https://github.com/armada-os/armada). It retains the upstream gaming
and desktop foundation while adapting boot, display, power, peripherals, and
Armada Control to tablet hardware.

Highlights of the current adaptation:

- Steam Gaming Mode and KDE Plasma, with FEX and Proton from ArmadaOS.
- A shared kernel build with device-specific device trees, drivers, and hardware
  configuration.
- kShimC multi-boot with separate internal-storage and TF-card Linux entries.
- Device-matched 30 / 60 / 90 / 120 / 165 Hz display modes.
- Dual-USB routing, audio, 5 GHz Wi-Fi, battery and temperature reporting.
- Tablet touch input, plus G9 / G9 2026 controller mapping, RGB control, and
  double-Home quick access.
- Tablet-aware Armada Control, including hiding fan controls on fanless devices.

Feature availability varies by device; see the device pages below for details.
This repository maintains adaptation sources and patches. Tablet installation
images and OTA integration are still in development.

## Device support

Current device work covers Lenovo Legion Y700 Gen 4 and Gen 5. Both share a Linux
**7.2.6** kernel, with separate device trees and hardware configuration.

| Device | Model / SoC | Main confirmed features | Details |
|---|---|---|---|
| Lenovo Legion Y700 Gen 4 | TB322FC / SM8750 | Android and internal Linux boot, Steam Gaming Mode, touch input, G9 controls and RGB | [Gen 4 status](devices/lenovo-tb322fc/README.md) |
| Lenovo Legion Tab Gen 5 / Y700 Gen 5 | TB323FU / SM8850P | Steam Gaming Mode, 165 Hz display, G9 2026 controls and RGB | [Gen 5 status](devices/lenovo-tb323fu/README.md) |

Gen 4 includes fixes for touch coordinates and display recovery when restarting
the Steam session. Charging, dual-USB behavior, updates, and remaining work are
listed in the [Gen 4 known issues](devices/lenovo-tb322fc/README.md#已知问题与待完成项).
Boot images remain model-specific and must not be cross-flashed.

## kShimC multi-boot

The tablet boot path uses **kShimC**, packaged with the boot payloads in the
Android boot image. It keeps the factory boot chain and presents a menu for:

- Android.
- ArmadaOS installed in internal storage.
- ArmadaOS installed on a TF / microSD card.
- Advanced boot: Linux diagnostics, Recovery, Fastboot, and 9008 / EDL.
- Language selection, restart, and shutdown.

The Linux kernel and device tree are boot payloads; Linux then mounts the root
filesystem selected by the internal-storage or TF-card entry. These are two
separate installation targets. The menu does not imply direct SD/UFS filesystem
support in kShimC.

<p align="center">
  <img src=".github/assets/kshimc-star-chart-en.png" alt="KShimC launcher — STAR_CHART theme, English menu" width="100%">
</p>

*STAR_CHART menu rendered from the current implementation; this is a software
preview, not a photograph of the tablet.*

<details>
<summary>Simplified Chinese menu preview</summary>

![KShimC launcher — Simplified Chinese menu](.github/assets/kshimc-star-chart-zh.png)

</details>

The repository's menu sources and Gen 5 build inputs are under
[`devices/lenovo-tb323fu/boot/`](devices/lenovo-tb323fu/boot/).
See the [build recipe](devices/lenovo-tb323fu/boot/star-chart-build/README.md)
for pinned dependencies. See each device page for available boot entries;
advanced boot actions have not all been individually validated on hardware.

## Documentation

| I want to… | Guide |
|---|---|
| Check Gen 4 support and known issues | [Y700 Gen 4 (TB322FC)](devices/lenovo-tb322fc/README.md) |
| Check Gen 5 support and known issues | [Y700 Gen 5 (TB323FU)](devices/lenovo-tb323fu/README.md) |
| Inspect the boot build | [kShimC Gen 5 build recipe](devices/lenovo-tb323fu/boot/star-chart-build/README.md) |
| Check component origins and versions | [Gen 4 sources](devices/lenovo-tb322fc/README.md#源码与来源) · [Gen 5 sources](devices/lenovo-tb323fu/docs/SOURCES.md) |
| Learn the upstream system | [Using ArmadaOS](https://armadaos.dev/using-armada/) |
| Report a tablet-branch issue | [Freedom issues](https://github.com/mydanyi/armadaos-freedom/issues) |

Upstream installation instructions describe upstream-supported devices. Use the
model-specific documentation here for tablet installation and boot.

## Development

The repository retains the upstream bootc image and package build foundation,
plus tablet sources and overlays under [`devices/`](devices/). These overlays
are not all integrated into the generic image recipes yet. Package development
is documented in [`packages/`](packages/README.md); upstream recipes use
[just](https://just.systems/) and [Podman](https://podman.io/):

```console
$ just check     # Run the test suite and check recipe formatting
$ just packages  # Build the packages the image consumes
$ just build     # Build the bootc container; not a ready-to-flash tablet image
$ just --list    # List development recipes
```

For device work, follow the relevant overlay/build instructions and preserve
model-based matching. Reports, device logs, credentials, backups, and local
build outputs are kept outside the repository.

## Credits

- System foundation: [ArmadaOS](https://github.com/armada-os/armada), built on
  [Fedora bootc](https://github.com/bootc-dev/bootc).
- Boot menu: [kShimC](https://github.com/sunflower2333/kShimC/tree/feature/2610).
- Armada logo: [Rax](https://github.com/Raxcoms).

See the [upstream credits](https://armadaos.dev/project/credits/) for more
contributors and the [device documentation](#device-support) for component
origins and pinned versions. Individual components retain their source
attribution and licenses.

## License

Original armadaos-freedom software and scripts are licensed under **GPL-2.0-or-later**, unless a file or component specifies otherwise. Copyright (C) 2026 danyi and the armadaos-freedom contributors, for their original contributions. Inherited ArmadaOS code and other components retain their copyrights and applicable licenses. See [LICENSE](LICENSE) and [licensing and copyright](LICENSING.md) ([简体中文](LICENSING.zh-CN.md)).
