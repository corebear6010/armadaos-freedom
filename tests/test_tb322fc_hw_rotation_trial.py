#!/usr/bin/env python3
"""Keep the TB322FC hardware-rotation trial settings aligned."""

import json
import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class Tb322fcHardwareRotationTrial(unittest.TestCase):
    def test_panel_reports_270_degrees(self):
        text = (ROOT / "packages/kernel/dts/sm8750-lenovo-elden.dts").read_text()
        panel_start = text.index("panel0: panel@0")
        panel = text[panel_start:text.index("ports {", panel_start)]
        self.assertIn("rotation = <270>;", panel)

        driver = (
            ROOT
            / "packages/kernel/patches"
            / "0141-drm-panel-add-nt36536-and-dsc-packet-support.patch"
        ).read_text()
        self.assertIn(
            "+	.get_orientation = novatek_get_orientation,",
            driver,
        )
        self.assertIn(
            "+	ret = of_drm_get_panel_orientation(dev->of_node, &ctx->orientation);",
            driver,
        )

    def test_gamescope_uses_hardware_rotation(self):
        config = (
            ROOT / "system_files/usr/lib/armada/devices/lenovo-tb322fc.conf"
        ).read_text()
        self.assertIn("ARMADA_PANEL_ORIENTATION=left\n", config)
        self.assertIn("ARMADA_GAMESCOPE_USE_ROTATION_SHADER=0\n", config)

        session = (
            ROOT
            / "system_files/usr/share/gamescope-session-plus/sessions.d/steam"
        ).read_text()
        self.assertIn("lenovo-tb322fc)", session)
        self.assertIn('ORIENTATION="${ARMADA_PANEL_ORIENTATION:-left}"', session)

    def test_package_enables_virtual_planes(self):
        document = json.loads(
            (ROOT / "build_files/edl-devices.json").read_text()
        )
        self.assertIn(
            "msm.dpu_use_virtual_planes=1",
            document["profiles"]["TB322FC"]["extraArgs"],
        )

    def test_adsp_starts_before_upower(self):
        script_path = (
            ROOT
            / "system_files/usr/libexec/armada/armada-adsp-late-start"
        )
        script = script_path.read_text()
        self.assertIn(
            'grep -azFxq lenovo,elden "$compat"',
            script,
        )

        unit = (
            ROOT
            / "system_files/usr/lib/systemd/system"
            / "armada-adsp-late-start.service"
        ).read_text()
        self.assertIn("Before=upower.service\n", unit)
        self.assertIn("After=local-fs.target systemd-udev-trigger.service\n", unit)
        self.assertIn(
            "ExecStart=/usr/libexec/armada/armada-adsp-late-start\n", unit
        )

        build = (
            ROOT / "build_files/40-vendor-system-files.sh"
        ).read_text()
        self.assertIn(
            "systemctl enable armada-adsp-late-start.service\n",
            build,
        )

        with tempfile.TemporaryDirectory() as temporary:
            compatible = pathlib.Path(temporary) / "compatible"
            compatible.write_bytes(b"lenovo,baldur\0qcom,sm8850\0")
            environment = os.environ.copy()
            environment["ARMADA_ADSP_DT_COMPATIBLE"] = str(compatible)
            result = subprocess.run(
                ["/bin/sh", str(script_path)],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not lenovo,elden", result.stdout)

    def test_adsp_script_accepts_expected_remoteproc(self):
        script_path = (
            ROOT
            / "system_files/usr/libexec/armada/armada-adsp-late-start"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            compatible = root / "compatible"
            compatible.write_bytes(b"lenovo,elden\0qcom,sm8750\0")

            firmware = root / "adsp.mdt"
            firmware.write_bytes(b"firmware")

            remoteproc = root / "remoteproc" / "remoteproc0"
            remoteproc.mkdir(parents=True)
            (remoteproc / "name").write_text("adsp\n")
            (remoteproc / "firmware").write_text("qcom/sm8750/adsp.mdt\n")
            (remoteproc / "state").write_text("running\n")

            battery = root / "battery"
            battery.mkdir()
            (battery / "capacity").write_text("55\n")
            (battery / "voltage_now").write_text("7800000\n")

            environment = os.environ.copy()
            environment.update(
                {
                    "ARMADA_ADSP_DT_COMPATIBLE": str(compatible),
                    "ARMADA_ADSP_FIRMWARE": str(firmware),
                    "ARMADA_ADSP_REMOTEPROC_DIR": str(remoteproc.parent),
                    "ARMADA_ADSP_BATTERY_DIR": str(battery),
                }
            )
            result = subprocess.run(
                ["/bin/sh", str(script_path)],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ADSP is running", result.stdout)


if __name__ == "__main__":
    unittest.main()
