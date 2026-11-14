#!/usr/bin/env python3
"""Check Android touch handoff with the profiles used by real runtime builds."""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--cc', default='cc')
parser.add_argument('--crdk-include', type=Path, required=True)
args, remaining = parser.parse_known_args()


class AndroidTouchProfileTest(unittest.TestCase):
    def check_profile(self, profile, enabled):
        with tempfile.TemporaryDirectory(prefix='android-touch-profile-') as tmp:
            generated = Path(tmp)
            env = dict(os.environ, srctree=str(ROOT / 'src'), ARCH='aarch64',
                       HOST_ARCH='x86_64', HOST_INFO='test', BUILD_TIME='test', VERSION='0.0.1')
            subprocess.run([sys.executable, '-B', str(ROOT / 'tools/configure_kconfig.py'),
                            '--root', str(ROOT), '--config', str(ROOT / 'configs' / profile),
                            '--header', str(generated / 'config.h'),
                            '--cmake', str(generated / 'config.cmake')],
                           cwd=ROOT, env=env, check=True, capture_output=True, text=True)
            command = [args.cc, '-E', '-P', '-DKSHIM_TB323FU_ANDROID_TOUCH_4M8_TRIAL=0',
                       '-DKSHIM_STANDALONE=0', '-DCR_FREESTANDING=1']
            for include in (generated, ROOT / 'src/include', ROOT / 'lib/libfdt/include',
                            args.crdk_include, ROOT / 'ports/crdk/include'):
                command.extend(['-I', str(include)])
            command.append(str(ROOT / 'src/core/main.c'))
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(result.stderr)
            handoff = re.search(r'\btouch_rc\s*=\s*tb323fu_android_touch_spi_trial\s*\(', result.stdout)
            self.assertEqual(bool(handoff), enabled,
                             profile + ': normal build lost or unexpectedly enabled Android touch handoff')
            if enabled:
                self.assertIn('clean_android_trial_fdt(runtime_fdt)', result.stdout)

    def test_gen5_keeps_android_touch_handoff(self):
        self.check_profile('tb323fu_unified_defconfig', True)

    def test_gen4_does_not_enable_gen5_touch_handoff(self):
        self.check_profile('tb322fc_android_defconfig', False)


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0]] + remaining)