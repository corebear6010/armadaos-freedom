#!/usr/bin/python3
"""P001 source identity contract tests for the EDL raw producer.

The producer currently hardpins 244b745 and rejects every other revision.
These tests describe the future contract instead: an arbitrary nonzero
lowercase 40-hex revision flows through --source-commit, is checked against
the source checkout HEAD, and is cross-checked against the OCI revision
label and manifest digest without weakening provenance.

Every test is behavioural and pure: the producer is loaded through
importlib, git and OCI access go through the module helpers, and no
subprocess, network or block device is touched. RED here is expected until
the producer implements validate_source_checkout and
validate_image_provenance and drops the pinned constant.
"""

import contextlib
import copy
import importlib.machinery
import importlib.util
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PRODUCER = ROOT / "build_files/make-edl-raw-package.py"

BOOT_UUID = "1b4e28ba-2fa1-11d2-883f-0016d3cca427"
ROOT_UUID = "0f8fad5b-d9cb-469f-a165-70867728950e"

# The revision that the producer must no longer hardcode. It stays a legal
# value only because it is itself a nonzero lowercase 40-hex revision.
LEGACY_PINNED_COMMIT = "244b74516d99d06cb0518609ce4b842ab278be8a"

# Two distinct future revisions: accepting only one of them would still be a
# disguised hardpin.
FUTURE_COMMIT = "9c1f4d7a2b8e50316fd4a9c27e0b58d316a4f7c2"
OTHER_COMMIT = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"

MANIFEST_DIGEST = "sha256:" + "3f" * 32
OTHER_MANIFEST_DIGEST = "sha256:" + "7a" * 32

# Deliberately arbitrary and not derived from any commit: the version must be
# carried verbatim into the OCI version label check.
VERSION = "2026.10-freedom-rc7"
SOURCE = "/srv/armada-freedom-source"


def _load_script(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


producer = _load_script("make_edl_raw_package_source_identity", PRODUCER)


def provenance_config(commit=FUTURE_COMMIT, version=VERSION):
    return {
        "architecture": "arm64",
        "os": "linux",
        "config": {
            "Labels": {
                "org.opencontainers.image.revision": commit,
                "org.opencontainers.image.version": version,
            }
        },
    }


def cli_argv(source_commit=FUTURE_COMMIT):
    return [
        "--source",
        SOURCE,
        "--oci",
        "registry.example/armada/freedom:seed",
        "--manifest-digest",
        MANIFEST_DIGEST,
        "--origin",
        "registry.example/armada/freedom",
        "--runtime",
        "/srv/runtime/TB322FC.bin",
        "--boot-uuid",
        BOOT_UUID,
        "--root-uuid",
        ROOT_UUID,
        "--version",
        VERSION,
        "--work",
        "/srv/work",
        "--out",
        "/srv/out",
        "--source-commit",
        source_commit,
    ]


class BuildOptionsSourceCommitTests(unittest.TestCase):
    def test_accepts_arbitrary_nonzero_40_hex_commit_verbatim(self):
        for gib in (32, 48):
            for commit in (FUTURE_COMMIT, OTHER_COMMIT):
                with self.subTest(gib=gib, commit=commit):
                    options = producer.validate_build_options(
                        gib, BOOT_UUID, ROOT_UUID, commit
                    )
                    self.assertEqual(str(options["sourceCommit"]), commit)
                    self.assertNotEqual(str(options["sourceCommit"]), LEGACY_PINNED_COMMIT)
                    self.assertEqual(options["rootBytes"], gib * (1 << 30))
                    self.assertEqual(options["bootBytes"], 1 << 30)

    def test_still_accepts_the_legacy_revision_as_ordinary_input(self):
        options = producer.validate_build_options(
            32, BOOT_UUID, ROOT_UUID, LEGACY_PINNED_COMMIT
        )
        self.assertEqual(str(options["sourceCommit"]), LEGACY_PINNED_COMMIT)

    def test_rejects_zero_uppercase_and_malformed_commit(self):
        bad_commits = (
            "0" * 40,
            FUTURE_COMMIT.upper(),
            FUTURE_COMMIT[:39],
            FUTURE_COMMIT + "0",
            "",
            " " + FUTURE_COMMIT,
            FUTURE_COMMIT + "\n",
            "g" * 40,
        )
        for commit in bad_commits:
            with self.subTest(commit=commit):
                with self.assertRaises(ValueError):
                    producer.validate_build_options(32, BOOT_UUID, ROOT_UUID, commit)


class ImageProvenanceTests(unittest.TestCase):
    def validate(self, config, actual=MANIFEST_DIGEST, expected=MANIFEST_DIGEST,
                 commit=FUTURE_COMMIT, version=VERSION):
        return producer.validate_image_provenance(
            config, actual, expected, commit, version
        )

    def test_accepts_consistent_linux_arm64_provenance(self):
        for commit in (FUTURE_COMMIT, OTHER_COMMIT, LEGACY_PINNED_COMMIT):
            with self.subTest(commit=commit):
                self.assertIsNone(self.validate(provenance_config(commit=commit), commit=commit))

    def test_version_is_compared_exactly_without_244b745(self):
        for version in (VERSION, "1.0.0", "2026.10.24-nightly+sha", "v2"):
            with self.subTest(version=version):
                config = provenance_config(version=version)
                self.assertIsNone(
                    self.validate(config, commit=FUTURE_COMMIT, version=version)
                )

    def test_rejects_every_weakened_provenance_fact(self):
        cases = [
            (
                "manifest digest mismatch",
                provenance_config(),
                OTHER_MANIFEST_DIGEST,
                MANIFEST_DIGEST,
                FUTURE_COMMIT,
                VERSION,
            ),
            (
                "malformed actual manifest digest",
                provenance_config(),
                "sha256:3f",
                MANIFEST_DIGEST,
                FUTURE_COMMIT,
                VERSION,
            ),
            (
                "revision label mismatch",
                provenance_config(commit=OTHER_COMMIT),
                MANIFEST_DIGEST,
                MANIFEST_DIGEST,
                FUTURE_COMMIT,
                VERSION,
            ),
            (
                "uppercase revision label",
                provenance_config(commit=FUTURE_COMMIT.upper()),
                MANIFEST_DIGEST,
                MANIFEST_DIGEST,
                FUTURE_COMMIT,
                VERSION,
            ),
            (
                "version label mismatch",
                provenance_config(version="2026.10-freedom-rc6"),
                MANIFEST_DIGEST,
                MANIFEST_DIGEST,
                FUTURE_COMMIT,
                VERSION,
            ),
            (
                "version label case mismatch",
                provenance_config(version=VERSION.upper()),
                MANIFEST_DIGEST,
                MANIFEST_DIGEST,
                FUTURE_COMMIT,
                VERSION,
            ),
            (
                "architecture mismatch",
                dict(provenance_config(), architecture="amd64"),
                MANIFEST_DIGEST,
                MANIFEST_DIGEST,
                FUTURE_COMMIT,
                VERSION,
            ),
            (
                "os mismatch",
                dict(provenance_config(), os="darwin"),
                MANIFEST_DIGEST,
                MANIFEST_DIGEST,
                FUTURE_COMMIT,
                VERSION,
            ),
            (
                "labels absent",
                {"architecture": "arm64", "os": "linux"},
                MANIFEST_DIGEST,
                MANIFEST_DIGEST,
                FUTURE_COMMIT,
                VERSION,
            ),
            (
                "labels malformed",
                {
                    "architecture": "arm64",
                    "os": "linux",
                    "config": {"Labels": [FUTURE_COMMIT, VERSION]},
                },
                MANIFEST_DIGEST,
                MANIFEST_DIGEST,
                FUTURE_COMMIT,
                VERSION,
            ),
        ]
        for label, config, actual, expected, commit, version in cases:
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    self.validate(config, actual, expected, commit, version)

    def test_does_not_mutate_the_image_config(self):
        config = provenance_config()
        before = copy.deepcopy(config)
        self.assertIsNone(self.validate(config))
        self.assertEqual(config, before)


class SourceCheckoutTests(unittest.TestCase):
    def test_matches_head_and_queries_the_given_checkout(self):
        with mock.patch.object(
            producer, "_run", return_value=FUTURE_COMMIT + "\n"
        ) as run:
            result = producer.validate_source_checkout(SOURCE, FUTURE_COMMIT)
        self.assertIsNone(result)
        run.assert_called_once()
        argv = [str(part) for part in run.call_args[0][0]]
        self.assertEqual(argv, ["git", "-C", SOURCE, "rev-parse", "HEAD"])

    def test_rejects_checkout_head_that_differs(self):
        with mock.patch.object(producer, "_run", return_value=OTHER_COMMIT + "\n"):
            with self.assertRaises(ValueError):
                producer.validate_source_checkout(SOURCE, FUTURE_COMMIT)

    def test_tolerates_surrounding_whitespace_from_git(self):
        with mock.patch.object(
            producer, "_run", return_value="  " + FUTURE_COMMIT + "  \n"
        ):
            self.assertIsNone(producer.validate_source_checkout(SOURCE, FUTURE_COMMIT))


class SourceCommitCliTests(unittest.TestCase):
    def test_cli_requires_and_forwards_source_commit(self):
        with mock.patch.object(producer, "_produce") as produce:
            self.assertEqual(producer.main(cli_argv()), 0)
        produce.assert_called_once()
        args = produce.call_args[0][0]
        self.assertEqual(args.source_commit, FUTURE_COMMIT)
        self.assertEqual(args.version, VERSION)

    def test_cli_rejects_missing_source_commit(self):
        argv = cli_argv()
        index = argv.index("--source-commit")
        del argv[index:index + 2]
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                producer.main(argv)
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--source-commit", stderr.getvalue())

    def test_cli_forwards_an_unrelated_future_revision(self):
        with mock.patch.object(producer, "_produce") as produce:
            self.assertEqual(producer.main(cli_argv(OTHER_COMMIT)), 0)
        self.assertEqual(produce.call_args[0][0].source_commit, OTHER_COMMIT)


if __name__ == "__main__":
    unittest.main()
