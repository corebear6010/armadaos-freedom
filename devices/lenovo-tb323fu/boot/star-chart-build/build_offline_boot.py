#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline TB323FU multi-boot image packaging driver (Linux, stdlib only).

Drives the external MBKP packer, UnpackTool, the candidate boot repacker,
gzip and the official avbtool over one fixed multi-boot layout and asserts
byte-level round-trip correctness. Offline verification only: no device
access, no flashing, no reboot.

Source of truth for the manifest ABI:
  packaging/packer/inc/manifest.h  (ManifestHeader 60B, ManifestEntry 56B)
The 5 u64 values after the manifest header are a BACKUP of the original
stock base-image header (instructions, image_size@0x10, @0x20, @0x28,
@0x30), NOT packed-image sizes or self-offsets. The patched raw ARM64
header stores the real packed runtime size at 0x10, manifest offset at
0x20, manifest size at 0x28 and shim offset at 0x30.
"""

import argparse
import configparser
import gzip
import hashlib
import importlib.util
import json
import os
import re
import shutil
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
BOOT_MAGIC = b"ANDROID!"
MANIFEST_MAGIC = 0x464D504B
MANIFEST_HEADER_SIZE = 60
MANIFEST_ENTRY_SIZE = 56
MANIFEST_ENTRY_ALIGN = 4
AVB_MAGIC = b"AVBf"

MF_TYPE_LINUX = 1
MF_TYPE_SHIM = 3
MF_TYPE_BLOB = 4
MF_TYPE_DTB = 5
FLAG_BASE = 1
FLAG_EXEC = 4
FLAG_HAS_DTB = 8
FLAG_QC = 16

# name, type, flags, alignment, role
EXPECTED_ENTRIES = [
    ("原厂 Android", MF_TYPE_LINUX, FLAG_BASE | FLAG_EXEC, 21, "base"),
    ("机身 ArmadaOS", MF_TYPE_LINUX, FLAG_EXEC | FLAG_HAS_DTB, 21, "linux"),
    ("TF 卡 ArmadaOS", MF_TYPE_LINUX, FLAG_EXEC | FLAG_HAS_DTB, 21, "alias"),
    ("Linux 诊断模式", MF_TYPE_LINUX, FLAG_EXEC | FLAG_HAS_DTB, 21, "alias"),
    ("TB323FU Internal DTB", MF_TYPE_DTB, 0, 3, "dtb"),
    ("TB323FU TF DTB", MF_TYPE_DTB, 0, 3, "dtb"),
    ("TB323FU Diagnostic DTB", MF_TYPE_DTB, 0, 3, "dtb"),
    ("Attached Shim", MF_TYPE_SHIM, FLAG_EXEC, 2, "shim"),
]

# Optional external-initramfs extension: preserve entries 0..6 verbatim, add a
# shared blob at entry 7 and move the auto-attached shim to entry 8.
INITRD_ENTRY_NAME = "External Initramfs"
INITRD_ENTRY_INDEX = 7
INITRD_DT_PROPERTY = "kshim,initrd-entry"
STALE_INITRD_PROPERTIES = ("linux,initrd-start", "linux,initrd-end")
EXPECTED_INITRD_ENTRY = (INITRD_ENTRY_NAME, MF_TYPE_BLOB, 0, 16, "initrd")
EXPECTED_ENTRIES_EXTERNAL = (EXPECTED_ENTRIES[:INITRD_ENTRY_INDEX]
                             + [EXPECTED_INITRD_ENTRY]
                             + [EXPECTED_ENTRIES[INITRD_ENTRY_INDEX]])

# --default-entry choice -> (manifest default label, manifest default index).
# Indices 0..3 are the four Linux-menu entries of the fixed layout.
DEFAULT_ENTRY_TARGETS = {
    "android": ("Image-00", 0),
    "internal": ("Image-01", 1),
    "tf": ("Image-02", 2),
    "diagnostic": ("Image-03", 3),
}
DEFAULT_ENTRY = "internal"
TIMEOUT_MS = 8000

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


def require_tool(name):
    path = shutil.which(name)
    check("tool:%s" % name, path is not None, "not found in PATH")
    return path


def parse_arm64(data, label):
    check(label + ":size>=64", len(data) >= ARM64_HDR_SIZE, "len=%d" % len(data))
    check(label + ":raw-not-qc",
          data[0:len(QC_ABOOT_MAGIC)] != QC_ABOOT_MAGIC,
          "QC aboot wrapper present")
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


def diff_lines(canonical, candidate):
    return [(a, b) for a, b in zip(canonical, candidate) if a != b]


def payload(raw, entry):
    return raw[entry["offset"]:entry["offset"] + entry["size"]]


def expected_entries(external_initrd):
    return EXPECTED_ENTRIES_EXTERNAL if external_initrd else EXPECTED_ENTRIES


def external_image_section(initrd_path):
    """Config lines for the shared external-initrd blob.

    The packer's accepted type string is ``Blob`` (parse_image_type in
    src/pack_config.c), not ``BlobPath``. Alignment exponent 16 (64 KiB)
    keeps the page-rounded initrd away from the preceding selected DTB for
    every supported 4/16/64 KiB page size, and is conservative on unknown
    page sizes.
    """
    return [
        "[Image-%02d]" % INITRD_ENTRY_INDEX,
        "Name=" + INITRD_ENTRY_NAME,
        "Path=" + initrd_path,
        "Type=Blob",
        "BaseImage=false",
        "Align=16",
        "",
    ]


def blob_entry_errors(entry, blob):
    """Return every reason the manifest blob entry cannot be consumed."""
    errors = []
    if entry["type"] != MF_TYPE_BLOB:
        errors.append("type=%d" % entry["type"])
    if entry["flags"] != 0:
        errors.append("flags=%d" % entry["flags"])
    if entry["alignment"] != EXPECTED_INITRD_ENTRY[3]:
        errors.append("alignment=%d" % entry["alignment"])
    if entry["entry_offset"] != 0:
        errors.append("entry_offset=%d" % entry["entry_offset"])
    if entry["dtb_index"] != 0:
        errors.append("dtb_index=%d" % entry["dtb_index"])
    if entry["load_address"] != 0:
        errors.append("load_address=%d" % entry["load_address"])
    if entry["copy_size_max"] != 0:
        errors.append("copy_size_max=%d" % entry["copy_size_max"])
    if len(blob) == 0:
        errors.append("empty initrd")
    if entry["size"] != len(blob):
        errors.append("size=%d expected=%d" % (entry["size"], len(blob)))
    return errors


def without_initrd_properties(lines):
    """Drop exactly the DT properties this tool rewrites, keeping all else."""
    filtered = []
    for line in lines:
        if INITRD_DT_PROPERTY in line:
            continue
        if any(prop in line for prop in STALE_INITRD_PROPERTIES):
            continue
        filtered.append(line)
    return filtered


def main():
    global OUT, WORK, LOGS, TMP, ENV

    parser = argparse.ArgumentParser(
        description="Offline TB323FU multi-boot packaging driver")
    parser.add_argument("--stock-boot", required=True)
    parser.add_argument("--stock-image", required=True)
    parser.add_argument("--linux-image", required=True)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--tf-dtb", required=True)
    parser.add_argument("--internal-dtb", required=True)
    parser.add_argument("--initrd", default=None,
                        help="external initramfs blob; enables the 9-entry layout")
    parser.add_argument("--packer", required=True)
    parser.add_argument("--unpacker", required=True)
    parser.add_argument("--repacker", required=True)
    parser.add_argument("--avbtool", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--default-entry", choices=list(DEFAULT_ENTRY_TARGETS),
                        default=DEFAULT_ENTRY,
                        help="manifest default menu entry (default: %s)"
                        % DEFAULT_ENTRY)
    args = parser.parse_args()

    # Resolve every input and tool to an absolute path BEFORE any cwd change.
    inputs = {
        "stock_boot": os.path.abspath(args.stock_boot),
        "stock_image": os.path.abspath(args.stock_image),
        "linux_image": os.path.abspath(args.linux_image),
        "runtime": os.path.abspath(args.runtime),
        "tf_dtb": os.path.abspath(args.tf_dtb),
        "internal_dtb": os.path.abspath(args.internal_dtb),
        "packer": os.path.abspath(args.packer),
        "unpacker": os.path.abspath(args.unpacker),
        "repacker": os.path.abspath(args.repacker),
        "avbtool": os.path.abspath(args.avbtool),
    }
    if args.initrd:
        inputs["initrd"] = os.path.abspath(args.initrd)
    OUT = os.path.abspath(args.out)

    try:
        return run_driver(inputs, args.default_entry)
    except StageError as error:
        stage, message = error.stage, error.message
    except Exception as error:  # unexpected failure still leaves evidence
        stage, message = STAGE, "%s: %s" % (type(error).__name__, error)

    failure = {
        "stage": stage,
        "error": message,
        "hardware_validated": False,
        "offline_verified": False,
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


def args_out_raw():
    # OUT is already resolved; re-read the original argv value for symlink check.
    for index, value in enumerate(sys.argv):
        if value == "--out" and index + 1 < len(sys.argv):
            return sys.argv[index + 1]
    return OUT


def verify_boot_signatures(stock_boot_path, final_boot, repacker, avbtool):
    final = read_bytes(final_boot, "signed boot")
    final_kernel_size = parse_boot_header(final)["kernel_size"]
    _, _, _, ois, _, _, _ = struct.unpack(">4sIIQQQ28s", final[-64:])
    set_stage("avb")
    spec = importlib.util.spec_from_file_location("tb323fu_signed_repacker", repacker)
    signer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(signer)
    signature_verification = signer.verify_signed_metadata(
        stock_boot_path, final_boot, avbtool)
    verify_log = os.path.join(LOGS, "signed_metadata_verification.json")
    with open(verify_log, "w", encoding="utf-8") as handle:
        json.dump(signature_verification, handle, indent=2)
        handle.write("\n")
    kernel_end = 4096 + align_up(final_kernel_size, 4096)
    expected_ois = kernel_end + signature_verification["gki_signature_bytes"]
    check("avb:original_image_size", ois == expected_ois,
          "ois=%d expected=%d" % (ois, expected_ois))
    check("boot:kernel-padding",
          not any(final[4096 + final_kernel_size:kernel_end]),
          "non-zero kernel padding")
    check("avb:signed-metadata", signature_verification["signed_metadata_preserved"] and
          signature_verification["signature_valid"], "signed metadata verification failed")
    run([sys.executable, "-B", avbtool, "info_image", "--image", final_boot],
        "avbtool_info_image")
    info_log = os.path.join(LOGS, "avbtool_info_image.stdout")
    with open(info_log, encoding="utf-8", errors="replace") as handle:
        info_text = handle.read()
    algorithm_match = re.search(r"^\s*Algorithm:\s*(\S+)", info_text, re.M)
    check("avb:algorithm-line", algorithm_match is not None,
          "no anchored Algorithm line in info_image output")
    check("avb:original-signed-algorithm",
          algorithm_match.group(1) == signature_verification["metadata_algorithm"] and
          algorithm_match.group(1) != "NONE", "signed algorithm changed")

    return signature_verification, verify_log, info_log


def run_driver(inputs, default_entry=DEFAULT_ENTRY):
    prepare_environment(inputs)
    default_label, default_index = DEFAULT_ENTRY_TARGETS[default_entry]

    set_stage("validate-inputs")
    stock_boot = read_bytes(inputs["stock_boot"], "stock boot")
    stock_image = read_bytes(inputs["stock_image"], "stock image")
    linux_image = read_bytes(inputs["linux_image"], "linux image")
    runtime = read_bytes(inputs["runtime"], "runtime shim")
    tf_src = read_bytes(inputs["tf_dtb"], "TF dtb")
    internal_src = read_bytes(inputs["internal_dtb"], "internal dtb")
    external = "initrd" in inputs
    initrd = (read_bytes(inputs["initrd"], "external initrd")
              if external else None)
    if external:
        check("initrd:size", len(initrd) > 0, "empty initrd file")

    check("stock_boot:size", len(stock_boot) == BOOT_SIZE,
          "len=%d expected=%d" % (len(stock_boot), BOOT_SIZE))
    boot = parse_boot_header(stock_boot)
    check("stock_boot:magic", boot["magic"] == BOOT_MAGIC, repr(boot["magic"]))
    check("stock_boot:version", boot["version"] == 4, "version=%d" % boot["version"])
    check("stock_boot:header_size", boot["header_size"] == 1584,
          "header_size=%d" % boot["header_size"])
    check("stock_boot:ramdisk_size", boot["ramdisk_size"] == 0,
          "ramdisk_size=%d" % boot["ramdisk_size"])
    check("stock_boot:signature_size", boot["signature_size"] <=
          len(stock_boot) - 4096 - align_up(boot["kernel_size"], 4096),
          "GKI signature_size exceeds the partition")
    kernel_size = boot["kernel_size"]
    check("stock_boot:kernel_size",
          kernel_size != 0 and 4096 + kernel_size <= len(stock_boot),
          "kernel_size=%d" % kernel_size)
    check("stock_boot:kernel==stock_image",
          stock_boot[4096:4096 + kernel_size] == stock_image,
          "embedded kernel differs from --stock-image")
    check("stock_boot:kernel_len", len(stock_image) == kernel_size,
          "stock image len=%d kernel_size=%d" % (len(stock_image), kernel_size))

    stock_info = parse_arm64(stock_image, "stock_image")
    linux_info = parse_arm64(linux_image, "linux_image")
    check("runtime:size", len(runtime) == RUNTIME_SIZE,
          "len=%d expected=%d" % (len(runtime), RUNTIME_SIZE))

    # Source DTB digests, re-checked at the very end to prove no mutation.
    source_digests = {
        "stock_boot": sha256_bytes(stock_boot),
        "stock_image": sha256_bytes(stock_image),
        "linux_image": sha256_bytes(linux_image),
        "runtime": sha256_bytes(runtime),
        "tf_dtb": sha256_bytes(tf_src),
        "internal_dtb": sha256_bytes(internal_src),
    }
    if external:
        source_digests["initrd"] = sha256_bytes(initrd)

    set_stage("dtb")
    require_tool("fdtput")
    require_tool("fdtget")
    require_tool("dtc")
    require_tool("gzip")

    dtb_internal = os.path.join(WORK, "dtb_internal.dtb")
    dtb_tf = os.path.join(WORK, "dtb_tf.dtb")
    dtb_diag = os.path.join(WORK, "dtb_diag.dtb")
    dtb_check = os.path.join(WORK, "dtb_check.dtb")
    shutil.copyfile(inputs["internal_dtb"], dtb_internal)
    shutil.copyfile(inputs["tf_dtb"], dtb_tf)
    shutil.copyfile(inputs["internal_dtb"], dtb_diag)

    def fdtget_stage(dtb):
        _, out_log, _ = run(["fdtget", "-t", "s", dtb, "/chosen",
                             "armada,boot-stage"], "fdtget_" + os.path.basename(dtb))
        with open(out_log, "rb") as handle:
            return handle.read().decode("utf-8", "replace").strip()

    def fdtget_text(dtb, node, prop, tag):
        rc, out_log, _ = run(["fdtget", "-t", "s", dtb, node, prop], tag,
                             expect_success=False)
        if rc != 0:
            return None
        with open(out_log, "rb") as handle:
            return handle.read().decode("utf-8", "replace").strip()

    def fdtget_u32(dtb, node, prop, tag):
        rc, out_log, _ = run(["fdtget", "-t", "u", dtb, node, prop], tag,
                             expect_success=False)
        if rc != 0:
            return None
        with open(out_log, "rb") as handle:
            text = handle.read().decode("utf-8", "replace").strip()
        return int(text) if text.isdigit() else None

    def require_baldur_dts(dtb, label):
        bootargs = fdtget_text(dtb, "/chosen", "bootargs",
                               "fdtget_bootargs_" + label)
        check("dtb:%s:chosen-bootargs" % label, bootargs is not None,
              "missing /chosen bootargs")
        bootargs_tokens = bootargs.split()
        check("dtb:%s:bootargs-baldur-end" % label,
              "baldur.end=hold" in bootargs_tokens,
              "bootargs=%r" % bootargs)
        check("dtb:%s:bootargs-cluster-off" % label,
              "cpuidle_psci_domain.allow_cluster_off=0" in bootargs_tokens,
              "bootargs=%r" % bootargs)
        check("dtb:%s:bootargs-fixed-planes" % label,
              "msm.dpu_use_virtual_planes=0" in bootargs_tokens,
              "bootargs=%r" % bootargs)
        compatible = fdtget_text(dtb, "/", "compatible",
                                 "fdtget_compatible_" + label)
        check("dtb:%s:compatible" % label, compatible is not None,
              "missing root compatible")
        compatibles = compatible.split()
        check("dtb:%s:compatible-lenovo-baldur" % label,
              "lenovo,baldur" in compatibles, repr(compatible))
        check("dtb:%s:compatible-qcom-kaanapali" % label,
              "qcom,kaanapali" in compatibles, repr(compatible))

    def apply_initrd_handoff(dtb, label):
        # A missing stale property is expected; the derived-DTS comparison
        # below proves that none survives.
        for prop in STALE_INITRD_PROPERTIES:
            run(["fdtput", "-d", dtb, "/chosen", prop],
                "fdtput_del_%s_%s" % (label, prop.replace(",", "_")),
                expect_success=False)
        run(["fdtput", "-p", "-t", "u", dtb, "/chosen", INITRD_DT_PROPERTY,
             str(INITRD_ENTRY_INDEX)], "fdtput_initrd_" + label)
        value = fdtget_u32(dtb, "/chosen", INITRD_DT_PROPERTY,
                           "fdtget_initrd_" + label)
        check("dtb:%s:initrd-entry" % label, value == INITRD_ENTRY_INDEX,
              "value=%r expected=%d" % (value, INITRD_ENTRY_INDEX))

    if external:
        for label, dtb in (("internal", dtb_internal), ("tf", dtb_tf)):
            require_baldur_dts(dtb, label)
        for label, dtb in (("internal", dtb_internal), ("tf", dtb_tf),
                           ("diag", dtb_diag)):
            apply_initrd_handoff(dtb, label)

    original_stage = fdtget_stage(dtb_internal)
    check("dtb:original-stage", original_stage == "os", repr(original_stage))
    if external:
        tf_stage = fdtget_stage(dtb_tf)
        check("dtb:tf-stage", tf_stage == "os", repr(tf_stage))
    run(["fdtput", "-t", "s", dtb_diag, "/chosen", "armada,boot-stage",
         "maintenance"], "fdtput_diag")
    diagnostic_stage = fdtget_stage(dtb_diag)
    check("dtb:diagnostic-stage", diagnostic_stage == "maintenance",
          repr(diagnostic_stage))

    # Rigorous equality: revert a check-only copy back to 'os' and compare the
    # full normalised text to the source canonical text.
    shutil.copyfile(dtb_diag, dtb_check)
    run(["fdtput", "-t", "s", dtb_check, "/chosen", "armada,boot-stage", "os"],
        "fdtput_check")

    dts_internal = os.path.join(WORK, "internal.dts")
    dts_diag = os.path.join(WORK, "diag.dts")
    dts_check = os.path.join(WORK, "check.dts")
    run(["dtc", "-I", "dtb", "-O", "dts", "-s", dtb_internal],
        "dtc_internal", stdout_path=dts_internal)
    run(["dtc", "-I", "dtb", "-O", "dts", "-s", dtb_diag],
        "dtc_diag", stdout_path=dts_diag)
    run(["dtc", "-I", "dtb", "-O", "dts", "-s", dtb_check],
        "dtc_check", stdout_path=dts_check)

    source_dts = {}
    if external:
        dtb_source_internal = os.path.join(WORK, "dtb_source_internal.dtb")
        dtb_source_tf = os.path.join(WORK, "dtb_source_tf.dtb")
        shutil.copyfile(inputs["internal_dtb"], dtb_source_internal)
        shutil.copyfile(inputs["tf_dtb"], dtb_source_tf)
        dts_tf = os.path.join(WORK, "tf.dts")
        for label, dtb, path in (
                ("source_internal", dtb_source_internal,
                 os.path.join(WORK, "source_internal.dts")),
                ("source_tf", dtb_source_tf,
                 os.path.join(WORK, "source_tf.dts")),
                ("tf", dtb_tf, dts_tf)):
            run(["dtc", "-I", "dtb", "-O", "dts", "-s", dtb],
                "dtc_" + label, stdout_path=path)
            with open(path, encoding="utf-8", errors="replace") as handle:
                source_dts[label] = handle.read().splitlines()

    with open(dts_internal, encoding="utf-8", errors="replace") as handle:
        canonical = handle.read().splitlines()
    with open(dts_diag, encoding="utf-8", errors="replace") as handle:
        diag_lines = handle.read().splitlines()
    with open(dts_check, encoding="utf-8", errors="replace") as handle:
        check_lines = handle.read().splitlines()

    if external:
        for label, derived in (("internal", canonical), ("tf", source_dts["tf"])):
            source = source_dts["source_" + label]
            check("dtb:%s:diff-only-initrd" % label,
                  without_initrd_properties(source)
                  == without_initrd_properties(derived),
                  "derived DT changed more than initrd properties")
            added = [line for line in derived if INITRD_DT_PROPERTY in line]
            check("dtb:%s:single-initrd-property" % label, len(added) == 1,
                  "%d initrd properties" % len(added))
            stale = [line for line in derived
                     if any(prop in line for prop in STALE_INITRD_PROPERTIES)]
            check("dtb:%s:no-stale-initrd" % label, not stale,
                  "stale initrd bounds remain: %r" % stale)

    occurrences = [line for line in canonical if "armada,boot-stage" in line]
    check("dtb:unique-occurrence", len(occurrences) == 1,
          "found %d occurrences" % len(occurrences))
    check("dtb:check-roundtrip", check_lines == canonical,
          "reverted copy does not match canonical text")
    check("dtb:line-count", len(diag_lines) == len(canonical),
          "line count differs")
    differences = diff_lines(canonical, diag_lines)
    check("dtb:single-line-diff", len(differences) == 1,
          "%d differing lines" % len(differences))
    old_line, new_line = differences[0]
    check("dtb:diff-is-stage",
          "armada,boot-stage" in old_line and "armada,boot-stage" in new_line
          and '"os"' in old_line and '"maintenance"' in new_line,
          "unexpected diff: %r -> %r" % (old_line, new_line))

    internal_bytes = read_bytes(dtb_internal, "internal copy")
    tf_bytes = read_bytes(dtb_tf, "tf copy")
    diag_bytes = read_bytes(dtb_diag, "diagnostic copy")

    set_stage("config")
    def config_safe(path, label):
        check("config:safe:" + label,
              not any(ch in path for ch in "\n\r#;"),
              "unsafe character in path: %r" % path)
        return path

    packer = inputs["packer"]
    unpacker = inputs["unpacker"]
    repacker = inputs["repacker"]
    avbtool = inputs["avbtool"]
    check("tool:packer", os.path.isfile(packer), packer)
    check("tool:unpacker", os.path.isfile(unpacker), unpacker)
    check("tool:repacker", os.path.isfile(repacker), repacker)
    check("tool:avbtool", os.path.isfile(avbtool), avbtool)

    packed_kernel = os.path.join(OUT, "PackedKernel")
    config_path = os.path.join(WORK, "build.cfg")
    config_lines = [
        "[Pack]",
        "Shim=" + config_safe(inputs["runtime"], "shim"),
        "Output=" + config_safe(packed_kernel, "output"),
        "Default=" + default_label,
        "Timeout=%d" % TIMEOUT_MS,
        "",
        "[Image-00]",
        "Name=" + EXPECTED_ENTRIES[0][0],
        "Path=" + config_safe(inputs["stock_image"], "image00"),
        "Type=Linux",
        "BaseImage=true",
        "Align=21",
        "",
        "[Image-01]",
        "Name=" + EXPECTED_ENTRIES[1][0],
        "Path=" + config_safe(inputs["linux_image"], "image01"),
        "Type=Linux",
        "BaseImage=false",
        "Align=21",
        "EntryOffset=0x0",
        "DeviceTreeBlob=Image-04",
        "",
        "[Image-02]",
        "Name=" + EXPECTED_ENTRIES[2][0],
        "PayloadFrom=Image-01",
        "Type=Linux",
        "BaseImage=false",
        "Align=21",
        "EntryOffset=0x0",
        "DeviceTreeBlob=Image-05",
        "",
        "[Image-03]",
        "Name=" + EXPECTED_ENTRIES[3][0],
        "PayloadFrom=Image-01",
        "Type=Linux",
        "BaseImage=false",
        "Align=21",
        "EntryOffset=0x0",
        "DeviceTreeBlob=Image-06",
        "",
        "[Image-04]",
        "Name=" + EXPECTED_ENTRIES[4][0],
        "Path=" + config_safe(dtb_internal, "image04"),
        "Type=DTB",
        "BaseImage=false",
        "Align=3",
        "",
        "[Image-05]",
        "Name=" + EXPECTED_ENTRIES[5][0],
        "Path=" + config_safe(dtb_tf, "image05"),
        "Type=DTB",
        "BaseImage=false",
        "Align=3",
        "",
        "[Image-06]",
        "Name=" + EXPECTED_ENTRIES[6][0],
        "Path=" + config_safe(dtb_diag, "image06"),
        "Type=DTB",
        "BaseImage=false",
        "Align=3",
        "",
    ]
    if external:
        config_lines.extend(external_image_section(
            config_safe(inputs["initrd"], "image07")))
    config_lines.extend([
        "[Manifest]",
        "Type=Manifest",
        "",
    ])
    with open(config_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(config_lines))

    set_stage("pack")
    run([packer, config_path], "packer")
    raw = read_bytes(packed_kernel, "PackedKernel")

    set_stage("assert-pack")
    # Patched raw ARM64 header.
    check("raw:arm64-magic", raw[56:60] == ARM64_MAGIC, "bad magic")
    check("raw:image_size==len", le64(raw, 16) == len(raw),
          "image_size=%d len=%d" % (le64(raw, 16), len(raw)))
    manifest_offset = le64(raw, 32)
    manifest_size = le64(raw, 40)
    shim_offset = le64(raw, 48)
    check("raw:manifest-bounds",
          manifest_size >= MANIFEST_HEADER_SIZE
          and manifest_offset + manifest_size <= len(raw),
          "off=%d size=%d len=%d" % (manifest_offset, manifest_size, len(raw)))

    manifest = parse_manifest(raw, manifest_offset)
    check("manifest:magic", manifest["magic"] == MANIFEST_MAGIC,
          hex(manifest["magic"]))
    check("manifest:version",
          manifest["version_major"] == 0 and manifest["version_minor"] == 1,
          "%d.%d" % (manifest["version_major"], manifest["version_minor"]))
    specs = expected_entries(external)
    shim_index = len(specs) - 1
    check("manifest:count", manifest["count"] == len(specs),
          "%d" % manifest["count"])
    check("manifest:default", manifest["default"] == default_index,
          "%d" % manifest["default"])
    check("manifest:timeout", manifest["timeout"] == TIMEOUT_MS,
          "%d" % manifest["timeout"])

    expected_backup = (
        int.from_bytes(stock_image[0:8], "little"),
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

    for index, entry in enumerate(entries):
        name, e_type, e_flags, e_align, _role = specs[index]
        check("entry%d:name" % index,
              entry["name_bytes"].decode("utf-8") == name,
              entry["name_bytes"].decode("utf-8", "replace"))
        check("entry%d:name_len" % index,
              entry["name_len"] == len(name.encode("utf-8")),
              "%d" % entry["name_len"])
        check("entry%d:padding" % index,
              entry["pad"] == b"\x00" * len(entry["pad"]), "non-zero padding")
        check("entry%d:type" % index, entry["type"] == e_type,
              "%d" % entry["type"])
        check("entry%d:flags" % index, entry["flags"] == e_flags,
              "%d" % entry["flags"])
        check("entry%d:align" % index, entry["alignment"] == e_align,
              "%d" % entry["alignment"])
        check("entry%d:load" % index, entry["load_address"] == 0,
              "%d" % entry["load_address"])
        check("entry%d:copy_size_max" % index, entry["copy_size_max"] == 0,
              "%d" % entry["copy_size_max"])

    # Base image (Android) entry.
    base = entries[0]
    check("base:offset", base["offset"] == 0, "%d" % base["offset"])
    check("base:size", base["size"] == len(stock_image),
          "%d expected=%d" % (base["size"], len(stock_image)))
    check("base:no-has-dtb", (base["flags"] & FLAG_HAS_DTB) == 0, "HAS_DTB set")
    check("base:no-qc", (base["flags"] & FLAG_QC) == 0, "QC flag set")
    check("base:dtb_index", base["dtb_index"] == 0, "%d" % base["dtb_index"])
    check("base:entry_offset", base["entry_offset"] == 0,
          "%d" % base["entry_offset"])
    _restored = bytearray(payload(raw, base))
    for _i, _off in enumerate((0, 16, 32, 40, 48)):
        struct.pack_into("<Q", _restored, _off, manifest["backup"][_i])
    check("base:payload==stock_image", bytes(_restored) == stock_image,
          "base payload differs from stock image")

    # Non-base Linux entries share one payload span owned by entry 1.
    linux_offset = entries[1]["offset"]
    check("linux:offset-nonzero", linux_offset != 0, "zero offset")
    for index in (1, 2, 3):
        entry = entries[index]
        check("linux%d:size" % index, entry["size"] == len(linux_image),
              "%d expected=%d" % (entry["size"], len(linux_image)))
        check("linux%d:entry_offset" % index, entry["entry_offset"] == 0,
              "%d" % entry["entry_offset"])
        check("linux%d:no-base" % index, (entry["flags"] & FLAG_BASE) == 0,
              "BASE flag set")
        check("linux%d:payload==linux_image" % index,
              payload(raw, entry) == linux_image, "payload differs")
    check("linux:shared-span",
          entries[1]["offset"] == entries[2]["offset"] == entries[3]["offset"]
          and entries[1]["size"] == entries[2]["size"] == entries[3]["size"],
          "aliases do not share the exact span")
    check("linux:dtb-map",
          entries[1]["dtb_index"] == 4 and entries[2]["dtb_index"] == 5
          and entries[3]["dtb_index"] == 6, "unexpected DTB mapping")

    # DTB entries.
    dtb_payloads = {4: internal_bytes, 5: tf_bytes, 6: diag_bytes}
    for index in (4, 5, 6):
        entry = entries[index]
        check("dtb%d:offset-aligned" % index, entry["offset"] % 8 == 0,
              "offset=%d" % entry["offset"])
        check("dtb%d:size" % index, entry["size"] == len(dtb_payloads[index]),
              "%d expected=%d" % (entry["size"], len(dtb_payloads[index])))
        check("dtb%d:no-dtb-ref" % index, entry["dtb_index"] == 0,
              "%d" % entry["dtb_index"])
        body = payload(raw, entry)
        check("dtb%d:payload" % index, body == dtb_payloads[index],
              "payload differs")
        check("dtb%d:fdt-magic" % index, body[0:4] == b"\xd0\x0d\xfe\xed",
              "bad FDT magic")
        totalsize = struct.unpack_from(">I", body, 4)[0]
        check("dtb%d:totalsize" % index, 0 < totalsize <= len(body),
              "totalsize=%d len=%d" % (totalsize, len(body)))

    # Optional shared external-initramfs blob (entry 7, before the shim).
    if external:
        blob = entries[INITRD_ENTRY_INDEX]
        blob_errors = blob_entry_errors(blob, initrd)
        check("blob:entry", not blob_errors, "; ".join(blob_errors))
        check("blob:offset-aligned",
              blob["offset"] % (1 << EXPECTED_INITRD_ENTRY[3]) == 0,
              "offset=%d" % blob["offset"])
        check("blob:payload==initrd", payload(raw, blob) == initrd,
              "blob payload differs from --initrd")

    # Attached shim (last entry in both layouts).
    shim = entries[shim_index]
    check("shim:offset==header", shim["offset"] == shim_offset,
          "entry=%d header=%d" % (shim["offset"], shim_offset))
    check("shim:size", shim["size"] == RUNTIME_SIZE, "%d" % shim["size"])
    check("shim:payload==runtime", payload(raw, shim) == runtime,
          "shim payload differs from runtime input")

    # Memory slot layout: no overlap, zero tails/gaps, exact end.
    base_slot = max(base["size"], stock_info["image_size"])
    linux_slot = align_up(max(entries[1]["size"], linux_info["image_size"]), 4096)
    check("layout:shim-offset", shim_offset == align_up(base_slot, 4),
          "shim=%d expected=%d" % (shim_offset, align_up(base_slot, 4)))
    check("layout:shim-align", shim_offset % 4 == 0, "%d" % shim_offset)

    mask = (1 << 21) - 1
    expected_residue = (linux_info["text_offset"] - stock_info["text_offset"]) & mask
    check("layout:linux-align",
          (linux_offset & mask) == expected_residue,
          "offset residue=%d expected=%d" % (linux_offset & mask, expected_residue))

    regions = [
        ("base", 0, base_slot, base["size"]),
        ("shim", shim_offset, shim_offset + RUNTIME_SIZE, RUNTIME_SIZE),
        ("linux", linux_offset, linux_offset + linux_slot, entries[1]["size"]),
        ("dtb4", entries[4]["offset"], entries[4]["offset"] + entries[4]["size"],
         entries[4]["size"]),
        ("dtb5", entries[5]["offset"], entries[5]["offset"] + entries[5]["size"],
         entries[5]["size"]),
        ("dtb6", entries[6]["offset"], entries[6]["offset"] + entries[6]["size"],
         entries[6]["size"]),
    ]
    if external:
        blob = entries[INITRD_ENTRY_INDEX]
        regions.append(
            ("initrd", blob["offset"], blob["offset"] + blob["size"],
             blob["size"]))
    regions.append(("manifest", manifest_offset, manifest_offset + manifest_size,
                    manifest_size))
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
    check("layout:ends-at-manifest", previous_end == len(raw),
          "trailing bytes after manifest")

    set_stage("unpack")
    unpacked_dir = os.path.join(OUT, "unpacked")
    run([unpacker, packed_kernel, unpacked_dir], "unpacker")

    set_stage("assert-unpack")
    restored_base = read_bytes(os.path.join(unpacked_dir, "BaseImage"), "BaseImage")
    check("unpack:base-full", restored_base == stock_image,
          "restored BaseImage differs from stock image")
    check("unpack:base-first64", restored_base[:64] == stock_image[:64],
          "restored BaseImage header differs")
    restored_shim = read_bytes(os.path.join(unpacked_dir, "Shim.bin"), "Shim.bin")
    check("unpack:shim", restored_shim == runtime, "restored Shim differs")

    config = configparser.ConfigParser()
    config.optionxform = str
    config.read(os.path.join(unpacked_dir, "config.cfg"), encoding="utf-8")

    def section_ref(section):
        if not config.has_section(section):
            return None, None
        options = config[section]
        if "PayloadFrom" in options:
            return options["PayloadFrom"], None
        return None, options.get("Path")

    ref1_from, path1 = section_ref("Image-01")
    ref2_from, path2 = section_ref("Image-02")
    ref3_from, path3 = section_ref("Image-03")
    check("unpack:image01-path", path1 is not None, "Image-01 has no Path")
    alias2_ok = ref2_from == "Image-01" or (path2 is not None and path2 == path1)
    alias3_ok = ref3_from == "Image-01" or (path3 is not None and path3 == path1)
    check("unpack:alias2-refers-image01", alias2_ok,
          "Image-02 ref=%r path=%r" % (ref2_from, path2))
    check("unpack:alias3-refers-image01", alias3_ok,
          "Image-03 ref=%r path=%r" % (ref3_from, path3))
    distinct_paths = len({p for p in (path1, path2, path3) if p is not None})
    check("unpack:not-three-paths", distinct_paths < 3,
          "Image-01/02/03 use %d distinct Path entries" % distinct_paths)

    owner_linux = read_bytes(os.path.join(unpacked_dir, path1), "Image-01 linux")
    check("unpack:linux-owner", owner_linux == linux_image,
          "restored Linux owner differs from source")
    for index, section, expected in ((4, "Image-04", internal_bytes),
                                     (5, "Image-05", tf_bytes),
                                     (6, "Image-06", diag_bytes)):
        _, path = section_ref(section)
        check("unpack:dtb%d-path" % index, path is not None,
              "%s has no Path" % section)
        check("unpack:dtb%d-bytes" % index,
              read_bytes(os.path.join(unpacked_dir, path), section) == expected,
              "%s bytes differ" % section)

    if external:
        _, path7 = section_ref("Image-%02d" % INITRD_ENTRY_INDEX)
        check("unpack:initrd-path", path7 is not None, "Image-07 has no Path")
        check("unpack:initrd-bytes",
              read_bytes(os.path.join(unpacked_dir, path7),
                         "Image-%02d" % INITRD_ENTRY_INDEX) == initrd,
              "restored initrd differs from --initrd")

    set_stage("repack")
    run([packer, os.path.join(unpacked_dir, "config.cfg")], "repacker-packer")
    repacked = read_bytes(os.path.join(unpacked_dir, "RepackedKernel"),
                          "RepackedKernel")
    check("repack:byte-identical", repacked == raw,
          "repacked output differs from original packed kernel")

    set_stage("gzip")
    packed_gz = os.path.join(OUT, "PackedKernel.gz")
    run(["gzip", "-n", "-6", "-c", packed_kernel], "gzip", stdout_path=packed_gz)
    gz_bytes = read_bytes(packed_gz, "PackedKernel.gz")
    check("gzip:magic", gz_bytes[0:2] == b"\x1f\x8b", "bad gzip magic")
    check("gzip:mtime-zero", gz_bytes[4:8] == b"\x00\x00\x00\x00",
          "gzip mtime is not zero")
    check("gzip:roundtrip", gzip.decompress(gz_bytes) == raw,
          "decompressed bytes differ from packed kernel")
    # Whole-pack gzip is always produced; >96MiB raw is the expected shape and
    # is reported (not asserted, since it depends on the stock kernel size).
    raw_exceeds_boot_partition = len(raw) > BOOT_SIZE

    set_stage("boot")
    final_boot = os.path.join(OUT, "boot.img")
    repack_report = os.path.join(OUT, "repack_report.json")
    run([sys.executable, "-B", repacker,
         "--original", inputs["stock_boot"],
         "--kernel", packed_gz,
         "--output", final_boot,
         "--report", repack_report,
         "--avbtool", avbtool], "repacker")

    set_stage("assert-boot")
    final = read_bytes(final_boot, "FINALBOOT")
    final_boot_header = parse_boot_header(final)
    check("boot:size", len(final) == BOOT_SIZE,
          "len=%d expected=%d" % (len(final), BOOT_SIZE))
    check("boot:magic", final[0:8] == BOOT_MAGIC, repr(final[0:8]))
    check("boot:version", final_boot_header["version"] == 4,
          "version=%d" % final_boot_header["version"])
    check("boot:ramdisk_size", final_boot_header["ramdisk_size"] == 0,
          "ramdisk_size=%d" % final_boot_header["ramdisk_size"])
    check("boot:signature_size", final_boot_header["signature_size"] ==
          struct.unpack_from("<I", stock_boot, 1580)[0],
          "GKI signature_size changed")
    check("boot:header-prefix", final[0:8] == stock_boot[0:8],
          "first 8 bytes changed")
    check("boot:header-body", final[12:4096] == stock_boot[12:4096],
          "header differs beyond kernel_size field")
    final_kernel_size = final_boot_header["kernel_size"]
    check("boot:kernel_size", final_kernel_size == len(gz_bytes),
          "kernel_size=%d gzip len=%d" % (final_kernel_size, len(gz_bytes)))
    check("boot:kernel-bytes",
          final[4096:4096 + final_kernel_size] == gz_bytes,
          "embedded kernel is not the gzip input")
    check("boot:kernel-decode",
          gzip.decompress(final[4096:4096 + final_kernel_size]) == raw,
          "decoded embedded kernel differs from packed kernel")

    footer = final[-64:]
    avb_magic, avb_major, avb_minor, ois, vbo, vbs, _reserved = struct.unpack(
        ">4sIIQQQ28s", footer)
    check("avb:footer-magic", avb_magic == AVB_MAGIC, repr(avb_magic))
    check("avb:footer-major", avb_major == 1, "%d" % avb_major)
    signature_verification, verify_log, info_log = verify_boot_signatures(
        inputs["stock_boot"], final_boot, repacker, avbtool)

    set_stage("recheck-sources")
    for key, path in inputs.items():
        if key in ("packer", "unpacker", "repacker", "avbtool"):
            continue
        check("source-unchanged:%s" % key,
              sha256_file(path) == source_digests[key], "input changed: %s" % path)

    set_stage("report")
    report_entries = []
    for index, entry in enumerate(entries):
        name, e_type, e_flags, e_align, role = specs[index]
        report_entries.append({
            "index": index,
            "role": role,
            "name": name,
            "type": e_type,
            "flags": entry["flags"],
            "offset": entry["offset"],
            "size": entry["size"],
            "entry_offset": entry["entry_offset"],
            "dtb_index": entry["dtb_index"] if entry["flags"] & FLAG_HAS_DTB else None,
            "alignment": entry["alignment"],
            "sha256": sha256_bytes(payload(raw, entry)),
        })

    report = {
        "hardware_validated": False,
        "offline_verified": True,
        "sources": {
            "stock_boot": {"path": inputs["stock_boot"],
                           "length": len(stock_boot),
                           "sha256": source_digests["stock_boot"]},
            "stock_image": {"path": inputs["stock_image"],
                            "length": len(stock_image),
                            "sha256": source_digests["stock_image"],
                            "text_offset": stock_info["text_offset"],
                            "image_size": stock_info["image_size"],
                            "effective_slot": stock_info["effective_slot"]},
            "linux_image": {"path": inputs["linux_image"],
                            "length": len(linux_image),
                            "sha256": source_digests["linux_image"],
                            "text_offset": linux_info["text_offset"],
                            "image_size": linux_info["image_size"],
                            "effective_slot": linux_info["effective_slot"]},
            "runtime": {"path": inputs["runtime"],
                        "length": len(runtime),
                        "sha256": source_digests["runtime"]},
            "tf_dtb": {"path": inputs["tf_dtb"],
                       "length": len(tf_bytes),
                       "sha256": source_digests["tf_dtb"]},
            "internal_dtb": {"path": inputs["internal_dtb"],
                             "length": len(internal_bytes),
                             "sha256": source_digests["internal_dtb"]},
        },
        "tools": {
            "packer": inputs["packer"],
            "unpacker": inputs["unpacker"],
            "repacker": inputs["repacker"],
            "avbtool": inputs["avbtool"],
        },
        "config_path": config_path,
        "default_label": default_label,
        "default_index": default_index,
        "timeout_ms": TIMEOUT_MS,
        "manifest": {
            "magic": hex(manifest["magic"]),
            "version": "%d.%d" % (manifest["version_major"],
                                  manifest["version_minor"]),
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
            "packed_kernel_gz": {"path": packed_gz, "length": len(gz_bytes),
                                 "sha256": sha256_bytes(gz_bytes)},
            "repacked_kernel": {"path": os.path.join(unpacked_dir, "RepackedKernel"),
                                "sha256": sha256_bytes(repacked)},
            "final_boot": {"path": final_boot, "length": len(final),
                           "sha256": sha256_bytes(final)},
            "repack_report": repack_report,
        },
        "avb": {
            "algorithm": signature_verification["metadata_algorithm"],
            "original_image_size": ois,
            "vbmeta_offset": vbo,
            "vbmeta_size": vbs,
            "signature_verification_log": verify_log,
            "signed_metadata_preserved": True,
            "signature_valid": True,
            "content_digest_matches": signature_verification["content_digest_matches"],
            "info_image_log": info_log,
        },
        "caveats": [
            "Original signed metadata is preserved for the verified TB323FU ABL "
            "chain; changed payloads are not newly OEM-signed or validated by generic AVB.",
            "Offline byte-level round-trip does not validate the ABL whole-gzip "
            "decompression output window.",
            "Android, Linux and diagnostic real-hardware boot behaviour and the "
            "visual boot menu are NOT validated.",
            "No deployment, no flashing and no reboot happened; a real device "
            "has not run this image.",
            "hardware_validated is false: this run is offline verification only.",
        ],
        "commands": COMMANDS,
        "checks": CHECKS,
    }

    report["shim_index"] = shim_index
    report["external_initrd"] = {
        "enabled": external,
        "entry_index": INITRD_ENTRY_INDEX if external else None,
        "dt_property": INITRD_DT_PROPERTY if external else None,
        "shared_by_linux_entries": [1, 2, 3] if external else [],
    }
    if external:
        report["sources"]["initrd"] = {
            "path": inputs["initrd"],
            "length": len(initrd),
            "sha256": source_digests["initrd"],
        }

    with open(os.path.join(OUT, "report.json"), "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    sys.stdout.write("offline verification PASSED; report at %s\n"
                     % os.path.join(OUT, "report.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
