# kshim-tools

Target-side boot image tools for the kShim multi-boot work, packaged so the
normal Armada rootfs build can install them without an on-device compiler or
host paths.

## Contents

- `MultiBootKernelPatcher` — packs a kernel/DTB/shim image from an INI config.
- `UnpackTool` — unpacks such an image back into its inputs and config.
- `repack_boot_v4.py` — low-level kernel replacement in an Android v4 boot
  image. It emits an unsigned footer and is not a final output path.
- `avbtool.py` — Android Verified Boot image/signature helper used by the repack path.

Final boot images must pass through
`system_files/usr/libexec/armada/kshim-avb-sign.py`, which requires an AVB
private key, replaces the temporary `NONE` footer with `SHA256_RSA4096`, pins
the AOSP public-key fingerprint, and verifies the signed payload. The
TB322FC packaging drivers require `--avb-key`; they fail before producing
`boot.img` when signing is unavailable.

## Build contract

The shared package builder runs this directory as `/work` with the packages
root at `/src`, and collects artifacts from `./out`. CMake builds the vendored
sources with `BUILD_SHIMS=OFF` into a package-local `build/` directory: no shim
assembly and no cross binutils are involved. Executables link statically
against glibc and the build fails if either ELF is not AArch64.

The three vendored packer regression tests (`payload_from.py`,
`payload_from_nonbase.py`, `dtb_alignment.py`) run against the staged binaries
in package-local test directories, so a test failure stops the build. No project sources are downloaded; build dependencies come from Fedora packages.
No compiler is required at runtime.

Install layout used by the image build:

- `/usr/libexec/armada/kshim-tools/` — the four files above, mode 0755
- `/usr/share/licenses/armada-kshim-tools/` — license texts

## Provenance

- `vendor/packer/` and `vendor/packer/lib/inih/` are byte copies of the accepted
  packer sources: packer archive SHA256
  `4b4d9425c593d46b2018c24ced98e2e52d767d341d0e1354663feb218618385f` and
  inih archive SHA256
  `7d3524c6b92fda00e17a8ab527c99f4516c2fcf24becec5c89756c8b90d99f8c`.
  The packer comes from Project-Aloha/DualBootKernelPatcher
  (`2276163d066596cb0750e76cbfeec5c193b1779f`, local
  `aa819b0d1be76e83256f64cf77c0f6dfe4174eea`, plus later local non-base
  `PayloadFrom` aliases); it is not a clean upstream commit. inih is from
  <https://github.com/benhoyt/inih>; its exact commit was not recorded.
- `vendor/repack_boot_v4.py` SHA256
  `00cf5e4aeed664bb3827449abece176e52735c3a58f122448e66d27c6ba20468`
  matches the existing `devices/lenovo-tb323fu/boot/star-chart-build` copy.
- `vendor/avbtool.py` SHA256
  `e5a664a38db623da00f080219bc0ee60a640a9dc4a872803616fae4938ac749b`,
  carrying the AOSP MIT-style permission notice.

The vendored sources are not modified here. The packer is GPL-3.0-or-later
(DuoWoa/AlohaWoA Authors) and inih is BSD-3-Clause; both license texts ship in
`out/licenses` and under `/usr/share/licenses/armada-kshim-tools`.
