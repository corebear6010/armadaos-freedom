#!/usr/bin/python3
"""Regression tests for kshim-select-boot.py and armada-kshim-prepare.

Pure stdlib fixture tests: temporary real symlink trees, the actual selector
and wrapper, and a purpose-built recording fake packer. No network, device,
root, or real boot image tooling is touched.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
LIBEXEC = ROOT / "system_files/usr/libexec/armada"
SELECT = LIBEXEC / "kshim-select-boot.py"
WRAPPER = LIBEXEC / "armada-kshim-prepare"
ARGS_FILE = ROOT / "system_files/usr/lib/armada/bootimg-args"

NEW_COMMIT = "a" * 64
OLD_COMMIT = "b" * 64
BOOT_CSUM = "1" * 64
ROOT_UUID = "11111111-2222-3333-4444-555555555555"
BOOT_UUID = "66666666-7777-8888-9999-aaaaaaaaaaaa"

FAKE_BUILDER = """\
#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_RECORD"], "w", encoding="utf-8") as handle:
    json.dump(args, handle)
meta_path = args[args.index("--installer-result") + 1]
with open(meta_path, encoding="utf-8") as handle:
    meta = json.load(handle)
with open(os.environ["FAKE_RECORD"] + ".meta", "w", encoding="utf-8") as handle:
    json.dump(meta, handle)
code = int(os.environ.get("FAKE_EXIT", "0"))
if code:
    sys.stderr.write("fake builder deliberate failure\\n")
    sys.exit(code)
out = args[args.index("--out") + 1]
with open(os.path.join(out, "boot.img"), "wb") as handle:
    handle.write(b"FAKEBOOT")
with open(os.path.join(out, "report.json"), "w", encoding="utf-8") as handle:
    handle.write("{}\\n")
"""


def options_for(ostree, extra=""):
    return ("ostree=%s root=UUID=%s boot=UUID=%s rw quiet%s"
            % (ostree, ROOT_UUID, BOOT_UUID, extra))


class Tree:
    """A disposable BOOT + SYSROOT with native-ish deployments and BLS entries."""

    def __init__(self, root):
        self.root = Path(root)
        self.boot = self.root / "boot"
        self.sysroot = self.root / "sysroot"
        (self.boot / "loader/entries").mkdir(parents=True)
        (self.boot / "dtb/qcom").mkdir(parents=True)
        (self.boot / "linux-elden").write_bytes(b"KERNEL")
        (self.boot / "initramfs-elden.img").write_bytes(b"INITRD")
        (self.boot / "dtb/qcom/sm8750-lenovo-elden.dtb").write_bytes(b"DTB")
        (self.sysroot / "ostree/deploy/default/deploy").mkdir(parents=True)

    def deployment(self, commit, serial):
        path = self.sysroot / "ostree/deploy/default/deploy" / ("%s.%s" % (commit, serial))
        path.mkdir(parents=True)
        return path

    def slot_link(self, slot, csum, commit, serial, target=None):
        link = self.sysroot / "ostree" / slot / "default" / csum / str(serial)
        link.parent.mkdir(parents=True, exist_ok=True)
        if target is None:
            target = os.path.relpath(
                self.sysroot / "ostree/deploy/default/deploy" / ("%s.%s" % (commit, serial)),
                link.parent)
        os.symlink(target, link)
        return "/ostree/%s/default/%s/%s" % (slot, csum, serial)

    def bls(self, name, version, ostree, options=None, linux="linux-elden",
            initrd="initramfs-elden.img", fdtdir="dtb", extra=()):
        path = self.boot / "loader/entries" / name
        path.write_text("\n".join([
            "title %s" % name,
            "version %s" % version,
            "linux %s" % linux,
            "initrd %s" % initrd,
            "fdtdir %s" % fdtdir,
            "options %s" % (options if options is not None else options_for(ostree)),
        ] + list(extra)) + "\n", encoding="utf-8")
        return path

    def native_pair(self):
        """new203 installed plus rollback202, as on the true device."""
        self.deployment(NEW_COMMIT, 1)
        self.deployment(OLD_COMMIT, 2)
        new = self.slot_link("boot.1", BOOT_CSUM, NEW_COMMIT, 1)
        old = self.slot_link("boot.0", "2" * 64, OLD_COMMIT, 2)
        return (self.bls("new203.conf", 203, new),
                self.bls("rollback202.conf", 202, old))


def run_select(boot, sysroot, entry, output):
    return subprocess.run(
        [sys.executable, str(SELECT), "--boot-dir", str(boot),
         "--sysroot", str(sysroot), "--entry", str(entry),
         "--output", str(output)],
        capture_output=True, text=True)


class SelectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tree = Tree(os.path.realpath(self.tmp.name))
        self.new, self.old = self.tree.native_pair()

    def tearDown(self):
        self.tmp.cleanup()

    def select(self, entry, name="result.json"):
        output = self.tree.root / name
        proc = run_select(self.tree.boot, self.tree.sysroot, entry, output)
        data = json.loads(output.read_text(encoding="utf-8")) if output.exists() else None
        return proc, data

    def test_exports_real_deployment_not_boot_checksum(self):
        proc, data = self.select(self.new)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(data["deployment_checksum"], NEW_COMMIT)
        self.assertNotEqual(data["deployment_checksum"], BOOT_CSUM)
        self.assertEqual(data["filesystem_uuids"],
                         {"root": ROOT_UUID, "boot": BOOT_UUID})
        self.assertEqual(data["bls"], "loader/entries/new203.conf")
        self.assertEqual(data["image"], "linux-elden")
        self.assertEqual(data["initrd"], "initramfs-elden.img")
        self.assertEqual(data["fdtdir"], "dtb")
        self.assertEqual(set(data), {"deployment_checksum", "filesystem_uuids",
                                     "bls", "image", "initrd", "fdtdir", "options"})

    def test_second_entry_resolves_its_own_deployment(self):
        proc, data = self.select(self.old)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(data["deployment_checksum"], OLD_COMMIT)

    def test_options_preserved_verbatim(self):
        ostree = "/ostree/boot.1/default/%s/1" % BOOT_CSUM
        verbatim = options_for(
            ostree,
            " console=ttyS0 console=ttyS0 rootflags=subvol=root,space_cache=v2 "
            "loglevel=4 rw quiet *")
        entry = self.tree.bls("verbatim.conf", 300, ostree, options=verbatim)
        proc, data = self.select(entry)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(data["options"], verbatim)

    def test_boot_prefix_normalized(self):
        ostree = "/ostree/boot.1/default/%s/1" % BOOT_CSUM
        entry = self.tree.bls("prefix.conf", 301, ostree,
                              linux="/boot/linux-elden",
                              initrd="/boot/initramfs-elden.img",
                              fdtdir="/boot/dtb")
        proc, data = self.select(entry)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((data["image"], data["initrd"], data["fdtdir"]),
                         ("linux-elden", "initramfs-elden.img", "dtb"))

    def test_boot0_slot_and_loader_symlink(self):
        os.rename(self.tree.boot / "loader", self.tree.boot / "loader.0")
        os.symlink("loader.0", self.tree.boot / "loader")
        ostree = self.tree.slot_link("boot.0", "3" * 64, OLD_COMMIT, 2)
        entry = self.tree.bls("slot0.conf", 302, ostree)
        proc, data = self.select(self.tree.boot / "loader/entries/slot0.conf")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(data["deployment_checksum"], OLD_COMMIT)
        self.assertEqual(data["bls"], "loader/entries/slot0.conf")

    def assert_rejected(self, entry, needle):
        proc, data = self.select(entry)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(needle, proc.stderr)
        self.assertIsNone(data)

    def test_duplicate_linux_rejected(self):
        ostree = "/ostree/boot.1/default/%s/1" % BOOT_CSUM
        entry = self.tree.bls("dup-linux.conf", 310, ostree)
        with entry.open("a", encoding="utf-8") as handle:
            handle.write("linux linux-elden\n")
        self.assert_rejected(entry, "expected exactly one 'linux'")

    def test_duplicate_options_rejected(self):
        ostree = "/ostree/boot.1/default/%s/1" % BOOT_CSUM
        entry = self.tree.bls("dup-options.conf", 311, ostree,
                              extra=("options root=UUID=%s" % ROOT_UUID,))
        self.assert_rejected(entry, "expected exactly one 'options'")

    def test_duplicate_ostree_rejected(self):
        ostree = "/ostree/boot.1/default/%s/1" % BOOT_CSUM
        entry = self.tree.bls("dup-ostree.conf", 312, ostree,
                              options=options_for(ostree, " ostree=%s" % ostree))
        self.assert_rejected(entry, "exactly one ostree=")

    def test_duplicate_root_uuid_rejected(self):
        ostree = "/ostree/boot.1/default/%s/1" % BOOT_CSUM
        entry = self.tree.bls("dup-root.conf", 313, ostree,
                              options=options_for(ostree, " root=UUID=%s" % ROOT_UUID))
        self.assert_rejected(entry, "exactly one root=")

    def test_broken_deployment_link_rejected(self):
        ostree = self.tree.slot_link("boot.1", "4" * 64, OLD_COMMIT, 9)
        entry = self.tree.bls("broken.conf", 320, ostree)
        self.assert_rejected(entry, "deployment not installed")

    def test_outside_deployment_link_rejected(self):
        outside = self.tree.root / "outside-deploy"
        outside.mkdir()
        ostree = self.tree.slot_link("boot.1", "5" * 64, OLD_COMMIT, 9,
                                     target=str(outside))
        entry = self.tree.bls("outside-deploy.conf", 321, ostree)
        self.assert_rejected(entry, "escapes sysroot")

    def test_deployment_name_not_commit_serial_rejected(self):
        bad = self.tree.deployment("c" * 64, 1)
        bad.rename(bad.with_name("not-a-commit.1"))
        ostree = "/ostree/boot.1/default/%s/1" % ("6" * 64)
        link = self.tree.sysroot / "ostree/boot.1/default" / ("6" * 64) / "1"
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(os.path.relpath(bad.with_name("not-a-commit.1"), link.parent), link)
        entry = self.tree.bls("bad-name.conf", 322, ostree)
        self.assert_rejected(entry, "deployment name")

    def test_linux_asset_escape_rejected(self):
        outside = self.tree.root / "outside-kernel"
        outside.write_bytes(b"X")
        (self.tree.boot / "linux-elden").unlink()
        os.symlink(outside, self.tree.boot / "linux-elden")
        self.assert_rejected(self.new, "linux resolves outside boot")

    def test_missing_initrd_rejected(self):
        (self.tree.boot / "initramfs-elden.img").unlink()
        self.assert_rejected(self.new, "initrd missing")

    def test_missing_dtb_rejected(self):
        (self.tree.boot / "dtb/qcom/sm8750-lenovo-elden.dtb").unlink()
        self.assert_rejected(self.new, "Elden DT missing")

    def test_missing_bls_rejected(self):
        self.assert_rejected(self.tree.boot / "loader/entries/absent.conf",
                             "BLS entry missing")

    def test_entry_outside_boot_rejected(self):
        outside = self.tree.root / "outside.conf"
        outside.write_text("title x\n", encoding="utf-8")
        self.assert_rejected(outside, "resolves outside boot")


class WrapperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tree = Tree(os.path.realpath(self.tmp.name))
        self.new, self.old = self.tree.native_pair()
        self.libexec = self.tree.root / "libexec"
        self.libexec.mkdir()
        shutil.copy2(SELECT, self.libexec / "kshim-select-boot.py")
        (self.libexec / "kshim-build-boot.py").write_text(FAKE_BUILDER, encoding="utf-8")
        self.stock = self.tree.root / "stock.img"
        self.runtime = self.tree.root / "runtime.img"
        self.mkbootimg = self.tree.root / "mkbootimg"
        self.unpacker = self.tree.root / "unpack_bootimg"
        self.repacker = self.tree.root / "repack"
        self.avbtool = self.tree.root / "avbtool"
        self.avb_key = self.tree.root / "testkey_rsa4096.pem"
        self.avb_signer = self.libexec / "kshim-avb-sign.py"
        for path in (self.stock, self.runtime, self.mkbootimg,
                     self.unpacker, self.repacker, self.avbtool,
                     self.avb_key, self.avb_signer):
            path.write_bytes(b"TOOL")
        self.record = self.tree.root / "record.json"

    def tearDown(self):
        self.tmp.cleanup()

    def run_wrapper(self, out, label="t020", exit_code="0", boot=None,
                    entry=None, cwd=None, include_avb_key=True):
        env = dict(os.environ)
        env.update({"KSHIM_LIBEXEC": str(self.libexec), "ARGS_FILE": str(ARGS_FILE),
                    "FAKE_RECORD": str(self.record), "FAKE_EXIT": exit_code})
        argv = [str(WRAPPER), "--boot-dir", str(boot or self.tree.boot),
                "--sysroot", str(self.tree.sysroot),
                "--stock-boot", str(self.stock), "--runtime", str(self.runtime),
                "--packer", str(self.mkbootimg), "--unpacker", str(self.unpacker),
                "--repacker", str(self.repacker), "--avbtool", str(self.avbtool)]
        if include_avb_key:
            argv += ["--avb-key", str(self.avb_key)]
        argv += ["--avb-signer", str(self.avb_signer), "--out", str(out)]
        if label is not None:
            argv += ["--source-label", label]
        if entry is not None:
            argv += ["--entry", str(entry)]
        return subprocess.run(argv, capture_output=True, text=True, env=env, cwd=cwd)

    def test_complete_metadata_and_required_args_reach_builder(self):
        out = self.tree.root / "stage"
        proc = self.run_wrapper(out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((out / "candidate/boot.img").is_file())
        meta = json.loads((out / "installer-result.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["deployment_checksum"], NEW_COMMIT)
        self.assertEqual(meta["bls"], "loader/entries/new203.conf")
        seen = json.loads(Path(str(self.record) + ".meta").read_text(encoding="utf-8"))
        self.assertEqual(seen, meta)
        recorded = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertEqual(recorded, [
            "--layout", "installed",
            "--stock-boot", str(self.stock),
            "--runtime", str(self.runtime),
            "--packer", str(self.mkbootimg),
            "--unpacker", str(self.unpacker),
            "--repacker", str(self.repacker),
            "--avbtool", str(self.avbtool),
            "--avb-key", str(self.avb_key),
            "--avb-signer", str(self.avb_signer),
            "--installer-result", str(out / "installer-result.json"),
            "--boot-dir", str(self.tree.boot),
            "--out", str(out / "candidate"),
            "--default-entry", "internal",
            "--source-label", "t020",
        ])
        self.assertIn("candidate: %s" % (out / "candidate"), proc.stdout)

    def test_native_version_order_can_switch_to_rollback(self):
        first = self.tree.root / "stage1"
        proc = self.run_wrapper(first)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        meta = json.loads((first / "installer-result.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["bls"], "loader/entries/new203.conf")

        self.tree.bls("new203.conf", 202,
                      "/ostree/boot.1/default/%s/1" % BOOT_CSUM)
        self.tree.bls("rollback202.conf", 203,
                      "/ostree/boot.0/default/%s/2" % ("2" * 64))
        second = self.tree.root / "stage2"
        proc = self.run_wrapper(second)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        meta = json.loads((second / "installer-result.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["bls"], "loader/entries/rollback202.conf")
        self.assertEqual(meta["deployment_checksum"], OLD_COMMIT)

    def test_source_label_optional(self):
        out = self.tree.root / "stage"
        proc = self.run_wrapper(out, label=None)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        recorded = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertNotIn("--source-label", recorded)

    def old_entry_with_distinct_assets(self):
        ostree = "/ostree/boot.0/default/%s/2" % ("2" * 64)
        (self.tree.boot / "linux-elden-202").write_bytes(b"KERNEL202")
        (self.tree.boot / "initramfs-elden-202.img").write_bytes(b"INITRD202")
        options = options_for(ostree, " armada.revision=202")
        entry = self.tree.bls("rollback202.conf", 202, ostree,
                              linux="linux-elden-202",
                              initrd="initramfs-elden-202.img",
                              options=options)
        return entry, options

    def test_explicit_relative_old_entry_overrides_new_default(self):
        _, options = self.old_entry_with_distinct_assets()
        elsewhere = self.tree.root / "elsewhere"
        elsewhere.mkdir()
        out = self.tree.root / "stage"
        # Relative to --boot-dir, not the caller's cwd: elsewhere has no
        # loader/entries, so a cwd-relative resolve would fail.
        proc = self.run_wrapper(out, entry="loader/entries/rollback202.conf",
                                cwd=elsewhere)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        meta = json.loads((out / "installer-result.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["deployment_checksum"], OLD_COMMIT)
        self.assertEqual(meta["bls"], "loader/entries/rollback202.conf")
        self.assertEqual(meta["image"], "linux-elden-202")
        self.assertEqual(meta["initrd"], "initramfs-elden-202.img")
        self.assertEqual(meta["options"], options)
        self.assertTrue((out / "candidate/boot.img").is_file())
        seen = json.loads(Path(str(self.record) + ".meta").read_text(encoding="utf-8"))
        self.assertEqual(seen, meta)

    def test_explicit_missing_entry_rejected_without_candidate(self):
        out = self.tree.root / "stage"
        proc = self.run_wrapper(out, entry="loader/entries/absent.conf")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("BLS entry missing", proc.stderr)
        self.assertFalse((out / "candidate/boot.img").exists())
        self.assertFalse((out / "installer-result.json").exists())
        self.assertFalse(self.record.exists())

    def test_explicit_entry_outside_boot_rejected(self):
        outside = self.tree.root / "outside.conf"
        outside.write_text("title x\noptions ostree=/x\n", encoding="utf-8")
        out = self.tree.root / "stage"
        proc = self.run_wrapper(out, entry=outside)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("resolves outside boot", proc.stderr)
        self.assertFalse((out / "candidate/boot.img").exists())
        self.assertFalse((out / "installer-result.json").exists())

    def test_builder_failure_propagates_without_candidate(self):
        out = self.tree.root / "stage"
        proc = self.run_wrapper(out, exit_code="7")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("packer failed", proc.stderr)
        self.assertNotIn("candidate:", proc.stdout)
        self.assertFalse((out / "candidate/boot.img").exists())

    def test_missing_avb_key_rejected_before_output(self):
        out = self.tree.root / "stage"
        proc = self.run_wrapper(out, include_avb_key=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--avb-key is required", proc.stderr)
        self.assertFalse(out.exists())

    def test_non_empty_out_rejected(self):
        out = self.tree.root / "stage"
        out.mkdir()
        (out / "leftover").write_text("keep me\n", encoding="utf-8")
        proc = self.run_wrapper(out)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not empty", proc.stderr)
        self.assertTrue((out / "leftover").is_file())
        self.assertFalse((out / "installer-result.json").exists())

    def test_missing_bls_rejected(self):
        empty = self.tree.root / "emptyboot"
        (empty / "loader/entries").mkdir(parents=True)
        out = self.tree.root / "stage"
        proc = self.run_wrapper(out, boot=empty)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("no BLS entries", proc.stderr)


if __name__ == "__main__":
    unittest.main()
