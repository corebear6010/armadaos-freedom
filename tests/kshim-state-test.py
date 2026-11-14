#!/usr/bin/python3
"""Behaviour tests for armada-kshim-state.

Real temporary files, real hashing/copying, the real writer report validators,
the real fingerprint assembly, and the real sync/selector helper modules. Only
ordinary boundaries are replaced: root/profile checks, the native plan, the
native status document, the boot id, the block target, and the child
armada-kshim-sync invocation. No device, root, network, or real boot image is
touched here.
"""
import hashlib
import importlib.machinery
import importlib.util
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
STATE_TOOL = LIBEXEC / "armada-kshim-state"

loader = importlib.machinery.SourceFileLoader("armada_kshim_state_t025",
                                              str(STATE_TOOL))
spec = importlib.util.spec_from_loader(loader.name, loader)
state = importlib.util.module_from_spec(spec)
loader.exec_module(state)

SYNC = state.load_sync(str(LIBEXEC))
WRITER = SYNC.load_writer(str(LIBEXEC))
SELECTOR = SYNC.load_selector(str(LIBEXEC))

SMALL = 4096
MAGIC = b"ANDROID!"
PARTUUID = "11111111-2222-3333-4444-555555555555"
ROOT_UUID = "aaaaaaaa-1111-2222-3333-444444444444"
BOOT_UUID = "bbbbbbbb-1111-2222-3333-444444444444"
BOOT_B = "boot-id-B"
BOOT_C = "boot-id-C"
BOOT_W = "boot-id-W"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def padded(data):
    return (MAGIC + data).ljust(SMALL, b"\x00")[:SMALL]


def identity(checksum, serial, stateroot="default"):
    return {"checksum": checksum, "stateroot": stateroot, "serial": serial}


CURRENT_ID = identity("a" * 64, 1)
NEXT_ID = identity("b" * 64, 2)
OTHER_ID = identity("c" * 64, 3)


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.boot = self.root / "boot"
        (self.boot / "dtb/qcom").mkdir(parents=True)
        (self.boot / "vmlinuz-current").write_bytes(b"KERNEL-CURRENT")
        (self.boot / "initrd-current.img").write_bytes(b"INITRD-CURRENT")
        (self.boot / "vmlinuz-next").write_bytes(b"KERNEL-NEXT")
        (self.boot / "initrd-next.img").write_bytes(b"INITRD-NEXT")
        (self.boot / "dtb/qcom/sm8750-lenovo-elden.dtb").write_bytes(b"DTB")

        self.stock = self.root / "stock.img"
        self.stock.write_bytes(padded(b"STOCK"))
        self.runtime = self.root / "runtime.bin"
        self.runtime.write_bytes(b"RUNTIME-BYTES")
        self.avb_key = self.root / "testkey_rsa4096.pem"
        self.avb_key.write_bytes(b"PRIVATE-KEY")
        self.current_image = self.root / "current.img"
        self.current_bytes = padded(b"CURRENT-INSTALLED")
        self.current_image.write_bytes(self.current_bytes)
        self.target = self.root / "target.img"
        self.target.write_bytes(self.current_bytes)

        self.tools = self.root / "tools"
        self.tools.mkdir()
        for name in SYNC.TOOL_FILES.values():
            (self.tools / name).write_bytes(b"TOOL-" + name.encode())

        self.common = {
            "--state-dir": str(self.root / "state"),
            "--boot-dir": str(self.boot),
            "--sysroot": str(self.root / "sysroot"),
            "--libexec": str(LIBEXEC),
            "--args-file": str(ROOT / "system_files/usr/lib/armada/bootimg-args"),
            "--tools-dir": str(self.tools),
            "--target": str(self.target),
        }
        self.state_dir = self.root / "state"
        self.current = {
            "entry": "loader/entries/current.conf", "identity": dict(CURRENT_ID),
            "options": "ostree=/ostree/boot.1/default/current/0 root=UUID=%s "
                       "boot=UUID=%s rw" % (ROOT_UUID, BOOT_UUID),
            "image": "vmlinuz-current", "initrd": "initrd-current.img",
            "fdtdir": "dtb",
        }
        self.next = {
            "entry": "loader/entries/next.conf", "identity": dict(NEXT_ID),
            "options": "ostree=/ostree/boot.0/default/next/0 root=UUID=%s "
                       "boot=UUID=%s rw" % (ROOT_UUID, BOOT_UUID),
            "image": "vmlinuz-next", "initrd": "initrd-next.img",
            "fdtdir": "dtb",
        }
        self.plan = {"booted": dict(CURRENT_ID),
                     "candidate": dict(self.next), "fallback": dict(self.current)}
        self.status = self.status_document(NEXT_ID)
        self.boot_id = BOOT_B
        self.scenario = "written"
        self.child_calls = []

        for patch in (
                mock.patch.object(state, "load_sync", lambda libexec: SYNC),
                mock.patch.object(state, "require_root", lambda: None),
                mock.patch.object(state, "check_profile", lambda sync: None),
                mock.patch.object(state, "LOCK_PATH",
                                  str(self.root / "state.lock")),
                mock.patch.object(state, "read_boot_id", lambda: self.boot_id),
                mock.patch.object(state, "native_plan",
                                  lambda sync, args, selector: self.plan),
                mock.patch.object(state, "run_child_sync", self.fake_child),
                mock.patch.object(SYNC, "load_writer", lambda libexec: WRITER),
                mock.patch.object(SYNC, "load_selector",
                                  lambda libexec: SELECTOR),
                mock.patch.object(SYNC, "read_native_status",
                                  lambda: self.status),
                mock.patch.object(WRITER, "BOOT_BYTES", SMALL),
                mock.patch.object(WRITER, "probe_target", self.fake_probe),
                mock.patch.object(WRITER, "open_target_ro", self.fake_open_ro),
        ):
            patch.start()
        self.addCleanup(mock.patch.stopall)

    # -- boundaries -------------------------------------------------------

    def fake_probe(self, target, partuuid):
        return {"path": os.path.realpath(str(target)), "partuuid": partuuid,
                "rdev": 0, "major": 7, "minor": 0, "size": SMALL}

    def fake_open_ro(self, identity):
        return os.open(identity["path"], os.O_RDONLY)

    def status_document(self, identity_value, staged=True):
        records = [{"checksum": identity_value["checksum"],
                    "stateroot": identity_value["stateroot"],
                    "serial": identity_value["serial"],
                    "index": 0, "booted": True}]
        if staged:
            records.append({"checksum": "d" * 64, "stateroot": "default",
                            "serial": 4, "index": 1, "booted": False,
                            "staged": True})
        return {"deployments": records}

    def file_meta(self, path):
        data = Path(path).read_bytes()
        return {"path": str(path), "length": len(data), "sha256": sha(data)}

    def tool_meta(self, key):
        path = (LIBEXEC / SYNC.SIGNER_FILE
                if key == "avb_signer"
                else self.tools / SYNC.TOOL_FILES[key])
        return self.file_meta(path)

    def report_for(self, item, stock, runtime, image=None):
        boot = self.boot
        kernel = boot / item["image"]
        initrd = boot / item["initrd"]
        dtb = boot / item["fdtdir"] / SELECTOR.DTB_REL
        if image is None:
            image = self.image_bytes(item)
        return {
            "layout_mode": "installed",
            "offline_verified": True,
            "checks": [{"check": "installed:result-json", "ok": True}],
            "inputs": {
                "stock_boot": self.file_meta(stock),
                "runtime": self.file_meta(runtime),
                "linux_image": self.file_meta(kernel),
                "initrd": self.file_meta(initrd),
                "dtb": self.file_meta(dtb),
                "packer": self.tool_meta("packer"),
                "unpacker": self.tool_meta("unpacker"),
                "repacker": self.tool_meta("repacker"),
                "avbtool": self.tool_meta("avbtool"),
                "avb_signer": self.tool_meta("avb_signer"),
            },
            "outputs": {"final_boot": {"path": "/historical/boot.img",
                                       "length": SMALL,
                                       "sha256": sha(image)}},
            "installed_metadata": {
                "deployment_checksum": item["identity"]["checksum"],
                "filesystem_uuids": {"root": ROOT_UUID, "boot": BOOT_UUID},
                "bls": item["entry"],
                "options": item["options"],
                "image": item["image"],
                "initrd": item["initrd"],
                "fdtdir": item["fdtdir"],
            },
        }

    def write_report(self, path, document):
        Path(path).write_text(json.dumps(document), encoding="utf-8")

    def image_bytes(self, item):
        digest = hashlib.sha256()
        for relative in (item["image"], item["initrd"],
                         item["fdtdir"] + "/" + SELECTOR.DTB_REL):
            digest.update((self.boot / relative).read_bytes())
        return padded(item["entry"].encode() + digest.digest())

    def private(self, key):
        return self.state_dir / state.INPUT_NAMES[key]

    def load_state(self):
        return json.loads((self.state_dir / "state.json")
                          .read_text(encoding="utf-8"))

    def store_state(self, document):
        state.write_json_atomic(str(self.state_dir / "state.json"), document)

    # -- child boundary ---------------------------------------------------

    def fake_child(self, args, config, transaction_dir, before):
        self.child_calls.append(before)
        work = Path(transaction_dir) / "work"
        work.mkdir(parents=True, exist_ok=True)
        if self.scenario == "fail":
            (work / "result.json").write_text(
                json.dumps({"error": "native deployment selection changed"}),
                encoding="utf-8")
            return 1
        if self.scenario == "recovery-failed":
            return 3

        stock = self.private("stock_boot")
        runtime = self.private("runtime")
        candidate = self.image_bytes(self.next)
        fallback = self.image_bytes(self.current)

        def prepared(kind, item, image):
            base = work / kind / "candidate"
            base.mkdir(parents=True, exist_ok=True)
            (base / "boot.img").write_bytes(image)
            self.write_report(base / "report.json",
                              self.report_for(item, stock, runtime, image))

        if self.scenario == "written":
            prepared("candidate", self.next, candidate)
            prepared("fallback", self.current, fallback)
            self.target.write_bytes(candidate)
            status = "written"
            candidate_sha = sha(candidate)
            fallback_sha = sha(fallback)
            code = 0
        elif self.scenario == "repair":
            prepared("fallback", self.current, fallback)
            self.target.write_bytes(fallback)
            status = "written"
            candidate_sha = sha(fallback)
            fallback_sha = sha(fallback)
            code = 2
        elif self.scenario == "restored":
            prepared("candidate", self.next, candidate)
            prepared("fallback", self.current, fallback)
            self.target.write_bytes(fallback)
            status = "fallback-restored"
            candidate_sha = sha(candidate)
            fallback_sha = sha(fallback)
            code = 2
        elif self.scenario == "repair-unchanged":
            # The target already holds the verified fallback; the writer sees
            # the same bytes as its candidate input and records "unchanged".
            prepared("fallback", self.current, fallback)
            status = "unchanged"
            candidate_sha = sha(fallback)
            fallback_sha = sha(fallback)
            code = 2
        elif self.scenario == "repair-unequal":
            prepared("fallback", self.current, fallback)
            status = "unchanged"
            candidate_sha = sha(candidate)
            fallback_sha = sha(fallback)
            code = 2
        else:
            raise AssertionError("unknown scenario %r" % self.scenario)

        write = work / "write"
        write.mkdir(parents=True, exist_ok=True)
        (write / "transaction.json").write_text(json.dumps({
            "status": status,
            "expected_current_sha256": before,
            "current_sha256": before,
            "candidate_sha256": candidate_sha,
            "fallback_sha256": fallback_sha,
            "target": {"partuuid": PARTUUID},
        }), encoding="utf-8")
        return code

    # -- invocation -------------------------------------------------------

    def argv(self, action, **extra):
        values = dict(self.common)
        values["--state-dir"] = str(self.state_dir)
        values.update(extra)
        ordered = [action]
        for key in sorted(values):
            ordered.extend([key, values[key]])
        return ordered

    def provision(self, **extra):
        report = extra.pop("--current-report", None)
        if report is None:
            report = self.root / "current-report.json"
            self.write_report(report, self.report_for(self.current, self.stock,
                                                      self.runtime,
                                                      self.current_bytes))
        values = {
            "--stock-boot": str(self.stock),
            "--runtime": str(self.runtime),
            "--avb-key": str(self.avb_key),
            "--current-image": str(self.current_image),
            "--current-report": str(report),
            "--partuuid": PARTUUID,
        }
        values.update(extra)
        return self.invoke(self.argv("provision", **values))

    def invoke(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = state.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def provisioned(self):
        code, out, err = self.provision()
        self.assertEqual(code, 0, err)
        return self.load_state()

    def synced(self, scenario="written"):
        self.scenario = scenario
        return self.invoke(self.argv("sync"))

    # -- provision --------------------------------------------------------

    def test_provision_copies_private_inputs_without_touching_sources(self):
        stock_before = self.stock.read_bytes()
        runtime_before = self.runtime.read_bytes()
        avb_key_before = self.avb_key.read_bytes()
        target_before = self.target.read_bytes()
        document = self.provisioned()

        self.assertEqual(self.stock.read_bytes(), stock_before)
        self.assertEqual(self.runtime.read_bytes(), runtime_before)
        self.assertEqual(self.avb_key.read_bytes(), avb_key_before)
        self.assertEqual(self.target.read_bytes(), target_before)
        self.assertEqual(self.private("stock_boot").read_bytes(), stock_before)
        self.assertEqual(self.private("runtime").read_bytes(), runtime_before)
        self.assertEqual(self.private("avb_key").read_bytes(), avb_key_before)
        self.assertEqual(sha(self.private("stock_boot").read_bytes()),
                         sha(stock_before))
        self.assertEqual(
            oct(self.private("avb_key").stat().st_mode & 0o777), "0o600"
        )

        config = json.loads((self.state_dir / "config.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(config["schema"], 1)
        self.assertEqual(config["profile"], state.PROFILE)
        self.assertEqual(config["target"]["guid"], PARTUUID)
        self.assertEqual(config["target"]["size"], SMALL)
        self.assertEqual(config["inputs"]["stock_boot"]["path"],
                         "inputs/stock-boot.img")
        self.assertEqual(config["inputs"]["runtime"]["path"],
                         "inputs/runtime.bin")
        self.assertEqual(config["inputs"]["avb_key"]["path"],
                         "inputs/testkey_rsa4096.pem")
        self.assertEqual(config["inputs"]["avb_key"]["length"],
                         len(avb_key_before))
        self.assertEqual(config["inputs"]["avb_key"]["sha256"],
                         sha(avb_key_before))
        self.assertEqual(document["schema"], 1)
        self.assertEqual(document["expected_current_sha256"],
                         sha(self.current_bytes))
        self.assertEqual(document["payload"]["identity"], CURRENT_ID)
        self.assertIsNone(document["last_written_boot_id"])
        self.assertIsNone(document["pending"])
        self.assertEqual(document["owned_transactions"], [])
        self.assertEqual(document["boot_observation"]["label"], "observed")
        self.assertEqual(document["boot_observation"]["boot_id"], BOOT_B)
        self.assertEqual(document["boot_observation"]["identity"], CURRENT_ID)
        self.assertEqual(document["boot_observation"]["observed_raw_sha256"],
                         sha(self.current_bytes))
        self.assertEqual(document["last_result"]["status"], "provisioned")
        self.assertFalse((self.state_dir / "transactions").exists())
        self.assertEqual(oct(self.state_dir.stat().st_mode & 0o777), "0o700")

    def test_provision_refuses_mismatched_report_input_or_target(self):
        cases = []

        bad_metadata = self.report_for(self.current, self.stock, self.runtime,
                                       self.current_bytes)
        bad_metadata["installed_metadata"]["options"] += " changed"
        cases.append(({"report": bad_metadata},
                      "current report options does not match"))

        bad_final = self.report_for(self.current, self.stock, self.runtime,
                                    self.current_bytes)
        bad_final["outputs"]["final_boot"]["sha256"] = "0" * 64
        cases.append(({"report": bad_final},
                      "final_boot sha256 does not match"))

        bad_stock = self.root / "bad-stock.img"
        bad_stock.write_bytes(padded(b"DIFFERENT-STOCK"))
        cases.append(({"stock": bad_stock},
                      "stock boot hash does not match"))

        bad_runtime = self.root / "bad-runtime.bin"
        bad_runtime.write_bytes(b"RUNTIME" * 10)
        cases.append(({"runtime": bad_runtime},
                      "runtime length does not match"))

        cases.append(({"target_bytes": padded(b"OTHER-TARGET")},
                      "does not match the current image"))

        for index, (case, needle) in enumerate(cases):
            with self.subTest(needle=needle):
                self.state_dir = self.root / ("state-mismatch-%d" % index)
                extra = {}
                if "report" in case:
                    path = self.root / ("mismatch-%d.json" % index)
                    self.write_report(path, case["report"])
                    extra["--current-report"] = str(path)
                if "stock" in case:
                    extra["--stock-boot"] = str(case["stock"])
                if "runtime" in case:
                    extra["--runtime"] = str(case["runtime"])
                if "target_bytes" in case:
                    self.target.write_bytes(case["target_bytes"])
                else:
                    self.target.write_bytes(self.current_bytes)
                extra["--state-dir"] = str(self.state_dir)
                code, out, err = self.provision(**extra)
                self.assertEqual(code, 1, err)
                self.assertIn(needle, err)
                self.assertFalse(self.state_dir.exists())

    def test_provision_refuses_existing_state_dir(self):
        self.state_dir.mkdir(parents=True)
        sentinel = self.state_dir / "sentinel"
        sentinel.write_bytes(b"KEEP")
        code, out, err = self.provision()
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        self.assertEqual(sentinel.read_bytes(), b"KEEP")
        self.assertFalse((self.state_dir / "config.json").exists())

    def test_provision_reports_writer_errors_without_traceback(self):
        self.current_image.write_bytes(b"NOT-AN-IMAGE".ljust(SMALL, b"\x00"))
        code, out, err = self.provision()
        self.assertEqual(code, 1)
        self.assertIn("ANDROID", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(self.state_dir.exists())

    def test_sync_selection_error_is_reported_not_raised(self):
        with mock.patch.object(state, "native_plan",
                               side_effect=state.StateError(
                                   "staged deployment; finalized native BLS is "
                                   "not ready")):
            code, out, err = self.provision()
        self.assertEqual(code, 1)
        self.assertIn("staged deployment", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(self.state_dir.exists())

    # -- sync -------------------------------------------------------------

    def test_sync_noop_does_not_call_child(self):
        self.provisioned()
        code, out, err = self.synced("written")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.child_calls), 1)
        expected = sha(self.image_bytes(self.next))
        document = self.load_state()
        self.assertEqual(document["expected_current_sha256"], expected)
        self.assertEqual(document["payload"]["identity"], NEXT_ID)

        self.child_calls[:] = []
        code, out, err = self.synced("written")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.child_calls, [])
        self.assertEqual(self.load_state()["expected_current_sha256"], expected)

    def test_same_path_kernel_change_invalidates_fingerprint(self):
        self.provisioned()
        code, out, err = self.synced("written")
        self.assertEqual(code, 0, err)
        first = self.load_state()
        old_fingerprint = first["payload"]["fingerprint"]
        old_expected = first["expected_current_sha256"]
        self.child_calls[:] = []

        (self.boot / "vmlinuz-next").write_bytes(b"KERNEL-NEXT-REBUILT")
        code, out, err = self.synced("written")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.child_calls), 1)
        document = self.load_state()
        self.assertNotEqual(document["payload"]["fingerprint"],
                            old_fingerprint)
        self.assertEqual(document["payload"]["fingerprint"]["assets"]
                         ["linux_image"],
                         sha(b"KERNEL-NEXT-REBUILT"))
        self.assertNotEqual(document["expected_current_sha256"], old_expected)

    def test_sync_zero_writes_candidate_without_accepting_boot(self):
        provisioned = self.provisioned()
        observation = provisioned["boot_observation"]
        code, out, err = self.synced("written")
        self.assertEqual(code, 0, err)
        document = self.load_state()
        candidate = self.image_bytes(self.next)
        self.assertEqual(self.target.read_bytes(), candidate)
        self.assertEqual(document["expected_current_sha256"], sha(candidate))
        self.assertEqual(document["payload"]["identity"], NEXT_ID)
        self.assertEqual(document["last_result"]["status"], "written")
        self.assertEqual(document["last_result"]["sync_exit"], 0)
        self.assertEqual(document["last_written_boot_id"], BOOT_B)
        self.assertEqual(document["boot_observation"], observation)
        self.assertIsNone(document["pending"])

    def test_sync_two_installs_prepared_fallback_and_keeps_exit_two(self):
        for index, scenario in enumerate(("repair", "restored")):
            with self.subTest(scenario=scenario):
                self.state_dir = self.root / ("state-two-%d" % index)
                self.target.write_bytes(self.current_bytes)
                self.child_calls[:] = []
                self.provisioned()
                code, out, err = self.synced(scenario)
                self.assertEqual(code, 2, err)
                document = self.load_state()
                fallback = self.image_bytes(self.current)
                self.assertEqual(self.target.read_bytes(), fallback)
                self.assertEqual(document["expected_current_sha256"],
                                 sha(fallback))
                self.assertEqual(document["payload"]["identity"], CURRENT_ID)
                self.assertEqual(document["last_result"]["status"],
                                 "fallback-restored")
                self.assertEqual(document["last_result"]["sync_exit"], 2)
                self.assertEqual(document["last_written_boot_id"], BOOT_B)
                self.assertIsNone(document["pending"])
                self.assertIn("armada-kshim-sync", document["last_result"]
                              ["error"])

    def test_sync_two_unchanged_fallback_is_accepted_without_boot_id(self):
        fallback = self.image_bytes(self.current)
        self.current_bytes = fallback
        self.current_image.write_bytes(fallback)
        self.target.write_bytes(fallback)
        self.provisioned()
        code, out, err = self.synced("repair-unchanged")
        self.assertEqual(code, 2, err)
        document = self.load_state()
        self.assertEqual(self.target.read_bytes(), fallback)
        self.assertEqual(document["expected_current_sha256"], sha(fallback))
        self.assertEqual(document["payload"]["identity"], CURRENT_ID)
        self.assertEqual(document["last_result"]["status"],
                         "fallback-restored")
        self.assertEqual(document["last_result"]["sync_exit"], 2)
        self.assertIsNone(document["last_written_boot_id"])
        self.assertIsNone(document["pending"])
        self.assertIn("armada-kshim-sync", document["last_result"]["error"])

    def test_sync_two_unchanged_with_unequal_hashes_is_refused(self):
        fallback = self.image_bytes(self.current)
        self.current_bytes = fallback
        self.current_image.write_bytes(fallback)
        self.target.write_bytes(fallback)
        self.provisioned()
        code, out, err = self.synced("repair-unequal")
        self.assertEqual(code, 1)
        self.assertIn("does not match fallback_sha256", err)
        document = self.load_state()
        self.assertIsNotNone(document["pending"])
        self.assertEqual(self.target.read_bytes(), fallback)

    def test_sync_child_failure_retains_pending(self):
        self.provisioned()
        code, out, err = self.synced("fail")
        self.assertEqual(code, 1)
        self.assertIn("native deployment selection changed", err)
        document = self.load_state()
        self.assertIsNotNone(document["pending"])
        self.assertEqual(document["pending"]["before_sha256"],
                         sha(self.current_bytes))
        self.assertEqual(document["pending"]["calling_boot_id"], BOOT_B)
        self.assertIn(document["pending"]["id"],
                      document["owned_transactions"])
        self.assertTrue((self.state_dir / "transactions"
                         / document["pending"]["id"]).is_dir())

    # -- record-boot ------------------------------------------------------

    def test_record_boot_same_boot_is_not_an_observation(self):
        provisioned = self.provisioned()
        self.assertEqual(self.synced("written")[0], 0)
        self.status = self.status_document(NEXT_ID)
        code, out, err = self.invoke(self.argv("record-boot"))
        self.assertEqual(code, 0, err)
        self.assertIn("awaiting reboot", out)
        self.assertEqual(self.load_state()["boot_observation"],
                         provisioned["boot_observation"])

    def test_record_boot_new_boot_accepts_full_identity(self):
        provisioned = self.provisioned()
        self.assertEqual(self.synced("written")[0], 0)
        self.boot_id = BOOT_C
        self.status = self.status_document(NEXT_ID)
        code, out, err = self.invoke(self.argv("record-boot"))
        self.assertEqual(code, 0, err)
        document = self.load_state()
        observation = document["boot_observation"]
        self.assertEqual(observation["label"], "observed")
        self.assertEqual(observation["boot_id"], BOOT_C)
        self.assertEqual(observation["identity"], NEXT_ID)
        self.assertEqual(observation["identity"]["stateroot"], "default")
        self.assertEqual(observation["identity"]["serial"], 2)
        self.assertEqual(observation["observed_raw_sha256"],
                         sha(self.image_bytes(self.next)))
        self.assertNotEqual(observation, provisioned["boot_observation"])
        self.assertEqual(document["last_result"]["status"], "boot-observed")

    def test_record_boot_identity_mismatch_keeps_evidence(self):
        provisioned = self.provisioned()
        self.assertEqual(self.synced("written")[0], 0)
        before = self.load_state()
        self.boot_id = BOOT_C
        self.status = self.status_document(OTHER_ID)
        code, out, err = self.invoke(self.argv("record-boot"))
        self.assertEqual(code, 1)
        self.assertIn("does not match the installed payload", err)
        document = self.load_state()
        self.assertEqual(document["boot_observation"],
                         before["boot_observation"])
        self.assertEqual(document["payload"], before["payload"])

    # -- pending reconciliation -------------------------------------------

    def install_pending(self, status, target_bytes, record=True):
        self.provisioned()
        document = self.load_state()
        before = document["expected_current_sha256"]
        transaction_id = "20260101T000000Z-pending0001"
        work = self.state_dir / "transactions" / transaction_id / "work"
        base = work / "candidate" / "candidate"
        base.mkdir(parents=True)
        candidate = self.image_bytes(self.next)
        (base / "boot.img").write_bytes(candidate)
        self.write_report(base / "report.json",
                          self.report_for(self.next, self.private("stock_boot"),
                                          self.private("runtime"), candidate))
        write = work / "write"
        write.mkdir()
        record_value = {
            "status": status,
            "expected_current_sha256": before,
            "current_sha256": before,
            "candidate_sha256": sha(candidate),
            "fallback_sha256": sha(self.image_bytes(self.current)),
            "target": {"partuuid": PARTUUID},
        }
        if record:
            (write / "transaction.json").write_text(json.dumps(record_value),
                                                   encoding="utf-8")
        self.target.write_bytes(target_bytes)
        snapshot = document["payload"]["fingerprint"]
        document["pending"] = {
            "id": transaction_id, "plan": self.plan,
            "before_sha256": before, "calling_boot_id": BOOT_W,
            "builder_sha256": snapshot["builder"]["sha256"],
            "tool_sha256": dict(snapshot["tools"]),
        }
        document["owned_transactions"] = [transaction_id]
        self.store_state(document)
        return transaction_id, candidate, document

    def test_pending_final_record_recovers_without_another_write(self):
        transaction_id, candidate, before = self.install_pending(
            "written", self.image_bytes(self.next))
        self.child_calls[:] = []
        code, out, err = self.invoke(self.argv("sync"))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.child_calls, [])
        document = self.load_state()
        self.assertEqual(document["expected_current_sha256"], sha(candidate))
        self.assertEqual(document["payload"]["identity"], NEXT_ID)
        self.assertEqual(document["last_result"]["status"],
                         "recovered-interrupted")
        self.assertEqual(document["last_transaction"], transaction_id)
        self.assertEqual(document["last_written_boot_id"], BOOT_W)
        self.assertIsNone(document["pending"])
        self.assertEqual(document["owned_transactions"], [transaction_id])
        self.assertEqual(self.target.read_bytes(), candidate)

    def test_pending_unknown_partial_raw_needs_explicit_recovery(self):
        self.install_pending("prepared", padded(b"UNKNOWN-PARTIAL"))
        code, out, err = self.invoke(self.argv("sync"))
        self.assertEqual(code, 1)
        self.assertIn("explicit recovery needed", err)
        document = self.load_state()
        self.assertIsNotNone(document["pending"])
        self.assertEqual(document["pending"]["id"], "20260101T000000Z-pending0001")

    def test_pending_recovery_uses_recorded_tools_not_current_ones(self):
        transaction_id, candidate, document = self.install_pending(
            "written", self.image_bytes(self.next))
        old = document["payload"]["fingerprint"]
        packer = self.tools / SYNC.TOOL_FILES["packer"]
        packer.write_bytes(b"PACKER-UPDATED")
        new_packer_sha = sha(b"PACKER-UPDATED")
        self.assertNotEqual(old["tools"]["packer"], new_packer_sha)

        code, out, err = self.invoke(self.argv("record-boot"))
        self.assertEqual(code, 0, err)
        recovered = self.load_state()
        self.assertEqual(recovered["payload"]["fingerprint"]["tools"],
                         old["tools"])
        self.assertEqual(recovered["payload"]["fingerprint"]["builder"],
                         old["builder"])
        self.assertIsNone(recovered["pending"])

        self.child_calls[:] = []
        self.boot_id = BOOT_C
        self.status = self.status_document(NEXT_ID)
        code, out, err = self.invoke(self.argv("sync"))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.child_calls), 1)
        document = self.load_state()
        self.assertEqual(document["payload"]["fingerprint"]["tools"]["packer"],
                         new_packer_sha)
        self.assertEqual(document["payload"]["fingerprint"]["tools"]
                         ["unpacker"], old["tools"]["unpacker"])

    def test_pending_report_tool_mismatch_is_refused(self):
        transaction_id, candidate, document = self.install_pending(
            "written", self.image_bytes(self.next))
        report_path = (self.state_dir / "transactions" / transaction_id
                       / "work" / "candidate" / "candidate" / "report.json")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["inputs"]["packer"]["sha256"] = "0" * 64
        self.write_report(report_path, report)
        code, out, err = self.invoke(self.argv("sync"))
        self.assertEqual(code, 1)
        self.assertIn("does not match the pending tool snapshot", err)
        self.assertIsNotNone(self.load_state()["pending"])
        self.assertEqual(self.target.read_bytes(), candidate)

    def test_pending_without_tool_provenance_is_refused(self):
        self.install_pending("written", self.image_bytes(self.next))
        document = self.load_state()
        del document["pending"]["tool_sha256"]
        self.store_state(document)
        code, out, err = self.invoke(self.argv("sync"))
        self.assertEqual(code, 1)
        self.assertIn("pending tool_sha256", err)
        self.assertIsNotNone(self.load_state()["pending"])

    def test_pending_recovery_failed_exits_three(self):
        self.install_pending("recovery-failed", self.current_bytes, record=True)
        code, out, err = self.invoke(self.argv("sync"))
        self.assertEqual(code, 3)
        self.assertIn("explicit recovery needed", err)
        self.assertIsNotNone(self.load_state()["pending"])

    # -- pruning ----------------------------------------------------------

    def test_observation_prunes_only_managed_old_transactions(self):
        self.provisioned()
        self.assertEqual(self.synced("written")[0], 0)
        document = self.load_state()
        latest = document["last_transaction"]
        self.assertIsNotNone(latest)
        transaction_root = self.state_dir / "transactions"
        old = transaction_root / "old-tx"
        old.mkdir()
        (old / "before.img").write_bytes(b"OLD")
        unknown = transaction_root / "unknown-sentinel"
        unknown.mkdir()
        (unknown / "keep.txt").write_bytes(b"KEEP")
        document["owned_transactions"].append("old-tx")
        self.store_state(document)

        self.boot_id = BOOT_C
        self.status = self.status_document(NEXT_ID)
        code, out, err = self.invoke(self.argv("record-boot"))
        self.assertEqual(code, 0, err)
        after = self.load_state()
        self.assertTrue((transaction_root / latest).is_dir())
        self.assertFalse(old.exists())
        self.assertTrue(unknown.is_dir())
        self.assertEqual((unknown / "keep.txt").read_bytes(), b"KEEP")
        self.assertEqual(after["owned_transactions"], [latest])
        self.assertTrue(self.private("stock_boot").is_file())
        self.assertTrue(self.private("runtime").is_file())
        self.assertEqual(after["boot_observation"]["boot_id"], BOOT_C)


if __name__ == "__main__":
    unittest.main()
