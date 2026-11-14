#!/usr/bin/env python3
"""Black-box tests for system_files/usr/libexec/tb323fu/tb323fu-audio-prepare.

Every case runs the real helper (bash) with all its TB323FU_* path overrides
pointed into a fresh temporary directory: a fake device tree, a fake fixed
firmware bundle, a fake remoteproc tree and a writable firmware_class parameter
file. Nothing under /sys, /var, /run, /etc or /usr is read or written, no helper
logic is mocked, and only the files inside the temporary directory are
inspected.

Run: python3 tests/test_tb323fu_audio_prepare.py
"""

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
HELPER = REPO / "system_files" / "usr" / "libexec" / "tb323fu" / "tb323fu-audio-prepare"
BASH = shutil.which("bash")

FW_REL = "qcom/kaanapali/lenovo/baldur"
TOPO_REL = "qcom/kaanapali/LENOVO-TB323FU-tplg.bin"
ADSP_FW = FW_REL + "/adsp.mbn"
DSP_CONF = "wireplumber/wireplumber.conf.d/61-tb323fu-speakers-dsp.conf"
BUNDLE_FILES = ("adsp.mbn", "adsp_dtb.mbn", "aw882xx_acf.bin")


@unittest.skipIf(BASH is None, "bash is required to execute the helper")
class AudioPrepareTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="tb323fu-audio-prepare-")
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)

        self.compat = self.root / "compatible"
        self.write(self.compat, b"lenovo,baldur\x00qcom,kaanapali\x00")

        # One fixed per-model bundle, laid out exactly as the image ships it.
        self.bundle = self.root / "bundle"
        for name in BUNDLE_FILES:
            if name == "aw882xx_acf.bin":
                path = self.bundle / name
            else:
                path = self.bundle / FW_REL / name
            self.write(path, b"firmware:" + name.encode())

        self.template = self.root / "61-tb323fu-speakers-dsp.conf"
        self.write(self.template, b"wireplumber node rule\n")

        self.topology = self.root / "LENOVO-TB323FU-tplg.bin"
        self.write(self.topology, b"topology-bytes")

        self.fw_param = self.root / "fwpath"
        self.write(self.fw_param, b"\n")

        self.run_fw = self.root / "run-fw"
        self.etc = self.root / "etc"

        self.procfs = self.root / "remoteproc"
        self.adsp = self.procfs / "remoteproc0"
        self.write(self.adsp / "name", b"adsp\n")
        self.write(self.adsp / "firmware", (ADSP_FW + "\n").encode())
        self.write(self.adsp / "state", b"offline\n")

        self.cdsp = self.procfs / "remoteproc1"
        self.write(self.cdsp / "name", b"cdsp\n")
        self.write(self.cdsp / "firmware", b"qcom/other/cdsp.mbn\n")
        self.write(self.cdsp / "state", b"offline\n")

    # -- helpers -------------------------------------------------------------

    def write(self, path, data):
        path = pathlib.Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def env(self, **overrides):
        env = dict(os.environ)
        env.update(
            {
                "TB323FU_DT_COMPATIBLE": str(self.compat),
                "TB323FU_REMOTEPROC_DIR": str(self.procfs),
                "TB323FU_FIRMWARE_DIR": str(self.bundle),
                "TB323FU_FIRMWARE_CLASS_PATH": str(self.fw_param),
                "TB323FU_RUN_FIRMWARE": str(self.run_fw),
                "TB323FU_ETC_DIR": str(self.etc),
                "TB323FU_DSP_TEMPLATE": str(self.template),
                "TB323FU_TOPOLOGY": str(self.topology),
            }
        )
        env.update({key: str(value) for key, value in overrides.items()})
        return env

    def run_helper(self, **overrides):
        return subprocess.run(
            [BASH, str(HELPER)],
            env=self.env(**overrides),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def snapshot(self):
        out = {}
        for path in sorted(self.root.rglob("*")):
            if path.is_file():
                out[str(path.relative_to(self.root))] = path.read_bytes()
        return out

    def state(self, rp=None):
        rp = self.adsp if rp is None else rp
        return (rp / "state").read_bytes()

    def assert_staged(self, dest):
        dest = pathlib.Path(dest)
        for name in ("adsp.mbn", "adsp_dtb.mbn"):
            self.assertEqual(
                (dest / FW_REL / name).read_bytes(),
                (self.bundle / FW_REL / name).read_bytes(),
            )
        self.assertEqual(
            (dest / "aw882xx_acf.bin").read_bytes(),
            (self.bundle / "aw882xx_acf.bin").read_bytes(),
        )
        self.assertEqual((dest / TOPO_REL).read_bytes(), self.topology.read_bytes())
        self.assertEqual((self.etc / DSP_CONF).read_bytes(), self.template.read_bytes())

    # -- model gate ----------------------------------------------------------

    def test_nonmodel_device_has_no_effect(self):
        self.write(self.compat, b"lenovo,otter\x00qcom,kaanapali\x00")
        before = self.snapshot()
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.state(), b"offline\n")

    def test_missing_compatible_has_no_effect(self):
        before = self.snapshot()
        result = self.run_helper(TB323FU_DT_COMPATIBLE=self.root / "absent")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.state(), b"offline\n")

    def test_gen4_token_is_not_the_exact_model(self):
        self.write(self.compat, b"lenovo,baldur-gen4\x00qcom,kaanapali-gen4\x00")
        before = self.snapshot()
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not lenovo,baldur", result.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.state(), b"offline\n")

    # -- preflight: nothing is written, the DSP is never started -------------

    def _assert_preflight_failed(self):
        before = self.snapshot()
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0, "helper must fail preflight")
        self.assertIn("not starting the ADSP", result.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.state(), b"offline\n")
        self.assertFalse(self.run_fw.exists())
        self.assertFalse((self.etc / "wireplumber").exists())

    def test_missing_bundled_firmware_fails_before_staging(self):
        (self.bundle / "aw882xx_acf.bin").unlink()
        self._assert_preflight_failed()

    def test_empty_bundled_firmware_fails_before_staging(self):
        self.write(self.bundle / FW_REL / "adsp_dtb.mbn", b"")
        self._assert_preflight_failed()

    def test_empty_topology_fails_before_staging(self):
        self.write(self.topology, b"")
        self._assert_preflight_failed()

    def test_missing_topology_fails_before_staging(self):
        self.topology.unlink()
        self._assert_preflight_failed()

    def test_empty_template_fails_before_staging(self):
        self.write(self.template, b"")
        self._assert_preflight_failed()

    # -- firmware_class path -------------------------------------------------

    def test_empty_fwparam_becomes_the_absolute_run_path(self):
        self.write(self.fw_param, b"")
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fw_param.read_bytes(), (str(self.run_fw) + "\n").encode())
        self.assert_staged(self.run_fw)
        self.assertEqual(self.state(), b"start")

    def test_whitespace_only_fwparam_becomes_the_absolute_run_path(self):
        self.write(self.fw_param, b"\n\n")
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fw_param.read_bytes(), (str(self.run_fw) + "\n").encode())
        self.assert_staged(self.run_fw)

    def test_existing_fwparam_with_spaces_is_preserved(self):
        dest = self.root / "firmware dir with spaces"
        original = (str(dest) + "\n").encode()
        self.write(self.fw_param, original)
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fw_param.read_bytes(), original)
        self.assert_staged(dest)

    def test_unrelated_files_stay_byte_identical(self):
        dest = self.root / "shared-firmware"
        gpu = dest / "qcom/gpu/gpu.mbn"
        touch = dest / "touch/touch.bin"
        self.write(gpu, b"gpu-bytes")
        self.write(touch, b"touch-bytes")
        self.write(self.fw_param, (str(dest) + "\n").encode())

        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(gpu.read_bytes(), b"gpu-bytes")
        self.assertEqual(touch.read_bytes(), b"touch-bytes")
        self.assert_staged(dest)

    def test_relative_fwparam_is_rejected(self):
        self.write(self.fw_param, b"relative/firmware\n")
        before = self.snapshot()
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is not absolute", result.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.state(), b"offline\n")
        self.assertFalse(self.run_fw.exists())

    def test_relative_run_path_is_rejected_when_fwparam_is_empty(self):
        self.write(self.fw_param, b"")
        result = self.run_helper(TB323FU_RUN_FIRMWARE="relative-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is not absolute", result.stderr)
        self.assertEqual(self.state(), b"offline\n")
        self.assertFalse((self.root / "relative-run").exists())

    def test_fwparam_write_failure_stops_before_the_dsp(self):
        self.fw_param.unlink()
        self.fw_param.mkdir()
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot write", result.stderr)
        self.assertEqual(self.state(), b"offline\n")
        # Staging already happened; only the parameter update failed.
        self.assertTrue((self.run_fw / FW_REL / "adsp.mbn").exists())

    # -- staging layout ------------------------------------------------------

    def test_topology_is_installed_at_the_exact_kernel_path(self):
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.run_fw / TOPO_REL).read_bytes(), self.topology.read_bytes()
        )
        self.assertFalse((self.run_fw / FW_REL / "LENOVO-TB323FU-tplg.bin").exists())
        self.assertFalse((self.run_fw / "LENOVO-TB323FU-tplg.bin").exists())

    # -- remoteproc handling -------------------------------------------------

    def test_offline_adsp_is_started(self):
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state(), b"start")
        self.assertIn("started adsp", result.stderr)

    def test_running_adsp_is_left_alone(self):
        self.write(self.adsp / "state", b"running\n")
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state(), b"running\n")
        self.assertIn("already running", result.stderr)

    def test_cdsp_is_never_touched(self):
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state(), b"start")
        self.assertEqual(self.state(self.cdsp), b"offline\n")
        self.assertEqual((self.cdsp / "name").read_bytes(), b"cdsp\n")
        self.assertEqual((self.cdsp / "firmware").read_bytes(), b"qcom/other/cdsp.mbn\n")

    def test_wrong_adsp_firmware_is_rejected(self):
        self.write(self.adsp / "firmware", b"qcom/kaanapali/lenovo/baldur/other.mbn\n")
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected", result.stderr)
        self.assertEqual(self.state(), b"offline\n")
        self.assertEqual(self.state(self.cdsp), b"offline\n")

    # -- standalone: no Android partition tooling ----------------------------

    def test_standalone_without_android_tools(self):
        # Synthetic stand-ins, not real tools: each records its own name and
        # fails loudly, so any invocation by the helper is detectable.
        tools = self.root / "fakebin"
        marker = self.root / "android-tool-called"
        for name in ("mount", "findmnt", "lpdump", "lpunpack", "adb"):
            script = pathlib.Path(tools / name)
            self.write(
                script,
                (
                    "#!/bin/sh\n"
                    "printf '%s\\n' \"$0\" >> '" + marker.as_posix() + "'\n"
                    "exit 99\n"
                ).encode(),
            )
            script.chmod(0o755)

        path = str(tools) + os.pathsep + os.environ.get("PATH", "")
        result = self.run_helper(PATH=path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state(), b"start")
        self.assertFalse(
            marker.exists(),
            marker.read_text() if marker.exists() else "",
        )


if __name__ == "__main__":
    unittest.main()
