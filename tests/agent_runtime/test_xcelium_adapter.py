#!/usr/bin/env python3
"""Isolated Xcelium adapter qualification with a controlled fake xrun."""
from __future__ import annotations

import ast
import json
import os
import tempfile
import unittest
from pathlib import Path

from adapters.eda import (
    TrustedEdaBoundaryError,
    XceliumAdapter,
    XceliumRunConfiguration,
)


FAKE_XRUN = r'''#!/usr/bin/env python3
import os
import pathlib
import re
import sys
import time

here = pathlib.Path(__file__).resolve().parent
counter = here / "invocations.txt"
old = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(old + 1))
args = sys.argv[1:]
if "-version" in args:
    print("xrun(64): 24.09-s001: Xcelium simulator")
    raise SystemExit(0)

sources = []
for value in args:
    path = pathlib.Path(value)
    if path.suffix in {".sv", ".svh", ".v"} and path.is_file():
        sources.append(path.read_text())
content = "\n".join(sources)
if "FAKE_TIMEOUT" in content:
    time.sleep(5)

log = pathlib.Path(args[args.index("-l") + 1])
log.parent.mkdir(parents=True, exist_ok=True)
if "FAKE_LICENSE_FAILURE" in content:
    message = "xrun: *F,NOLICN: license checkout failed"
    log.write_text(message)
    print(message, file=sys.stderr)
    raise SystemExit(2)
if "FAKE_COMPILE_FAILURE" in content:
    message = "xmvlog: *E,SYNERR: controlled syntax failure"
    log.write_text(message)
    print(message, file=sys.stderr)
    raise SystemExit(1)
if "FAKE_ELABORATION_FAILURE" in content:
    message = "xmelab: *E,CUVMUR: controlled elaboration failure"
    log.write_text(message)
    print(message, file=sys.stderr)
    raise SystemExit(1)

database = pathlib.Path(args[args.index("-xmlibdirname") + 1])
database.mkdir(parents=True, exist_ok=True)
(database / "snapshot.bin").write_bytes(b"controlled-xcelium-snapshot")
if "FAKE_INTERNAL_SYMLINK" in content:
    (database / "snapshot.link").symlink_to("snapshot.bin")
if "FAKE_EXTERNAL_SYMLINK" in content:
    (database / "escape.link").symlink_to(pathlib.Path(__file__).resolve())
pathlib.Path("xrun.history").write_text("controlled-history")
messages = ["xrun: compile/elaboration completed"]
if "-elaborate" not in args:
    match = re.search(r"DV_[A-Z0-9_]+_PASS", content)
    if match:
        messages.append(match.group(0))
    if "FAKE_UVM_FAILURE" in content:
        messages.append("UVM_ERROR : 1")
    if "FAKE_SIMULATION_FAILURE" in content:
        messages.append("xmsim: *E,RNQUIE: controlled simulation failure")
    if "FAKE_WAVES" in content:
        pathlib.Path("dump.vcd").write_text("$date controlled $end")
if "-covworkdir" in args:
    coverage = pathlib.Path(args[args.index("-covworkdir") + 1])
    coverage.mkdir(parents=True, exist_ok=True)
    (coverage / "scope.ucm").write_bytes(b"controlled-coverage")
message = "\n".join(messages)
log.write_text(message)
print(message)
raise SystemExit(0)
'''


class XceliumAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.result = self.root / "result"
        self.tools = self.root / "tools"
        self.tools.mkdir()
        self.xrun = self.tools / "xrun"
        self.xrun.write_text(FAKE_XRUN, encoding="utf-8")
        self.xrun.chmod(0o755)
        self.source = self.root / "tb.sv"
        self._write_source()

    def _write_source(self, extra: str = "") -> None:
        self.source.write_text(
            "module tb; initial begin $display(\"DV_TINY_PASS\"); "
            "$finish; end endmodule\n// FAKE_WAVES\n{}\n".format(extra),
            encoding="utf-8")

    def adapter(self, *, timeout: int = 10) -> XceliumAdapter:
        return XceliumAdapter(
            self.root, self.result, "JOB.XCELIUM.ADAPTER.TEST",
            self.xrun, "XCELIUMENV.TEST.24_09", {
                "PATH": "{}{}{}".format(
                    self.tools, os.pathsep, "/usr/bin"),
                "LM_LICENSE_FILE": "27000@private-license-host",
                "LC_ALL": "C",
            }, timeout_seconds=timeout)

    def configuration(self, **overrides) -> XceliumRunConfiguration:
        values = {
            "sources": ("tb.sv",),
            "top": "tb",
            "include_dirs": (),
            "defines": ("WIDTH=32",),
            "plusargs": ("+MODE=SMOKE",),
            "uvm": False,
            "uvm_test": None,
            "seed": 17,
            "timescale": "1ns/1ps",
            "access": "rwc",
            "coverage": False,
            "waves": True,
            "pass_marker": "DV_TINY_PASS",
        }
        values.update(overrides)
        return XceliumRunConfiguration(**values)

    def invocation_count(self) -> int:
        path = self.tools / "invocations.txt"
        return int(path.read_text()) if path.exists() else 0

    def test_version_build_and_run_produce_isolated_evidence(self):
        adapter = self.adapter()
        version = adapter.probe_version()
        self.assertEqual("PASS", version.evidence["execution_status"])
        self.assertIn("24.09", version.evidence["simulator_version"])

        build = adapter.build_only("BUILD.SMOKE", self.configuration())
        self.assertEqual("PASS", build.evidence["execution_status"])
        self.assertIsNone(build.evidence["pass_marker_found"])
        self.assertIn(
            "-elaborate",
            [item["value"] for item in build.request["argv_template"]])

        run = adapter.run("RUN.SMOKE", self.configuration())
        self.assertEqual("PASS", run.evidence["execution_status"])
        self.assertTrue(run.evidence["pass_marker_found"])
        self.assertEqual(
            "ISOLATED_XCELIUM_ADAPTER_NO_PROJECT_AUTHORITY",
            run.evidence["qualification_scope"])
        kinds = {item["kind"] for item in run.evidence["artifacts"]}
        self.assertEqual(
            {"XCELIUM_DATABASE", "XRUN_LOG", "TRACE_VCD"}, kinds)
        persisted = json.loads(
            (adapter.job_root / run.evidence_path).read_text())
        self.assertEqual(run.evidence, persisted)
        self.assertNotIn(
            "private-license-host",
            json.dumps({"request": run.request, "evidence": run.evidence}))

    def test_compile_only_has_no_top_or_elaboration(self):
        result = self.adapter().compile_only(
            "COMPILE.UVM", self.configuration(top=None))
        values = [item["value"] for item in result.request["argv_template"]]
        self.assertEqual("COMPILE", result.request["phase"])
        self.assertIn("-compile", values)
        self.assertNotIn("-elaborate", values)
        self.assertNotIn("-top", values)
        self.assertNotIn("-snapshot", values)
        self.assertNotIn("-access", values)
        self.assertEqual("PASS", result.evidence["execution_status"])

    def test_adapter_has_no_project_workflow_dependency(self):
        module_path = Path(__file__).resolve().parents[2] / \
            "adapters/eda/xcelium.py"
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)
        self.assertFalse(any(name == "runtime" or name.startswith("runtime.")
                             for name in imports))
        self.assertFalse(any(name == "application" or
                             name.startswith("application.")
                             for name in imports))
        self.assertFalse(any(name == "domain" or name.startswith("domain.")
                             for name in imports))

    def test_uvm_coverage_and_typed_arguments(self):
        adapter = self.adapter()
        result = adapter.run("RUN.UVM", self.configuration(
            uvm=True, uvm_test="base_test", coverage=True))
        values = [item["value"] for item in result.request["argv_template"]]
        self.assertIn("-uvm", values)
        self.assertIn("+UVM_TESTNAME=base_test", values)
        self.assertIn("-svseed", values)
        self.assertIn("-covworkdir", values)
        self.assertEqual("PASS", result.evidence["execution_status"])
        self.assertIn(
            "COVERAGE_DATABASE",
            {item["kind"] for item in result.evidence["artifacts"]})

    def test_exact_replay_does_not_execute_xrun_again(self):
        first = self.adapter().run("RUN.REPLAY", self.configuration())
        count = self.invocation_count()
        replay = self.adapter().run("RUN.REPLAY", self.configuration())
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.evidence, replay.evidence)
        self.assertEqual(count, self.invocation_count())

    def test_replay_rejects_artifact_tamper(self):
        result = self.adapter().run("RUN.TAMPER", self.configuration())
        output = self.adapter().job_root / result.request["output_subdir"]
        (output / "dump.vcd").write_text("tampered", encoding="utf-8")
        with self.assertRaises(TrustedEdaBoundaryError) as caught:
            self.adapter().run("RUN.TAMPER", self.configuration())
        self.assertEqual("STALE_EVIDENCE", caught.exception.diagnostics[0]["code"])

    def test_replay_rejects_unregistered_output_tamper(self):
        result = self.adapter().run("RUN.TREE.TAMPER", self.configuration())
        output = self.adapter().job_root / result.request["output_subdir"]
        (output / "xrun.history").write_text("tampered", encoding="utf-8")
        with self.assertRaises(TrustedEdaBoundaryError) as caught:
            self.adapter().run("RUN.TREE.TAMPER", self.configuration())
        self.assertEqual("STALE_EVIDENCE", caught.exception.diagnostics[0]["code"])

    def test_internal_artifact_symlink_is_recorded_and_replay_safe(self):
        self._write_source("// FAKE_INTERNAL_SYMLINK")
        result = self.adapter().run("RUN.INTERNAL.SYMLINK", self.configuration())
        output = self.adapter().job_root / result.request["output_subdir"]
        link = output / "xcelium.d/snapshot.link"
        self.assertTrue(link.is_symlink())
        self.assertEqual("snapshot.bin", str(link.readlink()))
        self.assertTrue(self.adapter().run(
            "RUN.INTERNAL.SYMLINK", self.configuration()).replayed)

        link.unlink()
        link.symlink_to("../xrun.log")
        with self.assertRaises(TrustedEdaBoundaryError) as caught:
            self.adapter().run("RUN.INTERNAL.SYMLINK", self.configuration())
        self.assertEqual("STALE_EVIDENCE", caught.exception.diagnostics[0]["code"])

    def test_external_artifact_symlink_is_rejected(self):
        self._write_source("// FAKE_EXTERNAL_SYMLINK")
        with self.assertRaises(TrustedEdaBoundaryError) as caught:
            self.adapter().run("RUN.EXTERNAL.SYMLINK", self.configuration())
        self.assertEqual(
            "TOOL_PERMISSION_DENIED", caught.exception.diagnostics[0]["code"])

    def test_same_execution_id_rejects_source_drift(self):
        self.adapter().run("RUN.DRIFT", self.configuration())
        self._write_source("// changed")
        with self.assertRaises(TrustedEdaBoundaryError) as caught:
            self.adapter().run("RUN.DRIFT", self.configuration())
        self.assertEqual(
            "CONFLICTING_REPLAY", caught.exception.diagnostics[0]["code"])

    def test_failure_classification(self):
        cases = (
            ("FAKE_COMPILE_FAILURE", "COMPILE_FAILED", "FAIL"),
            ("FAKE_ELABORATION_FAILURE", "ELABORATION_FAILED", "FAIL"),
            ("FAKE_SIMULATION_FAILURE", "SIMULATION_FAILED", "FAIL"),
            ("FAKE_UVM_FAILURE", "UVM_FAILURE", "FAIL"),
            ("FAKE_LICENSE_FAILURE", "LICENSE_UNAVAILABLE", "BLOCKED_TOOL"),
        )
        for index, (source, diagnostic, status) in enumerate(cases):
            with self.subTest(diagnostic=diagnostic):
                self._write_source("// {}".format(source))
                result = self.adapter().run(
                    "RUN.FAILURE.{}".format(index), self.configuration())
                self.assertEqual(status, result.evidence["execution_status"])
                self.assertIn(diagnostic, result.evidence["diagnostic_codes"])

    def test_missing_pass_marker_and_timeout_fail_closed(self):
        self.source.write_text(
            "module tb; initial $finish; endmodule\n", encoding="utf-8")
        missing = self.adapter().run(
            "RUN.NO.MARKER", self.configuration(waves=False))
        self.assertEqual("FAIL", missing.evidence["execution_status"])
        self.assertIn(
            "PASS_MARKER_MISSING", missing.evidence["diagnostic_codes"])

        self._write_source("// FAKE_TIMEOUT")
        timeout = self.adapter(timeout=1).run(
            "RUN.TIMEOUT", self.configuration(waves=False))
        self.assertEqual("BLOCKED_TOOL", timeout.evidence["execution_status"])
        self.assertTrue(timeout.evidence["timed_out"])
        self.assertIn("PROCESS_TIMEOUT", timeout.evidence["diagnostic_codes"])

    def test_unsafe_configuration_and_environment_are_rejected(self):
        with self.assertRaises(TrustedEdaBoundaryError) as caught:
            self.adapter().run("RUN.UNSAFE", self.configuration(
                defines=("WIDTH=$(id)",)))
        self.assertEqual(
            "DV_OWNER", caught.exception.diagnostics[0]["required_owner"])
        preloaded = XceliumAdapter.from_preloaded_environment(
            self.root, self.result, "JOB.XCELIUM.UVM.ENV",
            "XCELIUMENV.UVM.24_09", environment={
                "PATH": "{}{}{}".format(self.tools, os.pathsep, "/usr/bin"),
                "UVMHOME": "/approved/xcelium/uvm",
            }, xrun_path=self.xrun)
        self.assertEqual(
            "/approved/xcelium/uvm", preloaded._private_environment["UVMHOME"])
        with self.assertRaises(TrustedEdaBoundaryError):
            XceliumAdapter.from_preloaded_environment(
                self.root, self.result, "JOB.XCELIUM.MISSING",
                "XCELIUMENV.MISSING", environment={"PATH": "/usr/bin"})
        with self.assertRaises(TrustedEdaBoundaryError):
            XceliumAdapter.from_preloaded_environment(
                self.root, self.result, "JOB.XCELIUM.UNSAFE",
                "XCELIUMENV.UNSAFE", environment={"PATH": "/usr/bin"},
                allowed_environment_names=("PATH", "SECRET_TOKEN"))


if __name__ == "__main__":
    unittest.main()
