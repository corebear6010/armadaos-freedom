#!/usr/bin/python3
"""Behaviour tests for armada-kshim-sync.

Real temporary BOOT/SYSROOT trees, real native-style BLS entries and ostree
symlinks, the real selector semantics, and the real writer report validators.
Only native status, the device-profile probe, the root check, the block
target boundary, the packer wrapper, and the writer invocation are replaced at
ordinary function level. No device, root, network, ostree command, or real boot
image tool is touched here.
"""
import importlib.machinery
import importlib.util
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
LIBEXEC = ROOT / "system_files/usr/libexec/armada"
SYNC = LIBEXEC / "armada-kshim-sync"
ARGS_FILE = ROOT / "system_files/usr/lib/armada/bootimg-args"

loader = importlib.machinery.SourceFileLoader("armada_kshim_sync_t024",
                                              str(SYNC))
spec = importlib.util.spec_from_loader(loader.name, loader)
sync = importlib.util.module_from_spec(spec)
loader.exec_module(sync)

SELECTOR = sync.load_selector(str(LIBEXEC))
WRITER = sync.load_writer(str(LIBEXEC))
REAL_CHECK_PROFILE = sync.check_profile

SMALL = 4096
MAGIC = b"ANDROID!"
PARTUUID = "11111111-2222-3333-4444-555555555555"
ROOT_UUID = "aaaaaaaa-1111-2222-3333-444444444444"
BOOT_UUID = "bbbbbbbb-1111-2222-3333-444444444444"
COMMIT_A = "a" * 64
COMMIT_B = "b" * 64


def sha(data):
    return hashlib.sha256(data).hexdigest()


def old_bytes():
    return MAGIC + b"O" * (SMALL - len(MAGIC))


def record(checksum, serial, index, booted=False, stateroot="default",
           staged=False):
    return {
        "checksum": checksum,
        "stateroot": stateroot,
        "refspec": None,
        "serial": serial,
        "index": index,
        "booted": booted,
        "pending": False,
        "rollback": False,
        "finalization-locked": False,
        "soft-reboot-target": False,
        "staged": staged,
        "pinned": False,
        "unlocked": "none",
    }


def status_document(*records):
    return {"deployments": list(records)}


class Tree:
    """Disposable BOOT/SYSROOT with native-style deployments and BLS entries."""

    def __init__(self, root):
        self.root = Path(root)
        self.boot = self.root / "boot"
        self.sysroot = self.root / "sysroot"
        (self.boot / "loader/entries").mkdir(parents=True)
        (self.boot / "dtb/qcom").mkdir(parents=True)
        (self.boot / "linux-A").write_bytes(b"KERNEL-A")
        (self.boot / "linux-B").write_bytes(b"KERNEL-B")
        (self.boot / "initrd-A.img").write_bytes(b"INITRD-A")
        (self.boot / "initrd-B.img").write_bytes(b"INITRD-B")
        (self.boot / "dtb/qcom/sm8750-lenovo-elden.dtb").write_bytes(b"DTB")
        self.meta = {}
        self.slots = {}

    def deployment(self, commit, serial, stateroot="default"):
        path = (self.sysroot / "ostree/deploy" / stateroot / "deploy"
                / ("%s.%d" % (commit, serial)))
        path.mkdir(parents=True)
        return path

    def slot(self, slot, boot_csum, commit, serial, stateroot="default"):
        key = (slot, boot_csum, serial, stateroot)
        if key in self.slots:
            return self.slots[key]
        link = self.sysroot / "ostree" / slot / stateroot / boot_csum / str(serial)
        link.parent.mkdir(parents=True, exist_ok=True)
        deploy = (self.sysroot / "ostree/deploy" / stateroot / "deploy"
                  / ("%s.%d" % (commit, serial)))
        os.symlink(os.path.relpath(deploy, link.parent), link)
        value = "/ostree/%s/%s/%s/%d" % (slot, stateroot, boot_csum, serial)
        self.slots[key] = value
        return value

    def bls(self, name, version, ostree, commit, serial, stateroot="default",
            kernel="linux-A", initrd="initrd-A.img", fdtdir="dtb"):
        rel = "loader/entries/%s" % name
        options = ("ostree=%s root=UUID=%s boot=UUID=%s rw quiet"
                   % (ostree, ROOT_UUID, BOOT_UUID))
        (self.boot / rel).write_text("\n".join([
            "title %s" % name,
            "version %d" % version,
            "linux %s" % kernel,
            "initrd %s" % initrd,
            "fdtdir %s" % fdtdir,
            "options %s" % options,
        ]) + "\n", encoding="utf-8")
        self.meta[rel] = {
            "checksum": commit, "serial": serial, "stateroot": stateroot,
            "options": options, "image": kernel, "initrd": initrd,
            "fdtdir": fdtdir,
        }
        return rel

    def rewrite_options(self, entry, options):
        path = self.boot / entry
        lines = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("options "):
                line = "options " + options
            lines.append(line)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_pair(tree):
    tree.deployment(COMMIT_A, 1)
    tree.deployment(COMMIT_B, 2)
    a_link = tree.slot("boot.1", "1" * 64, COMMIT_A, 1)
    b_link = tree.slot("boot.0", "2" * 64, COMMIT_B, 2)
    candidate = tree.bls("candidate.conf", 203, a_link, COMMIT_A, 1,
                         kernel="linux-A", initrd="initrd-A.img")
    fallback = tree.bls("fallback.conf", 202, b_link, COMMIT_B, 2,
                        kernel="linux-B", initrd="initrd-B.img")
    return candidate, fallback, {"a": a_link, "b": b_link}


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(os.path.realpath(self.tmp.name))
        self.tree = Tree(base / "tree")
        self.cand, self.fall, self.links = build_pair(self.tree)
        self.status = status_document(record(COMMIT_A, 1, 0),
                                      record(COMMIT_B, 2, 1, booted=True))

        self.stock = self.tree.root / "stock.img"
        self.stock.write_bytes(b"STOCK-IMAGE")
        self.stock_sha = sha(self.stock.read_bytes())
        self.runtime = self.tree.root / "runtime.img"
        self.runtime.write_bytes(b"RUNTIME-IMAGE")
        self.runtime_sha = sha(self.runtime.read_bytes())
        self.avb_key = self.tree.root / "testkey_rsa4096.pem"
        self.avb_key.write_bytes(b"PRIVATE-KEY")
        self.tools = self.tree.root / "tools"
        self.tools.mkdir()
        for name in sync.TOOL_FILES.values():
            (self.tools / name).write_bytes(b"TOOL")
        self.target = self.tree.root / "target.img"
        self.target.write_bytes(old_bytes())
        self.work = self.tree.root / "work"
        self.lock_path = self.tree.root / "updater.lock"

        self.prepare_calls = []
        self.writer_calls = []
        self.writer_code = 0
        self.prepare_fail_entry = None
        self.prepare_fail_suffix = None
        self.mutate_entry = None
        self.report_hash_override = {}
        self.report_check_fail_suffix = None
        self.break_link_during_fallback = False

        for patch in (
                mock.patch.object(sync, "check_profile", lambda: None),
                mock.patch.object(sync, "require_root", lambda: None),
                mock.patch.object(sync, "LOCK_PATH", str(self.lock_path)),
                mock.patch.object(sync, "load_selector",
                                  lambda libexec: SELECTOR),
                mock.patch.object(sync, "load_writer", lambda libexec: WRITER),
                mock.patch.object(WRITER, "BOOT_BYTES", SMALL),
                mock.patch.object(WRITER, "probe_target",
                                  lambda target, partuuid: {
                                      "path": str(target), "partuuid": partuuid,
                                      "rdev": 0, "major": 7, "minor": 0,
                                      "size": SMALL}),
                mock.patch.object(WRITER, "open_target_ro",
                                  lambda identity: os.open(identity["path"],
                                                           os.O_RDONLY)),
                mock.patch.object(sync, "run_prepare", self.fake_prepare),
                mock.patch.object(sync, "run_writer", self.fake_writer),
        ):
            patch.start()
        self.addCleanup(mock.patch.stopall)

    def fake_prepare(self, args, entry, out):
        self.prepare_calls.append((entry, out))
        if (self.prepare_fail_entry == entry
                and (self.prepare_fail_suffix is None
                     or out.endswith(self.prepare_fail_suffix))):
            return 1
        meta = self.tree.meta[entry]
        override = {}
        for suffix, values in self.report_hash_override.items():
            if out.endswith(suffix):
                override = values
        candidate_dir = os.path.join(out, "candidate")
        os.makedirs(candidate_dir, exist_ok=True)
        data = (MAGIC + entry.encode("utf-8")).ljust(SMALL, b"\x00")[:SMALL]
        image = os.path.join(candidate_dir, "boot.img")
        with open(image, "wb") as handle:
            handle.write(data)
        report = {
            "layout_mode": "installed",
            "offline_verified": True,
            "checks": [{"check": "installed:result-json",
                        "ok": not (self.report_check_fail_suffix is not None
                                   and out.endswith(
                                       self.report_check_fail_suffix))}],
            "inputs": {
                "stock_boot": {
                    "path": "/historical/stock.img",
                    "length": len(b"STOCK-IMAGE"),
                    "sha256": override.get("stock", self.stock_sha),
                },
                "runtime": {
                    "path": "/historical/runtime.img",
                    "length": len(b"RUNTIME-IMAGE"),
                    "sha256": override.get("runtime", self.runtime_sha),
                },
            },
            "outputs": {"final_boot": {"path": "/historical/boot.img",
                                       "length": SMALL,
                                       "sha256": sha(data)}},
            "installed_metadata": {
                "deployment_checksum": meta["checksum"],
                "filesystem_uuids": {"root": ROOT_UUID, "boot": BOOT_UUID},
                "bls": entry,
                "options": meta["options"],
                "image": meta["image"],
                "initrd": meta["initrd"],
                "fdtdir": meta["fdtdir"],
            },
        }
        with open(os.path.join(candidate_dir, "report.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(report, handle)
        if self.mutate_entry is not None and out.endswith("/fallback"):
            current = self.tree.meta[self.mutate_entry]["options"]
            self.tree.rewrite_options(self.mutate_entry,
                                      current + " armada.sync=changed")
        if self.break_link_during_fallback and out.endswith("/fallback"):
            link = (self.tree.sysroot / "ostree/boot.1/default"
                    / ("1" * 64) / "1")
            stale = (self.tree.sysroot / "ostree/deploy/default/deploy"
                     / ("%s.9" % ("c" * 64)))
            os.unlink(link)
            os.symlink(os.path.relpath(stale, link.parent), link)
        return 0

    def fake_writer(self, args, candidate, fallback, partuuid, expected,
                    transaction_dir):
        self.writer_calls.append({
            "candidate": candidate, "fallback": fallback,
            "partuuid": partuuid, "expected": expected,
            "dir": transaction_dir,
        })
        if self.writer_code not in (0, 2, 3):
            return self.writer_code
        if self.writer_code == 0:
            os.makedirs(transaction_dir, exist_ok=True)
            with open(os.path.join(transaction_dir, "transaction.json"), "w",
                      encoding="utf-8") as handle:
                json.dump({"status": "written"}, handle)
        return self.writer_code

    def plan_argv(self):
        return ["--plan-only", "--boot-dir", str(self.tree.boot),
                "--sysroot", str(self.tree.sysroot),
                "--libexec", str(LIBEXEC), "--args-file", str(ARGS_FILE)]

    def full_argv(self, overrides=None):
        values = {
            "--boot-dir": str(self.tree.boot),
            "--sysroot": str(self.tree.sysroot),
            "--libexec": str(LIBEXEC),
            "--args-file": str(ARGS_FILE),
            "--tools-dir": str(self.tools),
            "--stock-boot": str(self.stock),
            "--stock-sha256": self.stock_sha,
            "--runtime": str(self.runtime),
            "--runtime-sha256": self.runtime_sha,
            "--avb-key": str(self.avb_key),
            "--partuuid": PARTUUID,
            "--expected-current-sha256": sha(old_bytes()),
            "--work": str(self.work),
            "--target": str(self.target),
        }
        values.update(overrides or {})
        argv = []
        for key in sorted(values):
            argv.extend([key, values[key]])
        return argv

    def invoke(self, argv, status=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(sync, "read_native_status",
                               lambda: self.status if status is None else status):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = sync.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def result(self):
        return json.loads((Path(self.work) / "result.json")
                          .read_text(encoding="utf-8"))

    def test_correct_pair_and_fallback_prepared_first(self):
        code, out, err = self.invoke(self.full_argv())
        self.assertEqual(code, 0, err)
        self.assertEqual([entry for entry, _ in self.prepare_calls],
                         [self.fall, self.cand])
        self.assertTrue(self.prepare_calls[0][1].endswith("/fallback"))
        self.assertTrue(self.prepare_calls[1][1].endswith("/candidate"))
        self.assertEqual(len(self.writer_calls), 1)
        call = self.writer_calls[0]
        self.assertIn("/candidate/", call["candidate"]["image"])
        self.assertIn("/fallback/", call["fallback"]["image"])
        self.assertNotEqual(call["candidate"]["image"],
                            call["fallback"]["image"])

        plan = json.loads((Path(self.work) / "plan.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(plan["candidate"]["identity"],
                         {"checksum": COMMIT_A, "stateroot": "default",
                          "serial": 1})
        self.assertEqual(plan["booted"],
                         {"checksum": COMMIT_B, "stateroot": "default",
                          "serial": 2})
        self.assertEqual(plan["candidate"]["entry"], self.cand)
        self.assertEqual(plan["fallback"]["entry"], self.fall)

        result = self.result()
        self.assertEqual(result["phase"], "write")
        self.assertEqual(result["status"], "written")
        self.assertEqual(result["writer_exit"], 0)
        self.assertIsNone(result["error"])

    def test_reuses_fallback_payload_when_candidate_identity_matches(self):
        self.status = status_document(record(COMMIT_A, 1, 0, booted=True))
        code, out, err = self.invoke(self.full_argv())
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.prepare_calls), 1)
        self.assertEqual(self.prepare_calls[0][0], self.cand)
        call = self.writer_calls[0]
        self.assertEqual(call["candidate"]["image"], call["fallback"]["image"])
        self.assertEqual(call["candidate"]["report"], call["fallback"]["report"])

    def test_serial_and_stateroot_disambiguate_booted_identity(self):
        tree = Tree(Path(self.tmp.name) / "identity")
        tree.deployment(COMMIT_A, 1)
        tree.deployment(COMMIT_A, 5)
        tree.deployment(COMMIT_A, 5, stateroot="alt")
        a1 = tree.slot("boot.1", "1" * 64, COMMIT_A, 1)
        a5 = tree.slot("boot.0", "2" * 64, COMMIT_A, 5)
        alt5 = tree.slot("boot.1", "3" * 64, COMMIT_A, 5, stateroot="alt")
        candidate = tree.bls("cand.conf", 203, a1, COMMIT_A, 1)
        fallback = tree.bls("fall.conf", 202, a5, COMMIT_A, 5)
        tree.bls("alt.conf", 201, alt5, COMMIT_A, 5, stateroot="alt")
        self.tree = tree
        self.status = status_document(record(COMMIT_A, 1, 0),
                                      record(COMMIT_A, 5, 1, booted=True),
                                      record(COMMIT_A, 5, 2,
                                             stateroot="alt"))

        code, out, err = self.invoke(self.plan_argv())
        self.assertEqual(code, 0, err)
        plan = json.loads(out)
        self.assertEqual(plan["candidate"]["entry"], candidate)
        self.assertEqual(plan["fallback"]["entry"], fallback)
        self.assertEqual(plan["fallback"]["identity"],
                         {"checksum": COMMIT_A, "stateroot": "default",
                          "serial": 5})

    def test_staged_deployment_refused(self):
        cases = (
            status_document(record(COMMIT_A, 1, 0),
                            record(COMMIT_B, 2, 1, booted=True, staged=True)),
            status_document(record(COMMIT_A, 1, 0, staged=True),
                            record(COMMIT_B, 2, 1, booted=True)),
        )
        for staged in cases:
            with self.subTest(status=staged):
                code, out, err = self.invoke(self.plan_argv(), status=staged)
                self.assertEqual(code, 1)
                self.assertIn("staged", err)
                self.assertEqual(self.prepare_calls, [])
                self.assertEqual(self.writer_calls, [])

    def test_missing_or_ambiguous_booted_refused(self):
        missing = status_document(record(COMMIT_A, 1, 0),
                                  record(COMMIT_B, 2, 1))
        code, out, err = self.invoke(self.plan_argv(), status=missing)
        self.assertEqual(code, 1)
        self.assertIn("exactly one booted deployment", err)

        self.tree.bls("duplicate.conf", 201, self.links["b"], COMMIT_B, 2,
                      kernel="linux-B", initrd="initrd-B.img")
        code, out, err = self.invoke(self.plan_argv())
        self.assertEqual(code, 1)
        self.assertIn("maps to 2 BLS entries", err)
        self.assertEqual(self.prepare_calls, [])
        self.assertEqual(self.writer_calls, [])

    def test_default_entry_must_match_index_zero(self):
        self.status = status_document(record(COMMIT_B, 2, 0, booted=True),
                                      record(COMMIT_A, 1, 1))
        code, out, err = self.invoke(self.plan_argv())
        self.assertEqual(code, 1)
        self.assertIn("does not match native default deployment at index 0", err)
        self.assertEqual(self.prepare_calls, [])
        self.assertEqual(self.writer_calls, [])

    def test_invalid_inputs_rejected_before_pack_or_write(self):
        cases = (
            ({"--stock-sha256": "0" * 64}, "does not match"),
            ({"--runtime-sha256": "0" * 64}, "does not match"),
            ({"--expected-current-sha256": "zz"}, "64 lowercase hex"),
            ({"--expected-current-sha256": "0" * 64}, "current target hash"),
        )
        for overrides, needle in cases:
            with self.subTest(overrides=overrides):
                code, out, err = self.invoke(self.full_argv(overrides))
                self.assertEqual(code, 1)
                self.assertIn(needle, err)
                self.assertEqual(self.prepare_calls, [])
                self.assertEqual(self.writer_calls, [])
                self.assertFalse(Path(self.work).exists())

    def test_candidate_failure_installs_prepared_fallback(self):
        self.prepare_fail_entry = self.cand
        self.prepare_fail_suffix = "/candidate"
        code, out, err = self.invoke(self.full_argv())
        self.assertEqual(code, 2, err)
        self.assertEqual(len(self.writer_calls), 1)
        call = self.writer_calls[0]
        self.assertEqual(call["candidate"], call["fallback"])
        self.assertIn("/fallback/", call["candidate"]["image"])
        result = self.result()
        self.assertEqual(result["status"], "fallback-installed")
        self.assertEqual(result["writer_exit"], 0)
        self.assertIn("candidate preparation failed", result["error"])
        self.assertEqual(result["phase"], "fallback-repair")

    def test_fallback_failure_performs_no_write(self):
        self.prepare_fail_entry = self.fall
        self.prepare_fail_suffix = "/fallback"
        code, out, err = self.invoke(self.full_argv())
        self.assertEqual(code, 1)
        self.assertIn("fallback preparation failed", err)
        self.assertEqual(self.writer_calls, [])
        self.assertTrue(Path(self.work).is_dir())
        self.assertFalse((Path(self.work) / "write").exists())
        result = self.result()
        self.assertEqual(result["status"], "fallback-prep-failed")
        self.assertIsNone(result["writer_exit"])

    def test_writer_exit_two_and_three_are_preserved(self):
        for code_value, status_value in ((2, "fallback-restored"),
                                         (3, "recovery-failed")):
            with self.subTest(code=code_value):
                self.writer_code = code_value
                self.work = self.tree.root / ("work-writer-%d" % code_value)
                code, out, err = self.invoke(self.full_argv())
                self.assertEqual(code, code_value)
                result = self.result()
                self.assertEqual(result["writer_exit"], code_value)
                self.assertEqual(result["status"], status_value)
                self.assertIn("kshim-write-boot exited %d" % code_value,
                              result["error"])

    def test_native_change_during_preparation_blocks_writer(self):
        self.mutate_entry = self.cand
        code, out, err = self.invoke(self.full_argv())
        self.assertEqual(code, 1)
        self.assertIn("changed during preparation", err)
        self.assertEqual(self.writer_calls, [])
        self.assertFalse((Path(self.work) / "write").exists())
        result = self.result()
        self.assertEqual(result["status"], "native-changed")
        self.assertEqual(result["phase"], "recheck")

    def test_broken_native_link_during_preparation_blocks_writer(self):
        self.break_link_during_fallback = True
        code, out, err = self.invoke(self.full_argv())
        self.assertEqual(code, 1, err)
        self.assertEqual(self.writer_calls, [])
        self.assertFalse((Path(self.work) / "write").exists())
        result = self.result()
        self.assertEqual(result["status"], "native-changed")
        self.assertEqual(result["phase"], "recheck")

    def test_mismatched_candidate_report_hash_repairs_fallback(self):
        self.report_hash_override = {"/candidate": {"runtime": "0" * 64}}
        code, out, err = self.invoke(self.full_argv())
        self.assertEqual(code, 2, err)
        self.assertEqual(len(self.writer_calls), 1)
        self.assertEqual(self.writer_calls[0]["candidate"],
                         self.writer_calls[0]["fallback"])
        result = self.result()
        self.assertEqual(result["status"], "fallback-installed")
        self.assertIn("inputs.runtime.sha256", result["error"])

    def test_mismatched_fallback_report_hash_blocks_write(self):
        self.report_hash_override = {"/fallback": {"stock": "0" * 64}}
        code, out, err = self.invoke(self.full_argv())
        self.assertEqual(code, 1)
        self.assertIn("inputs.stock_boot.sha256", err)
        self.assertEqual(self.writer_calls, [])
        self.assertFalse((Path(self.work) / "write").exists())
        self.assertEqual(self.result()["status"], "fallback-prep-failed")

    def test_candidate_report_check_failure_repairs_fallback(self):
        self.report_check_fail_suffix = "/candidate"
        code, out, err = self.invoke(self.full_argv())
        self.assertEqual(code, 2, err)
        self.assertEqual(len(self.writer_calls), 1)
        self.assertEqual(self.writer_calls[0]["candidate"],
                         self.writer_calls[0]["fallback"])
        self.assertEqual(self.result()["status"], "fallback-installed")
        self.assertIn("report check 0 failed", self.result()["error"])

    def test_plan_only_is_read_only(self):
        with mock.patch.object(WRITER, "probe_target",
                               side_effect=AssertionError("no target probe")):
            code, out, err = self.invoke(self.plan_argv())
        self.assertEqual(code, 0, err)
        plan = json.loads(out)
        self.assertEqual(plan["candidate"]["entry"], self.cand)
        self.assertEqual(plan["fallback"]["entry"], self.fall)
        self.assertEqual(plan["booted"]["checksum"], COMMIT_B)
        self.assertEqual(self.prepare_calls, [])
        self.assertEqual(self.writer_calls, [])
        self.assertFalse(Path(self.work).exists())
        self.assertFalse(Path(self.lock_path).exists())

    def test_plan_only_broken_native_link_reports_error(self):
        link = (self.tree.sysroot / "ostree/boot.1/default"
                / ("1" * 64) / "1")
        stale = (self.tree.sysroot / "ostree/deploy/default/deploy"
                 / ("%s.9" % ("c" * 64)))
        os.unlink(link)
        os.symlink(os.path.relpath(stale, link.parent), link)
        code, out, err = self.invoke(self.plan_argv())
        self.assertEqual(code, 1)
        self.assertIn("deployment not installed", err)
        self.assertEqual(self.prepare_calls, [])
        self.assertEqual(self.writer_calls, [])

    def test_profile_gate_requires_both_compatibles(self):
        path = self.tree.root / "compatible"
        path.write_bytes(b"qcom,sm8750\0lenovo,elden\0")
        REAL_CHECK_PROFILE(str(path))
        path.write_bytes(b"qcom,sm8750\0lenovo,other\0")
        with self.assertRaises(sync.SyncError):
            REAL_CHECK_PROFILE(str(path))


if __name__ == "__main__":
    unittest.main()
