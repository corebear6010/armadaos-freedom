#!/usr/bin/python3
"""RED contract tests for the generic per-model profile producer.

These tests describe the contract the producer has to grow into: model
profiles come from a validated devices configuration, runtime keys are the
profile IDs of that configuration instead of a fixed table, and the boot
recipe is derived from profile data only. Nothing here is model specific, so
a third arbitrary profile must behave exactly like the checked-in ones.

Every case is expected to fail until the producer is generalized.
"""

import importlib.machinery
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PRODUCER = ROOT / "build_files/make-edl-raw-package.py"

MIB = 1 << 20
RUNTIME_BYTES = 4 * MIB
BOOT_FORMAT = "android-boot-v4"
BOOT_PARTITION_BYTES = 100663296
OTHER_BOOT_PARTITION_BYTES = 134217728

# Arbitrary synthetic profile IDs; "THIRD" only proves the table is not fixed.
MODEL_ORDER = ("A", "B", "THIRD")

VERBOSE_MODE = "verbose"
RDBREAK_MODE = "rd-break"
DIAGNOSTIC_TAIL = (
    "loglevel=8",
    "ignore_loglevel",
    "printk.time=1",
    "initcall_debug",
    "rd.debug",
)


def _load_script(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


producer = _load_script("make_edl_raw_package_profiles", PRODUCER)


def model_profile(
    model,
    boot_partition_bytes=BOOT_PARTITION_BYTES,
    diagnostic_mode=VERBOSE_MODE,
    extra_args=(),
):
    """One synthetic profile shaped like build_files/edl-devices.json."""
    return {
        "displayName": "Synthetic " + model,
        "compatible": {"soc": "SYN-" + model.upper(), "device": model.lower()},
        "dtb": "synthetic/" + model.lower() + ".dtb",
        "required": [model.lower() + ",board", "synthetic,platform"],
        "forbidden": [model.lower() + ",other", "synthetic,foreign"],
        "extraArgs": list(extra_args),
        "runtimeConfig": "configs/" + model.lower() + "_defconfig",
        "runtimeCmakeArgs": ["-DSYNTHETIC_MODEL=" + model],
        "bootPartitionBytes": boot_partition_bytes,
        "diagnosticMode": diagnostic_mode,
    }


def synthetic_configuration(order=MODEL_ORDER, **overrides):
    configuration = {
        "schemaVersion": 1,
        "runtimeSource": "devices/synthetic/boot/runtime-source",
        "profiles": {model: model_profile(model) for model in order},
    }
    configuration.update(overrides)
    return configuration


class RequestedModelTests(unittest.TestCase):
    def setUp(self):
        self.configuration = synthetic_configuration()

    def _runtimes(self, models=MODEL_ORDER):
        return {
            model: {"path": "/synthetic/" + model + ".runtime", "bytes": RUNTIME_BYTES}
            for model in models
        }

    def test_default_requires_every_configured_profile_in_config_order(self):
        runtimes = self._runtimes()
        reversed_runtimes = {model: runtimes[model] for model in reversed(MODEL_ORDER)}
        for candidate in (runtimes, reversed_runtimes):
            with self.subTest(order=tuple(candidate)):
                selected = producer._requested_models(candidate, None, self.configuration)
                self.assertIsInstance(selected, (list, tuple))
                self.assertEqual(list(selected), list(MODEL_ORDER))
                self.assertIn("THIRD", list(selected))

    def test_subset_is_returned_in_config_order(self):
        selected = producer._requested_models(
            self._runtimes(("A", "THIRD")), ["THIRD", "A"], self.configuration
        )
        self.assertEqual(list(selected), ["A", "THIRD"])

    def test_incomplete_or_mismatched_selection_is_rejected(self):
        cases = (
            ("missing configured runtime", self._runtimes(("A", "B")), None),
            ("runtime outside the configuration", dict(self._runtimes(), UNLISTED={}), None),
            ("no runtimes at all", {}, None),
            ("unknown requested model", self._runtimes(), ["A", "ZZZ"]),
            ("duplicate requested model", self._runtimes(), ["A", "A"]),
            ("empty requested list", self._runtimes(), []),
            ("extra runtime outside the requested subset", self._runtimes(), ["THIRD", "A"]),
            ("requested runtime not supplied", self._runtimes(("A",)), ["THIRD", "A"]),
        )
        for label, runtimes, requested in cases:
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    producer._requested_models(runtimes, requested, self.configuration)


class RuntimeParsingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="edl-profiles-runtime.")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.configuration = synthetic_configuration()
        self.runtime = self._sparse_runtime("runtime.bin")

    def _sparse_runtime(self, name, size=RUNTIME_BYTES):
        path = self.root / name
        with open(path, "wb") as stream:
            stream.truncate(size)
        return path

    def test_dynamic_profile_id_is_accepted(self):
        parsed = producer._parse_runtimes(
            ["THIRD=" + str(self.runtime)], profiles=self.configuration["profiles"]
        )
        self.assertEqual(list(parsed), ["THIRD"])
        self.assertEqual(Path(parsed["THIRD"]["path"]).resolve(), self.runtime.resolve())
        self.assertEqual(parsed["THIRD"]["bytes"], RUNTIME_BYTES)

    def test_unknown_profile_id_is_rejected(self):
        for profiles in (self.configuration["profiles"], None):
            with self.subTest(profiles=None if profiles is None else sorted(profiles)):
                with self.assertRaises(ValueError):
                    producer._parse_runtimes(["GHOST=" + str(self.runtime)], profiles=profiles)

    def test_duplicate_profile_mapping_is_rejected(self):
        with self.assertRaises(ValueError):
            producer._parse_runtimes(
                ["THIRD=" + str(self.runtime), "THIRD=" + str(self.runtime)],
                profiles=self.configuration["profiles"],
            )

    def test_runtime_size_must_be_exactly_4mib(self):
        for size in (RUNTIME_BYTES - 1, RUNTIME_BYTES + 1, 0):
            with self.subTest(size=size):
                wrong = self._sparse_runtime("runtime-%d.bin" % size, size=size)
                with self.assertRaises(ValueError):
                    producer._parse_runtimes(
                        ["THIRD=" + str(wrong)], profiles=self.configuration["profiles"]
                    )

    def test_bare_path_is_rejected_for_multiple_profiles(self):
        for profiles in (self.configuration["profiles"], None):
            with self.subTest(profiles=None if profiles is None else sorted(profiles)):
                with self.assertRaises(ValueError):
                    producer._parse_runtimes([str(self.runtime)], profiles=profiles)

    def test_bare_path_maps_to_the_unique_profile(self):
        parsed = producer._parse_runtimes(
            [str(self.runtime)], profiles={"THIRD": model_profile("THIRD")}
        )
        self.assertEqual(list(parsed), ["THIRD"])
        self.assertEqual(Path(parsed["THIRD"]["path"]).resolve(), self.runtime.resolve())


class BootCompatibilityTests(unittest.TestCase):
    def test_boot_compatibility_follows_the_profile_partition_size(self):
        for size in (BOOT_PARTITION_BYTES, OTHER_BOOT_PARTITION_BYTES):
            with self.subTest(size=size):
                profile = model_profile("THIRD", boot_partition_bytes=size)
                self.assertEqual(
                    producer._boot_compatibility(profile),
                    {"format": BOOT_FORMAT, "partitionBytes": size},
                )


class DiagnosticBootArgsTests(unittest.TestCase):
    def test_verbose_mode_cleans_quiet_loglevel_and_rd_break(self):
        normal = (
            "rw console=tty0 root=UUID=1111 boot=UUID=2222 quiet "
            "loglevel=3 loglevel=7 rd.break rd.break=pre-mount "
            "ignore_loglevel printk.time=1 rd.debug console=tty0"
        )
        expected = [
            "rw",
            "console=tty0",
            "root=UUID=1111",
            "boot=UUID=2222",
            "ignore_loglevel",
            "printk.time=1",
            "rd.debug",
            "console=tty0",
            "loglevel=8",
            "initcall_debug",
        ]
        result = producer._diagnostic_boot_args(
            normal, model_profile("THIRD", diagnostic_mode=VERBOSE_MODE)
        )
        self.assertEqual(result, " ".join(expected))
        self.assertEqual(result.split(), expected)

    def test_verbose_mode_keeps_duplicate_tokens_and_appends_nothing_twice(self):
        normal = "rw rd.debug rd.debug printk.time=1 printk.time=1 loglevel=4"
        expected = [
            "rw",
            "rd.debug",
            "rd.debug",
            "printk.time=1",
            "printk.time=1",
            "loglevel=8",
            "ignore_loglevel",
            "initcall_debug",
        ]
        result = producer._diagnostic_boot_args(
            normal, model_profile("THIRD", diagnostic_mode=VERBOSE_MODE)
        )
        self.assertEqual(result, " ".join(expected))
        self.assertEqual(result.split(), expected)

    def test_verbose_mode_normalizes_existing_loglevel_to_append_position(self):
        normal = "rw loglevel=8 ignore_loglevel printk.time=1 initcall_debug rd.debug"
        expected = [
            "rw",
            "ignore_loglevel",
            "printk.time=1",
            "initcall_debug",
            "rd.debug",
            "loglevel=8",
        ]
        result = producer._diagnostic_boot_args(
            normal, model_profile("THIRD", diagnostic_mode=VERBOSE_MODE)
        )
        self.assertEqual(result, " ".join(expected))
        self.assertEqual(result.split(), expected)

    def test_verbose_mode_appends_the_full_tail_to_an_empty_recipe(self):
        result = producer._diagnostic_boot_args(
            "quiet", model_profile("THIRD", diagnostic_mode=VERBOSE_MODE)
        )
        self.assertEqual(result, " ".join(DIAGNOSTIC_TAIL))
        self.assertEqual(result.split(), list(DIAGNOSTIC_TAIL))

    def test_rd_break_mode_only_appends_the_break_argument(self):
        normal = "rw quiet loglevel=3 root=UUID=1111"
        breaking = producer._diagnostic_boot_args(
            normal, model_profile("THIRD", diagnostic_mode=RDBREAK_MODE)
        )
        self.assertEqual(breaking, normal + " rd.break=pre-mount")

        other = producer._diagnostic_boot_args(
            normal, model_profile("A", diagnostic_mode=RDBREAK_MODE)
        )
        self.assertEqual(other, breaking)


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="edl-profiles-cli.")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.configuration_path = self.root / "custom-devices.json"
        self.configuration_path.write_text(
            json.dumps(synthetic_configuration(), indent=2) + "\n", encoding="utf-8"
        )
        self.runtime = self.root / "runtime.bin"
        with open(self.runtime, "wb") as stream:
            stream.truncate(RUNTIME_BYTES)

    def _argv(self):
        return [
            "--source", str(self.root / "source"),
            "--source-commit", "0123456789abcdef0123456789abcdef01234567",
            "--oci", "ghcr.io/synthetic/armada:1.4.0",
            "--manifest-digest", "sha256:" + "b" * 64,
            "--origin", "ghcr.io/synthetic/armada:1.4.0",
            "--devices-config", str(self.configuration_path),
            "--runtime", "THIRD=" + str(self.runtime),
            "--runtime", "A=" + str(self.runtime),
            "--model", "THIRD",
            "--model", "A",
            "--boot-uuid", "1b4e28ba-2fa1-11d2-883f-0016d3cca427",
            "--root-uuid", "0f8fad5b-d9cb-469f-a165-70867728950e",
            "--version", "2026.10.24",
            "--work", str(self.root / "work"),
            "--out", str(self.root / "out"),
        ]

    def test_models_and_custom_devices_config_reach_the_producer(self):
        with mock.patch.object(producer, "_produce") as produce:
            code = producer.main(self._argv())
        self.assertEqual(code, 0)
        produce.assert_called_once()
        args = produce.call_args[0][0]
        self.assertEqual(list(args.model), ["THIRD", "A"])
        self.assertEqual(
            Path(str(args.devices_config)).resolve(), self.configuration_path.resolve()
        )


if __name__ == "__main__":
    unittest.main()
