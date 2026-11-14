#!/usr/bin/env python3
"""Offline regression tests for the TB323FU signed-metadata boot chain."""
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
REPACK = Path(os.environ.get('KSHIM_REPACKER', ROOT / 'devices/lenovo-tb323fu/boot/star-chart-build/repack_boot_v4.py'))
AVBTOOL = Path(os.environ.get('KSHIM_AVBTOOL', ROOT / 'packages/kshim-tools/vendor/avbtool.py'))
BUILDER = Path(os.environ.get('KSHIM_OFFLINE_BUILDER', REPACK.with_name('build_offline_boot.py')))


def run(*args):
    return subprocess.run(list(map(str, args)), capture_output=True, text=True)


def vbmeta(image):
    _, _, _, _, offset, size, _ = struct.unpack('>4sIIQQQ28s', image[-64:])
    return image[offset:offset + size]


class SignedBootTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory()
        cls.key = Path(cls.shared.name) / 'test-only.pem'
        result = run('openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:4096', '-out', cls.key)
        if result.returncode:
            raise RuntimeError(result.stderr)
        spec = importlib.util.spec_from_file_location('gen5_repacker', REPACK)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    @classmethod
    def tearDownClass(cls):
        cls.shared.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.original = self.directory / 'original.img'
        self.kernel = self.directory / 'kernel'
        self.output = self.directory / 'result.img'
        self.report = self.directory / 'report.json'
        self.make_source()

    def kernel_bytes(self, fill):
        kernel = bytearray([fill] * 4096)
        struct.pack_into('<Q', kernel, 16, len(kernel))
        kernel[56:60] = b'ARM\x64'
        return bytes(kernel)

    def make_source(self, algorithm='SHA256_RSA4096', signature=b'', signature_size=None):
        header = bytearray(4096)
        header[:8] = b'ANDROID!'
        struct.pack_into('<I', header, 8, 4096)
        struct.pack_into('<I', header, 20, 1584)
        struct.pack_into('<I', header, 40, 4)
        struct.pack_into('<I', header, 1580, len(signature) if signature_size is None else signature_size)
        self.original.write_bytes(header + self.kernel_bytes(17) + signature)
        args = [sys.executable, '-B', AVBTOOL, 'add_hash_footer', '--image', self.original,
                '--partition_size', str(1 << 20), '--partition_name', 'boot',
                '--algorithm', algorithm, '--salt', '00112233']
        if algorithm != 'NONE':
            args += ['--key', self.key]
        result = run(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.kernel.write_bytes(self.kernel_bytes(23))

    def repack(self):
        return run(sys.executable, '-B', REPACK, '--original', self.original,
                   '--kernel', self.kernel, '--output', self.output,
                   '--report', self.report, '--avbtool', AVBTOOL)

    def test_changed_kernel_preserves_signed_vbmeta(self):
        before = self.original.read_bytes()
        result = self.repack()
        self.assertEqual(result.returncode, 0, result.stderr)
        after = self.output.read_bytes()
        self.assertEqual(vbmeta(after), vbmeta(before))
        self.assertEqual(after[4096:8192], self.kernel.read_bytes())
        report = json.loads(self.report.read_text())
        self.assertFalse(report['unsigned'])
        self.assertTrue(report['avb']['signature_valid'])
        self.assertTrue(report['avb']['signed_metadata_preserved'])
        self.assertFalse(report['avb']['content_digest_matches'])

    def test_unchanged_kernel_roundtrip_is_identical(self):
        self.kernel.write_bytes(self.kernel_bytes(17))
        result = self.repack()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_bytes(), self.original.read_bytes())

    def test_unsigned_source_is_rejected(self):
        self.make_source(algorithm='NONE')
        result = self.repack()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())

    def test_missing_footer_is_rejected(self):
        self.original.write_bytes(self.original.read_bytes()[:8192])
        result = self.repack()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_corrupt_signature_is_rejected(self):
        data = bytearray(self.original.read_bytes())
        offset = struct.unpack_from('>Q', data, len(data) - 44)[0]
        data[offset + 256 + 40] ^= 1
        self.original.write_bytes(data)
        result = self.repack()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_gki_signature_is_preserved_with_or_without_header_size(self):
        signature = vbmeta(self.original.read_bytes())
        signature += bytes((-len(signature)) % 4096)
        for size in (0, len(signature)):
            with self.subTest(signature_size=size):
                self.output.unlink(missing_ok=True)
                self.report.unlink(missing_ok=True)
                self.make_source(signature=signature, signature_size=size)
                result = self.repack()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.output.read_bytes()[8192:8192 + len(signature)], signature)
                self.output.unlink()
                self.report.unlink()

    def offline_gate(self):
        spec = importlib.util.spec_from_file_location('gen5_offline_builder', BUILDER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.LOGS = str(self.directory)
        module.WORK = str(self.directory)
        module.ENV = os.environ.copy()
        return module.verify_boot_signatures(str(self.original), str(self.output),
                                             str(REPACK), str(AVBTOOL))

    def test_offline_builder_uses_signed_metadata_gate(self):
        result = self.repack()
        self.assertEqual(result.returncode, 0, result.stderr)
        verified, log, info = self.offline_gate()
        self.assertEqual(verified['metadata_algorithm'], 'SHA256_RSA4096')
        self.assertTrue(json.loads(Path(log).read_text())['signature_valid'])
        self.assertIn('SHA256_RSA4096', Path(info).read_text())

    def test_offline_builder_rejects_missing_signed_metadata(self):
        self.make_source(algorithm='NONE')
        self.output.write_bytes(self.original.read_bytes())
        with self.assertRaises(SystemExit):
            self.offline_gate()

    def test_write_gate_rejects_changed_signed_metadata(self):
        result = self.repack()
        self.assertEqual(result.returncode, 0, result.stderr)
        after = bytearray(self.output.read_bytes())
        offset = struct.unpack_from('>Q', after, len(after) - 44)[0]
        after[offset + 256 + 40] ^= 1
        self.output.write_bytes(after)
        with self.assertRaises(SystemExit):
            self.module.verify_signed_metadata(self.original, self.output, AVBTOOL)

    def test_write_gate_accepts_exact_signed_metadata(self):
        result = self.repack()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.module.verify_signed_metadata(self.original, self.output, AVBTOOL)
        self.assertTrue(report['signature_valid'])
        self.assertTrue(report['signed_metadata_preserved'])
        self.assertEqual(report['metadata_algorithm'], 'SHA256_RSA4096')


if __name__ == '__main__':
    unittest.main()