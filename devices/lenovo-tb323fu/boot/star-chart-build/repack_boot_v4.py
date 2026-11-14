#!/usr/bin/env python3
"""Repack boot v4 for the verified TB323FU signed-metadata ABL chain."""
import argparse
import gzip
import hashlib
import importlib.util
import json
import os
import struct
import sys
import zlib

HDR = 4096
HDR_SIZE = 1584
MAGIC = b"ANDROID!"
KERN_MAGIC = b"ARM\x64"

# Fixed DT-probe diagnostic carrier. The cmdline field is Android boot header
# bytes [44:1580] (1536 bytes): NUL-terminated text followed by zero padding.
CMDLINE_START = 44
CMDLINE_END = 1580
CMDLINE_LEN = CMDLINE_END - CMDLINE_START
PROBE_VALUE = "pending" + "_" * 73          # 80 ASCII bytes
PROBE_PARAM_PROBE = "kshimreport.kshim_probe"
PROBE_PARAM_S0 = "kshimreport.kshim_s0"
# Exactly two ordinary cmdline parameters, single-space separated: 206 bytes.
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


def die(msg):
    raise SystemExit("error: " + msg)


def build_probe_field(field):
    """Validate the original cmdline field and return (new_field, original_text).

    Diagnostic mode only. Never normalises the original bytes: the text up to
    the first NUL is preserved verbatim (including whitespace) and the fixed
    206-byte suffix is appended with a single separator when text is nonempty.
    """
    if len(field) != CMDLINE_LEN:
        die("cmdline field length %d != %d" % (len(field), CMDLINE_LEN))
    nul = field.find(b"\x00")
    if nul < 0:
        die("original cmdline has no NUL terminator")
    if any(field[nul + 1:]):
        die("original cmdline has nonzero bytes after the first NUL")
    orig_text = bytes(field[:nul])
    for token in orig_text.split():
        key = token.split(b"=", 1)[0]
        if key in PROBE_CONFLICT_KEYS:
            die("original cmdline already defines %s"
                % key.decode("ascii", "replace"))
    new_text = (orig_text + b" " + PROBE_SUFFIX) if orig_text else PROBE_SUFFIX
    if len(new_text) > CMDLINE_LEN - 1:
        die("resulting cmdline text %d exceeds %d bytes"
            % (len(new_text), CMDLINE_LEN - 1))
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


def align(n, a=4096):
    return (n + a - 1) // a * a


def sha256(b):
    return hashlib.sha256(b).hexdigest()


def read_file(path, what):
    if not os.path.isfile(path):
        die("%s not found: %s" % (what, path))
    with open(path, "rb") as f:
        return f.read()


def load_avbtool(path):
    if not os.path.isfile(path):
        die("avbtool not found: %s" % path)
    spec = importlib.util.spec_from_file_location("_avbtool_under_test", path)
    if spec is None or spec.loader is None:
        die("cannot load avbtool module from %s" % path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    saved = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        die("failed to import avbtool from %s: %s" % (path, e))
    finally:
        sys.dont_write_bytecode = saved
    return mod


def parse_avb_source(mod, image_path):
    handler = None
    try:
        handler = mod.ImageHandler(image_path, read_only=True)
        return mod.Avb()._parse_image(handler)
    except Exception as e:
        die("failed to parse AVB metadata in %s: %s" % (image_path, e))
    finally:
        if handler is not None:
            handler._image.close()


def signed_boot_parts(data, mod):
    if len(data) < HDR + 64 or data[:8] != MAGIC:
        die("not a complete Android boot image")
    ks, rs = struct.unpack_from("<II", data, 8)
    hs = struct.unpack_from("<I", data, 20)[0]
    version = struct.unpack_from("<I", data, 40)[0]
    ss = struct.unpack_from("<I", data, 1580)[0]
    if not ks or rs or hs != HDR_SIZE or version != 4:
        die("expected boot v4 with a kernel and no ramdisk")
    magic, major, minor, original_size, offset, size, _ = struct.unpack(
        ">4sIIQQQ28s", data[-64:])
    kernel_end = HDR + align(ks)
    if (magic != b"AVBf" or major != 1 or original_size != offset or
            offset % HDR or offset < kernel_end or size < 256 or
            offset + size > len(data) - 64):
        die("invalid or unsupported signed AVB layout")
    signature = data[kernel_end:original_size]
    if ss > len(signature) or (signature and signature[:4] != b"AVB0"):
        die("GKI signature range is invalid")
    if any(data[HDR + ks:kernel_end]):
        die("kernel page padding is not zero")
    blob = data[offset:offset + size]
    if blob[:4] != b"AVB0":
        die("vbmeta magic is invalid")
    try:
        header = mod.AvbVBMetaHeader(blob[:256])
        algorithm, _ = mod.lookup_algorithm_by_type(header.algorithm_type)
        if (header.algorithm_type == 0 or not header.authentication_data_block_size or
                not header.signature_size or not header.public_key_size):
            die("signed AVB metadata required; algorithm NONE is not allowed")
        if 256 + header.authentication_data_block_size + header.auxiliary_data_block_size != size:
            die("vbmeta block sizes do not match its footer")
        if not mod.verify_vbmeta_signature(header, blob):
            die("vbmeta signature verification failed")
    except Exception as error:
        die("cannot verify signed vbmeta: %s" % error)
    return {"kernel_size": ks, "kernel_end": kernel_end,
            "signature": signature, "vbmeta": blob,
            "original_size": original_size, "vbmeta_offset": offset,
            "algorithm": algorithm, "authentication_bytes": header.authentication_data_block_size}


def verify_signed_metadata(reference_path, candidate_path, avbtool):
    """Verify preserved signatures; this does not authenticate changed payloads."""
    mod = load_avbtool(str(avbtool))
    reference = read_file(reference_path, "signature reference")
    candidate = read_file(candidate_path, "candidate")
    before = signed_boot_parts(reference, mod)
    after = signed_boot_parts(candidate, mod)
    if len(reference) != len(candidate):
        die("candidate partition length changed")
    if before["vbmeta"] != after["vbmeta"]:
        die("candidate vbmeta differs from the signed reference")
    if before["signature"] != after["signature"]:
        die("candidate GKI signature differs from the reference")
    if reference[1580:1584] != candidate[1580:1584]:
        die("candidate GKI signature_size changed")
    _, _, descriptors, _ = parse_avb_source(mod, str(reference_path))
    hashes = [d for d in descriptors if isinstance(d, mod.AvbHashDescriptor)]
    if len(hashes) != 1 or hashes[0].partition_name != "boot":
        die("reference must contain one boot hash descriptor")
    descriptor = hashes[0]
    digest = hashlib.new(descriptor.hash_algorithm)
    digest.update(bytes(descriptor.salt))
    digest.update(candidate[:descriptor.image_size])
    content_matches = (descriptor.image_size <= len(candidate) and
                       digest.digest() == bytes(descriptor.digest))
    return {"footer_present": True, "metadata_algorithm": after["algorithm"],
            "authentication_bytes": after["authentication_bytes"],
            "signature_valid": True, "signed_metadata_preserved": True,
            "vbmeta_sha256": sha256(after["vbmeta"]),
            "gki_signature_sha256": sha256(after["signature"]),
            "gki_signature_bytes": len(after["signature"]),
            "vbmeta_offset": after["vbmeta_offset"],
            "vbmeta_size": len(after["vbmeta"]),
            "content_digest_matches": content_matches,
            "reference_sha256": sha256(reference), "candidate_sha256": sha256(candidate)}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--original", required=True)
    ap.add_argument("--kernel", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--avbtool", required=True)
    ap.add_argument("--dt-probe-report-slots", action="store_true")
    a = ap.parse_args()
    if os.path.realpath(a.output) == os.path.realpath(a.report):
        die("output and report paths are the same")
    for path, label in ((a.output, "output"), (a.report, "report")):
        if os.path.exists(path):
            die("refusing to overwrite existing %s: %s" % (label, path))
    original = read_file(a.original, "original")
    kernel = read_file(a.kernel, "kernel")
    parts = signed_boot_parts(original, load_avbtool(a.avbtool))
    if kernel[:2] == b"\x1f\x8b":
        try:
            plain = gzip.decompress(kernel)
        except (OSError, EOFError, zlib.error) as error:
            die("new kernel gzip is invalid: %s" % error)
        if len(plain) < 64 or struct.unpack_from("<Q", plain, 16)[0] != len(plain):
            die("new kernel gzip image_size differs from decoded length")
    else:
        plain = kernel
    if len(plain) < 64 or plain[56:60] != KERN_MAGIC:
        die("new kernel missing ARM64 magic at offset 56")
    output = bytearray(original[:HDR])
    struct.pack_into("<I", output, 8, len(kernel))
    if a.dt_probe_report_slots:
        field, _ = build_probe_field(original[CMDLINE_START:CMDLINE_END])
        output[CMDLINE_START:CMDLINE_END] = field
    output += kernel
    output += bytes(align(len(output)) - len(output))
    output += parts["signature"]
    output += bytes(align(len(output)) - len(output))
    offset = len(output)
    output += parts["vbmeta"]
    if len(output) > len(original) - 64:
        die("signed image exceeds the original partition length")
    output += bytes(len(original) - 64 - len(output))
    footer = bytearray(original[-64:])
    struct.pack_into(">QQQ", footer, 12, offset, offset, len(parts["vbmeta"]))
    output += footer
    owned = False
    try:
        with open(a.output, "xb") as handle:
            handle.write(output)
        owned = True
        verified = verify_signed_metadata(a.original, a.output, a.avbtool)
        report = {"source": a.original, "kernel": a.kernel, "output": a.output,
                  "original_length": len(original), "old_kernel_size": parts["kernel_size"],
                  "new_kernel_length": len(kernel), "output_length": len(output),
                  "partition_limit": len(original), "sha256_original": sha256(original),
                  "sha256_new_kernel": sha256(kernel), "sha256_output": sha256(output),
                  "header_version": 4, "unsigned": False, "no_device_validation": True,
                  "avb": verified,
                  "warning": "Preserves the original signed metadata for the verified TB323FU ABL chain; does not generate a new OEM signature or validate changed payloads under generic AVB."}
        if a.dt_probe_report_slots:
            report["dt_probe_report_slots"] = probe_report_slots()
        with open(a.report, "x", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except BaseException:
        if owned:
            os.remove(a.output)
        raise
    print("wrote %s (%d bytes)" % (a.output, len(output)))
    print("wrote %s" % a.report)


if __name__ == "__main__":
    main()
