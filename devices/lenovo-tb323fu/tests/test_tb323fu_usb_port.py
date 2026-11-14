"""Tests for the TB323FU USB routing script (rootfs/usr/libexec/tb323fu/tb323fu-usb-port).

The script is a /bin/sh program that routes the single dwc3 to the USB-C
connector actually in use.  These tests copy the script text and redirect its
/sys and /run/lock paths into a TemporaryDirectory, then run it with a mocked
PATH (gpiodetect/gpioget/gpioset/logger/sleep).  No live /sys, GPIO, or device
is touched; flock is the container's real util-linux.

The regression under test: a charger on port0 (sink/device) must never force
gpio30 low while a gamepad on port1 (source/host) is the real host -- the old
single-port rule tore down the xHCI of the connector in use.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "rootfs", "usr", "libexec", "tb323fu")
SCRIPT = os.path.join(SRC, "tb323fu-usb-port")
SH = shutil.which("sh") or "/bin/sh"

# --- mocked PATH tools (generated here, never installed) --------------------

MOCKS = {
    "gpiodetect": """#!/bin/sh
# only the f100000.pinctrl chip, found by name not by a hard-coded id
echo 'gpiochip8 [f100000.pinctrl] (150 lines)'
""",
    "gpioget": """#!/bin/sh
# --as-is --numeric -c <chip> 30: return the recorded level
if [ -n "$MOCK_GPIO_READ_FAIL" ]; then
	echo 'gpioget: injected read failure' >&2
	exit 1
fi
if [ -f "$MOCK_GPIO_STATE" ]; then
	cat "$MOCK_GPIO_STATE"
else
	echo 0
fi
""",
    "gpioset": """#!/bin/sh
# record every call, then apply it or inject a write failure
for a in "$@"; do last=$a; done
val=${last#30=}
echo "gpioset $*" >> "$MOCK_CALLS"
if [ -n "$MOCK_GPIO_WRITE_FAIL" ]; then
	echo 'gpioset: injected write failure' >&2
	exit 1
fi
printf '%s\\n' "$val" > "$MOCK_GPIO_STATE"
""",
    "logger": """#!/bin/sh
echo "$*" >> "$MOCK_LOG"
""",
    "sleep": """#!/bin/sh
# mocked sleep: never waits; optionally advances the fake UDC when the role is
# already device, so the dual-sink probe can observe enumeration
if [ -n "$MOCK_UDC_STATE" ] &&
	[ "$(cat "$MOCK_ROLE_STATE" 2>/dev/null)" = device ]; then
	printf 'configured\\n' > "$MOCK_UDC_STATE"
fi
exit 0
""",
}


class UsbPortTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = self._tmp.name
        self.sysdir = os.path.join(self.base, "sys")
        os.makedirs(self.sysdir, exist_ok=True)
        os.makedirs(os.path.join(self.base, "run", "lock"), exist_ok=True)

        self.mockbin = os.path.join(self.base, "bin")
        os.makedirs(self.mockbin, exist_ok=True)
        for name, body in MOCKS.items():
            path = os.path.join(self.mockbin, name)
            self._write_file(path, body)
            os.chmod(path, 0o755)

        self.gpio_state = os.path.join(self.base, "gpio30")
        self.calls = os.path.join(self.base, "calls.log")
        self.logfile = os.path.join(self.base, "logger.log")
        self.role_file = os.path.join(
            self.sysdir, "class/usb_role/a600000.usb-role-switch/role")
        self.udc_file = os.path.join(
            self.sysdir, "class/udc/a600000.usb/state")

        self.script = self._materialize_script()

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _write_file(path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)

    def _materialize_script(self):
        """Copy the script and point its /sys and /run/lock at a temp tree."""
        with open(SCRIPT, "r", encoding="utf-8") as handle:
            text = handle.read()
        base = self.base.replace(os.sep, "/")
        text = text.replace("/run/lock/", base + "/run/lock/")
        text = text.replace("/sys/", self.sysdir.replace(os.sep, "/") + "/")
        path = os.path.join(self.base, "tb323fu-usb-port")
        self._write_file(path, text)
        return path

    def _fixture(self, port0=None, port1=None, role="device",
                 udc="not attached", func="g1"):
        """portN: None (no partner) or (power_role, data_role) strings."""
        files = {
            "firmware/devicetree/base/compatible": "lenovo,baldur\0qcom,kaanapali\0",
            "class/usb_role/a600000.usb-role-switch/role": role + "\n",
            "class/udc/a600000.usb/state": udc + "\n",
            "class/udc/a600000.usb/function": func + "\n",
        }
        for idx, port in ((0, port0), (1, port1)):
            if port is None:
                continue
            power, data = port
            files["class/typec/port%d/power_role" % idx] = power + "\n"
            files["class/typec/port%d/data_role" % idx] = data + "\n"
            files["class/typec/port%d-partner" % idx] = ""
        for rel, content in files.items():
            self._write_file(os.path.join(self.sysdir, rel), content)

    def _gpio(self, value):
        self._write_file(self.gpio_state, value + "\n")

    def _read(self, path):
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def _role(self):
        return self._read(self.role_file).strip()

    def _udc(self):
        return self._read(self.udc_file).strip()

    def _gpio_value(self):
        return self._read(self.gpio_state).strip()

    def _role_mtime(self):
        return os.stat(self.role_file).st_mtime_ns

    def _gpioset_calls(self):
        if not os.path.exists(self.calls):
            return []
        with open(self.calls, "r", encoding="utf-8") as handle:
            return [line.strip() for line in handle if line.strip()]

    def _log(self):
        if not os.path.exists(self.logfile):
            return ""
        with open(self.logfile, "r", encoding="utf-8") as handle:
            return handle.read()

    def _run(self, args=(), advance_udc=False, extra_env=None):
        env = os.environ.copy()
        env["PATH"] = self.mockbin + os.pathsep + env.get("PATH", "")
        env["MOCK_GPIO_STATE"] = self.gpio_state
        env["MOCK_CALLS"] = self.calls
        env["MOCK_LOG"] = self.logfile
        env["MOCK_ROLE_STATE"] = self.role_file
        if advance_udc:
            env["MOCK_UDC_STATE"] = self.udc_file
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [SH, self.script, *args], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


LONG_SINK = ("source [sink]", "host [device]")    # charger on port0 (bottom)
SHORT_HOST = ("[source] sink", "[host] device")   # gamepad on port1 (side)
LONG_HOST = ("[source] sink", "[host] device")    # real host on port0
SHORT_SINK = ("source [sink]", "host [device]")   # sink on port1


class BoardIsolationTests(UsbPortTestBase):
    def _assert_untouched(self):
        before = self._role_mtime()
        result = self._run()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._role(), "device")
        self.assertEqual(self._role_mtime(), before)
        self.assertEqual(self._gpio_value(), "0")
        self.assertEqual(self._gpioset_calls(), [])
        self.assertFalse(os.path.exists(os.path.join(
            self.base, "run/lock/tb323fu-usb-port.lock")))

    def test_other_board_does_not_touch_routing(self):
        self._fixture(port0=LONG_SINK, port1=SHORT_HOST)
        self._gpio("0")
        self._write_file(os.path.join(self.sysdir,
            "firmware/devicetree/base/compatible"),
            "lenovo,baldur-other\0qcom,kaanapali\0")
        self._assert_untouched()

    def test_missing_board_identity_does_not_touch_routing(self):
        self._fixture(port0=LONG_SINK, port1=SHORT_HOST)
        self._gpio("0")
        os.unlink(os.path.join(self.sysdir,
            "firmware/devicetree/base/compatible"))
        self._assert_untouched()

    def test_gen4_board_does_not_touch_routing(self):
        # the real Gen4 compatible string: no Baldur identity, so untouched
        self._fixture(port0=LONG_SINK, port1=SHORT_HOST)
        self._gpio("0")
        self._write_file(os.path.join(self.sysdir,
            "firmware/devicetree/base/compatible"),
            "lenovo,elden\0qcom,sm8750\0")
        self._assert_untouched()

    def test_same_soc_without_baldur_identity_does_not_touch_routing(self):
        # a generic same-SoC board string alone must not match by SoC
        self._fixture(port0=LONG_SINK, port1=SHORT_HOST)
        self._gpio("0")
        self._write_file(os.path.join(self.sysdir,
            "firmware/devicetree/base/compatible"), "qcom,kaanapali\0")
        self._assert_untouched()


class HostCandidateTests(UsbPortTestBase):
    def test_charger_port0_gamepad_port1_routes_side_host(self):
        # the real-hardware state: a sink/device charger on port0 must not win
        self._fixture(port0=LONG_SINK, port1=SHORT_HOST, role="device")
        self._gpio("0")

        result = self._run()

        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._role(), "host")
        self.assertEqual(self._gpio_value(), "1")
        calls = self._gpioset_calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("30=1", calls[0])
        # no device probe: the fake UDC was never toggled
        self.assertEqual(self._udc(), "not attached")
        self.assertNotIn("UDC configured after toggle", self._log())
        self.assertNotIn("UDC not configured after 5s", self._log())

    def test_host_wins_even_with_configured_udc(self):
        # a real host bypasses the configured-gadget guard entirely
        self._fixture(port0=LONG_SINK, port1=SHORT_HOST,
                      role="device", udc="configured")
        self._gpio("0")

        result = self._run()

        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._role(), "host")
        self.assertEqual(self._gpio_value(), "1")
        # the guard was bypassed, never preserved: the UDC is untouched
        self.assertEqual(self._udc(), "configured")

    def test_mirror_host_on_port0_routes_bottom(self):
        self._fixture(port0=LONG_HOST, port1=SHORT_SINK, role="device")
        self._gpio("1")

        result = self._run()

        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._role(), "host")
        self.assertEqual(self._gpio_value(), "0")
        calls = self._gpioset_calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("30=0", calls[0])


class HostReconcileTests(UsbPortTestBase):
    def test_stable_host_reconcile_writes_nothing(self):
        self._fixture(port0=LONG_SINK, port1=SHORT_HOST, role="host")
        self._gpio("1")
        mtime_before = self._role_mtime()

        first = self._run(["host-reconcile"])
        second = self._run(["host-reconcile"])

        self.assertEqual(first.returncode, 0)
        self.assertEqual(second.returncode, 0)
        self.assertEqual(self._role(), "host")
        self.assertEqual(self._gpio_value(), "1")
        self.assertEqual(self._gpioset_calls(), [])
        self.assertEqual(self._role_mtime(), mtime_before)

    def test_reconcile_restores_host_after_device_steal(self):
        self._fixture(port0=LONG_SINK, port1=SHORT_HOST, role="device")
        self._gpio("1")

        result = self._run(["host-reconcile"])

        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._role(), "host")
        # gpio30 was already routed, so the role is the only write
        self.assertEqual(self._gpioset_calls(), [])
        self.assertEqual(self._gpio_value(), "1")

    def test_no_candidate_reconcile_is_noop(self):
        # charger only (sink/device): a stray role change must not route or probe
        self._fixture(port0=LONG_SINK, port1=None, role="device")
        self._gpio("0")

        result = self._run(["host-reconcile"])

        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._role(), "device")
        self.assertEqual(self._gpio_value(), "0")
        self.assertEqual(self._gpioset_calls(), [])
        self.assertNotIn("UDC configured after toggle", self._log())

    def test_host_removed_reconcile_does_not_reforce_host(self):
        # the old host is gone; a stale role change must not force it back
        self._fixture(port0=None, port1=None, role="device")
        self._gpio("1")

        result = self._run(["host-reconcile"])

        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._role(), "device")
        self.assertEqual(self._gpio_value(), "1")
        self.assertEqual(self._gpioset_calls(), [])

    def test_gpio_write_failure_returns_nonzero(self):
        self._fixture(port0=LONG_SINK, port1=SHORT_HOST, role="device")
        self._gpio("0")

        result = self._run(["host-reconcile"],
                           extra_env={"MOCK_GPIO_WRITE_FAIL": "1"})

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._role(), "device")   # role not written after the
        self.assertTrue(self._gpioset_calls())     # failed write is recorded

    def test_gpio_read_failure_returns_nonzero(self):
        self._fixture(port0=LONG_SINK, port1=SHORT_HOST, role="device")
        self._gpio("0")

        result = self._run(["host-reconcile"],
                           extra_env={"MOCK_GPIO_READ_FAIL": "1"})

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._role(), "device")
        self.assertEqual(self._gpioset_calls(), [])


class DevicePathTests(UsbPortTestBase):
    def test_single_port_pc_sink_routes_device(self):
        # only the side connector has a PC the tablet sinks from: not a host
        self._fixture(port0=None, port1=("source [sink]", "host [device]"),
                      role="none")
        self._gpio("0")

        result = self._run()

        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._role(), "device")
        self.assertEqual(self._gpio_value(), "1")
        calls = self._gpioset_calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("30=1", calls[0])

    def test_single_port_pc_sink_preserves_configured_gadget(self):
        self._fixture(port0=None, port1=("source [sink]", "host [device]"),
                      role="device", udc="configured")
        self._gpio("0")

        result = self._run()

        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._gpioset_calls(), [])
        self.assertEqual(self._role(), "device")
        self.assertEqual(self._udc(), "configured")

    def test_single_port_pc_sink_preserves_suspended_gadget(self):
        self._fixture(port0=None, port1=("source [sink]", "host [device]"),
                      role="device", udc="suspended")
        self._gpio("0")

        result = self._run()

        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._gpioset_calls(), [])
        self.assertEqual(self._role(), "device")

    def test_dual_sink_still_probes_and_enumerates(self):
        self._fixture(port0=("source [sink]", "host [device]"),
                      port1=("source [sink]", "host [device]"),
                      role="device", udc="not attached")
        self._gpio("0")

        result = self._run(advance_udc=True)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._role(), "device")
        self.assertEqual(self._udc(), "configured")
        self.assertTrue(self._gpioset_calls())
        self.assertIn("UDC configured after toggle", self._log())


if __name__ == "__main__":
    unittest.main()
