"""Production-only warning gate: no retries, global lint flags or false success."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from scripts.check_production_warnings import BuildFacts, run


class ProductionWarningTests(unittest.TestCase):
    def warning(self, facts, package="workspace", level="warning"):
        with contextlib.redirect_stderr(io.StringIO()) as output:
            facts.feed(json.dumps({"reason": "compiler-message", "package_id": package,
                                  "message": {"level": level, "message": "unused item",
                                              "rendered": "visible diagnostic\n", "spans": []}}))
        self.assertIn("visible diagnostic", output.getvalue())

    def finished(self, facts):
        facts.feed('{"reason":"build-finished","success":true}')

    def test_clean_completed_build_passes(self):
        facts = BuildFacts({"workspace"})
        self.finished(facts)
        self.assertEqual(facts.exit_code(0), 0)

    def test_workspace_warning_fails_but_dependency_warning_does_not(self):
        facts = BuildFacts({"workspace"})
        self.finished(facts)
        self.warning(facts, "dependency")
        self.assertEqual(facts.exit_code(0), 0)
        self.warning(facts)
        self.assertEqual(facts.exit_code(0), 1)
        self.assertEqual(facts.warning_count, 1)

    def test_compile_failure_and_signal_preserve_failure(self):
        facts = BuildFacts({"workspace"})
        self.warning(facts, level="error")
        self.assertEqual(facts.exit_code(101), 101)
        self.assertEqual(facts.exit_code(-9), 137)

    def test_missing_or_failed_build_summary_cannot_pass(self):
        facts = BuildFacts({"workspace"})
        self.assertEqual(facts.exit_code(0), 1)
        facts.feed('{"reason":"build-finished","success":false}')
        self.assertEqual(facts.exit_code(0), 1)

    def test_malformed_output_cannot_pass(self):
        for invalid in ("broken", "[]"):
            facts = BuildFacts({"workspace"})
            with contextlib.redirect_stderr(io.StringIO()):
                facts.feed(invalid)
            self.finished(facts)
            self.assertEqual(facts.exit_code(0), 1)

    def test_report_is_bounded_without_hiding_warning_count(self):
        facts = BuildFacts({"workspace"})
        for _ in range(120):
            self.warning(facts)
        report = facts.report(0)
        self.assertEqual(report["warning_count"], 120)
        self.assertEqual(len(report["warnings"]), 100)
        self.assertTrue(report["truncated"])
        self.assertEqual(report["attempts_executed"], 1)


class ProductionInvocationTests(unittest.TestCase):
    def test_actual_gate_invokes_production_check_once_and_preserves_exit_status(self):
        for code in (0, 101):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temp:
                report = Path(temp) / "report.json"
                child = MagicMock()
                child.stdout = io.StringIO(json.dumps({"reason": "build-finished", "success": code == 0}) + "\n")
                child.wait.return_value = code
                with patch("scripts.check_production_warnings.subprocess.run") as metadata, \
                        patch("scripts.check_production_warnings.subprocess.Popen") as process, \
                        contextlib.redirect_stdout(io.StringIO()):
                    metadata.return_value.stdout = '{"workspace_members":["workspace"]}'
                    process.return_value.__enter__.return_value = child
                    self.assertEqual(run(report, "dogfood"), code)
                    metadata.assert_called_once()
                    process.assert_called_once()
                    self.assertEqual(process.call_args.args[0], [
                        "cargo", "check", "--locked", "--workspace", "--bins",
                        "--profile", "dogfood", "--message-format=json"])
                self.assertEqual(json.loads(report.read_text())["cargo_exit_code"], code)

    def test_metadata_failure_removes_stale_success_report(self):
        with tempfile.TemporaryDirectory() as temp:
            report = Path(temp) / "report.json"
            report.write_text('{"exit_code":0}')
            with patch("scripts.check_production_warnings.subprocess.run", side_effect=OSError("no cargo")):
                with self.assertRaises(OSError):
                    run(report, "dogfood")
            self.assertFalse(report.exists())


if __name__ == "__main__":
    unittest.main()
