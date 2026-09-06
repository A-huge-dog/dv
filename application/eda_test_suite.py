"""EDA-001's single, binding-first Xcelium test-suite tool.

This is deliberately a small application use case.  It does not know about
Provider, Reviewer, approval, or Project-loop checkpoints.  The only authority
it accepts is an immutable binding containing both RTL and testcase bytes.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from time import monotonic
from typing import Any, Mapping, Sequence

from adapters.eda import (
    TrustedEdaBoundaryError, XceliumAdapter, XceliumRunConfiguration,
)
from contracts.validator import accepted, validate
from infrastructure.persistence.atomic_artifact import publish_immutable_text


EDA001_VERSION = "EDA-001.0"
BINDING_DIRECTORY = "audit/eda001/bindings"
QUALIFICATION_DIRECTORY = "audit/eda001/qualification"
EXECUTION_DIRECTORY = "audit/eda001/executions"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_TESTCASE_ID = re.compile(r"^[A-Z][A-Z0-9_.-]{0,127}$")
_PASS_MARKER = re.compile(r"^DV_[A-Z0-9_]+_PASS$")
_SAFE_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_SAFE_DEFINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:=[A-Za-z0-9_./:+@=-]+)?$")


def _error(code: str, message: str, path: str = "") -> TrustedEdaBoundaryError:
    return TrustedEdaBoundaryError(message, [{
        "schema_version": "1.1", "code": code, "severity": "ERROR",
        "message": message, "path": path, "source_id": "",
        "required_owner": "DV_OWNER", "required_artifact_kind": "EDA-001",
        "required_decision_kind": "NONE",
    }])


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (not isinstance(value, str) or not _SAFE_PATH.fullmatch(value) or
            path.is_absolute() or ".." in path.parts):
        raise _error("INVALID_SCHEMA", "binding path is unsafe", "sources.path")
    return path


def _regular(root: Path, relative: str) -> Path:
    candidate = root / _relative(relative)
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as caught:
        raise _error("TOOL_PERMISSION_DENIED", "binding source escapes workspace",
                     relative) from caught
    if not candidate.is_file() or candidate.is_symlink():
        raise _error("BLOCKED_INPUT", "binding source is missing or unsafe", relative)
    return resolved


def _immutable_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = json.dumps(dict(value), sort_keys=True, indent=2,
                         ensure_ascii=False) + "\n"
    publish_immutable_text(
        path, encoded,
        lambda message: _error("CONFLICTING_REPLAY", message, str(path)),
        "immutable EDA-001 artifact conflicts with existing bytes")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as caught:
        raise _error("PARTIAL_ARTIFACT", "EDA-001 artifact is unavailable", str(path)) from caught
    if not isinstance(value, dict):
        raise _error("INVALID_SCHEMA", "EDA-001 artifact must be an object", str(path))
    return value


def _require_contract(kind: str, value: Mapping[str, Any]) -> None:
    if not accepted(validate(kind, dict(value))):
        raise _error("INVALID_SCHEMA", "{} contract is invalid".format(kind))


def _binding_digest(workspace_root: Path, binding: Mapping[str, Any]) -> str:
    """Hash canonical metadata and the exact source bytes as one value.

    Individual source hashes are intentionally neither persisted nor returned.
    Length delimiters make the byte stream unambiguous while allowing files to
    be read in chunks rather than retained in memory.
    """
    projected = copy.deepcopy(dict(binding))
    projected.pop("binding_fingerprint", None)
    digest = hashlib.sha256()
    digest.update(b"EDA-001 binding\\0")
    digest.update(_canonical(projected))
    for source in projected["sources"]:
        digest.update(b"\\0source\\0")
        digest.update(source["path"].encode("utf-8"))
        digest.update(b"\\0" + source["role"].encode("ascii") + b"\\0")
        with _regular(workspace_root, source["path"]).open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def _binding_path(job_root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if (path.is_absolute() or ".." in path.parts or
            not relative.startswith(BINDING_DIRECTORY + "/") or
            path.suffix != ".json"):
        raise _error("TOOL_PERMISSION_DENIED", "binding path is outside EDA-001", relative)
    result = (job_root / path).resolve()
    try:
        result.relative_to((job_root / BINDING_DIRECTORY).resolve())
    except ValueError as caught:
        raise _error("TOOL_PERMISSION_DENIED", "binding path escapes Job", relative) from caught
    return result


def _validate_binding(binding: Mapping[str, Any], workspace_root: Path,
                      job_id: str | None = None) -> None:
    required = {"schema_version", "artifact_kind", "binding_id", "job_id", "sources",
                "dut", "testbench", "parameters", "include_dirs", "defines",
                "testcases", "binding_fingerprint"}
    if (set(binding) != required or binding.get("schema_version") != "1.0" or
            binding.get("artifact_kind") != "EDA_TEST_SUITE_BINDING" or
            not isinstance(binding.get("binding_id"), str) or
            not binding["binding_id"].startswith("BINDING.EDA.")):
        raise _error("INVALID_SCHEMA", "EDA test-suite binding is invalid")
    if job_id is not None and binding.get("job_id") != job_id:
        raise _error("STALE_EVIDENCE", "binding belongs to another Job", "job_id")
    sources = binding.get("sources")
    if not isinstance(sources, list) or not 2 <= len(sources) <= 256:
        raise _error("INVALID_SCHEMA", "binding needs RTL and testcase sources", "sources")
    paths: set[str] = set()
    roles: set[str] = set()
    for source in sources:
        if (not isinstance(source, dict) or set(source) != {"path", "role"} or
                source["role"] not in {"RTL", "TESTBENCH"} or
                source["path"] in paths):
            raise _error("INVALID_SCHEMA", "binding source declaration is invalid", "sources")
        _regular(workspace_root, source["path"])
        paths.add(source["path"])
        roles.add(source["role"])
    if roles != {"RTL", "TESTBENCH"}:
        raise _error("INVALID_SCHEMA", "binding needs at least one RTL and testbench", "sources")
    for field in ("dut", "testbench"):
        value = binding.get(field)
        if not isinstance(value, dict) or set(value) != {"top"} or not _IDENTIFIER.fullmatch(value["top"]):
            raise _error("INVALID_SCHEMA", "binding top is invalid", field)
    params = binding.get("parameters")
    if not isinstance(params, dict) or any(not _IDENTIFIER.fullmatch(name) or
                                           (type(value) is not int and type(value) is not bool)
                                           for name, value in params.items()):
        raise _error("INVALID_SCHEMA", "binding parameters are invalid", "parameters")
    includes = binding.get("include_dirs")
    if (not isinstance(includes, list) or len(includes) != len(set(includes)) or
            any(not isinstance(item, str) for item in includes)):
        raise _error("INVALID_SCHEMA", "binding include directories are invalid", "include_dirs")
    for item in includes:
        directory = workspace_root / _relative(item)
        if not directory.is_dir() or directory.is_symlink():
            raise _error("BLOCKED_INPUT", "binding include directory is unavailable", item)
    defines = binding.get("defines")
    if (not isinstance(defines, list) or len(defines) != len(set(defines)) or
            any(not isinstance(item, str) or not _SAFE_DEFINE.fullmatch(item) for item in defines)):
        raise _error("INVALID_SCHEMA", "binding defines are invalid", "defines")
    tests = binding.get("testcases")
    if not isinstance(tests, list) or not tests:
        raise _error("INVALID_SCHEMA", "binding testcase suite is empty", "testcases")
    ids: set[str] = set()
    testbench_paths = {s["path"] for s in sources if s["role"] == "TESTBENCH"}
    expected_fields = {"id", "source_paths", "selected_test", "seed", "timeout_seconds", "pass_marker", "uvm"}
    for testcase in tests:
        if (not isinstance(testcase, dict) or set(testcase) != expected_fields or
                not isinstance(testcase["id"], str) or not _TESTCASE_ID.fullmatch(testcase["id"]) or
                testcase["id"] in ids or not _IDENTIFIER.fullmatch(testcase["selected_test"]) or
                type(testcase["seed"]) is not int or not 1 <= testcase["seed"] <= 2147483647 or
                type(testcase["timeout_seconds"]) is not int or not 1 <= testcase["timeout_seconds"] <= 3600 or
                not isinstance(testcase["uvm"], bool) or
                not isinstance(testcase["pass_marker"], str) or
                not _PASS_MARKER.fullmatch(testcase["pass_marker"]) or
                not isinstance(testcase["source_paths"], list) or not testcase["source_paths"] or
                len(testcase["source_paths"]) != len(set(testcase["source_paths"])) or
                not set(testcase["source_paths"]).issubset(testbench_paths)):
            raise _error("INVALID_SCHEMA", "testcase declaration is invalid", "testcases")
        ids.add(testcase["id"])
    if binding.get("binding_fingerprint") != _binding_digest(workspace_root, binding):
        raise _error("STALE_EVIDENCE", "binding fingerprint does not match exact bytes", "binding_fingerprint")
    _require_contract("eda_test_suite_binding", binding)


def build_eda_test_suite_binding(
        *, job_root: Path, workspace_root: Path, job_id: str,
        binding_id: str, sources: Sequence[Mapping[str, str]], dut_top: str,
        testbench_top: str, testcases: Sequence[Mapping[str, Any]],
        parameters: Mapping[str, int | bool] | None = None,
        include_dirs: Sequence[str] = (), defines: Sequence[str] = ()
        ) -> tuple[str, dict[str, Any]]:
    """Create one immutable binding.  No per-file fingerprint is published."""
    binding = {
        "schema_version": "1.0", "artifact_kind": "EDA_TEST_SUITE_BINDING",
        "binding_id": binding_id, "job_id": job_id,
        "sources": [dict(item) for item in sources],
        "dut": {"top": dut_top}, "testbench": {"top": testbench_top},
        "parameters": dict(parameters or {}), "include_dirs": list(include_dirs),
        "defines": list(defines), "testcases": [dict(item) for item in testcases],
        "binding_fingerprint": "0" * 64,
    }
    # Validate all declarations before taking the immutable byte snapshot.
    probe = copy.deepcopy(binding)
    probe["binding_fingerprint"] = _binding_digest(Path(workspace_root), probe)
    _validate_binding(probe, Path(workspace_root), job_id)
    binding["binding_fingerprint"] = probe["binding_fingerprint"]
    relative = "{}/{}.json".format(BINDING_DIRECTORY, binding_id.casefold())
    path = _binding_path(Path(job_root), relative)
    _immutable_json(path, binding)
    return relative, copy.deepcopy(binding)


@dataclass(frozen=True)
class EdaTestSuiteResult:
    final_result: dict[str, Any]
    final_result_path: str
    artifact_index_path: str
    replayed: bool


class EdaTestSuiteTool:
    """The only EDA-001 tool: binding -> qualification -> Xcelium suite."""

    def __init__(self, workspace_root: Path, result_root: Path, job_id: str,
                 adapter: XceliumAdapter):
        self.workspace_root = Path(workspace_root).resolve()
        self.result_root = Path(result_root).resolve()
        self.job_id = job_id
        self.job_root = (self.result_root / "jobs" / job_id).resolve()
        self.adapter = adapter
        if adapter.job_id != job_id:
            raise _error("TOOL_PERMISSION_DENIED", "adapter belongs to another Job")

    @classmethod
    def from_preloaded_environment(
            cls, workspace_root: Path, result_root: Path, job_id: str,
            environment_identity: str, *, timeout_seconds: int = 120,
            environment: Mapping[str, str] | None = None,
            xrun_path: Path | None = None) -> "EdaTestSuiteTool":
        """Resolve only an already-loaded Xcelium deployment environment.

        In particular this never evaluates ``module load`` or a setup-script
        string; executable discovery is a direct PATH lookup performed by the
        adapter.
        """
        adapter = XceliumAdapter.from_preloaded_environment(
            workspace_root, result_root, job_id, environment_identity,
            timeout_seconds=timeout_seconds, environment=environment,
            xrun_path=xrun_path)
        return cls(workspace_root, result_root, job_id, adapter)

    def _qualification(self) -> dict[str, Any]:
        """Qualify the already-loaded executable without exposing private env."""
        token = self.adapter.environment_identity.casefold()
        relative = "{}/{}.json".format(QUALIFICATION_DIRECTORY, token)
        path = self.job_root / relative
        if path.exists():
            value = _read_json(path)
            if (value.get("artifact_kind") != "EDA_ENVIRONMENT_QUALIFICATION" or
                    value.get("environment_identity") != self.adapter.environment_identity):
                raise _error("STALE_EVIDENCE", "environment qualification is stale", relative)
            return value
        version = self.adapter.probe_version("EDA001.QUALIFY")
        evidence = version.evidence
        codes = evidence["diagnostic_codes"]
        if "LICENSE_UNAVAILABLE" in codes:
            state = "LICENSE_UNAVAILABLE"
        elif "RUNTIME_DEPENDENCY_MISSING" in codes:
            state = "RUNTIME_DEPENDENCY_MISSING"
        elif "VERSION_MISMATCH" in codes:
            state = "VERSION_MISMATCH"
        elif evidence["execution_status"] == "BLOCKED_TOOL":
            state = "BLOCKED_TOOL"
        elif evidence["execution_status"] == "PASS":
            state = "READY"
        else:
            state = "BLOCKED_TOOL"
        value = {
            "schema_version": "1.0", "artifact_kind": "EDA_ENVIRONMENT_QUALIFICATION",
            "environment_identity": self.adapter.environment_identity,
            "status": state, "simulator_version": evidence["simulator_version"],
            "diagnostic_codes": codes,
        }
        _require_contract("eda_environment_qualification", value)
        _immutable_json(path, value)
        return value

    @staticmethod
    def _adapter_error_report(testcase: Mapping[str, Any], evidence: Mapping[str, Any],
                              phase: str, job_root: Path) -> dict[str, Any]:
        log = next((item["relative_path"] for item in evidence.get("logs", [])
                    if item["kind"] == "XRUN_LOG"), None)
        excerpt = ""
        if log is not None:
            try:
                excerpt = (job_root / log).read_text(
                    encoding="utf-8", errors="replace")[:4096]
            except OSError:
                excerpt = ""
        location = re.search(
            r"((?:WORKSPACE::)?[A-Za-z0-9_./-]+\\.(?:sv|svh|v))[:(]([0-9]+)",
            excerpt)
        return {
            "testcase_id": testcase["id"], "phase": phase,
            "native_diagnostic_codes": list(evidence.get("diagnostic_codes", [])),
            "native_message": excerpt.splitlines()[-1] if excerpt.splitlines() else "",
            "source": (None if location is None else {
                "path": location.group(1), "line": int(location.group(2))}),
            "log_excerpt": excerpt,
            "raw_log_reference": log,
        }

    @staticmethod
    def _artifact_index(job_root: Path, paths: Sequence[tuple[str, str]]) -> dict[str, Any]:
        entries = []
        for kind, relative in sorted(set(paths), key=lambda item: item[1]):
            path = job_root / relative
            if not path.is_file() or path.is_symlink():
                raise _error("STALE_EVIDENCE", "registered EDA artifact is missing", relative)
            # This is an integrity digest, not another binding/lineage value.
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            entries.append({"kind": kind, "relative_path": relative,
                            "size_bytes": path.stat().st_size,
                            "integrity_digest": digest})
        value = {"schema_version": "1.0", "artifact_kind": "EDA_ARTIFACT_INDEX",
                 "artifacts": entries}
        _require_contract("eda_artifact_index", value)
        return value

    def _verify_replay(self, request: Mapping[str, Any], final: Mapping[str, Any],
                       index: Mapping[str, Any], binding: Mapping[str, Any]) -> None:
        if (request.get("binding_fingerprint") != binding["binding_fingerprint"] or
                final.get("binding_fingerprint") != binding["binding_fingerprint"] or
                final.get("execution_id") != request.get("execution_id") or
                index.get("artifact_kind") != "EDA_ARTIFACT_INDEX"):
            raise _error("STALE_EVIDENCE", "EDA suite replay lineage is stale")
        for item in index.get("artifacts", []):
            if set(item) != {"kind", "relative_path", "size_bytes", "integrity_digest"}:
                raise _error("STALE_EVIDENCE", "artifact index is malformed")
            path = self.job_root / item["relative_path"]
            if (not path.is_file() or path.is_symlink() or
                    path.stat().st_size != item["size_bytes"] or
                    hashlib.sha256(path.read_bytes()).hexdigest() != item["integrity_digest"]):
                raise _error("STALE_EVIDENCE", "EDA replay artifact bytes changed", item["relative_path"])

    def execute_eda_test_suite(self, binding_path: str, binding_fingerprint: str,
                               coverage: bool = False) -> EdaTestSuiteResult:
        """Execute exactly the bound RTL + explicit testcase suite.

        The public request intentionally has only a binding path, the one
        binding fingerprint, and coverage selection.  Waves are not an input.
        """
        if not isinstance(coverage, bool) or not re.fullmatch(r"[0-9a-f]{64}", binding_fingerprint):
            raise _error("INVALID_SCHEMA", "EDA tool request is invalid")
        path = _binding_path(self.job_root, binding_path)
        binding = _read_json(path)
        _validate_binding(binding, self.workspace_root, self.job_id)
        if binding_fingerprint != binding["binding_fingerprint"]:
            raise _error("STALE_EVIDENCE", "binding fingerprint mismatch", "binding_fingerprint")
        # Recompute directly before every execution/replay to catch source or metadata drift.
        if _binding_digest(self.workspace_root, binding) != binding_fingerprint:
            raise _error("STALE_EVIDENCE", "binding bytes changed", "binding_fingerprint")
        token = binding_fingerprint[:16].upper()
        execution_id = "EDA001.{}".format(token)
        base = "{}/{}".format(EXECUTION_DIRECTORY, execution_id.casefold())
        request_relative = base + ".request.json"
        final_relative = base + ".final_result.json"
        index_relative = base + ".artifact_index.json"
        request_path = self.job_root / request_relative
        final_path = self.job_root / final_relative
        index_path = self.job_root / index_relative
        request = {"schema_version": "1.0", "artifact_kind": "EDA_TEST_SUITE_REQUEST",
                   "execution_id": execution_id, "job_id": self.job_id,
                   "binding_path": binding_path, "binding_fingerprint": binding_fingerprint,
                   "coverage": coverage}
        _require_contract("eda_test_suite_request", request)
        if request_path.exists() or final_path.exists() or index_path.exists():
            if not all(item.is_file() for item in (request_path, final_path, index_path)):
                raise _error("PARTIAL_ARTIFACT", "EDA suite replay artifacts are incomplete")
            if _read_json(request_path) != request:
                raise _error("CONFLICTING_REPLAY", "execution ID has a different request")
            final = _read_json(final_path)
            index = _read_json(index_path)
            self._verify_replay(request, final, index, binding)
            return EdaTestSuiteResult(final, final_relative, index_relative, True)
        qualification = self._qualification()
        if qualification["status"] != "READY":
            raise _error(qualification["status"], "Xcelium environment is not ready")
        _immutable_json(request_path, request)
        all_sources = tuple(item["path"] for item in binding["sources"])
        base_config = {
            "sources": all_sources, "top": binding["testbench"]["top"],
            "parameters": tuple(sorted(binding["parameters"].items())),
            "include_dirs": tuple(binding["include_dirs"]), "defines": tuple(binding["defines"]),
            "waves": False,
        }
        build = self.adapter.build_only(
            "{}.BUILD".format(execution_id), XceliumRunConfiguration(
                **base_config,
                uvm=any(testcase["uvm"] for testcase in binding["testcases"])))
        build_evidence = build.evidence
        testcase_results: list[dict[str, Any]] = []
        errors: list[tuple[str, str]] = []
        coverage_directories: list[str] = []
        registered: list[tuple[str, str]] = [("FINAL_RESULT", final_relative)]
        for item in build_evidence.get("logs", []):
            registered.append(("LOG", item["relative_path"]))
        if build_evidence["execution_status"] != "PASS":
            for testcase in binding["testcases"]:
                testcase_results.append({"id": testcase["id"], "seed": testcase["seed"],
                    "execution_status": "NOT_RUN", "exit_code": None,
                    "pass_marker_observed": None, "duration_seconds": 0.0})
                report = self._adapter_error_report(testcase, build_evidence, "BUILD", self.job_root)
                relative = base + ".errors.{}.json".format(testcase["id"].casefold())
                _immutable_json(self.job_root / relative, report)
                errors.append(("ERROR_REPORT", relative))
        else:
            for testcase in binding["testcases"]:
                started = monotonic()
                config = XceliumRunConfiguration(
                    **base_config, plusargs=("+DV_SELECTED_TEST={}".format(testcase["selected_test"]),),
                    uvm=testcase["uvm"], uvm_test=(testcase["selected_test"] if testcase["uvm"] else None),
                    seed=testcase["seed"], coverage=coverage,
                    pass_marker=testcase["pass_marker"],
                    timeout_seconds=testcase["timeout_seconds"])
                run = self.adapter.run("{}.{}".format(execution_id, testcase["id"]), config)
                evidence = run.evidence
                duration = round(monotonic() - started, 6)
                testcase_results.append({"id": testcase["id"], "seed": testcase["seed"],
                    "execution_status": evidence["execution_status"], "exit_code": evidence["exit_code"],
                    "pass_marker_observed": evidence["pass_marker_found"], "duration_seconds": duration})
                for log in evidence.get("logs", []):
                    registered.append(("LOG", log["relative_path"]))
                if evidence["execution_status"] == "PASS" and coverage:
                    coverage_directories.extend(
                        item["relative_path"] for item in evidence.get("artifacts", [])
                        if item["kind"] == "COVERAGE_DATABASE")
                if evidence["execution_status"] != "PASS":
                    report = self._adapter_error_report(testcase, evidence, "SIMULATION", self.job_root)
                    relative = base + ".errors.{}.json".format(testcase["id"].casefold())
                    _immutable_json(self.job_root / relative, report)
                    errors.append(("ERROR_REPORT", relative))
        passed = sum(item["execution_status"] == "PASS" for item in testcase_results)
        not_run = sum(item["execution_status"] == "NOT_RUN" for item in testcase_results)
        # The compact public summary has only pass/fail/not-run buckets;
        # a tool-blocked simulation is consequently a failed testcase, while
        # preserving its precise BLOCKED_TOOL status in the per-test result.
        failed = len(testcase_results) - passed - not_run
        coverage_result = {"status": "DISABLED", "report_paths": [],
                           "categories": {name: "unavailable" for name in (
                               "statement", "branch", "toggle", "expression", "fsm",
                               "assertion", "functional")}}
        if coverage:
            coverage_result = (
                self.adapter.merge_coverage(execution_id, coverage_directories)
                if coverage_directories else
                {**coverage_result, "status": "UNAVAILABLE"})
            registered.extend(("COVERAGE_REPORT", item)
                              for item in coverage_result["report_paths"])
        final = {"schema_version": "1.0", "artifact_kind": "EDA_TEST_SUITE_FINAL_RESULT",
                 "execution_id": execution_id, "job_id": self.job_id,
                 "binding_fingerprint": binding_fingerprint,
                 "environment_identity": self.adapter.environment_identity,
                 "all_testcases_passed": passed == len(testcase_results) and not failed and not not_run,
                 "testcase_total": len(testcase_results), "passed": passed,
                 "failed": failed, "not_run": not_run, "testcases": testcase_results,
                 "coverage": {"status": coverage_result["status"],
                              "included_testcases": [item["id"] for item in testcase_results if item["execution_status"] == "PASS"],
                              "categories": coverage_result["categories"],
                              "native_report_paths": coverage_result["report_paths"]}}
        _require_contract("eda_test_suite_final_result", final)
        _immutable_json(final_path, final)
        registered.extend(errors)
        # The index itself is registered only after its own bytes are final; it
        # therefore indexes result/log/error artifacts, never waveform files.
        index = self._artifact_index(self.job_root, registered)
        _immutable_json(index_path, index)
        return EdaTestSuiteResult(copy.deepcopy(final), final_relative, index_relative, False)


def execute_eda_test_suite(tool: EdaTestSuiteTool, binding_path: str,
                           binding_fingerprint: str, coverage: bool = False) -> EdaTestSuiteResult:
    """Named Tool entry point used by integration code and tests."""
    return tool.execute_eda_test_suite(binding_path, binding_fingerprint, coverage)


__all__ = ["BINDING_DIRECTORY", "EDA001_VERSION", "EdaTestSuiteResult",
           "EdaTestSuiteTool", "build_eda_test_suite_binding",
           "execute_eda_test_suite"]
