"""Keep one root-suite owner while optional tool contracts get complete coverage."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]


def job(text: str, name: str) -> str:
    match = re.search(r"(?ms)^  " + re.escape(name) + r":\n.*?(?=^  [A-Za-z0-9_-]+:|\Z)", text)
    if match is None:
        raise AssertionError(f"missing required CI job: {name}")
    return match.group()


class ContractOwnershipTests(unittest.TestCase):
    def test_root_integration_remains_mandatory_without_duplicate_contract_build(self) -> None:
        ci = (ROOT / ".github/workflows/ci.yml").read_text()
        contract = job(ci, "contract")
        self.assertIn("cargo test --locked -p webcodex-tool-contracts --all-features --lib", contract)
        self.assertNotRegex(contract, r"cargo test[^\n]*-p webcodex(?:\s|$)")
        rust = job(ci, "test-linux-rust")
        self.assertIn("- shard: server", rust)
        self.assertIn('packages: "-p webcodex"', rust)
        self.assertIn("scripts/ci_rust_test.py", rust)
        self.assertNotIn("--lib", rust)  # Bin/integration targets stay covered too.
        gate = job(ci, "test")
        self.assertIn("test-linux-rust", gate)
        self.assertIn('"$RUST_RESULT" != success', gate)

    def test_production_warning_gate_is_separate_from_test_feature_unification(self) -> None:
        ci = (ROOT / ".github/workflows/ci.yml").read_text()
        for lane in ("test-linux-tooling", "test-windows-core", "test-macos-core"):
            body = job(ci, lane)
            self.assertIn("scripts/check_production_warnings.py", body)
            self.assertIn("target/ci-build-reports/*.json", body)
        windows = job(ci, "test-windows-core")
        self.assertIn('throw "Windows upgrade tests failed"', windows)
        self.assertIn('throw "Windows service tests failed"', windows)

    def test_native_upgrade_and_protocol_evidence_are_not_model_name_compatibility(self) -> None:
        ci = (ROOT / ".github/workflows/ci.yml").read_text()
        for required in ["mcp-conformance", "test-windows-core", "test-windows-runner", "test-macos-core"]:
            self.assertTrue(job(ci, required))
        self.assertIn("webcodex-environment --lib upgrade::", job(ci, "test-windows-core"))
        self.assertIn("runner-real-process-tests", job(ci, "test-windows-runner"))


if __name__ == "__main__":
    unittest.main()
