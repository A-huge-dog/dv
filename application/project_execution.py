"""Freeze reviewed Project inputs and execute the implemented Xcelium suite."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from contracts.validator import accepted, load_document, validate
from domain.artifacts import artifact_fingerprint
from domain.uvm_testcase import validate_generated_tests
from application.uvm_generation import UvmGenerationHandler
from infrastructure.persistence.atomic_artifact import publish_immutable_bytes, publish_immutable_text
from scripts.dvlib import canonical_hash

PJ003_WORKFLOW_VERSION = "PJ-003.2"
EXECUTION_INPUT_PATH = "audit/pj003_execution_input.json"
EXECUTION_BUNDLE_PATH = "audit/pj003_execution_bundle.json"
EXECUTION_REQUEST_PATH = "audit/pj003_execution_request.json"
EXECUTION_EVIDENCE_PATH = "audit/pj003_execution_evidence.json"
EXECUTION_RESULT_PATH = "audit/pj003_execution_result.json"
REVIEW_COMPLETE_PATH = "audit/project_review_complete.json"
LEGACY_REVIEW_PATH = "audit/oches001_human_review_checkpoint.json"

def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _human_checkpoint_fingerprint(value: Mapping[str, Any]) -> str:
    projected = copy.deepcopy(dict(value))
    projected.pop("checkpoint_fingerprint", None)
    bundle = projected.get("bundle_fingerprints")
    if isinstance(bundle, dict) and "checkpoint" in bundle:
        bundle["checkpoint"] = "0" * 64
    return canonical_hash(projected)


def _safe_relative(relative: str) -> PurePosixPath:
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or ".." in path.parts or \
            any(part in {"", "."} or part.startswith(".") for part in path.parts):
        raise ValueError("artifact path is unsafe")
    return path


def _regular(job_root: Path, relative: str) -> Path:
    pure = _safe_relative(relative)
    path = job_root / pure
    if not path.is_file() or path.is_symlink():
        raise ValueError("artifact is missing or unsafe")
    try:
        path.resolve().relative_to(job_root.resolve())
    except ValueError as caught:
        raise ValueError("artifact escapes the current Job") from caught
    return path


def _immutable_json(path: Path, value: Mapping[str, Any], error) -> None:
    encoded = json.dumps(
        dict(value), sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    publish_immutable_text(
        path, encoded,
        lambda message: error("CONFLICTING_REPLAY", message),
        "immutable PJ-003 artifact conflicts with existing bytes")


def _immutable_bytes(path: Path, value: bytes, error) -> None:
    publish_immutable_bytes(
        path, value,
        lambda message: error("CONFLICTING_REPLAY", message),
        "immutable PJ-003 artifact conflicts with existing bytes")


def _require_schema(kind: str, value: Mapping[str, Any], error) -> None:
    if not accepted(validate(kind, dict(value))):
        raise error("INVALID_SCHEMA", "{} contract is invalid".format(kind))


def _checkpoint(
        state: str, manifest: Mapping[str, Any], authority_path: str,
        authority_fingerprint: str) -> dict[str, Any]:
    value = {
        "schema_version": "1.0",
        "workflow_version": PJ003_WORKFLOW_VERSION,
        "state": state,
        "job_id": manifest["job_id"],
        "input_fingerprint": manifest["input_fingerprint"],
        "authority_path": authority_path,
        "authority_fingerprint": authority_fingerprint,
        "checkpoint_id": "CHECKPOINT.PROJECT.PJ003.{}".format(
            canonical_hash({
                "state": state,
                "job_id": manifest["job_id"],
                "authority": authority_fingerprint,
            })[:16].upper()),
        "checkpoint_fingerprint": "0" * 64,
    }
    value["checkpoint_fingerprint"] = artifact_fingerprint(
        value, "checkpoint_fingerprint")
    return value


def _load_fingerprinted(
        job_root: Path, relative: str, kind: str, fingerprint_field: str,
        error) -> dict[str, Any]:
    try:
        value = load_document(_regular(job_root, relative))
    except Exception as caught:
        raise error("PARTIAL_ARTIFACT", "{} is unavailable".format(kind)) \
            from caught
    _require_schema(kind, value, error)
    if value.get(fingerprint_field) != artifact_fingerprint(
            value, fingerprint_field):
        raise error("STALE_EVIDENCE", "{} fingerprint is stale".format(kind))
    return value


@dataclass(frozen=True)
class ProjectActionResult:
    checkpoint: dict[str, Any]
    output_references: tuple[str, ...]
    replayed: bool


@dataclass(frozen=True)
class PrepareExecutionInput:
    job_root: Path
    manifest: dict[str, Any]
    checkpoint: dict[str, Any]
    execution: dict[str, Any]


class PrepareExecutionHandler:
    """Snapshot a verified final review without inventing human authority."""
    def __init__(self, error: type[Exception]):
        self.error = error

    def handle(self, command: PrepareExecutionInput) -> ProjectActionResult:
        root, manifest, checkpoint, error = command.job_root, command.manifest, command.checkpoint, self.error
        source_path = (LEGACY_REVIEW_PATH if checkpoint.get("state") == "AWAITING_HUMAN_REVIEW"
                       else checkpoint["source_checkpoint_path"])
        if (load_document(_regular(root, source_path)) != checkpoint or
                checkpoint.get("checkpoint_fingerprint") != _human_checkpoint_fingerprint(checkpoint) or
                checkpoint.get("job_id") != manifest["job_id"] or
                checkpoint.get("input_fingerprint") != manifest["input_fingerprint"]):
            raise error("STALE_EVIDENCE", "review checkpoint does not bind this Job")
        bundle = checkpoint["bundle_fingerprints"]
        documents = {}
        references = []
        for key, field, fingerprint in (
                ("candidate_metadata_path", "candidate_fingerprint", "testcase"),
                ("review_report_path", "report_fingerprint", "review"),
                ("review_validation_path", "validation_fingerprint", "review_validation"),
                ("scenario_ac_map_path", "artifact_fingerprint", "scenario_ac_map"),
                ("ac_testcase_map_path", "artifact_fingerprint", "ac_testcase_map")):
            path = _regular(root, checkpoint[key])
            value = load_document(path)
            if value.get(field) != artifact_fingerprint(value, field) or value.get(field) != bundle[fingerprint]:
                raise error("STALE_EVIDENCE", "reviewed artifact fingerprint is stale")
            schema = {"candidate_metadata_path": "project_testcase_candidate",
                      "review_report_path": "project_testcase_review_report",
                      "review_validation_path": "project_testcase_review_validation",
                      "scenario_ac_map_path": "scenario_ac_map", "ac_testcase_map_path": "ac_testcase_map"}[key]
            _require_schema(schema, value, error)
            documents[key] = value
            references.append({"path": checkpoint[key], "byte_fingerprint": _sha256_bytes(path.read_bytes())})
        request_path = _regular(root, checkpoint["review_request_path"])
        request = load_document(request_path)
        if (request.get("request_fingerprint") != artifact_fingerprint(request, "request_fingerprint") or
                request.get("artifact_roots") != documents["review_report_path"]["artifact_roots"]):
            raise error("STALE_EVIDENCE", "review request is stale")
        references.extend({"path": path, "byte_fingerprint": _sha256_bytes(_regular(root, path).read_bytes())}
                          for path in (source_path, checkpoint["review_request_path"]))
        for relative in checkpoint["artifact_unit_index_paths"].values():
            index = load_document(_regular(root, relative))
            paths = [relative, *[child["path"] for child in index["children"]]]
            if index.get("metadata", {}).get("assembly_path"):
                paths.append(index["metadata"]["assembly_path"])
            references.extend({"path": path, "byte_fingerprint": _sha256_bytes(_regular(root, path).read_bytes())}
                              for path in paths)
        candidate = documents["candidate_metadata_path"]
        content = _regular(root, candidate["output_path"]).read_bytes()
        if _sha256_bytes(content) != candidate["content_fingerprint"]:
            raise error("STALE_EVIDENCE", "generated testcase source drifted")
        generated_path = "staging/generated/uvm/generated_tests_manifest.r{:03d}.json".format(candidate["revision"])
        generated = load_document(_regular(root, generated_path))
        _require_schema("generated_uvm_tests_manifest", generated, error)
        if (generated["manifest_fingerprint"] != artifact_fingerprint(generated, "manifest_fingerprint") or
                [row["testcase_id"] for row in generated["testcases"]] != candidate["implemented_testcase_ids"] or
                generated["skipped_testcases"] != candidate["skipped_testcases"]):
            raise error("STALE_EVIDENCE", "generated manifest does not bind the candidate")
        validate_generated_tests(content.decode("utf-8"), generated, error)
        capability = request["runtime_capability"]
        files = capability["uvm_context_files"]
        if (canonical_hash([{"logical_path": item["logical_path"], "fingerprint": item["fingerprint"]}
                            for item in files]) != bundle["effective_uvm"] or
                capability["aggregate_fingerprint"] != bundle["effective_uvm"]):
            raise error("STALE_EVIDENCE", "effective UVM root is stale")
        frozen = []
        for item in files:
            logical = _safe_relative(item["logical_path"]).as_posix()
            data = item["content"].encode("utf-8")
            if _sha256_bytes(data) != item["fingerprint"]:
                raise error("STALE_EVIDENCE", "effective UVM source drifted")
            relative = "execution/inputs/uvm/" + logical
            _immutable_bytes(root / relative, data, error)
            frozen.append({"logical_path": logical, "path": relative, "byte_fingerprint": item["fingerprint"]})
        testcase_path = "execution/inputs/generated_tests.sv"
        manifest_path = "execution/inputs/generated_tests_manifest.json"
        _immutable_bytes(root / testcase_path, content, error)
        _immutable_bytes(root / manifest_path, _regular(root, generated_path).read_bytes(), error)
        report = documents["review_report_path"]
        coverage = (not generated["skipped_testcases"] and
                    not request.get("scenario_spec_issues", {}).get("scenario_ids") and
                    all(row["status"] == "COVERED" for row in report["ac_reviews"]))
        value = {
            "schema_version": "1.0", "artifact_kind": "PROJECT_EXECUTION_INPUT",
            "job_id": manifest["job_id"], "input_fingerprint": manifest["input_fingerprint"],
            "source_checkpoint_path": source_path,
            "source_checkpoint_fingerprint": checkpoint["checkpoint_fingerprint"],
            "bundle_fingerprints": copy.deepcopy(bundle), "references": references,
            "candidate_id": candidate["candidate_id"],
            "testcase": {"path": testcase_path, "byte_fingerprint": _sha256_bytes(content),
                         "manifest_path": manifest_path,
                         "manifest_byte_fingerprint": _sha256_bytes(_regular(root, manifest_path).read_bytes())},
            "uvm": frozen, "testcases": generated["testcases"],
            "skipped_testcases": generated["skipped_testcases"],
            "findings": report["findings"], "ac_reviews": report["ac_reviews"],
            "full_spec_coverage_complete": coverage,
            "execution": copy.deepcopy(command.execution), "snapshot_fingerprint": "0" * 64,
        }
        value["snapshot_fingerprint"] = artifact_fingerprint(value, "snapshot_fingerprint")
        _require_schema("project_execution_input", value, error)
        _immutable_json(root / EXECUTION_INPUT_PATH, value, error)
        result = _checkpoint("READY_FOR_BINDING", manifest, EXECUTION_INPUT_PATH, value["snapshot_fingerprint"])
        _immutable_json(root / "audit/pj003_ready_for_binding.json", result, error)
        return ProjectActionResult(result, (EXECUTION_INPUT_PATH, "audit/pj003_ready_for_binding.json"), False)


def load_execution_input(root: Path, manifest: Mapping[str, Any], error) -> dict[str, Any]:
    value = _load_fingerprinted(root, EXECUTION_INPUT_PATH, "project_execution_input", "snapshot_fingerprint", error)
    if value["job_id"] != manifest["job_id"] or value["input_fingerprint"] != manifest["input_fingerprint"]:
        raise error("STALE_EVIDENCE", "execution input belongs to another Job")
    for item in [*value["references"], *value["uvm"], value["testcase"],
                 {"path": value["testcase"]["manifest_path"], "byte_fingerprint": value["testcase"]["manifest_byte_fingerprint"]}]:
        if _sha256_bytes(_regular(root, item["path"]).read_bytes()) != item["byte_fingerprint"]:
            raise error("STALE_EVIDENCE", "frozen execution input drifted")
    return value


@dataclass(frozen=True)
class BindExecutionBundleInput:
    job_root: Path
    workspace_root: Path
    manifest: dict[str, Any]
    checkpoint: dict[str, Any]


class BindExecutionBundleHandler:
    """Bind baseline RTL and build a deterministic compilation entry point."""
    def __init__(self, error: type[Exception]):
        self.error = error

    def handle(self, command: BindExecutionBundleInput) -> ProjectActionResult:
        root, error = command.job_root, self.error
        snapshot = load_execution_input(root, command.manifest, error)
        if command.checkpoint.get("authority_fingerprint") != snapshot["snapshot_fingerprint"]:
            raise error("STALE_EVIDENCE", "execution preparation is stale")
        sources = []
        for item in command.manifest["rtl"]["sources"]:
            path = item["baseline_path"]
            if _sha256_bytes(_regular(command.workspace_root, path).read_bytes()) != item["fingerprint"]:
                raise error("STALE_EVIDENCE", "baseline RTL drifted")
            sources.append({"path": path, "byte_fingerprint": item["fingerprint"]})
        prefix = root.relative_to(command.workspace_root).as_posix() + "/"
        units = UvmGenerationHandler._compilation_units({"effective_files": [
            {**item, "fingerprint": item["byte_fingerprint"]} for item in snapshot["uvm"]]})
        includes = set()
        packages, tops = [], []
        for item in snapshot["uvm"]:
            includes.add(str(PurePosixPath(prefix + item["path"]).parent))
            includes.add(prefix + "execution/inputs/uvm")
        for item in units:
            text = _regular(root, item["path"]).read_text()
            packages.extend(re.findall(r"\bpackage\s+(\w+)\s*;", text))
            if re.search(r"\brun_test\s*\(", text):
                tops.extend(re.findall(r"\bmodule\s+(\w+)", text))
            sources.append({"path": prefix + item["path"], "byte_fingerprint": item["fingerprint"]})
        if len(tops) != 1:
            raise error("BLOCKED_INPUT", "effective UVM must identify exactly one run_test top")
        # Classes are compiled inside one framework package, importing the selected UVM packages.
        # Framework subclasses emit the manifest marker at normal final-phase completion.
        wrapper = ["package dv_project_generated_pkg;", "import uvm_pkg::*;", '`include "uvm_macros.svh"']
        wrapper += ["import {}::*;".format(name) for name in packages if name != "uvm_pkg"]
        wrapper += ['`include "generated_tests.sv"']
        for testcase in snapshot["testcases"]:
            name = "dv_exec_" + testcase["uvm_class"]
            wrapper += [
                "class {} extends {};".format(name, testcase["uvm_class"]),
                "`uvm_component_utils({})".format(name),
                'function new(string name="{}", uvm_component parent=null); super.new(name,parent); endfunction'.format(name),
                "function void final_phase(uvm_phase phase);",
                "uvm_report_server rs; super.final_phase(phase); rs=uvm_report_server::get_server();",
                'if(rs.get_severity_count(UVM_ERROR)==0 && rs.get_severity_count(UVM_FATAL)==0) $display("{}");'.format(testcase["pass_marker"]),
                "endfunction", "endclass"]
        wrapper += ["endpackage", "module dv_project_execution_top;",
                    "import dv_project_generated_pkg::*;",
                    "{} platform();".format(tops[0]), "endmodule", ""]
        wrapper_path = "execution/inputs/project_tests_pkg.sv"
        _immutable_bytes(root / wrapper_path, "\n".join(wrapper).encode(), error)
        includes.add(prefix + "execution/inputs")
        sources.append({"path": prefix + wrapper_path, "byte_fingerprint": _sha256_bytes(_regular(root, wrapper_path).read_bytes())})
        value = {
            "schema_version": "1.0", "artifact_kind": "PROJECT_EXECUTION_BUNDLE",
            "job_id": command.manifest["job_id"], "input_fingerprint": command.manifest["input_fingerprint"],
            "snapshot_fingerprint": snapshot["snapshot_fingerprint"],
            "sources": sources, "include_dirs": sorted(includes), "top": "dv_project_execution_top",
            "execution": snapshot["execution"], "binding_fingerprint": "0" * 64,
        }
        value["binding_fingerprint"] = artifact_fingerprint(value, "binding_fingerprint")
        _require_schema("project_execution_bundle", value, error)
        _immutable_json(root / EXECUTION_BUNDLE_PATH, value, error)
        result = _checkpoint("READY_FOR_EXECUTION", command.manifest, EXECUTION_BUNDLE_PATH, value["binding_fingerprint"])
        _immutable_json(root / "audit/pj003_ready_for_execution.json", result, error)
        return ProjectActionResult(result, (EXECUTION_BUNDLE_PATH, "audit/pj003_ready_for_execution.json"), False)


def load_execution_bundle(root: Path, workspace: Path, manifest: Mapping[str, Any], error) -> dict[str, Any]:
    snapshot = load_execution_input(root, manifest, error)
    binding = _load_fingerprinted(root, EXECUTION_BUNDLE_PATH,
                                 "project_execution_bundle", "binding_fingerprint", error)
    if (binding["snapshot_fingerprint"] != snapshot["snapshot_fingerprint"] or
            binding["job_id"] != manifest["job_id"] or binding["input_fingerprint"] != manifest["input_fingerprint"] or
            binding["execution"] != snapshot["execution"]):
        raise error("STALE_EVIDENCE", "execution binding is stale")
    for item in binding["sources"]:
        if _sha256_bytes(_regular(workspace, item["path"]).read_bytes()) != item["byte_fingerprint"]:
            raise error("STALE_EVIDENCE", "bound execution source drifted")
    return binding


@dataclass(frozen=True)
class ExecuteTestcaseInput:
    job_root: Path
    workspace_root: Path
    manifest: dict[str, Any]
    checkpoint: dict[str, Any]
    build: Callable[[str, Mapping[str, Any]], Any]
    run: Callable[[str, Mapping[str, Any]], Any]


def _adapter_pair(value: Any) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    if hasattr(value, "request") and hasattr(value, "evidence"):
        return (copy.deepcopy(value.request), copy.deepcopy(value.evidence),
                str(value.request_path), str(value.evidence_path))
    if isinstance(value, Mapping):
        request = copy.deepcopy(dict(value["request"]))
        evidence = copy.deepcopy(dict(value["evidence"]))
        return (request, evidence, str(value.get("request_path", "")),
                str(value.get("evidence_path", "")))
    raise TypeError("trusted EDA adapter returned an unsupported result")


def _adapter_summary(
        result: Any, expected_phase: str, request: Mapping[str, Any],
        job_id: str, environment_fingerprint: str, error) -> dict[str, Any]:
    adapter_request, evidence, request_path, evidence_path = _adapter_pair(result)
    _require_schema("xcelium_execution_request", adapter_request, error)
    _require_schema("xcelium_execution_evidence", evidence, error)
    if (adapter_request.get("request_fingerprint") != artifact_fingerprint(adapter_request, "request_fingerprint") or
            evidence.get("evidence_fingerprint") != artifact_fingerprint(evidence, "evidence_fingerprint") or
            adapter_request.get("phase") != expected_phase or
            evidence.get("phase") != expected_phase or
            adapter_request.get("job_id") != job_id or
            evidence.get("job_id") != job_id or
            adapter_request.get("environment_fingerprint") !=
                environment_fingerprint or
            evidence.get("environment_fingerprint") != environment_fingerprint or
            evidence.get("request_fingerprint") !=
                adapter_request.get("request_fingerprint") or
            adapter_request.get("source_fingerprints") != sorted([{
                "path": path,
                "fingerprint": _sha256_bytes(
                    _regular(Path(request["workspace_root"]), path).read_bytes()),
            } for path in request["configuration"]["sources"]],
                key=lambda item: item["path"])):
        raise error("STALE_EVIDENCE", "trusted EDA evidence is not exact")
    expected_request_path = "audit/xcelium/{}.request.json".format(
        adapter_request["request_id"].removeprefix(
            "XCELIUM.REQUEST.").casefold())
    expected_evidence_path = expected_request_path.replace(
        ".request.json", ".evidence.json")
    if request_path != expected_request_path or evidence_path != expected_evidence_path:
        raise error("STALE_EVIDENCE", "trusted EDA evidence paths are not canonical")
    return {
        "request_path": request_path,
        "evidence_path": evidence_path,
        "request_fingerprint": adapter_request["request_fingerprint"],
        "evidence_fingerprint": evidence["evidence_fingerprint"],
        "phase": expected_phase,
        "status": evidence["execution_status"],
        "output_subdir": adapter_request["output_subdir"],
        "output_tree_fingerprint": evidence["output_tree_fingerprint"],
        "output_tree_size_bytes": evidence["output_tree_size_bytes"],
        "output_tree_file_count": evidence["output_tree_file_count"],
    }


class ExecuteTestcaseHandler:
    """Build first, then resume each exact implemented testcase independently."""
    def __init__(self, error: type[Exception]):
        self.error = error

    def handle(self, command: ExecuteTestcaseInput) -> ProjectActionResult:
        root, error = command.job_root, self.error
        snapshot = load_execution_input(root, command.manifest, error)
        binding = load_execution_bundle(root, command.workspace_root, command.manifest, error)
        if command.checkpoint.get("authority_fingerprint") != binding["binding_fingerprint"]:
            raise error("STALE_EVIDENCE", "execution binding is stale")
        configuration = {
            "sources": [item["path"] for item in binding["sources"]],
            "include_dirs": binding["include_dirs"], "top": binding["top"],
            **binding["execution"]["constraints"],
        }
        token = binding["binding_fingerprint"][:16].upper()
        execution_id = "EXECUTION.PROJECT." + token
        request = {
            "schema_version": "1.0", "artifact_kind": "PROJECT_EXECUTION_REQUEST",
            "execution_id": execution_id, "job_id": binding["job_id"],
            "binding_path": EXECUTION_BUNDLE_PATH,
            "binding_fingerprint": binding["binding_fingerprint"],
            "configuration": configuration, "testcases": snapshot["testcases"],
            "execution": binding["execution"], "request_fingerprint": "0" * 64,
        }
        request["request_fingerprint"] = artifact_fingerprint(request, "request_fingerprint")
        _require_schema("project_execution_request", request, error)
        _immutable_json(root / EXECUTION_REQUEST_PATH, request, error)
        build, runs, rows, diagnostics = None, [], [], []
        if snapshot["testcases"]:
            try:
                result = command.build("PJ003.BUILD." + token, configuration)
                build = _adapter_summary(result, "BUILD", {**request, "workspace_root": str(command.workspace_root)},
                                         binding["job_id"], binding["execution"]["environment_fingerprint"], error)
                diagnostics.extend(_adapter_pair(result)[1].get("diagnostic_codes", []))
            except Exception as caught:
                codes = {item.get("code") for item in getattr(caught, "diagnostics", [])}
                codes.add(getattr(caught, "code", None))
                if not codes & {"BLOCKED_TOOL", "LICENSE_UNAVAILABLE", "RUNTIME_DEPENDENCY_MISSING"}:
                    raise
                diagnostics.extend(sorted(code for code in codes if code))
        else:
            diagnostics.append("NO_EXECUTABLE_TESTCASES")
        for testcase in snapshot["testcases"]:
            row = {**testcase, "status": "BLOCKED", "exit_code": None,
                   "pass_marker_observed": None, "uvm_error_count": None, "uvm_fatal_count": None,
                   "logs": [], "diagnostic_codes": []}
            if build is not None and build["status"] == "PASS":
                run_configuration = {
                    **configuration, "seed": testcase["seed"],
                    "timeout_seconds": testcase["timeout_seconds"],
                    "pass_marker": testcase["pass_marker"],
                    "uvm_test": "dv_exec_" + testcase["uvm_class"],
                    "plusargs": ["+DV_TESTCASE_ID=" + testcase["testcase_id"]],
                }
                result = command.run("PJ003.RUN.{}.{}".format(token, testcase["testcase_id"]), run_configuration)
                summary = _adapter_summary(result, "RUN", {
                    **request, "configuration": run_configuration, "workspace_root": str(command.workspace_root)},
                    binding["job_id"], binding["execution"]["environment_fingerprint"], error)
                runs.append(summary)
                evidence = _adapter_pair(result)[1]
                status = "BLOCKED" if evidence["execution_status"] == "BLOCKED_TOOL" else "FAIL"
                if (evidence["execution_status"] == "PASS" and evidence["exit_code"] == 0 and
                        evidence["pass_marker_found"] is True and evidence["uvm_error_count"] == 0 and
                        evidence["uvm_fatal_count"] == 0):
                    status = "PASS"
                row.update(status=status, exit_code=evidence["exit_code"],
                           pass_marker_observed=evidence["pass_marker_found"],
                           uvm_error_count=evidence["uvm_error_count"], uvm_fatal_count=evidence["uvm_fatal_count"],
                           logs=evidence["logs"], diagnostic_codes=evidence["diagnostic_codes"])
                diagnostics.extend(evidence["diagnostic_codes"])
            else:
                row["diagnostic_codes"] = ["BUILD_NOT_PASSED"]
            rows.append(row)
        counts = {"implemented": len(rows), "skipped": len(snapshot["skipped_testcases"]),
                  "executed": len(runs), "passed": sum(row["status"] == "PASS" for row in rows),
                  "failed": sum(row["status"] == "FAIL" for row in rows),
                  "blocked": sum(row["status"] == "BLOCKED" for row in rows)}
        status = ("FAIL" if counts["failed"] or (build is not None and build["status"] == "FAIL") else
                  "BLOCKED" if not rows or counts["blocked"] else "PASS")
        summary = {"generation_complete": True, "review_complete": True, **counts,
                   "full_spec_coverage_complete": snapshot["full_spec_coverage_complete"],
                   "full_verification_passed": bool(rows) and status == "PASS" and
                       snapshot["full_spec_coverage_complete"] and not snapshot["findings"]}
        evidence = {
            "schema_version": "1.0", "artifact_kind": "PROJECT_EXECUTION_EVIDENCE",
            "evidence_id": "EVIDENCE.PROJECT." + token, "execution_id": execution_id,
            "request_fingerprint": request["request_fingerprint"], "job_id": binding["job_id"],
            "binding_fingerprint": binding["binding_fingerprint"], "execution_status": status,
            "build": build, "runs": runs, "testcases": rows,
            "skipped_testcases": snapshot["skipped_testcases"], "findings": snapshot["findings"],
            "ac_reviews": snapshot["ac_reviews"], "summary": summary,
            "diagnostic_codes": sorted(set(diagnostics)),
            "qualification_scope": "IMPLEMENTED_TESTCASE_XCELIUM_EXECUTION", "evidence_fingerprint": "0" * 64,
        }
        evidence["evidence_fingerprint"] = artifact_fingerprint(evidence, "evidence_fingerprint")
        _require_schema("project_execution_evidence", evidence, error)
        _immutable_json(root / EXECUTION_EVIDENCE_PATH, evidence, error)
        terminal = _checkpoint("EXECUTION_" + status, command.manifest, EXECUTION_EVIDENCE_PATH, evidence["evidence_fingerprint"])
        _immutable_json(root / EXECUTION_RESULT_PATH, terminal, error)
        return ProjectActionResult(terminal, (EXECUTION_REQUEST_PATH, EXECUTION_EVIDENCE_PATH, EXECUTION_RESULT_PATH), False)
