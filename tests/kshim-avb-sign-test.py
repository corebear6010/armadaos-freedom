#!/usr/bin/env python3
"""Regression tests for the mandatory kShim AVB signing stage."""

import hashlib
import json
import pathlib
import re
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SIGNER = ROOT / "system_files/usr/libexec/armada/kshim-avb-sign.py"
KSHIM_BUILDER = ROOT / "system_files/usr/libexec/armada/kshim-build-boot.py"
ANDROID_BUILDER = ROOT / "devices/lenovo-tb322fc/boot/build_android_boot.py"
AVBTOOL = ROOT / "packages/kshim-tools/vendor/avbtool.py"
BOOT_SIZE = 96 * 1024 * 1024
SALT = "11" * 32


def run(command, check=True):
    return subprocess.run(
        [str(item) for item in command],
        capture_output=True,
        text=True,
        check=check,
    )


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def add_footer(image, algorithm, key=None, release="test-release"):
    command = [
        sys.executable,
        "-B",
        AVBTOOL,
        "add_hash_footer",
        "--image",
        image,
        "--partition_size",
        str(BOOT_SIZE),
        "--partition_name",
        "boot",
        "--algorithm",
        algorithm,
        "--hash_algorithm",
        "sha256",
        "--salt",
        SALT,
        "--rollback_index",
        "42",
        "--rollback_index_location",
        "3",
        "--internal_release_string",
        release,
        "--prop",
        "test.property:value",
    ]
    if key is not None:
        command.extend(("--key", str(key)))
    run(command)


def avb_original_size(path):
    with open(path, "rb") as handle:
        handle.seek(-64, 2)
        footer = handle.read(64)
    magic, _major, _minor, original_size, _vbo, _vbs, _reserved = (
        struct.unpack(">4sIIQQQ28s", footer)
    )
    if magic != b"AVBf":
        raise AssertionError("missing AVB footer")
    return original_size


def public_key_fingerprints(key, stock):
    public = key.with_suffix(".pub")
    run(
        [
            sys.executable,
            "-B",
            AVBTOOL,
            "extract_public_key",
            "--key",
            key,
            "--output",
            public,
        ]
    )
    sha256 = sha256_file(public)
    info = run(
        [
            sys.executable,
            "-B",
            AVBTOOL,
            "info_image",
            "--image",
            stock,
        ]
    ).stdout
    match = re.search(r"^\s*Public key \(sha1\):\s*(\S+)", info, re.M)
    if not match:
        raise AssertionError("public key sha1 missing from stock info")
    return sha256, match.group(1)


class SignerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.key = self.root / "testkey.pem"
        self.stock = self.root / "stock.img"
        self.target = self.root / "target.img"
        run(["openssl", "genrsa", "-out", str(self.key), "2048"])

        self.stock_payload = b"STOCK-BOOT-" + b"A" * 4096
        self.target_payload = b"TARGET-BOOT-" + b"B" * 4096
        self.stock.write_bytes(self.stock_payload)
        self.target.write_bytes(self.target_payload)
        add_footer(
            self.stock, "SHA256_RSA2048", key=self.key, release="stock-release"
        )
        add_footer(self.target, "NONE", release="unsigned")

        self.key_sha256, self.key_sha1 = public_key_fingerprints(
            self.key, self.stock
        )
        self.report = self.root / "signing-report.json"

    def tearDown(self):
        self.temp.cleanup()

    def sign(self, key=None):
        return run(
            [
                sys.executable,
                "-B",
                SIGNER,
                "--image",
                self.target,
                "--stock-boot",
                self.stock,
                "--avbtool",
                AVBTOOL,
                "--key",
                key or self.key,
                "--report",
                self.report,
                "--partition-size",
                str(BOOT_SIZE),
                "--algorithm",
                "SHA256_RSA2048",
                "--release-string",
                "test-release",
                "--expected-key-sha256",
                self.key_sha256,
                "--expected-key-sha1",
                self.key_sha1,
            ],
            check=False,
        )

    def test_signs_and_verifies_none_footer_without_changing_payload(self):
        original_size = avb_original_size(self.target)
        proc = self.sign()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.target.stat().st_size, BOOT_SIZE)
        with open(self.target, "rb") as handle:
            self.assertEqual(
                handle.read(len(self.target_payload)), self.target_payload
            )

        info = run(
            [
                sys.executable,
                "-B",
                AVBTOOL,
                "info_image",
                "--image",
                self.target,
            ]
        ).stdout
        self.assertIn("Algorithm:                SHA256_RSA2048", info)
        self.assertIn("Release String:           'test-release'", info)
        self.assertIn("Public key (sha1):        " + self.key_sha1, info)
        self.assertIn("Rollback Index:           42\n", info)
        self.assertIn("Rollback Index Location:  3\n", info)
        self.assertIn("Salt:                  " + SALT, info)
        self.assertIn("Prop: test.property -> 'value'", info)

        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(report["original_image_size"], original_size)
        self.assertEqual(report["avb"]["algorithm"], "SHA256_RSA2048")
        self.assertEqual(report["avb"]["key_public_sha256"], self.key_sha256)
        self.assertEqual(report["avb"]["key_public_sha1"], self.key_sha1)
        self.assertEqual(report["stock"]["salt"], SALT)
        self.assertEqual(report["stock"]["rollback_index"], 42)
        self.assertEqual(report["stock"]["rollback_index_location"], 3)
        self.assertEqual(
            report["stock"]["properties"],
            [{"key": "test.property", "value": "value"}],
        )

    def test_wrong_key_is_rejected_without_modifying_image(self):
        before = sha256_file(self.target)
        wrong = self.root / "wrong.pem"
        run(["openssl", "genrsa", "-out", str(wrong), "2048"])
        proc = self.sign(key=wrong)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("public key SHA256 mismatch", proc.stderr)
        self.assertEqual(sha256_file(self.target), before)

    def test_builders_require_avb_key(self):
        common = [
            "--stock-boot",
            "stock",
            "--runtime",
            "runtime",
            "--packer",
            "packer",
            "--unpacker",
            "unpacker",
            "--repacker",
            "repacker",
            "--avbtool",
            "avbtool",
            "--out",
            "out",
        ]
        kshim = run(
            [sys.executable, "-B", KSHIM_BUILDER, "--layout", "maintenance",
             "--linux-image", "linux", "--dtb", "dtb", "--initrd", "initrd",
             "--bootargs", "quiet", *common],
            check=False,
        )
        self.assertEqual(kshim.returncode, 2)
        self.assertIn("--avb-key", kshim.stderr)

        android = run(
            [sys.executable, "-B", ANDROID_BUILDER, *common],
            check=False,
        )
        self.assertEqual(android.returncode, 2)
        self.assertIn("--avb-key", android.stderr)


if __name__ == "__main__":
    unittest.main()
