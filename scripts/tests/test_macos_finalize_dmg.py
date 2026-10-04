"""Execute the real DMG control flow with fake native tools; no signing keys."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipIf(sys.platform == "win32", "POSIX shell fixture; runs in Linux release preflight")
class DmgTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.scripts = self.root / "scripts"
        self.scripts.mkdir()
        self.script = self.scripts / "macos_finalize_dmg.sh"
        shutil.copyfile(ROOT / "scripts/macos_finalize_dmg.sh", self.script)
        (self.scripts / "macos_finalize_desktop.sh").write_text(
            '#!/bin/sh\n[ "${FAIL_STAGE:-}" != sign-app ] || { echo signing-failed >&2; exit 23; }\n')
        hdiutil = self.bin / "hdiutil"
        hdiutil.write_text('''#!/usr/bin/env python3
import os,sys
from pathlib import Path
args=sys.argv[1:]
assert '-quiet' not in args, 'diagnostics must remain visible'
command=args[0]
print('native '+command+' diagnostic',flush=True)
if os.environ.get('FAIL_STAGE')==command:
 print('injected native failure',file=sys.stderr); sys.exit(23)
if command=='create':
 Path(args[-1]).write_bytes(b'finished-image')
elif command=='attach':
 mount=Path(args[args.index('-mountpoint')+1])
 (mount/'probe').write_text('disk image preflight\\n')
 if os.environ.get('FAIL_STAGE')!='inspect-app': (mount/'WebCodex Desktop.app').mkdir()
''')
        hdiutil.chmod(0o755)
        ditto = self.bin / "ditto"
        ditto.write_text('#!/usr/bin/env python3\nimport shutil,sys\nshutil.copytree(sys.argv[1],sys.argv[2])\n')
        ditto.chmod(0o755)
        self.dmg = self.root / "candidate.dmg"
        self.dmg.write_bytes(b'original-image')
        self.diag = self.root / "diagnostics"
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        TMPDIR=str(self.root), WEBCODEX_DMG_DIAGNOSTICS_DIR=str(self.diag))

    def run_script(self, *args, fail=""):
        env = dict(self.env, FAIL_STAGE=fail)
        return subprocess.run(["bash", str(self.script), *map(str, args)], env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)

    def test_preflight_checks_create_attach_read_and_detach(self):
        result = self.run_script("--preflight")
        self.assertEqual(result.returncode, 0, result.stderr)
        for stage in ("create", "attach", "verify", "detach"):
            self.assertIn("stage=" + stage, result.stdout)
        self.assertNotIn("sign-app", result.stdout)

    def test_preflight_failure_is_visible_and_preserves_exit_code(self):
        for stage in ("create", "attach", "detach"):
            with self.subTest(stage=stage):
                result = self.run_script("--preflight", fail=stage)
                self.assertEqual(result.returncode, 23, result.stderr)
                self.assertIn("injected native failure", result.stderr)
                self.assertIn("failed stage=" + stage, result.stderr)

    def test_failed_finalize_preserves_original_and_stage_evidence(self):
        for stage in ("attach", "detach", "create", "verify", "sign-app", "inspect-app"):
            with self.subTest(stage=stage):
                result = self.run_script(self.dmg, "self-signed", "fixture-identity", fail=stage)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                expected = "verify-image" if stage == "verify" else stage
                self.assertIn("failed stage=" + expected, result.stderr)
                self.assertEqual(self.dmg.read_bytes(), b"original-image")
                logs = "".join(p.read_text() for p in self.diag.iterdir())
                self.assertIn("failed stage=" + expected, logs)
                self.assertNotIn("fixture-identity", logs)
                if stage == "detach":
                    self.assertEqual(result.returncode, 23)
                    self.assertIn("cleanup detach failed; retained scratch=", logs)
                    for scratch in self.root.glob("webcodex-final-dmg.*"):
                        shutil.rmtree(scratch)  # Fake mounts only; isolate subtests.
                self.assertEqual(list(self.root.glob("webcodex-final-dmg.*")), [])

    def test_success_verifies_then_atomically_replaces_candidate(self):
        result = self.run_script(self.dmg, "self-signed", "fixture-identity")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.dmg.read_bytes(), b"finished-image")
        self.assertLess(result.stdout.index("stage=verify-image"), result.stdout.index("stage=replace"))
        self.assertEqual(list(self.root.glob("candidate.dmg.final.*")), [])

    def test_bad_mode_and_missing_dmg_fail_before_work(self):
        result = self.run_script(self.dmg, "unknown", "identity")
        self.assertEqual(result.returncode, 2)
        result = self.run_script(self.root / "missing.dmg", "self-signed", "identity")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.diag.exists())

    def test_adhoc_does_not_resign_or_replace(self):
        result = self.run_script(self.dmg, "adhoc", "-")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.dmg.read_bytes(), b"original-image")
        self.assertFalse(self.diag.exists())


class DmgWorkflowTests(unittest.TestCase):
    def test_preflight_precedes_expensive_native_builds(self):
        from scripts.tests.test_ci_contract_ownership import job
        for workflow, lane, build in (
            ("release-build.yml", "macos", "Build and verify native macOS candidate"),
            ("extended-native.yml", "macos-intel", "Check macOS Intel runtime surfaces"),
            ("ci.yml", "test-macos-desktop", "Check Desktop Rust"),
        ):
            text = job((ROOT / ".github/workflows" / workflow).read_text(), lane)
            self.assertLess(text.index("macos_finalize_dmg.sh --preflight"), text.index(build))
        supplemental = (ROOT / ".github/workflows/release-desktop-darwin-x64.yml").read_text()
        self.assertLess(supplemental.index("macos_finalize_dmg.sh --preflight"),
                        supplemental.index("Build macOS Intel Desktop DMG"))
        self.assertIn("path: target/dmg-diagnostics", supplemental)


if __name__ == "__main__":
    unittest.main()
