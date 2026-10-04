#!/usr/bin/env python3
"""Check production binaries once, failing on workspace warnings without RUSTFLAGS.

Keep this separate from test/all-targets builds: their dev-dependency feature
unification and cfg(test) can hide production-only dead code. Dependencies retain
their own lint policy. Diagnostics stream to CI; only bounded facts are uploaded.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys


class BuildFacts:
    def __init__(self, members: set[str]) -> None:
        self.members = members
        self.warning_count = 0
        self.warnings: list[dict] = []
        self.finished = False
        self.invalid_output = False

    def feed(self, line: str) -> None:
        try:
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError("expected Cargo object")
        except ValueError:
            self.invalid_output = True
            print(line.rstrip(), file=sys.stderr)
            return
        if item.get("reason") == "build-finished":
            self.finished = item.get("success") is True
        if item.get("reason") != "compiler-message":
            return
        message = item["message"]
        # Cargo JSON otherwise hides compiler output, including fatal errors.
        if message.get("rendered"):
            print(message["rendered"], end="", file=sys.stderr, flush=True)
        if message.get("level") != "warning" or item.get("package_id") not in self.members:
            return
        self.warning_count += 1
        if len(self.warnings) < 100:
            span = next((s for s in message.get("spans", []) if s.get("is_primary")), {})
            self.warnings.append({"message": str(message.get("message", ""))[:500],
                                  "path": str(span.get("file_name", ""))[:500],
                                  "line": span.get("line_start")})

    def exit_code(self, child_code: int) -> int:
        if child_code:
            return child_code if child_code > 0 else 128 - child_code
        return int(self.warning_count > 0 or not self.finished or self.invalid_output)

    def report(self, child_code: int) -> dict:
        return {"version": 1, "cargo_exit_code": child_code,
                "exit_code": self.exit_code(child_code), "build_finished": self.finished,
                "invalid_output": self.invalid_output, "warning_count": self.warning_count,
                "warnings": self.warnings, "truncated": self.warning_count > len(self.warnings),
                "attempts_executed": 1}


def run(report_path: Path, profile: str) -> int:
    # A failed metadata/spawn attempt must not upload an older successful report.
    report_path.unlink(missing_ok=True)
    metadata = subprocess.run(
        ["cargo", "metadata", "--locked", "--no-deps", "--format-version=1"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True, encoding="utf-8", check=True)
    members = set(json.loads(metadata.stdout)["workspace_members"])
    if not members:
        raise ValueError("Cargo metadata returned no workspace members")
    facts = BuildFacts(members)
    command = ["cargo", "check", "--locked", "--workspace", "--bins",
               "--profile", profile, "--message-format=json"]
    print("[production] " + " ".join(command), flush=True)
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          text=True, encoding="utf-8", errors="replace") as child:
        assert child.stdout is not None
        for line in child.stdout:
            facts.feed(line)
        code = child.wait()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(facts.report(code), indent=2) + "\n", encoding="utf-8")
    print(f"[production] workspace warnings={facts.warning_count}; cargo exit={code}; report={report_path}")
    return facts.exit_code(code)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("dev", "dogfood", "release"), default="dogfood")
    parser.add_argument("--report", type=Path, default=Path("target/ci-build-reports/production.json"))
    args = parser.parse_args()
    try:
        return run(args.report, args.profile)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"[production] unable to complete warning gate: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
