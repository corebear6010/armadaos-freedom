"""Behavioral tests for build_files/split-package-archive.py."""

import hashlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HELPER_PATH = Path(__file__).resolve().parents[1] / "build_files" / "split-package-archive.py"


def _load_helper():
    spec = importlib.util.spec_from_file_location("split_package_archive_helper", HELPER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load helper from %s" % HELPER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper = _load_helper()


def _temp_root():
    root = os.environ.get("TEST_ARTIFACT_ROOT")
    if not root:
        raise RuntimeError("TEST_ARTIFACT_ROOT must be set to an existing directory")
    if not Path(root).is_dir():
        raise RuntimeError("TEST_ARTIFACT_ROOT is not an existing directory: %s" % root)
    return root


class SplitArchiveTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=_temp_root())
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _make_input(self, payload, folder="src"):
        directory = self.root / folder
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "bundle.zip"
        path.write_bytes(payload)
        return path

    def test_byte_partition_boundaries(self):
        cases = [(10, 5, [5, 5]), (11, 5, [5, 5, 1]), (3, 5, [3])]
        for size, part_bytes, expected_sizes in cases:
            payload = bytes(range(size))
            with self.subTest(size=size, part_bytes=part_bytes):
                src = self._make_input(payload, folder="src-%d-%d" % (size, part_bytes))
                out = self.root / ("out-%d-%d" % (size, part_bytes))
                manifest = helper.split_archive(src, out, part_bytes=part_bytes)

                names = ["bundle.zip.%03d" % i for i in range(1, len(expected_sizes) + 1)]
                self.assertEqual([p["fileName"] for p in manifest["parts"]], names)
                volumes = [out / name for name in names]
                self.assertEqual([v.stat().st_size for v in volumes], expected_sizes)
                self.assertEqual([p["bytes"] for p in manifest["parts"]], expected_sizes)
                self.assertEqual(b"".join(v.read_bytes() for v in volumes), payload)
                self.assertEqual(src.read_bytes(), payload)

                self.assertEqual(manifest["schemaVersion"], 1)
                self.assertEqual(manifest["format"], "raw-split")
                self.assertEqual(manifest["partSizeBytes"], part_bytes)
                self.assertEqual(manifest["archive"]["fileName"], "bundle.zip")
                self.assertEqual(manifest["archive"]["bytes"], size)
                self.assertEqual(
                    manifest["archive"]["sha256"], hashlib.sha256(payload).hexdigest()
                )

                offset = 0
                for entry, expected_size in zip(manifest["parts"], expected_sizes):
                    chunk = payload[offset:offset + expected_size]
                    self.assertEqual(entry["sha256"], hashlib.sha256(chunk).hexdigest())
                    offset += expected_size
                self.assertEqual(offset, size)

                self.assertEqual(
                    sorted(p.name for p in out.iterdir()),
                    sorted(names + ["bundle.zip.parts.json"]),
                )
                on_disk = json.loads(
                    (out / "bundle.zip.parts.json").read_text(encoding="utf-8")
                )
                self.assertEqual(on_disk, manifest)

    def test_cli_default_part_bytes(self):
        payload = b"PK\x03\x04tiny-default-payload"
        src = self._make_input(payload)
        out = self.root / "out"
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = helper.main(["--input", str(src), "--output", str(out)])

        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        manifest = json.loads(stdout.getvalue())
        self.assertEqual(manifest["partSizeBytes"], 1992294400)
        self.assertEqual(len(manifest["parts"]), 1)
        self.assertEqual(manifest["parts"][0]["bytes"], len(payload))
        self.assertEqual((out / "bundle.zip.001").read_bytes(), payload)
        self.assertEqual(manifest["archive"]["sha256"], hashlib.sha256(payload).hexdigest())

    def test_invalid_part_bytes_rejected(self):
        src = self._make_input(b"abcde")
        bad_values = [True, False, 0, -1, 2147483648, 2147483649, 1.5, "5"]
        for index, value in enumerate(bad_values):
            with self.subTest(value=value):
                out = self.root / ("bad-%d" % index)
                with self.assertRaises(ValueError):
                    helper.split_archive(src, out, part_bytes=value)
                self.assertFalse(out.exists())

    def test_max_part_bytes_accepted(self):
        payload = b"tiny"
        src = self._make_input(payload)
        out = self.root / "out"
        manifest = helper.split_archive(src, out, part_bytes=2147483647)
        self.assertEqual(manifest["partSizeBytes"], 2147483647)
        self.assertEqual(len(manifest["parts"]), 1)
        self.assertEqual((out / "bundle.zip.001").read_bytes(), payload)

    def test_existing_part_collision(self):
        payload = bytes(range(10))
        src = self._make_input(payload)
        out = self.root / "out"
        out.mkdir()
        keeper = out / "bundle.zip.001"
        keeper.write_bytes(b"keeper-bytes")
        neighbor = out / "keep.txt"
        neighbor.write_bytes(b"neighbor")

        with self.assertRaises(FileExistsError):
            helper.split_archive(src, out, part_bytes=5)

        self.assertEqual(keeper.read_bytes(), b"keeper-bytes")
        self.assertEqual(neighbor.read_bytes(), b"neighbor")
        self.assertEqual(src.read_bytes(), payload)
        self.assertEqual(
            sorted(p.name for p in out.iterdir()), ["bundle.zip.001", "keep.txt"]
        )

    def test_existing_manifest_collision(self):
        payload = b"abcde"
        src = self._make_input(payload)
        out = self.root / "out"
        out.mkdir()
        manifest_path = out / "bundle.zip.parts.json"
        manifest_path.write_bytes(b"{}")

        with self.assertRaises(FileExistsError):
            helper.split_archive(src, out, part_bytes=5)

        self.assertEqual(manifest_path.read_bytes(), b"{}")
        self.assertEqual(src.read_bytes(), payload)
        self.assertEqual(
            sorted(p.name for p in out.iterdir()), ["bundle.zip.parts.json"]
        )

    def test_expected_sha_mismatch_removes_only_owned_outputs(self):
        payload = bytes(range(11))
        src = self._make_input(payload)
        out = self.root / "out"
        out.mkdir()
        keep = out / "keep.txt"
        keep.write_bytes(b"keep")

        with self.assertRaises(ValueError):
            helper.split_archive(src, out, part_bytes=5, expected_sha256="0" * 64)

        self.assertEqual(keep.read_bytes(), b"keep")
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["keep.txt"])
        self.assertEqual(src.read_bytes(), payload)

    def test_expected_sha_uppercase_accepted(self):
        payload = bytes(range(7))
        src = self._make_input(payload)
        out = self.root / "out"
        digest = hashlib.sha256(payload).hexdigest().upper()

        manifest = helper.split_archive(src, out, part_bytes=5, expected_sha256=digest)

        self.assertEqual(manifest["archive"]["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(len(manifest["parts"]), 2)
        self.assertEqual((out / "bundle.zip.001").read_bytes(), payload[:5])
        self.assertEqual((out / "bundle.zip.002").read_bytes(), payload[5:])

    def test_empty_and_missing_input_rejected(self):
        out = self.root / "out"
        empty = self.root / "empty-src" / "bundle.zip"
        empty.parent.mkdir()
        empty.write_bytes(b"")

        with self.assertRaises(ValueError):
            helper.split_archive(empty, out, part_bytes=5)
        self.assertFalse(out.exists())

        missing = self.root / "missing-src" / "bundle.zip"
        with self.assertRaises(ValueError):
            helper.split_archive(missing, out, part_bytes=5)
        self.assertFalse(out.exists())

    def test_cli_expected_sha_mismatch(self):
        payload = bytes(range(10))
        src = self._make_input(payload)
        out = self.root / "out"
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = helper.main([
                "--input", str(src),
                "--output", str(out),
                "--part-bytes", "5",
                "--expected-sha256", "0" * 64,
            ])

        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("sha256", stderr.getvalue().lower())
        owned = list(out.iterdir()) if out.exists() else []
        self.assertEqual(owned, [])
        self.assertEqual(src.read_bytes(), payload)


if __name__ == "__main__":
    unittest.main()
