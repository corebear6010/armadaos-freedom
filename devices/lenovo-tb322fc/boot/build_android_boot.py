#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline TB322FC Android-only boot packaging driver (Linux, stdlib only).

Drives the external MBKP packer, the supplied UnpackTool, the narrow v4 boot
repacker and the official avbtool over a single base-only layout: one stock
Android Image plus the packer auto-added Attached Shim. Raw (uncompressed)
Android only -- no Linux / custom DTB / initrd / gzip path.

Offline verification only: no device access, no flashing and no reboot.
hardware_tested is always false for every source version, and offline_only is
always true. The .096 firmware is an offline-only candidate and is NOT claimed
to match the current .259 build; any later input that satisfies this exact raw
v4 format is accepted unchanged.

Manifest ABI source of truth:
  external MultiBootKernelPatcher inc/manifest.h (ManifestHeader 60B, ManifestEntry 56B)
The 5 u64 values after the manifest header are a BACKUP of the original stock
base-image header (code0+code1@0x00, image_size@0x10, res2@0x20, res3@0x28,
res4@0x30), NOT packed sizes or self-offsets. The patched raw ARM64 header
stores the real packed runtime size at 0x10, the manifest offset at 0x20, the
manifest size at 0x28 and the shim offset at 0x30.
"""

import argparse
import configparser
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True

BOOT_SIZE = 96 * 1024 * 1024
RUNTIME_SIZE = 4 * 1024 * 1024
ARM64_HDR_SIZE = 0x40
ARM64_MAGIC = b"ARM\x64"
QC_ABOOT_MAGIC = b"UNCOMPRESSED_IMG"
GZIP_MAGIC = b"\x1f\x8b"
BOOT_MAGIC = b"ANDROID!"
MANIFEST_MAGIC = 0x464D504B
MANIFEST_HEADER_SIZE = 60
MANIFEST_ENTRY_SIZE = 56
MANIFEST_ENTRY_ALIGN = 4
AVB_MAGIC = b"AVBf"
MF_TYPE_LINUX = 1
MF_TYPE_SHIM = 3
FLAG_BASE = 1
FLAG_EXEC = 4
FLAG_HAS_DTB = 8
FLAG_QC = 16
BL_RANGE = 0x8000000
DEFAULT_LABEL = "Image-00"
DEFAULT_INDEX = 0
TIMEOUT_MS = 8000
BASE_NAME = "原厂 Android"
SHIM_NAME = "Attached Shim"

# Fixed DT-probe diagnostic carrier (see repack_boot_v4.py). The boot header
# cmdline field is bytes [44:1580] (1536 bytes): NUL-terminated text + padding.
CMDLINE_START = 44
CMDLINE_END = 1580
CMDLINE_LEN = CMDLINE_END - CMDLINE_START
PROBE_VALUE = "pending" + "_" * 73          # 80 ASCII bytes
PROBE_PARAM_PROBE = "kshimreport.kshim_probe"
PROBE_PARAM_S0 = "kshimreport.kshim_s0"
PROBE_SUFFIX = (PROBE_PARAM_PROBE + "=" + PROBE_VALUE + " " +
                PROBE_PARAM_S0 + "=" + PROBE_VALUE).encode("ascii")
PROBE_CONFLICT_KEYS = frozenset((
    b"kshimreport.kshim_probe",
    b"kshimreport.kshim_s0",
    b"androidboot.kshim_probe",
    b"androidboot.kshim_s0",
))
PROBE_CAVEAT = (
    "DT-only diagnostic carrier: the ordinary kshimreport prefix is converted "
    "in-place by runtime into an equal-length androidboot prefix; real hardware "
    "report availability is unverified. It does not fix the boot menu or hardware."
)

# __file__ lives at <devices>/lenovo-tb322fc/boot/build_android_boot.py, so
# three dirname hops land on the repository "devices" directory (parents[2]).
DEFAULT_REPACKER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "lenovo-tb323fu", "boot", "star-chart-build", "repack_boot_v4.py")
DEFAULT_AVB_SIGNER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "..", "system_files", "usr", "libexec", "armada", "kshim-avb-sign.py")
AVB_ALGORITHM = "SHA256_RSA4096"
AVB_KEY_PUBLIC_SHA1 = "2597c218aae470a130f61162feaae70afd97f011"
AVB_RELEASE_STRING = "freedom-edl-direct"

STAGE = "init"
CHECKS = []
COMMANDS = []

OUT = None
OUT_OWNED = False
WORK = None
LOGS = None
TMP = None
ENV = None


class StageError(Exception):
    def __init__(self, stage, message):
        super().__init__(message)
        self.stage = stage
        self.message = message


def set_stage(stage):
    global STAGE
    STAGE = stage


def check(name, condition, detail=""):
    CHECKS.append({"check": name, "ok": bool(condition), "detail": detail})
    if not condition:
        raise StageError(STAGE, name + ((": " + detail) if detail else ""))


def le32(buf, offset):
    return struct.unpack_from("<I", buf, offset)[0]


def le64(buf, offset):
    return struct.unpack_from("<Q", buf, offset)[0]


def align_up(value, alignment):
    return (value + alignment - 1) // alignment * alignment


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_bytes(path, label):
    if not os.path.isfile(path):
        raise StageError(STAGE, "%s not found: %s" % (label, path))
    with open(path, "rb") as handle:
        return handle.read()


def run(cmd, name, stdout_path=None, expect_success=True):
    COMMANDS.append({"name": name, "argv": list(cmd)})
    err_log = os.path.join(LOGS, name + ".stderr")
    out_log = os.path.join(LOGS, name + ".stdout")
    if stdout_path is not None:
        with open(stdout_path, "wb") as fout, open(err_log, "wb") as ferr:
            proc = subprocess.run(cmd, stdout=fout, stderr=ferr, cwd=WORK, env=ENV)
    else:
        with open(out_log, "wb") as fout, open(err_log, "wb") as ferr:
            proc = subprocess.run(cmd, stdout=fout, stderr=ferr, cwd=WORK, env=ENV)
    if expect_success and proc.returncode != 0:
        raise StageError(
            STAGE, "command failed rc=%d: %s" % (proc.returncode, " ".join(cmd)))
    return proc.returncode, out_log, err_log


def stdout_text(name):
    with open(os.path.join(LOGS, name + ".stdout"), encoding="utf-8",
              errors="replace") as handle:
        return handle.read()


def parse_arm64(data, label):
    check(label + ":size>=64", len(data) >= ARM64_HDR_SIZE, "len=%d" % len(data))
    check(label + ":raw-not-qc",
          data[0:len(QC_ABOOT_MAGIC)] != QC_ABOOT_MAGIC,
          "QC aboot wrapper present")
    check(label + ":raw-not-gzip", data[0:2] != GZIP_MAGIC,
          "gzip stream present; raw Android Image required")
    check(label + ":magic", data[56:60] == ARM64_MAGIC, "bad ARM64 magic at 0x38")
    text_offset = le64(data, 8)
    image_size = le64(data, 16)
    check(label + ":image_size", image_size >= 64, "image_size=%d" % image_size)
    slot = max(len(data), image_size)
    return {"text_offset": text_offset, "image_size": image_size,
            "length": len(data), "effective_slot": slot}


def parse_manifest(buf, offset):
    magic, vmaj, vmin, count, default, timeout = struct.unpack_from(
        "<IHHIII", buf, offset)
    backup = struct.unpack_from("<QQQQQ", buf, offset + 20)
    entries = []
    cursor = offset + MANIFEST_HEADER_SIZE
    for _ in range(count):
        (e_off, e_size, e_type, e_flags, e_load, e_entry, e_dtb, e_align,
         e_copy, e_namelen) = struct.unpack_from("<QQIIQIIIQI", buf, cursor)
        name_bytes = buf[cursor + MANIFEST_ENTRY_SIZE:
                         cursor + MANIFEST_ENTRY_SIZE + e_namelen]
        record = align_up(MANIFEST_ENTRY_SIZE + e_namelen, MANIFEST_ENTRY_ALIGN)
        pad = buf[cursor + MANIFEST_ENTRY_SIZE + e_namelen: cursor + record]
        entries.append({
            "offset": e_off, "size": e_size, "type": e_type, "flags": e_flags,
            "load_address": e_load, "entry_offset": e_entry,
            "dtb_index": e_dtb, "alignment": e_align, "copy_size_max": e_copy,
            "name_len": e_namelen, "name_bytes": name_bytes, "pad": pad,
            "record_size": record,
        })
        cursor += record
    return {"magic": magic, "version_major": vmaj, "version_minor": vmin,
            "count": count, "default": default, "timeout": timeout,
            "backup": backup, "entries": entries, "end": cursor}


def parse_boot_header(data):
    return {
        "magic": data[0:8],
        "kernel_size": le32(data, 8),
        "ramdisk_size": le32(data, 12),
        "header_size": le32(data, 20),
        "version": le32(data, 40),
        "signature_size": le32(data, 1580),
    }


def payload(raw, entry):
    return raw[entry["offset"]:entry["offset"] + entry["size"]]


def is_branch(word):
    return (word & 0xfc000000) == 0x14000000


def build_probe_field(field):
    """Validate the original cmdline field and return (new_field, original_text).

    Diagnostic mode only. Mirrors repack_boot_v4.py: original text up to the
    first NUL is preserved verbatim and the fixed 206-byte suffix is appended
    with a single separator when the original text is nonempty.
    """
    check("dt-probe:field-len", len(field) == CMDLINE_LEN, "len=%d" % len(field))
    nul = field.find(b"\x00")
    check("dt-probe:nul-terminator", nul >= 0, "no NUL terminator in cmdline field")
    check("dt-probe:post-nul-zero", not any(field[nul + 1:]),
          "nonzero bytes after the first NUL")
    orig_text = bytes(field[:nul])
    for token in orig_text.split():
        key = token.split(b"=", 1)[0]
        check("dt-probe:no-conflict",
              key not in PROBE_CONFLICT_KEYS,
              "cmdline already defines %s" % key.decode("ascii", "replace"))
    new_text = (orig_text + b" " + PROBE_SUFFIX) if orig_text else PROBE_SUFFIX
    check("dt-probe:length", len(new_text) <= CMDLINE_LEN - 1,
          "resulting cmdline text %d exceeds %d bytes" % (len(new_text), CMDLINE_LEN - 1))
    new_field = new_text + b"\x00" + b"\x00" * (CMDLINE_LEN - len(new_text) - 1)
    return new_field, orig_text


def probe_report_slots():
    return {
        "slots": {
            PROBE_PARAM_PROBE: PROBE_VALUE,
            PROBE_PARAM_S0: PROBE_VALUE,
        },
        "value_bytes": 80,
        "allowed_header_changes": [[8, 12], [CMDLINE_START, CMDLINE_END]],
        "hardware_tested": False,
        "caveat": PROBE_CAVEAT,
    }


def main():
    global OUT, WORK, LOGS, TMP, ENV

    parser = argparse.ArgumentParser(
        description="Offline TB322FC Android-only boot packaging driver")
    parser.add_argument("--stock-boot", required=True)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--packer", required=True)
    parser.add_argument("--unpacker", required=True)
    parser.add_argument("--avbtool", required=True)
    parser.add_argument("--avb-key", required=True)
    parser.add_argument("--avb-signer", default=DEFAULT_AVB_SIGNER)
    parser.add_argument("--repacker", default=None)
    parser.add_argument("--out", required=True)
    parser.add_argument("--source-label", default="")
    parser.add_argument("--dt-probe-report-slots", action="store_true")
    args = parser.parse_args()

    # Resolve every input and tool to an absolute path BEFORE any cwd change.
    inputs = {
        "stock_boot": os.path.abspath(args.stock_boot),
        "runtime": os.path.abspath(args.runtime),
        "packer": os.path.abspath(args.packer),
        "unpacker": os.path.abspath(args.unpacker),
        "avbtool": os.path.abspath(args.avbtool),
        "avb_key": os.path.abspath(args.avb_key),
        "avb_signer": os.path.abspath(args.avb_signer),
        "repacker": os.path.abspath(args.repacker or DEFAULT_REPACKER),
    }
    OUT = os.path.abspath(args.out)

    try:
        return run_driver(inputs, args.source_label, args.dt_probe_report_slots)
    except StageError as error:
        stage, message = error.stage, error.message
    except Exception as error:  # unexpected failure still leaves evidence
        stage, message = STAGE, "%s: %s" % (type(error).__name__, error)

    failure = {
        "stage": stage,
        "error": message,
        "source_label": args.source_label,
        "offline_verified": False,
        "offline_only": True,
        "hardware_tested": False,
        "checks": CHECKS,
        "commands": COMMANDS,
    }
    if OUT_OWNED:
        try:
            with open(os.path.join(OUT, "failure.json"), "w",
                      encoding="utf-8") as handle:
                json.dump(failure, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
        except OSError:
            pass
    sys.stderr.write("FAILED at stage '%s': %s\n" % (stage, message))
    return 1


def args_out_raw():
    for index, value in enumerate(sys.argv):
        if value == "--out" and index + 1 < len(sys.argv):
            return sys.argv[index + 1]
    return OUT


def prepare_environment(inputs):
    global WORK, LOGS, TMP, ENV, OUT_OWNED

    set_stage("prepare")
    for key, path in inputs.items():
        check("input:%s" % key, os.path.isfile(path), path)
    check("out:not-symlink", not os.path.islink(args_out_raw()), "output is a symlink")
    if os.path.exists(OUT):
        check("out:is-dir", os.path.isdir(OUT), "exists and is not a directory")
        check("out:empty", not os.listdir(OUT), "existing directory is not empty")
    else:
        os.makedirs(OUT)

    real_out = os.path.realpath(OUT)
    for key, path in inputs.items():
        real_in = os.path.realpath(path)
        check("out:distinct:%s" % key, real_in != real_out, "output equals input")
        check("out:not-inside:%s" % key,
              not real_in.startswith(real_out + os.sep),
              "input %s lives inside --out" % key)

    OUT_OWNED = True
    WORK = os.path.join(OUT, "work")
    LOGS = os.path.join(OUT, "logs")
    TMP = os.path.join(OUT, "tmp")
    for path in (WORK, LOGS, TMP):
        os.makedirs(path)

    ENV = dict(os.environ)
    ENV["PYTHONDONTWRITEBYTECODE"] = "1"
    ENV["TMPDIR"] = TMP
    ENV["TEMP"] = TMP
    ENV["TMP"] = TMP
    tempfile.tempdir = TMP
    os.chdir(WORK)


def run_driver(inputs, source_label, dt_probe_report_slots=False):
    prepare_environment(inputs)

    set_stage("validate-inputs")
    stock_boot = read_bytes(inputs["stock_boot"], "stock boot")
    runtime = read_bytes(inputs["runtime"], "runtime shim")

    check("stock_boot:size", len(stock_boot) == BOOT_SIZE,
          "len=%d expected=%d" % (len(stock_boot), BOOT_SIZE))
    boot = parse_boot_header(stock_boot)
    check("stock_boot:magic", boot["magic"] == BOOT_MAGIC, repr(boot["magic"]))
    check("stock_boot:version", boot["version"] == 4, "version=%d" % boot["version"])
    check("stock_boot:header_size", boot["header_size"] == 1584,
          "header_size=%d" % boot["header_size"])
    check("stock_boot:ramdisk_size", boot["ramdisk_size"] == 0,
          "ramdisk_size=%d" % boot["ramdisk_size"])
    check("stock_boot:signature_size", boot["signature_size"] == 0,
          "signature_size=%d" % boot["signature_size"])
    kernel_size = boot["kernel_size"]
    check("stock_boot:kernel_size",
          kernel_size != 0 and 4096 + kernel_size <= len(stock_boot),
          "kernel_size=%d" % kernel_size)

    stock_image = stock_boot[4096:4096 + kernel_size]
    check("stock_image:len", len(stock_image) == kernel_size,
          "len=%d kernel_size=%d" % (len(stock_image), kernel_size))
    stock_info = parse_arm64(stock_image, "stock_image")

    check("runtime:size", len(runtime) == RUNTIME_SIZE,
          "len=%d expected=%d" % (len(runtime), RUNTIME_SIZE))

    # The stock image must carry an AVB footer for the reused narrow workflow.
    stock_has_footer = len(stock_boot) >= 64 and stock_boot[-64:-60] == AVB_MAGIC
    check("stock_boot:avb-footer", stock_has_footer,
          "stock boot image has no AVB footer")

    for key in ("packer", "unpacker", "avbtool", "repacker",
                "avb_key", "avb_signer"):
        check("tool:%s" % key, os.path.isfile(inputs[key]), inputs[key])

    # Source hashes for every supplied tool plus stock and runtime.
    source_digests = {key: sha256_file(path) for key, path in inputs.items()}
    source_sizes = {key: os.path.getsize(path) for key, path in inputs.items()}

    stock_path = os.path.join(WORK, "StockImage")
    with open(stock_path, "wb") as handle:
        handle.write(stock_image)

    set_stage("config")
    def config_safe(path, label):
        check("config:safe:" + label,
              not any(ch in path for ch in "\n\r#;"),
              "unsafe character in path: %r" % path)
        return path

    packed_kernel = os.path.join(OUT, "PackedKernel")
    config_path = os.path.join(WORK, "build.cfg")
    config_lines = [
        "[Pack]",
        "Shim=" + config_safe(inputs["runtime"], "shim"),
        "Output=" + config_safe(packed_kernel, "output"),
        "Default=" + DEFAULT_LABEL,
        "Timeout=%d" % TIMEOUT_MS,
        "",
        "[Image-00]",
        "Name=" + BASE_NAME,
        "Path=" + config_safe(stock_path, "image00"),
        "Type=Linux",
        "BaseImage=true",
        "Align=21",
        "",
        "[Manifest]",
        "Type=Manifest",
        "",
    ]
    with open(config_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(config_lines))

    set_stage("pack")
    run([inputs["packer"], config_path], "packer")
    raw = read_bytes(packed_kernel, "PackedKernel")

    set_stage("assert-pack")
    # Patched raw ARM64 header.
    check("raw:arm64-magic", raw[56:60] == ARM64_MAGIC, "bad magic")
    check("raw:image_size==len", le64(raw, 16) == len(raw),
          "image_size=%d len=%d" % (le64(raw, 16), len(raw)))
    check("raw:text_offset-intact", le64(raw, 8) == stock_info["text_offset"],
          "text_offset changed")
    check("raw:flags-intact", le64(raw, 0x18) == le64(stock_image, 0x18),
          "flags changed")
    manifest_offset = le64(raw, 32)
    manifest_size = le64(raw, 40)
    shim_offset = le64(raw, 48)
    check("raw:manifest-bounds",
          manifest_size >= MANIFEST_HEADER_SIZE
          and manifest_offset + manifest_size <= len(raw),
          "off=%d size=%d len=%d" % (manifest_offset, manifest_size, len(raw)))

    manifest = parse_manifest(raw, manifest_offset)
    check("manifest:magic", manifest["magic"] == MANIFEST_MAGIC, hex(manifest["magic"]))
    check("manifest:version",
          manifest["version_major"] == 0 and manifest["version_minor"] == 1,
          "%d.%d" % (manifest["version_major"], manifest["version_minor"]))
    check("manifest:count", manifest["count"] == 2, "%d" % manifest["count"])
    check("manifest:default", manifest["default"] == DEFAULT_INDEX,
          "%d" % manifest["default"])
    check("manifest:timeout", manifest["timeout"] == TIMEOUT_MS,
          "%d" % manifest["timeout"])

    expected_backup = (
        le64(stock_image, 0),
        le64(stock_image, 16),
        le64(stock_image, 32),
        le64(stock_image, 40),
        le64(stock_image, 48),
    )
    check("manifest:backup", manifest["backup"] == expected_backup,
          "backup=%r expected=%r" % (manifest["backup"], expected_backup))

    entries = manifest["entries"]
    check("manifest:end", manifest["end"] == manifest_offset + manifest_size,
          "entry table does not fill manifest")

    for index, name in ((0, BASE_NAME), (1, SHIM_NAME)):
        entry = entries[index]
        check("entry%d:name" % index,
              entry["name_bytes"].decode("utf-8") == name,
              entry["name_bytes"].decode("utf-8", "replace"))
        check("entry%d:name_len" % index,
              entry["name_len"] == len(name.encode("utf-8")), "%d" % entry["name_len"])
        check("entry%d:padding" % index,
              entry["pad"] == b"\x00" * len(entry["pad"]), "non-zero record padding")
        check("entry%d:load" % index, entry["load_address"] == 0,
              "%d" % entry["load_address"])
        check("entry%d:copy_size_max" % index, entry["copy_size_max"] == 0,
              "%d" % entry["copy_size_max"])

    # Entry 0: original Android base image.
    base = entries[0]
    check("base:type", base["type"] == MF_TYPE_LINUX, "%d" % base["type"])
    check("base:flags", base["flags"] == (FLAG_BASE | FLAG_EXEC), "%d" % base["flags"])
    check("base:align", base["alignment"] == 21, "%d" % base["alignment"])
    check("base:offset", base["offset"] == 0, "%d" % base["offset"])
    check("base:size", base["size"] == len(stock_image),
          "%d expected=%d" % (base["size"], len(stock_image)))
    check("base:no-has-dtb", (base["flags"] & FLAG_HAS_DTB) == 0, "HAS_DTB set")
    check("base:no-qc", (base["flags"] & FLAG_QC) == 0, "QC flag set")
    check("base:entry_offset", base["entry_offset"] == 0, "%d" % base["entry_offset"])
    check("base:dtb_index", base["dtb_index"] == 0, "%d" % base["dtb_index"])

    # Entry 1: Attached Shim (auto-added by the packer).
    shim = entries[1]
    check("shim:type", shim["type"] == MF_TYPE_SHIM, "%d" % shim["type"])
    check("shim:flags", shim["flags"] == FLAG_EXEC, "%d" % shim["flags"])
    check("shim:align", shim["alignment"] == 2, "%d" % shim["alignment"])
    check("shim:offset==header", shim["offset"] == shim_offset,
          "entry=%d header=%d" % (shim["offset"], shim_offset))
    check("shim:size", shim["size"] == RUNTIME_SIZE, "%d" % shim["size"])
    check("shim:entry_offset", shim["entry_offset"] == 0, "%d" % shim["entry_offset"])
    check("shim:dtb_index", shim["dtb_index"] == 0, "%d" % shim["dtb_index"])
    check("shim:payload==runtime", payload(raw, shim) == runtime,
          "shim payload differs from runtime input")

    # Restore the backed-up header slots in the copied base payload and require
    # whole-image byte equality (not just a hash or the first 64 bytes).
    restored = bytearray(payload(raw, base))
    for slot, offset in enumerate((0, 16, 32, 40, 48)):
        struct.pack_into("<Q", restored, offset, manifest["backup"][slot])
    check("base:payload==stock_image", bytes(restored) == stock_image,
          "base payload differs from stock image")

    # Memory slot layout: base effective slot, shim and manifest in order.
    base_slot = max(len(stock_image), stock_info["image_size"])
    S = align_up(base_slot, 4)
    check("layout:shim-offset", shim_offset == S,
          "shim=%d expected=%d" % (shim_offset, S))
    check("layout:shim-align", S % 4 == 0, "%d" % S)
    check("layout:shim-in-bl-range", S < BL_RANGE, "%d" % S)
    expected_manifest = align_up(S + RUNTIME_SIZE, 8)
    check("layout:manifest-offset", manifest_offset == expected_manifest,
          "manifest=%d expected=%d" % (manifest_offset, expected_manifest))
    check("layout:ends-at-manifest", manifest_offset + manifest_size == len(raw),
          "trailing bytes after manifest")

    regions = [
        ("base", 0, S, len(stock_image)),
        ("shim", shim_offset, shim_offset + RUNTIME_SIZE, RUNTIME_SIZE),
        ("manifest", manifest_offset, manifest_offset + manifest_size,
         manifest_size),
    ]
    previous_end = 0
    for name, start, end, used in regions:
        check("layout:%s:ordered" % name, start >= previous_end,
              "start=%d previous_end=%d" % (start, previous_end))
        check("layout:%s:in-file" % name, end <= len(raw),
              "end=%d len=%d" % (end, len(raw)))
        if start > previous_end:
            check("layout:%s:gap-zero" % name,
                  raw[previous_end:start] == b"\x00" * (start - previous_end),
                  "non-zero gap before %s" % name)
        check("layout:%s:tail-zero" % name,
              raw[start + used:end] == b"\x00" * (end - start - used),
              "non-zero tail in %s" % name)
        previous_end = end

    # Entry patch verifier: packer redirects the base head to the shim.
    w0 = le32(raw, 0)
    w1 = le32(raw, 4)
    expected_w0 = 0x94000000 | ((S >> 2) & 0x03ffffff)
    check("patch:w0", w0 == expected_w0,
          "w0=%08x expected=%08x" % (w0, expected_w0))
    imm = w0 & 0x03ffffff
    if imm & 0x02000000:
        imm -= 0x04000000
    check("patch:w0-target", imm * 4 == S, "target=%d S=%d" % (imm * 4, S))
    orig_w0 = le32(stock_image, 0)
    orig_w1 = le32(stock_image, 4)
    if is_branch(orig_w0) and not is_branch(orig_w1):
        expected_w1 = 0x14000000 | ((orig_w0 - 1) & 0x03ffffff)
    else:
        check("patch:orig-w1-is-branch", is_branch(orig_w1), "%08x" % orig_w1)
        expected_w1 = orig_w1
    check("patch:w1", w1 == expected_w1, "w1=%08x expected=%08x" % (w1, expected_w1))

    set_stage("unpack")
    unpacked_dir = os.path.join(OUT, "unpacked")
    run([inputs["unpacker"], packed_kernel, unpacked_dir], "unpacker")

    set_stage("assert-unpack")
    restored_base = read_bytes(os.path.join(unpacked_dir, "BaseImage"), "BaseImage")
    check("unpack:base-full", restored_base == stock_image,
          "restored BaseImage differs from stock image")
    restored_shim = read_bytes(os.path.join(unpacked_dir, "Shim.bin"), "Shim.bin")
    check("unpack:shim", restored_shim == runtime, "restored Shim differs")

    # Round-trip: repack the emitted config and require byte identity.
    emitted = configparser.ConfigParser()
    emitted.optionxform = str
    emitted.read(os.path.join(unpacked_dir, "config.cfg"), encoding="utf-8")
    repacked_name = "RepackedKernel"
    if emitted.has_section("Pack") and emitted["Pack"].get("Output"):
        repacked_name = os.path.basename(emitted["Pack"]["Output"])
    run([inputs["packer"], os.path.join(unpacked_dir, "config.cfg")], "repacker-packer")
    repacked = read_bytes(os.path.join(unpacked_dir, repacked_name), "repacked kernel")
    check("repack:byte-identical", repacked == raw,
          "repacked output differs from original packed kernel")

    set_stage("repack")
    final_boot = os.path.join(OUT, "boot.img")
    repack_report = os.path.join(OUT, "repack_report.json")
    repack_cmd = [sys.executable, "-B", inputs["repacker"],
                  "--original", inputs["stock_boot"],
                  "--kernel", packed_kernel,
                  "--output", final_boot,
                  "--report", repack_report,
                  "--avbtool", inputs["avbtool"]]
    if dt_probe_report_slots:
        repack_cmd.append("--dt-probe-report-slots")
    run(repack_cmd, "repacker")

    set_stage("avb-sign")
    signing_report = os.path.join(OUT, "avb-signing.json")
    run([sys.executable, "-B", inputs["avb_signer"],
         "--image", final_boot,
         "--stock-boot", inputs["stock_boot"],
         "--avbtool", inputs["avbtool"],
         "--key", inputs["avb_key"],
         "--report", signing_report], "avb-signing")
    with open(signing_report, encoding="utf-8") as handle:
        signing = json.load(handle)

    set_stage("assert-boot")
    final = read_bytes(final_boot, "boot.img")
    final_boot_header = parse_boot_header(final)
    check("boot:size", len(final) == BOOT_SIZE,
          "len=%d expected=%d" % (len(final), BOOT_SIZE))
    check("boot:magic", final[0:8] == BOOT_MAGIC, repr(final[0:8]))
    check("boot:version", final_boot_header["version"] == 4,
          "version=%d" % final_boot_header["version"])
    check("boot:ramdisk_size", final_boot_header["ramdisk_size"] == 0,
          "ramdisk_size=%d" % final_boot_header["ramdisk_size"])
    check("boot:signature_size", final_boot_header["signature_size"] == 0,
          "signature_size=%d" % final_boot_header["signature_size"])
    check("boot:header-prefix", final[0:8] == stock_boot[0:8],
          "first 8 bytes changed")
    probe_field = None
    if dt_probe_report_slots:
        probe_field, probe_orig_text = build_probe_field(
            stock_boot[CMDLINE_START:CMDLINE_END])
        check("boot:header-body-lo",
              final[12:CMDLINE_START] == stock_boot[12:CMDLINE_START],
              "header [12:44] changed")
        check("boot:header-body-hi",
              final[CMDLINE_END:4096] == stock_boot[CMDLINE_END:4096],
              "header [1580:4096] changed")
        check("boot:cmdline-field",
              final[CMDLINE_START:CMDLINE_END] == probe_field,
              "cmdline field differs from the diagnostic field")
        if probe_orig_text:
            check("boot:cmdline-prefix",
                  final[CMDLINE_START:CMDLINE_START + len(probe_orig_text)] == probe_orig_text,
                  "cmdline lost the original prefix")
    else:
        check("boot:header-body", final[12:4096] == stock_boot[12:4096],
              "header differs beyond kernel_size field")
    final_kernel_size = final_boot_header["kernel_size"]
    check("boot:kernel_size", final_kernel_size == len(raw),
          "kernel_size=%d raw len=%d" % (final_kernel_size, len(raw)))
    check("boot:kernel-bytes", final[4096:4096 + final_kernel_size] == raw,
          "embedded kernel is not the raw packed kernel")

    footer = final[-64:]
    avb_magic, avb_major, avb_minor, ois, vbo, vbs, _reserved = struct.unpack(
        ">4sIIQQQ28s", footer)
    check("avb:footer-magic", avb_magic == AVB_MAGIC, repr(avb_magic))
    check("avb:footer-major", avb_major == 1, "%d" % avb_major)
    expected_ois = 4096 + align_up(len(raw), 4096)
    check("avb:original_image_size", ois == expected_ois,
          "ois=%d expected=%d" % (ois, expected_ois))
    check("avb:vbmeta-bounds",
          vbs > 0 and vbo >= ois and vbo + vbs <= len(final) - 64,
          "vbo=%d vbs=%d" % (vbo, vbs))
    check("boot:kernel-padding",
          final[4096 + final_kernel_size:ois] == b"\x00" * (ois - 4096 - final_kernel_size),
          "non-zero kernel padding")

    _sf = struct.unpack(">4sIIQQQ28s", stock_boot[-64:])
    stock_orig_size, stock_vbo, stock_vbs = _sf[3], _sf[4], _sf[5]
    check("avb:stock-vbmeta-size", stock_vbs > 0, "stock footer vbmeta_size is zero")
    check("avb:stock-vbmeta-bounds",
          stock_vbo >= stock_orig_size and stock_vbo + stock_vbs <= len(stock_boot) - 64,
          "stock footer vbmeta region out of bounds")
    check("avb:fresh-metadata",
          final[vbo:vbo + vbs] != stock_boot[stock_vbo:stock_vbo + stock_vbs],
          "vbmeta block matches stale stock value")

    set_stage("avb")
    verify_log = os.path.join(LOGS, "avbtool_verify_image.stdout")
    run([sys.executable, "-B", inputs["avbtool"], "verify_image", "--image", final_boot,
         "--key", inputs["avb_key"]],
        "avbtool_verify_image")
    run([sys.executable, "-B", inputs["avbtool"], "info_image", "--image", final_boot],
        "avbtool_info_image")
    info_log = os.path.join(LOGS, "avbtool_info_image.stdout")
    algorithm_match = re.search(r"^\s*Algorithm:\s*(\S+)", stdout_text("avbtool_info_image"), re.M)
    check("avb:algorithm-line", algorithm_match is not None,
          "no anchored Algorithm line in info_image output")
    check("avb:algorithm-signed", algorithm_match.group(1) == AVB_ALGORITHM,
          "algorithm=%s" % algorithm_match.group(1))
    info_text = stdout_text("avbtool_info_image")
    check("avb:key-sha1", AVB_KEY_PUBLIC_SHA1 in info_text,
          "AVB public key SHA1 is not the pinned AOSP test key")
    check("avb:release-string", AVB_RELEASE_STRING in info_text,
          "AVB release string is not %s" % AVB_RELEASE_STRING)
    check("avb:signing-report",
          signing.get("avb", {}).get("signature_verified") is True,
          "AVB signer did not report verified output")

    set_stage("recheck-sources")
    for key, path in inputs.items():
        check("source-unchanged:%s" % key,
              sha256_file(path) == source_digests[key], "input changed: %s" % path)

    set_stage("report")
    report_entries = []
    for index, (name, role) in enumerate(((BASE_NAME, "base"), (SHIM_NAME, "shim"))):
        entry = entries[index]
        report_entries.append({
            "index": index,
            "role": role,
            "name": name,
            "type": entry["type"],
            "flags": entry["flags"],
            "offset": entry["offset"],
            "size": entry["size"],
            "entry_offset": entry["entry_offset"],
            "dtb_index": entry["dtb_index"],
            "alignment": entry["alignment"],
            "sha256": sha256_bytes(payload(raw, entry)),
        })

    report = {
        "offline_verified": True,
        "offline_only": True,
        "hardware_tested": False,
        "source_label": source_label,
        "inputs": {
            key: {"path": inputs[key], "length": source_sizes[key],
                  "sha256": source_digests[key]}
            for key in sorted(inputs) if key != "avb_key"
        },
        "stock_image": {
            "length": len(stock_image),
            "sha256": sha256_bytes(stock_image),
            "text_offset": stock_info["text_offset"],
            "image_size": stock_info["image_size"],
            "effective_slot": stock_info["effective_slot"],
        },
        "runtime": {
            "length": len(runtime),
            "sha256": source_digests["runtime"],
        },
        "config_path": config_path,
        "default_label": DEFAULT_LABEL,
        "default_index": DEFAULT_INDEX,
        "timeout_ms": TIMEOUT_MS,
        "uncompressed": True,
        "manifest": {
            "magic": hex(manifest["magic"]),
            "version": "%d.%d" % (manifest["version_major"], manifest["version_minor"]),
            "count": manifest["count"],
            "offset": manifest_offset,
            "size": manifest_size,
            "backup": list(manifest["backup"]),
        },
        "entries": report_entries,
        "layout": [
            {"name": name, "start": start, "end": end, "used": used}
            for name, start, end, used in regions
        ],
        "outputs": {
            "packed_kernel": {"path": packed_kernel, "length": len(raw),
                              "sha256": sha256_bytes(raw)},
            "repacked_kernel": {"path": os.path.join(unpacked_dir, repacked_name),
                                "sha256": sha256_bytes(repacked)},
            "final_boot": {"path": final_boot, "length": len(final),
                           "sha256": sha256_bytes(final)},
            "repack_report": repack_report,
            "avb_signing_report": signing_report,
        },
        "boot": {
            "kernel_size": final_kernel_size,
            "kernel_offset": 4096,
            "avb": {
                "algorithm": algorithm_match.group(1),
                "signed": True,
                "key_public_sha1": AVB_KEY_PUBLIC_SHA1,
                "release_string": AVB_RELEASE_STRING,
                "original_image_size": ois,
                "vbmeta_offset": vbo,
                "vbmeta_size": vbs,
                "signing": signing,
                "verify_image_log": verify_log,
                "info_image_log": info_log,
            },
        },
        "caveats": [
            "Raw uncompressed Android only; no Linux, custom DTB, initrd or gzip path.",
            "Offline byte-level round-trip plus official avbtool success is NOT "
            "hardware validation.",
            "The output is force-signed with the pinned AOSP SHA256_RSA4096 "
            "test key; the original Lenovo OEM signature is not preserved.",
            ".096 is an offline-only candidate and is not claimed to match the "
            "current .259 build; a current .259 candidate requires a matching "
            "original boot backup.",
            "Android boot, the visual boot menu, screen output and key handling are "
            "NOT validated.",
        ],
        "commands": COMMANDS,
        "checks": CHECKS,
    }

    if dt_probe_report_slots:
        report["dt_probe_report_slots"] = probe_report_slots()
        report["caveats"].append(PROBE_CAVEAT)

    with open(os.path.join(OUT, "report.json"), "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    sys.stdout.write("offline verification PASSED; report at %s\n"
                     % os.path.join(OUT, "report.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
