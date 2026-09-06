"""EDA-002 execution path with a dynamic deployment-owned UVM platform.

Only RTL and approved generated tests are bound.  ``UvmPlatform`` source is
supplied at execution time and never serialized.  Its *public capability*
identity/fingerprint is nevertheless bound, so a platform update that changes
the testcase boundary cannot silently alter an approved Job.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from adapters.eda import XceliumAdapter, XceliumRunConfiguration
from contracts.validator import accepted, validate
from domain.uvm_testcase import validate_generated_tests
from domain.uvm_context import RuntimeCapability
from infrastructure.persistence.atomic_artifact import publish_immutable_text


BINDING_PATH = "audit/eda002/uvm_testcase_binding.json"
RESULT_DIRECTORY = "audit/eda002/executions"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if not isinstance(relative, str) or pure.is_absolute() or ".." in pure.parts:
        raise ValueError("artifact path is unsafe")
    path = root / pure
    if not path.is_file() or path.is_symlink():
        raise ValueError("artifact is missing or unsafe")
    path.resolve().relative_to(root.resolve())
    return path


def _write(path: Path, value: Mapping[str, Any]) -> None:
    publish_immutable_text(
        path, json.dumps(dict(value), sort_keys=True, indent=2) + "\n",
        lambda message: ValueError(message), "immutable EDA-002 artifact conflicts")


@dataclass(frozen=True)
class UvmPlatform:
    """Ephemeral deployment input; never place this in a Project artifact."""
    sources: tuple[str, ...]
    testbench_top: str
    capability: RuntimeCapability
    include_dirs: tuple[str, ...] = ()
    defines: tuple[str, ...] = ()


def build_uvm_testcase_binding(*, workspace_root: Path, job_root: Path,
                               manifest: Mapping[str, Any],
                               approval_path: str,
                               capability: RuntimeCapability,
                               generated_source_path: str = "approved/generated/uvm/generated_tests.sv",
                               generated_manifest_path: str = "approved/generated/uvm/generated_tests_manifest.json") -> dict[str, Any]:
    """Publish the immutable Job binding while excluding all UVM platform data."""
    source = _safe(job_root, generated_source_path)
    manifest_file = _safe(job_root, generated_manifest_path)
    approval = _safe(job_root, approval_path)
    generated_manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    validate_generated_tests(
        source.read_text(encoding="utf-8"), generated_manifest,
        capability, ValueError)
    rtl = []
    for item in manifest["rtl"]["sources"]:
        path = str(item["baseline_path"])
        file = _safe(workspace_root, path)
        rtl.append({"path": path, "byte_fingerprint": _sha(file)})
    binding = {
        "schema_version": "1.0", "artifact_kind": "UVM_TESTCASE_BINDING",
        "binding_id": "BINDING.UVM.{}".format(hashlib.sha256(_canonical({
            "job_id": manifest["job_id"], "source": _sha(source),
            "manifest": _sha(manifest_file)})).hexdigest()[:16].upper()),
        "job_id": manifest["job_id"], "rtl": rtl,
        "dut": {"top": manifest["rtl"]["top"], "parameters": copy.deepcopy(manifest["rtl"]["parameters"])},
        "generated_tests": {"source_path": generated_source_path,
                            "source_fingerprint": _sha(source),
                            "manifest_path": generated_manifest_path,
                            "manifest_fingerprint": _sha(manifest_file)},
        "approval": {"path": approval_path, "fingerprint": _sha(approval)},
        "capability": {"id": capability.capability_id,
                       "aggregate_fingerprint": capability.aggregate_fingerprint},
        "binding_fingerprint": "0" * 64,
    }
    binding["binding_fingerprint"] = hashlib.sha256(_canonical({
        key: value for key, value in binding.items() if key != "binding_fingerprint"})).hexdigest()
    if not accepted(validate("uvm_testcase_binding", binding)):
        raise ValueError("UVM testcase binding contract is invalid")
    _write(job_root / BINDING_PATH, binding)
    return binding


class UvmTestcaseExecutor:
    """Build once, then run every manifest entry against the current platform."""
    def __init__(self, workspace_root: Path, job_root: Path, adapter: XceliumAdapter):
        self.workspace_root, self.job_root, self.adapter = Path(workspace_root), Path(job_root), adapter

    def execute(self, binding: Mapping[str, Any], platform: UvmPlatform) -> dict[str, Any]:
        if not accepted(validate("uvm_testcase_binding", dict(binding))):
            raise ValueError("UVM testcase binding contract is invalid")
        generated = binding["generated_tests"]
        source = _safe(self.job_root, generated["source_path"])
        manifest_file = _safe(self.job_root, generated["manifest_path"])
        if _sha(source) != generated["source_fingerprint"] or _sha(manifest_file) != generated["manifest_fingerprint"]:
            raise ValueError("approved generated testcase bytes changed")
        uvm_manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        if (binding.get("capability") != {
                "id": platform.capability.capability_id,
                "aggregate_fingerprint": platform.capability.aggregate_fingerprint}):
            raise ValueError("runtime capability contract drifted")
        validate_generated_tests(source.read_text(encoding="utf-8"), uvm_manifest,
                                 platform.capability, ValueError)
        if not platform.sources or not platform.testbench_top:
            raise ValueError("deployment UVM platform is unavailable")
        # Platform source paths are intentionally used only in memory.  They
        # are not copied into binding/result/replay documents.
        sources = tuple(platform.sources) + tuple(item["path"] for item in binding["rtl"]) + (str((self.job_root / generated["source_path"]).relative_to(self.workspace_root)),)
        token = hashlib.sha256(_canonical({"binding": binding["binding_fingerprint"], "environment": self.adapter.environment_fingerprint})).hexdigest()[:16].upper()
        execution_id = "EDA002.{}".format(token)
        base = XceliumRunConfiguration(sources=sources, top=platform.testbench_top,
            parameters=tuple(sorted(binding["dut"]["parameters"].items())), include_dirs=platform.include_dirs,
            defines=platform.defines, uvm=True, waves=False, coverage=False)
        build = self.adapter.build_only(execution_id + ".BUILD", base)
        rows = []
        blocked = build.evidence["execution_status"] == "BLOCKED_TOOL"
        for testcase in uvm_manifest["testcases"]:
            if build.evidence["execution_status"] != "PASS":
                rows.append({"testcase_id": testcase["testcase_id"], "uvm_class": testcase["uvm_class"], "seed": testcase["seed"], "status": "BLOCKED" if blocked else "NOT_RUN", "exit_code": None, "pass_marker_observed": None, "uvm_error_count": None, "uvm_fatal_count": None, "logs": []})
                continue
            run = self.adapter.run(execution_id + "." + testcase["testcase_id"].replace("TC.", ""), XceliumRunConfiguration(
                **{**base.__dict__, "plusargs": ("+DV_TESTCASE_ID={}".format(testcase["testcase_id"]),),
                   "uvm_test": testcase["uvm_testname"], "seed": testcase["seed"],
                   "timeout_seconds": testcase["timeout_seconds"], "pass_marker": testcase["pass_marker"]}))
            evidence = run.evidence
            status = "PASS" if (evidence["execution_status"] == "PASS" and
                                evidence["exit_code"] == 0 and
                                evidence["uvm_error_count"] == 0 and
                                evidence["uvm_fatal_count"] == 0 and
                                evidence["pass_marker_found"] is True) else (
                "BLOCKED" if evidence["execution_status"] == "BLOCKED_TOOL" else "FAIL")
            rows.append({"testcase_id": testcase["testcase_id"], "uvm_class": testcase["uvm_class"], "seed": testcase["seed"], "status": status, "exit_code": evidence["exit_code"], "pass_marker_observed": evidence["pass_marker_found"], "uvm_error_count": evidence["uvm_error_count"], "uvm_fatal_count": evidence["uvm_fatal_count"], "logs": [item["relative_path"] for item in evidence["logs"]]})
        final_status = "BLOCKED" if any(row["status"] == "BLOCKED" for row in rows) else ("FAIL" if any(row["status"] != "PASS" for row in rows) else "PASS")
        final = {"schema_version": "1.0", "artifact_kind": "UVM_TESTCASE_FINAL_RESULT", "execution_id": execution_id,
                 "job_id": binding["job_id"], "binding_fingerprint": binding["binding_fingerprint"],
                 "environment_identity": self.adapter.environment_identity, "environment_fingerprint": self.adapter.environment_fingerprint,
                 "status": final_status, "testcases": rows}
        if not accepted(validate("uvm_testcase_final_result", final)):
            raise ValueError("UVM testcase result contract is invalid")
        _write(self.job_root / RESULT_DIRECTORY / (execution_id.casefold() + ".json"), final)
        return final


__all__ = ["BINDING_PATH", "RESULT_DIRECTORY", "UvmPlatform", "UvmTestcaseExecutor", "build_uvm_testcase_binding"]
