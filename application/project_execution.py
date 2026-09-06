"""PJ-003 Human authority, exact binding, and Project execution actions."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from contracts.validator import accepted, load_document, validate
from domain.artifacts import artifact_fingerprint
from infrastructure.persistence.atomic_artifact import (
    publish_immutable_bytes,
    publish_immutable_text,
)
from scripts.dvlib import canonical_hash


PJ003_WORKFLOW_VERSION = "PJ-003.1"
APPROVED_TESTCASE_PATH = "approved/generated/manifests/project_testcase.json"
TESTCASE_DECISION_PATH = "audit/pj003_testcase_decision.json"
EXECUTION_AUTHORIZATION_PATH = "audit/pj003_execution_authorization.json"
EXECUTION_BUNDLE_PATH = "audit/pj003_execution_bundle.json"
EXECUTION_REQUEST_PATH = "audit/pj003_execution_request.json"
EXECUTION_EVIDENCE_PATH = "audit/pj003_execution_evidence.json"
EXECUTION_RESULT_PATH = "audit/pj003_execution_result.json"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _human_checkpoint_fingerprint(value: Mapping[str, Any]) -> str:
    projected = copy.deepcopy(dict(value))
    projected.pop("checkpoint_fingerprint", None)
    bundle = projected.get("bundle_fingerprints")
    if isinstance(bundle, dict) and "checkpoint" in bundle:
        bundle["checkpoint"] = "0" * 64
    return canonical_hash(projected)


def _utc_value(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as caught:
        raise ValueError("timestamp is invalid") from caught
    if parsed.tzinfo is None:
        raise ValueError("timestamp lacks a timezone")
    return parsed.astimezone(timezone.utc)


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


def _agent_identities(manifest: Mapping[str, Any]) -> set[str]:
    return {
        str(reference[field]).casefold()
        for section in manifest["agent_profile"]["bindings"].values()
        for reference in section.values()
        for field in ("provider_id", "model_id")
    }


@dataclass(frozen=True)
class RecordHumanDecisionInput:
    job_root: Path
    manifest: dict[str, Any]
    checkpoint: dict[str, Any]
    approval_request: dict[str, Any]
    decision: dict[str, Any]


@dataclass(frozen=True)
class ProjectActionResult:
    checkpoint: dict[str, Any]
    output_references: tuple[str, ...]
    replayed: bool


class RecordHumanDecisionHandler:
    """Persist exactly one DV_OWNER testcase decision without closing the Job."""

    def __init__(self, error: type[Exception]):
        self.error = error

    def handle(self, command: RecordHumanDecisionInput) -> ProjectActionResult:
        error = self.error
        manifest = command.manifest
        checkpoint = command.checkpoint
        approval = command.approval_request
        decision = command.decision
        _require_schema("approval_request", approval, error)
        _require_schema("approval_decision", decision, error)
        if (checkpoint.get("state") != "AWAITING_HUMAN_REVIEW" or
                checkpoint.get("job_id") != manifest["job_id"] or
                checkpoint.get("input_fingerprint") !=
                    manifest["input_fingerprint"] or
                checkpoint.get("checkpoint_fingerprint") !=
                    _human_checkpoint_fingerprint(checkpoint) or
                checkpoint.get("bundle_fingerprints") !=
                    approval.get("bundle_fingerprints") or
                checkpoint.get("checkpoint_id") != approval.get("checkpoint_id") or
                approval.get("required_role") != "DV_OWNER" or
                approval.get("validation_status") != "PASS"):
            raise error("STALE_EVIDENCE", "Human approval bundle is stale")
        if (decision.get("approval_request_id") !=
                approval.get("approval_request_id") or
                decision.get("job_id") != manifest["job_id"] or
                decision.get("thread_id") != approval.get("thread_id") or
                decision.get("candidate_fingerprint") !=
                    approval.get("candidate_fingerprint") or
                decision.get("checkpoint_id") != checkpoint.get("checkpoint_id") or
                decision.get("approver_role") != "DV_OWNER" or
                not set(approval.get("validation_artifact_ids", ())).issubset(
                    decision.get("evidence_ids", ())) or
                decision.get("approver_identity", "").casefold() in
                    _agent_identities(manifest)):
            raise error(
                "INVALID_APPROVAL_PROVENANCE",
                "Human decision does not bind the exact reviewed bundle")

        # The source checkpoint validator has already checked the whole bundle;
        # repeat the decisive CLEAN and testcase byte checks at the action edge.
        try:
            report = load_document(_regular(
                command.job_root, checkpoint["review_report_path"]))
            validation = load_document(_regular(
                command.job_root, checkpoint["review_validation_path"]))
            candidate = load_document(_regular(
                command.job_root, checkpoint["candidate_metadata_path"]))
            review_request = load_document(_regular(
                command.job_root, checkpoint["review_request_path"]))
            candidate_bytes = _regular(
                command.job_root, candidate["output_path"]).read_bytes()
        except Exception as caught:
            raise error("PARTIAL_ARTIFACT", "reviewed testcase bundle is incomplete") \
                from caught
        if (report.get("verdict") != "CLEAN" or report.get("findings") or
                checkpoint.get("error_count") != 0 or
                validation.get("validation_fingerprint") !=
                    checkpoint["bundle_fingerprints"].get("review_validation") or
                candidate.get("candidate_fingerprint") !=
                    approval.get("candidate_fingerprint") or
                candidate.get("candidate_fingerprint") !=
                    checkpoint["bundle_fingerprints"].get("testcase") or
                _sha256_bytes(candidate_bytes) !=
                    candidate.get("content_fingerprint")):
            raise error("STALE_EVIDENCE", "testcase approval requires exact CLEAN evidence")

        decision_record = copy.deepcopy(decision)
        decision_fingerprint = canonical_hash(decision_record)
        _immutable_json(
            command.job_root / TESTCASE_DECISION_PATH, decision_record, error)
        if decision["decision"] != "APPROVE":
            paused = _checkpoint(
                "PAUSED_BY_HUMAN", manifest, TESTCASE_DECISION_PATH,
                decision_fingerprint)
            paused["decision"] = decision["decision"]
            paused["checkpoint_fingerprint"] = artifact_fingerprint(
                paused, "checkpoint_fingerprint")
            _require_schema("project_execution_checkpoint", paused, error)
            relative = "audit/pj003_paused_by_human.json"
            _immutable_json(command.job_root / relative, paused, error)
            return ProjectActionResult(
                paused, (TESTCASE_DECISION_PATH, relative), False)

        approved_source = "approved/generated/uvm/generated_tests.sv"
        staged_manifest = (
            "staging/generated/uvm/generated_tests_manifest.r{:03d}.json".format(
                candidate["revision"]))
        try:
            manifest_bytes = _regular(command.job_root, staged_manifest).read_bytes()
        except ValueError as caught:
            raise error("PARTIAL_ARTIFACT", "generated UVM testcase manifest is missing") from caught
        approved_manifest = "approved/generated/uvm/generated_tests_manifest.json"
        _immutable_bytes(command.job_root / approved_source, candidate_bytes, error)
        _immutable_bytes(command.job_root / approved_manifest, manifest_bytes, error)
        runtime_uvm = review_request.get("runtime_capability", {})
        if (runtime_uvm.get("aggregate_fingerprint") !=
                checkpoint["bundle_fingerprints"].get("effective_uvm") or
                not isinstance(runtime_uvm.get("uvm_context_files"), list)):
            raise error("STALE_EVIDENCE", "reviewed effective UVM root is stale")
        approved_uvm_files = []
        for item in runtime_uvm["uvm_context_files"]:
            try:
                logical = _safe_relative(str(item["logical_path"]))
                content = str(item["content"]).encode("utf-8")
            except (KeyError, ValueError) as caught:
                raise error("STALE_EVIDENCE", "reviewed UVM file is unsafe") \
                    from caught
            fingerprint = _sha256_bytes(content)
            if fingerprint != item.get("fingerprint"):
                raise error("STALE_EVIDENCE", "reviewed UVM file drifted")
            relative = (PurePosixPath("approved/generated/uvm/effective") /
                        logical).as_posix()
            _immutable_bytes(command.job_root / relative, content, error)
            approved_uvm_files.append({
                "logical_path": logical.as_posix(), "path": relative,
                "byte_fingerprint": fingerprint,
            })
        authority = {
            "schema_version": "1.0",
            "artifact_kind": "PROJECT_APPROVED_TESTCASE",
            "job_id": manifest["job_id"],
            "input_fingerprint": manifest["input_fingerprint"],
            "source_checkpoint_path":
                "audit/oches001_human_review_checkpoint.json",
            "source_checkpoint_fingerprint":
                checkpoint["checkpoint_fingerprint"],
            "testcase_decision_path": TESTCASE_DECISION_PATH,
            "testcase_decision_fingerprint":
                decision_fingerprint,
            "approved_testcase_path": approved_source,
            "approved_testcase_fingerprint": _sha256_bytes(candidate_bytes),
            "approved_manifest_path": approved_manifest,
            "approved_manifest_fingerprint": _sha256_bytes(manifest_bytes),
            "candidate_id": candidate["candidate_id"],
            "candidate_fingerprint": candidate["candidate_fingerprint"],
            "effective_uvm_root": runtime_uvm["aggregate_fingerprint"],
            "approved_uvm_files": approved_uvm_files,
            "bundle_fingerprints": copy.deepcopy(
                checkpoint["bundle_fingerprints"]),
            "authority_fingerprint": "0" * 64,
        }
        authority["authority_fingerprint"] = artifact_fingerprint(
            authority, "authority_fingerprint")
        _require_schema("project_approved_testcase", authority, error)
        _immutable_json(command.job_root / APPROVED_TESTCASE_PATH, authority, error)
        next_checkpoint = _checkpoint(
            "AWAITING_EXECUTION_AUTHORIZATION", manifest,
            APPROVED_TESTCASE_PATH, authority["authority_fingerprint"])
        relative = "audit/pj003_awaiting_execution_authorization.json"
        _require_schema("project_execution_checkpoint", next_checkpoint, error)
        _immutable_json(command.job_root / relative, next_checkpoint, error)
        return ProjectActionResult(next_checkpoint, (
            TESTCASE_DECISION_PATH, approved_source, approved_manifest,
            *[item["path"] for item in approved_uvm_files],
            APPROVED_TESTCASE_PATH,
            relative), False)


@dataclass(frozen=True)
class RecordExecutionAuthorizationInput:
    job_root: Path
    manifest: dict[str, Any]
    checkpoint: dict[str, Any]
    authorization: dict[str, Any]


class RecordExecutionAuthorizationHandler:
    """Persist a second, explicit DV_OWNER execution authority."""

    def __init__(self, error: type[Exception], *, now: Callable[[], datetime] | None = None):
        self.error = error
        self.now = now or (lambda: datetime.now(timezone.utc))

    def handle(
            self, command: RecordExecutionAuthorizationInput
            ) -> ProjectActionResult:
        error = self.error
        approved = _load_fingerprinted(
            command.job_root, APPROVED_TESTCASE_PATH,
            "project_approved_testcase", "authority_fingerprint", error)
        authorization = copy.deepcopy(command.authorization)
        _require_schema("project_execution_authorization", authorization, error)
        if authorization["authorization_fingerprint"] != artifact_fingerprint(
                authorization, "authorization_fingerprint"):
            raise error("INVALID_APPROVAL_PROVENANCE",
                        "execution authorization fingerprint is invalid")
        if (command.checkpoint.get("state") !=
                "AWAITING_EXECUTION_AUTHORIZATION" or
                command.checkpoint.get("authority_fingerprint") !=
                    approved["authority_fingerprint"] or
                authorization["job_id"] != command.manifest["job_id"] or
                authorization["testcase_approval_fingerprint"] !=
                    approved["authority_fingerprint"] or
                authorization["authorizer_role"] != "DV_OWNER" or
                authorization["authorizer_identity"].casefold() in
                    _agent_identities(command.manifest)):
            raise error("INVALID_APPROVAL_PROVENANCE",
                        "execution authorization does not bind the approval")
        decision = load_document(_regular(
            command.job_root, approved["testcase_decision_path"]))
        if (canonical_hash(decision) !=
                approved["testcase_decision_fingerprint"] or
                authorization["authorization_id"] == decision.get("decision_id") or
                decision.get("decision") != "APPROVE"):
            raise error("INVALID_APPROVAL_PROVENANCE",
                        "testcase approval cannot substitute for execution authority")
        try:
            authorized = _utc_value(authorization["authorized_at"])
            expires = _utc_value(authorization["expires_at"])
        except ValueError as caught:
            raise error("INVALID_SCHEMA", str(caught)) from caught
        now = self.now().astimezone(timezone.utc)
        if authorized > now or expires <= authorized or expires <= now:
            raise error("EXPIRED_AUTHORIZATION",
                        "execution authorization is not currently valid")
        if authorization["constraints"]["timeout_seconds"] > 3600:
            raise error("INVALID_APPROVAL_PROVENANCE",
                        "execution authorization exceeds the trusted policy")
        _immutable_json(
            command.job_root / EXECUTION_AUTHORIZATION_PATH,
            authorization, error)
        next_checkpoint = _checkpoint(
            "READY_FOR_BINDING", command.manifest,
            EXECUTION_AUTHORIZATION_PATH,
            authorization["authorization_fingerprint"])
        relative = "audit/pj003_ready_for_binding.json"
        _require_schema("project_execution_checkpoint", next_checkpoint, error)
        _immutable_json(command.job_root / relative, next_checkpoint, error)
        return ProjectActionResult(
            next_checkpoint, (EXECUTION_AUTHORIZATION_PATH, relative), False)


@dataclass(frozen=True)
class BindApprovedBundleInput:
    job_root: Path
    workspace_root: Path
    manifest: dict[str, Any]
    checkpoint: dict[str, Any]


class BindApprovedBundleHandler:
    """Bind immutable baseline RTL bytes to the exact approved testcase."""

    def __init__(self, error: type[Exception]):
        self.error = error

    def handle(self, command: BindApprovedBundleInput) -> ProjectActionResult:
        error = self.error
        approved = _load_fingerprinted(
            command.job_root, APPROVED_TESTCASE_PATH,
            "project_approved_testcase", "authority_fingerprint", error)
        authorization = _load_fingerprinted(
            command.job_root, EXECUTION_AUTHORIZATION_PATH,
            "project_execution_authorization", "authorization_fingerprint", error)
        try:
            if _utc_value(authorization["expires_at"]) <= datetime.now(
                    timezone.utc):
                raise error("EXPIRED_AUTHORIZATION",
                            "execution authorization expired before binding")
        except ValueError as caught:
            raise error("INVALID_SCHEMA", str(caught)) from caught
        if (command.checkpoint.get("state") != "READY_FOR_BINDING" or
                command.checkpoint.get("authority_fingerprint") !=
                    authorization["authorization_fingerprint"] or
                approved["job_id"] != command.manifest["job_id"] or
                authorization["job_id"] != command.manifest["job_id"] or
                authorization["testcase_approval_fingerprint"] !=
                    approved["authority_fingerprint"]):
            raise error("STALE_EVIDENCE", "binding authority is stale")
        testcase_path = _regular(
            command.job_root, approved["approved_testcase_path"])
        testcase_fingerprint = _sha256_bytes(testcase_path.read_bytes())
        if testcase_fingerprint != approved["approved_testcase_fingerprint"]:
            raise error("STALE_EVIDENCE", "approved testcase bytes were modified")
        decision = load_document(_regular(
            command.job_root, approved["testcase_decision_path"]))
        if (decision.get("decision") != "APPROVE" or
                canonical_hash(decision) !=
                    approved["testcase_decision_fingerprint"]):
            raise error("STALE_EVIDENCE", "testcase approval decision is stale")
        approved_uvm = []
        for item in approved["approved_uvm_files"]:
            path = _regular(command.job_root, item["path"])
            fingerprint = _sha256_bytes(path.read_bytes())
            if fingerprint != item["byte_fingerprint"]:
                raise error("STALE_EVIDENCE", "approved UVM bytes drifted")
            approved_uvm.append(copy.deepcopy(item))
        effective_root = canonical_hash([{
            "logical_path": item["logical_path"],
            "fingerprint": item["byte_fingerprint"],
        } for item in approved_uvm])
        if (effective_root != approved["effective_uvm_root"] or
                effective_root != approved["bundle_fingerprints"].get(
                    "effective_uvm")):
            raise error("STALE_EVIDENCE", "approved effective UVM root drifted")
        rtl = []
        for source in command.manifest["rtl"]["sources"]:
            relative = source["baseline_path"]
            try:
                path = _regular(command.workspace_root, relative)
            except ValueError as caught:
                raise error("STALE_EVIDENCE", "baseline RTL is unavailable") from caught
            fingerprint = _sha256_bytes(path.read_bytes())
            if fingerprint != source["fingerprint"]:
                raise error("STALE_EVIDENCE", "baseline RTL bytes were modified")
            rtl.append({
                "path": relative,
                "byte_fingerprint": fingerprint,
                "source_identity": source["source_identity"],
            })
        binding = {
            "schema_version": "1.0",
            "artifact_kind": "PROJECT_EXECUTION_BUNDLE",
            "binding_id": "BINDING.PROJECT.{}".format(canonical_hash({
                "approval": approved["authority_fingerprint"],
                "authorization": authorization["authorization_fingerprint"],
                "rtl": rtl,
                "uvm": approved["approved_uvm_files"],
            })[:16].upper()),
            "job_id": command.manifest["job_id"],
            "input_fingerprint": command.manifest["input_fingerprint"],
            "policy": {
                "policy_id": PJ003_WORKFLOW_VERSION,
                "source_profile_id": command.manifest["eda"]["profile_id"],
                "qualification_ceiling": "PORTABLE_TESTCASE_EXECUTION_ONLY",
            },
            "testcase": {
                "path": approved["approved_testcase_path"],
                "byte_fingerprint": testcase_fingerprint,
                "candidate_fingerprint": approved["candidate_fingerprint"],
                "approval_authority_fingerprint":
                    approved["authority_fingerprint"],
                "top": command.manifest["testcase"]["top"],
                "pass_marker": command.manifest["testcase"]["pass_marker"],
                "bundle_fingerprints": copy.deepcopy(
                    approved["bundle_fingerprints"]),
            },
            "rtl": rtl,
            "uvm": approved_uvm,
            "dut": {
                "top": command.manifest["rtl"]["top"],
                "parameters": copy.deepcopy(
                    command.manifest["rtl"]["parameters"]),
            },
            "testcase_approval": {
                "path": approved["testcase_decision_path"],
                "id": decision["decision_id"],
                "fingerprint": approved["testcase_decision_fingerprint"],
                "role": "DV_OWNER",
            },
            "execution_authorization": {
                "path": EXECUTION_AUTHORIZATION_PATH,
                "id": authorization["authorization_id"],
                "fingerprint": authorization["authorization_fingerprint"],
                "role": "DV_OWNER",
            },
            "execution": {
                key: copy.deepcopy(authorization[key])
                for key in ("profile_id", "executable_ref",
                            "environment_identity", "environment_fingerprint",
                            "constraints")
            },
            "binding_fingerprint": "0" * 64,
        }
        binding["binding_fingerprint"] = artifact_fingerprint(
            binding, "binding_fingerprint")
        _require_schema("project_execution_bundle", binding, error)
        _immutable_json(command.job_root / EXECUTION_BUNDLE_PATH, binding, error)
        next_checkpoint = _checkpoint(
            "READY_FOR_EXECUTION", command.manifest, EXECUTION_BUNDLE_PATH,
            binding["binding_fingerprint"])
        relative = "audit/pj003_ready_for_execution.json"
        _require_schema("project_execution_checkpoint", next_checkpoint, error)
        _immutable_json(command.job_root / relative, next_checkpoint, error)
        return ProjectActionResult(
            next_checkpoint, (EXECUTION_BUNDLE_PATH, relative), False)


@dataclass(frozen=True)
class ExecuteApprovedTestcaseInput:
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
    if (adapter_request.get("phase") != expected_phase or
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


class ExecuteApprovedTestcaseHandler:
    """Execute one exact binding and rebind isolated adapter evidence."""

    def __init__(self, error: type[Exception]):
        self.error = error

    def handle(self, command: ExecuteApprovedTestcaseInput) -> ProjectActionResult:
        error = self.error
        binding = _load_fingerprinted(
            command.job_root, EXECUTION_BUNDLE_PATH,
            "project_execution_bundle", "binding_fingerprint", error)
        if (command.checkpoint.get("state") != "READY_FOR_EXECUTION" or
                command.checkpoint.get("authority_fingerprint") !=
                    binding["binding_fingerprint"] or
                binding["job_id"] != command.manifest["job_id"]):
            raise error("STALE_EVIDENCE", "execution binding is stale")
        authorization = _load_fingerprinted(
            command.job_root, EXECUTION_AUTHORIZATION_PATH,
            "project_execution_authorization", "authorization_fingerprint", error)
        if (authorization["authorization_fingerprint"] !=
                binding["execution_authorization"]["fingerprint"] or
                _utc_value(authorization["expires_at"]) <= datetime.now(
                    timezone.utc)):
            raise error("EXPIRED_AUTHORIZATION",
                        "execution authorization is stale or expired")
        # Recheck every source byte immediately before crossing the EDA boundary.
        sources = [item["path"] for item in binding["rtl"]] + [
            "result/jobs/{}/{}".format(binding["job_id"], item["path"])
            for item in binding["uvm"]] + [
            "result/jobs/{}/{}".format(
                binding["job_id"], binding["testcase"]["path"])]
        expected = {
            item["path"]: item["byte_fingerprint"] for item in binding["rtl"]}
        expected.update({
            "result/jobs/{}/{}".format(binding["job_id"], item["path"]):
                item["byte_fingerprint"] for item in binding["uvm"]})
        expected[sources[-1]] = binding["testcase"]["byte_fingerprint"]
        for relative in sources:
            try:
                actual = _sha256_bytes(_regular(
                    command.workspace_root, relative).read_bytes())
            except ValueError as caught:
                raise error("STALE_EVIDENCE", "bound execution source is unavailable") \
                    from caught
            if actual != expected[relative]:
                raise error("STALE_EVIDENCE", "bound execution source has drifted")
        constraints = binding["execution"]["constraints"]
        configuration = {
            "sources": sources,
            "top": binding["testcase"]["top"],
            "pass_marker": binding["testcase"]["pass_marker"],
            "seed": constraints["seed"],
            "uvm": constraints["uvm"],
            "coverage": constraints["coverage"],
            "waves": constraints["waves"],
            "timeout_seconds": constraints["timeout_seconds"],
        }
        execution_id = "EXECUTION.PROJECT.{}".format(
            binding["binding_fingerprint"][:16].upper())
        project_request = {
            "schema_version": "1.0",
            "artifact_kind": "PROJECT_EXECUTION_REQUEST",
            "execution_id": execution_id,
            "job_id": binding["job_id"],
            "binding_path": EXECUTION_BUNDLE_PATH,
            "binding_fingerprint": binding["binding_fingerprint"],
            "authorization_fingerprint":
                binding["execution_authorization"]["fingerprint"],
            **{
                key: copy.deepcopy(binding["execution"][key])
                for key in ("profile_id", "executable_ref",
                            "environment_identity", "environment_fingerprint")
            },
            "configuration": configuration,
            "request_fingerprint": "0" * 64,
        }
        project_request["request_fingerprint"] = artifact_fingerprint(
            project_request, "request_fingerprint")
        _require_schema("project_execution_request", project_request, error)
        request_path = command.job_root / EXECUTION_REQUEST_PATH
        evidence_path = command.job_root / EXECUTION_EVIDENCE_PATH
        if request_path.exists() or evidence_path.exists():
            if not request_path.is_file() or not evidence_path.is_file():
                raise error("PARTIAL_ARTIFACT",
                            "Project execution request/evidence pair is incomplete")
            existing_request = load_document(request_path)
            evidence = load_document(evidence_path)
            if existing_request != project_request:
                raise error("CONFLICTING_REPLAY",
                            "execution ID is bound to a different request")
            _require_schema("project_execution_evidence", evidence, error)
            if evidence["evidence_fingerprint"] != artifact_fingerprint(
                    evidence, "evidence_fingerprint"):
                raise error("STALE_EVIDENCE", "Project execution evidence is stale")
            checkpoint = load_document(_regular(
                command.job_root, EXECUTION_RESULT_PATH))
            return ProjectActionResult(
                checkpoint, (EXECUTION_REQUEST_PATH, EXECUTION_EVIDENCE_PATH,
                             EXECUTION_RESULT_PATH), True)
        _immutable_json(request_path, project_request, error)
        adapter_request = {**project_request, "workspace_root": str(
            command.workspace_root)}
        token = binding["binding_fingerprint"][:16].upper()
        try:
            build_result = command.build(
                "PJ003.BUILD.{}".format(token), configuration)
        except Exception as caught:
            codes = [
                item.get("code") for item in getattr(caught, "diagnostics", ())
                if isinstance(item, Mapping)]
            if getattr(caught, "code", None) == "BLOCKED_TOOL":
                codes.append("BLOCKED_TOOL")
            codes = sorted({code for code in codes if isinstance(code, str)})
            if not set(codes) & {
                    "BLOCKED_TOOL", "LICENSE_UNAVAILABLE",
                    "RUNTIME_DEPENDENCY_MISSING"}:
                raise
            evidence = {
                "schema_version": "1.0",
                "artifact_kind": "PROJECT_EXECUTION_EVIDENCE",
                "evidence_id": "EVIDENCE.PROJECT.{}".format(token),
                "execution_id": execution_id,
                "request_fingerprint": project_request["request_fingerprint"],
                "job_id": binding["job_id"],
                "binding_fingerprint": binding["binding_fingerprint"],
                "authorization_fingerprint":
                    binding["execution_authorization"]["fingerprint"],
                "execution_status": "BLOCKED",
                "build": None,
                "run": None,
                "diagnostic_codes": codes,
                "qualification_scope": "PORTABLE_TESTCASE_XCELIUM_EXECUTION",
                "evidence_fingerprint": "0" * 64,
            }
            evidence["evidence_fingerprint"] = artifact_fingerprint(
                evidence, "evidence_fingerprint")
            _require_schema("project_execution_evidence", evidence, error)
            _immutable_json(evidence_path, evidence, error)
            terminal = _checkpoint(
                "EXECUTION_BLOCKED", command.manifest,
                EXECUTION_EVIDENCE_PATH, evidence["evidence_fingerprint"])
            _require_schema("project_execution_checkpoint", terminal, error)
            _immutable_json(command.job_root / EXECUTION_RESULT_PATH, terminal, error)
            return ProjectActionResult(terminal, (
                EXECUTION_REQUEST_PATH, EXECUTION_EVIDENCE_PATH,
                EXECUTION_RESULT_PATH), False)
        build = _adapter_summary(
            build_result, "BUILD", adapter_request, binding["job_id"],
            binding["execution"]["environment_fingerprint"], error)
        run = None
        if build["status"] == "PASS":
            run_result = command.run(
                "PJ003.RUN.{}".format(token), configuration)
            run = _adapter_summary(
                run_result, "RUN", adapter_request, binding["job_id"],
                binding["execution"]["environment_fingerprint"], error)
        statuses = [build["status"], *([] if run is None else [run["status"]])]
        if "BLOCKED_TOOL" in statuses:
            status = "BLOCKED"
        elif "FAIL" in statuses or run is None:
            status = "FAIL"
        else:
            status = "PASS"
        diagnostics = []
        for result in (build_result, run_result if run is not None else None):
            if result is None:
                continue
            diagnostics.extend(_adapter_pair(result)[1].get(
                "diagnostic_codes", ()))
        evidence = {
            "schema_version": "1.0",
            "artifact_kind": "PROJECT_EXECUTION_EVIDENCE",
            "evidence_id": "EVIDENCE.PROJECT.{}".format(token),
            "execution_id": execution_id,
            "request_fingerprint": project_request["request_fingerprint"],
            "job_id": binding["job_id"],
            "binding_fingerprint": binding["binding_fingerprint"],
            "authorization_fingerprint":
                binding["execution_authorization"]["fingerprint"],
            "execution_status": status,
            "build": build,
            "run": run,
            "diagnostic_codes": sorted(set(diagnostics)),
            "qualification_scope": "PORTABLE_TESTCASE_XCELIUM_EXECUTION",
            "evidence_fingerprint": "0" * 64,
        }
        evidence["evidence_fingerprint"] = artifact_fingerprint(
            evidence, "evidence_fingerprint")
        _require_schema("project_execution_evidence", evidence, error)
        _immutable_json(evidence_path, evidence, error)
        state = "EXECUTION_{}".format(status)
        terminal = _checkpoint(
            state, command.manifest, EXECUTION_EVIDENCE_PATH,
            evidence["evidence_fingerprint"])
        _require_schema("project_execution_checkpoint", terminal, error)
        _immutable_json(command.job_root / EXECUTION_RESULT_PATH, terminal, error)
        return ProjectActionResult(terminal, (
            EXECUTION_REQUEST_PATH, EXECUTION_EVIDENCE_PATH,
            EXECUTION_RESULT_PATH), False)


__all__ = [
    "APPROVED_TESTCASE_PATH", "BindApprovedBundleHandler",
    "BindApprovedBundleInput", "EXECUTION_AUTHORIZATION_PATH",
    "EXECUTION_BUNDLE_PATH", "EXECUTION_EVIDENCE_PATH",
    "EXECUTION_REQUEST_PATH", "EXECUTION_RESULT_PATH",
    "ExecuteApprovedTestcaseHandler", "ExecuteApprovedTestcaseInput",
    "PJ003_WORKFLOW_VERSION", "ProjectActionResult",
    "RecordExecutionAuthorizationHandler", "RecordExecutionAuthorizationInput",
    "RecordHumanDecisionHandler", "RecordHumanDecisionInput",
]
