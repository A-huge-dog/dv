"""OCHES002 current-Job read model and nine deterministic read-only tools."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping

from contracts.validator import accepted, load_document, validate
from core.project_incremental import load_index
from scripts.dvlib import canonical_hash, validate_schema
from core.project_oches003 import (
    RECORD_CONTRACTS, RepairRecordStore, record_fingerprint,
)


READ_TOOL_NAMES = (
    "get_issue",
    "get_unit",
    "get_direct_dependencies",
    "get_dependents",
    "get_spec_evidence",
    "get_repair_history",
    "compare_unit_revisions",
    "estimate_repair_impact",
    "get_budget_status",
)

ORCHESTRATOR_READ_TOOLS = frozenset(READ_TOOL_NAMES)
STAGE_READ_TOOLS = frozenset({
    "get_issue", "get_unit", "get_direct_dependencies",
    "get_spec_evidence", "get_repair_history", "compare_unit_revisions",
})

REJECTED_PLAN_DIAGNOSTICS = frozenset({
    "INVALID_REPAIR_PLAN",
    "STALE_EVIDENCE",
    "INVALID_AGENT_BINDING",
    "SCOPE_EXPANSION",
    "INSUFFICIENT_EVIDENCE",
    "UNKNOWN_OR_UNREPAIRABLE_ISSUE",
    "DUPLICATE_ISSUE_ROUTE",
    "WRONG_STAGE_TARGET_KIND",
    "UNKNOWN_TARGET",
    "INCOMPLETE_ERROR_ROUTING",
    "EMPTY_REPAIR_SCOPE",
    "REPLAN_REQUIRED",
})

_STRING_ARRAY = {
    "type": "array", "minItems": 1, "uniqueItems": True,
    "items": {"type": "string", "minLength": 1},
}
_NO_EXTRA = {"type": "object", "additionalProperties": False}
_READ_TOOL_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "get_issue": {
        **_NO_EXTRA, "required": ["issue_ids"],
        "properties": {"issue_ids": copy.deepcopy(_STRING_ARRAY)},
    },
    "get_unit": {
        **_NO_EXTRA, "required": ["unit_ids"],
        "properties": {"unit_ids": copy.deepcopy(_STRING_ARRAY)},
    },
    "get_direct_dependencies": {
        **_NO_EXTRA, "required": ["unit_ids"],
        "properties": {"unit_ids": copy.deepcopy(_STRING_ARRAY)},
    },
    "get_dependents": {
        **_NO_EXTRA, "required": ["unit_ids"],
        "properties": {"unit_ids": copy.deepcopy(_STRING_ARRAY)},
    },
    "get_spec_evidence": {
        **_NO_EXTRA, "required": ["evidence_refs"],
        "properties": {"evidence_refs": {
            "type": "array", "minItems": 1, "uniqueItems": True,
            "items": {
                **_NO_EXTRA,
                "required": [
                    "path", "line_start", "line_end",
                    "snippet_fingerprint"],
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "line_start": {"type": "integer", "minimum": 1},
                    "line_end": {"type": "integer", "minimum": 1},
                    "snippet_fingerprint": {
                        "type": "string", "pattern": "^[0-9a-f]{64}$"},
                },
            },
        }},
    },
    "get_repair_history": {
        **_NO_EXTRA, "required": ["identities"],
        "properties": {"identities": copy.deepcopy(_STRING_ARRAY)},
    },
    "compare_unit_revisions": {
        **_NO_EXTRA, "required": ["comparisons"],
        "properties": {"comparisons": {
            "type": "array", "minItems": 1, "uniqueItems": True,
            "items": {
                **_NO_EXTRA,
                "required": ["unit_id", "from_revision", "to_revision"],
                "properties": {
                    "unit_id": {"type": "string", "minLength": 1},
                    "from_revision": {"type": "integer", "minimum": 0},
                    "to_revision": {"type": "integer", "minimum": 0},
                },
            },
        }},
    },
    "estimate_repair_impact": {
        **_NO_EXTRA, "required": ["target_ids"],
        "properties": {"target_ids": copy.deepcopy(_STRING_ARRAY)},
    },
    "get_budget_status": {
        **_NO_EXTRA, "required": [], "properties": {},
    },
}

_TOOL_DESCRIPTIONS = {
    "get_issue": "Return exact current Reviewer findings by issue ID.",
    "get_unit": "Return exact current semantic units by unit ID.",
    "get_direct_dependencies":
        "Return exact direct upstream dependencies of current units.",
    "get_dependents":
        "Return exact direct downstream dependents of current units.",
    "get_spec_evidence":
        "Revalidate existing mapping/unit evidence against immutable Spec.",
    "get_repair_history":
        "Return matching authoritative current-Job repair records.",
    "compare_unit_revisions":
        "Return two exact unit revisions and their lossless structured diff.",
    "estimate_repair_impact":
        "Calculate target/dependent dirty closure without mutation.",
    "get_budget_status":
        "Return current call, elapsed-time, and session status without tokens.",
}


class ProjectToolError(RuntimeError):
    """Typed whole-call failure; no partial result may authorize progress."""

    def __init__(self, code: str, message: str):
        super().__init__("[{}] {}".format(code, message))
        self.code = code
        self.message = message


def read_tool_definitions(
        allowlist: Iterable[str] = READ_TOOL_NAMES) -> list[dict[str, Any]]:
    names = sorted(set(allowlist))
    unknown = set(names) - set(READ_TOOL_NAMES)
    if unknown:
        raise ProjectToolError(
            "TOOL_PERMISSION_DENIED", "read tool allow-list is invalid")
    return [{
        "name": name,
        "description": _TOOL_DESCRIPTIONS[name],
        "input_schema": copy.deepcopy(_READ_TOOL_INPUT_SCHEMAS[name]),
    } for name in names]


def _artifact_fingerprint(value: dict[str, Any], field: str) -> str:
    projected = copy.deepcopy(value)
    projected.pop(field, None)
    return canonical_hash(projected)


def _lineage_fingerprint(unit: dict[str, Any]) -> str:
    return canonical_hash({
        "unit_id": unit["unit_id"],
        "unit_kind": unit["unit_kind"],
        "content_fingerprint": unit["content_fingerprint"],
        "dependency_fingerprint": unit["dependency_fingerprint"],
    })


def _safe_relative(job_root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if (pure.is_absolute() or not pure.parts or
            any(part in {"", ".", ".."} or part.startswith(".")
                for part in pure.parts)):
        raise ProjectToolError("TOOL_PERMISSION_DENIED", "unsafe Job path")
    target = job_root.joinpath(*pure.parts)
    try:
        target.resolve(strict=False).relative_to(job_root.resolve())
    except ValueError as error:
        raise ProjectToolError(
            "TOOL_PERMISSION_DENIED", "Job path escapes current Job") from error
    if any(parent.is_symlink() for parent in [target, *target.parents]
           if parent != job_root.parent):
        raise ProjectToolError(
            "TOOL_PERMISSION_DENIED", "Job evidence path uses a symlink")
    return target


def _contains_identity(value: Any, identity: str) -> bool:
    if isinstance(value, dict):
        return any(_contains_identity(item, identity) for item in value.values())
    if isinstance(value, list):
        return any(_contains_identity(item, identity) for item in value)
    return value == identity


def _json_diff(before: Any, after: Any, path: str = "") -> list[dict[str, Any]]:
    if isinstance(before, dict) and isinstance(after, dict):
        changes = []
        for key in sorted(set(before) | set(after)):
            child = path + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in before:
                changes.append({"path": child, "before": None,
                                "after": copy.deepcopy(after[key])})
            elif key not in after:
                changes.append({"path": child,
                                "before": copy.deepcopy(before[key]),
                                "after": None})
            else:
                changes.extend(_json_diff(before[key], after[key], child))
        return changes
    if before != after:
        return [{"path": path or "/", "before": copy.deepcopy(before),
                 "after": copy.deepcopy(after)}]
    return []


class ProjectReadModel:
    """One immutable, fingerprint-validated view of a current Project Job."""

    def __init__(
            self, *, job_root: Path, job_id: str, input_fingerprint: str,
            spec_fingerprint: str, artifact_roots: dict[str, str],
            index_paths: Mapping[str, str], review_report_path: str,
            review_request_path: str,
            prior_review_artifact_roots: Mapping[str, str] | None = None,
            allowed_stale_stages: set[str] | None = None,
            budget_status: Mapping[str, Any] | Callable[[], Mapping[str, Any]]
            | None = None):
        self.job_root = Path(job_root)
        self.job_id = job_id
        self.input_fingerprint = input_fingerprint
        self.spec_fingerprint = spec_fingerprint
        self.artifact_roots = copy.deepcopy(artifact_roots)
        self.index_paths = dict(index_paths)
        self.review_report_path = review_report_path
        self.review_request_path = review_request_path
        self.prior_review_artifact_roots = copy.deepcopy(
            dict(prior_review_artifact_roots)) \
            if prior_review_artifact_roots is not None else None
        self.allowed_stale_stages = set(allowed_stale_stages or ())
        self._budget_status = budget_status or {}
        self.indexes: dict[str, dict[str, Any]] = {}
        self.units: dict[str, dict[str, Any]] = {}
        self._units_by_lineage: dict[tuple[str, str], dict[str, Any]] = {}
        self._load_current_snapshot()

    @classmethod
    def from_checkpoint(
            cls, job_root: Path, checkpoint: dict[str, Any],
            budget_status: Mapping[str, Any] | Callable[
                [], Mapping[str, Any]] | None = None) -> "ProjectReadModel":
        root = Path(job_root)
        job_id = checkpoint.get("job_id")
        if not isinstance(job_id, str):
            raise ProjectToolError(
                "STALE_EVIDENCE", "checkpoint has no current Job identity")
        try:
            map1 = load_document(_safe_relative(
                root, checkpoint["scenario_ac_map_path"]))
            map2 = load_document(_safe_relative(
                root, checkpoint["ac_testcase_map_path"]))
            candidate = load_document(_safe_relative(
                root, checkpoint["candidate_metadata_path"]))
            report = load_document(_safe_relative(
                root, checkpoint["review_report_path"]))
            request = load_document(_safe_relative(
                root, checkpoint["review_request_path"]))
        except (KeyError, OSError, ValueError) as error:
            raise ProjectToolError(
                "STALE_EVIDENCE", "current Job checkpoint bundle is incomplete") \
                from error
        candidate_contract = (
            "project_committed_testcase"
            if candidate.get("artifact_kind") ==
                "COMMITTED_PORTABLE_SV_TESTCASE"
            else "project_testcase_candidate")
        for value, contract, fingerprint_field in (
                (map1, "scenario_ac_map", "artifact_fingerprint"),
                (map2, "ac_testcase_map", "artifact_fingerprint"),
                (candidate, candidate_contract,
                 "candidate_fingerprint")):
            if value.get("job_id") != job_id:
                raise ProjectToolError(
                    "CROSS_JOB_ARTIFACT",
                    "checkpoint source artifact belongs to another Job")
            if (not accepted(validate(contract, value)) or
                    value.get(fingerprint_field) !=
                        _artifact_fingerprint(value, fingerprint_field)):
                raise ProjectToolError(
                    "STALE_EVIDENCE", "checkpoint source artifact is stale")
        index_paths = checkpoint.get("artifact_unit_index_paths") or {
            "stage1": "staging/units/stage1/index.current.r{:03d}.json".format(
                map1["revision"]),
            "stage2": "staging/units/stage2/index.current.r{:03d}.json".format(
                map2["revision"]),
            "stage3": "staging/units/stage3/index.current.r{:03d}.json".format(
                candidate["revision"]),
            "review": checkpoint.get("review_unit_index_path") or
                "staging/units/review/index.current.r{:03d}.json".format(
                    report["review_round"] - 1),
        }
        report_roots = report.get("artifact_roots")
        expected_roots = {
            "scenario_ac_map": map1["artifact_fingerprint"],
            "ac_testcase_map": map2["artifact_fingerprint"],
            "testcase": candidate["candidate_fingerprint"],
        }
        prior_roots = checkpoint.get("prior_review_artifact_roots")
        if prior_roots is None and report_roots != expected_roots:
            raise ProjectToolError(
                "STALE_EVIDENCE", "Reviewer roots do not bind current artifacts")
        if prior_roots is not None and report_roots != prior_roots:
            raise ProjectToolError(
                "STALE_EVIDENCE", "prior Reviewer roots are not exact")
        if checkpoint.get("artifact_roots") not in (None, expected_roots):
            raise ProjectToolError(
                "STALE_EVIDENCE", "checkpoint and Reviewer roots differ")
        allowed_stale_stages = set()
        if prior_roots is not None:
            allowed_stale_stages.add("REVIEW")
        if candidate.get("ac_testcase_map_fingerprint") != \
                map2.get("artifact_fingerprint"):
            allowed_stale_stages.update({"STAGE3", "REVIEW"})
        if map2.get("scenario_ac_map_fingerprint") != \
                map1.get("artifact_fingerprint"):
            allowed_stale_stages.update({"STAGE2", "STAGE3", "REVIEW"})
        return cls(
            job_root=root, job_id=job_id,
            input_fingerprint=checkpoint.get("input_fingerprint", ""),
            spec_fingerprint=request.get("spec_fingerprint", ""),
            artifact_roots=expected_roots, index_paths=index_paths,
            review_report_path=checkpoint["review_report_path"],
            review_request_path=checkpoint["review_request_path"],
            prior_review_artifact_roots=prior_roots,
            allowed_stale_stages=allowed_stale_stages,
            budget_status=budget_status)

    def _load_current_snapshot(self) -> None:
        if (self.job_root.name != self.job_id or
                not self.job_root.is_dir() or self.job_root.is_symlink()):
            raise ProjectToolError(
                "CROSS_JOB_ARTIFACT", "read model Job root is not exact")
        if (not isinstance(self.input_fingerprint, str) or
                len(self.input_fingerprint) != 64 or
                not isinstance(self.spec_fingerprint, str) or
                len(self.spec_fingerprint) != 64):
            raise ProjectToolError(
                "STALE_EVIDENCE", "read model authority fingerprints are invalid")
        for name, relative in sorted(self.index_paths.items()):
            try:
                index, units = load_index(
                    self.job_root, relative, ProjectToolError,
                    expected_job_id=self.job_id)
            except ProjectToolError:
                raise
            except Exception as error:
                raise ProjectToolError(
                    "STALE_EVIDENCE", "current unit index is invalid") from error
            if (index["input_fingerprint"] != self.input_fingerprint or
                    index["spec_fingerprint"] != self.spec_fingerprint):
                raise ProjectToolError(
                    "STALE_EVIDENCE", "current unit index authority is stale")
            self.indexes[name] = index
            for unit_id, unit in units.items():
                if unit_id in self.units:
                    raise ProjectToolError(
                        "STALE_EVIDENCE", "current unit identity collides")
                self.units[unit_id] = unit
                self._units_by_lineage[(unit["unit_kind"], unit_id)] = unit
        authority = None
        for index in self.indexes.values():
            current = {
                key: index[key] for key in (
                    "job_id", "input_fingerprint", "spec_fingerprint",
                    "policy_fingerprint", "owner_scope_fingerprint")}
            if authority is None:
                authority = current
            elif current != authority:
                raise ProjectToolError(
                    "STALE_EVIDENCE", "current unit indexes disagree on authority")
        self.policy_fingerprint = authority["policy_fingerprint"]
        self.owner_scope_fingerprint = authority["owner_scope_fingerprint"]
        self.report = self._load_exact_json(
            self.review_report_path, "project_testcase_review_report",
            "report_fingerprint")
        self.review_request = self._load_exact_json(
            self.review_request_path, "project_testcase_review_request",
            "request_fingerprint")
        if (self.report["job_id"] != self.job_id or
                self.review_request["job_id"] != self.job_id or
                self.report["input_fingerprint"] != self.input_fingerprint or
                self.review_request["input_fingerprint"] !=
                    self.input_fingerprint or
                self.review_request["spec_fingerprint"] !=
                    self.spec_fingerprint or
                self.report["artifact_roots"] != (
                    self.prior_review_artifact_roots or self.artifact_roots) or
                self.review_request["artifact_roots"] != (
                    self.prior_review_artifact_roots or self.artifact_roots)):
            raise ProjectToolError(
                "CROSS_JOB_ARTIFACT", "Reviewer snapshot authority is not exact")
        self._validate_dependency_graph(
            allow_stale_stages=self.allowed_stale_stages)
        self.spec_documents = self._load_spec_documents()
        self.evidence = self._collect_evidence()
        self.history = self._load_repair_history()
        self.artifact_root = canonical_hash({
            "job_id": self.job_id,
            "input_fingerprint": self.input_fingerprint,
            "spec_fingerprint": self.spec_fingerprint,
            "artifact_roots": self.artifact_roots,
            "unit_roots": {
                name: index["root_fingerprint"]
                for name, index in sorted(self.indexes.items())},
            "review_report_fingerprint": self.report["report_fingerprint"],
        })

    def _load_exact_json(
            self, relative: str, contract: str, fingerprint_field: str
            ) -> dict[str, Any]:
        path = _safe_relative(self.job_root, relative)
        if not path.is_file() or path.is_symlink():
            raise ProjectToolError("STALE_EVIDENCE", "snapshot file is missing")
        try:
            value = load_document(path)
        except Exception as error:
            raise ProjectToolError(
                "STALE_EVIDENCE", "snapshot file is malformed") from error
        if (not accepted(validate(contract, value)) or
                value.get(fingerprint_field) !=
                    _artifact_fingerprint(value, fingerprint_field)):
            raise ProjectToolError(
                "STALE_EVIDENCE", "snapshot artifact fingerprint is stale")
        return value

    def _validate_dependency_graph(
            self, *, allow_stale_stages: set[str] | None = None) -> None:
        allowed = allow_stale_stages or set()
        for unit in self.units.values():
            if unit.get("stage") in allowed:
                continue
            for dependency in unit["dependency_fingerprints"]:
                target = self._units_by_lineage.get((
                    dependency["kind"], dependency["identity"]))
                if (target is not None and dependency["fingerprint"] !=
                        _lineage_fingerprint(target)):
                    raise ProjectToolError(
                        "STALE_EVIDENCE",
                        "current direct dependency fingerprint is stale")

    def _load_spec_documents(self) -> dict[str, dict[str, Any]]:
        documents = {}
        for item in self.review_request["spec_evidence"]:
            path = item["path"]
            if path in documents:
                raise ProjectToolError(
                    "STALE_EVIDENCE", "Spec document path collides")
            content = item["content"]
            if hashlib.sha256(content.encode("utf-8")).hexdigest() != \
                    item["fingerprint"]:
                raise ProjectToolError(
                    "STALE_EVIDENCE", "complete Spec document is stale")
            documents[path] = copy.deepcopy(item)
        if canonical_hash([{
                "path": item["path"], "fingerprint": item["fingerprint"]}
                for item in self.review_request["spec_evidence"]
                ]) != self.spec_fingerprint:
            raise ProjectToolError(
                "STALE_EVIDENCE", "complete Spec set fingerprint is stale")
        return documents

    @staticmethod
    def _evidence_key(item: Mapping[str, Any]) -> tuple[str, int, int, str]:
        return (
            str(item.get("path", "")), int(item.get("line_start", 0)),
            int(item.get("line_end", 0)),
            str(item.get("snippet_fingerprint", "")),
        )

    def _validate_spec_evidence(self, item: Mapping[str, Any]) -> dict[str, Any]:
        key = self._evidence_key(item)
        path, start, end, fingerprint = key
        document = self.spec_documents.get(path)
        if document is None or start < 1 or end < start:
            raise ProjectToolError(
                "STALE_EVIDENCE", "Spec evidence reference is stale")
        lines = document["content"].splitlines()
        if end > len(lines):
            raise ProjectToolError(
                "STALE_EVIDENCE", "Spec evidence range exceeds baseline")
        snippet = "\n".join(lines[start - 1:end])
        if (hashlib.sha256(snippet.encode("utf-8")).hexdigest() != fingerprint or
                ("snippet" in item and item["snippet"] != snippet)):
            raise ProjectToolError(
                "STALE_EVIDENCE", "Spec evidence content is stale")
        return {
            "path": path, "line_start": start, "line_end": end,
            "snippet": snippet, "snippet_fingerprint": fingerprint,
            "source_fingerprint": document["fingerprint"],
        }

    def _collect_evidence(self) -> dict[tuple[str, int, int, str], dict[str, Any]]:
        candidates = []
        for unit in self.units.values():
            candidates.extend(unit.get("spec_evidence", []))
        for finding in self.report["findings"]:
            candidates.extend(finding["spec_evidence"])
        for review in self.report["ac_reviews"]:
            candidates.extend(review["spec_evidence"])
        result = {}
        for candidate in candidates:
            value = self._validate_spec_evidence(candidate)
            key = self._evidence_key(value)
            prior = result.get(key)
            if prior is not None and prior != value:
                raise ProjectToolError(
                    "STALE_EVIDENCE", "Spec evidence references conflict")
            result[key] = value
        return result

    def _load_repair_history(self) -> list[dict[str, Any]]:
        # Validate the append-only sequence/previous-record chain before any
        # record is made visible through a read tool.
        RepairRecordStore(
            self.job_root, job_id=self.job_id,
            input_fingerprint=self.input_fingerprint,
            spec_fingerprint=self.spec_fingerprint,
            policy_fingerprint=self.policy_fingerprint).records()
        plan_paths = {
            path.name.removeprefix("repair_plan.").removesuffix(".json"): path
            for path in self.job_root.glob(
                "staging/orchestrator/repair_plan.*.json")
        }
        receipt_paths = {
            path.name.removeprefix("router_receipt.").removesuffix(".json"): path
            for path in self.job_root.glob("audit/router_receipt.*.json")
            if not path.name.startswith("router_receipt.oches003.")
        }
        if set(plan_paths) != set(receipt_paths):
            raise ProjectToolError(
                "STALE_EVIDENCE",
                "repair plan and Router receipt history are not paired")

        records = []
        for token in sorted(plan_paths):
            records.extend(self._load_plan_receipt_history(
                plan_paths[token], receipt_paths[token]))

        patterns = (
            "staging/dispatch/*.json",
            "audit/job_regeneration_state.*.json",
            "staging/repair_records/**/*.json",
            "audit/repair_records/**/*.json",
        )
        by_path: dict[str, Path] = {}
        for pattern in patterns:
            for path in self.job_root.glob(pattern):
                relative = path.relative_to(self.job_root).as_posix()
                by_path[relative] = path
        contract_by_kind = {
            "FORMAL_DISPATCH": ("project_formal_dispatch", "dispatch_fingerprint"),
            "FAILURE_FEEDBACK": ("project_failure_feedback", "feedback_fingerprint"),
            "REGENERATION_STATE": (
                "project_job_regeneration_state", "state_fingerprint"),
        }
        for relative, path in sorted(by_path.items()):
            if not path.is_file() or path.is_symlink():
                raise ProjectToolError(
                    "STALE_EVIDENCE", "repair history path is invalid")
            try:
                value = load_document(path)
            except Exception as error:
                raise ProjectToolError(
                    "STALE_EVIDENCE", "repair history record is malformed") \
                    from error
            if "repair_dispatch." in relative:
                kind = "FORMAL_DISPATCH"
            elif "failure_feedback." in relative:
                kind = "FAILURE_FEEDBACK"
            elif "job_regeneration_state." in relative:
                kind = "REGENERATION_STATE"
            elif value.get("record_type") in RECORD_CONTRACTS:
                kind = value["record_type"]
            else:
                raise ProjectToolError(
                    "STALE_EVIDENCE",
                    "repair history contains an uncontracted record")
            if value.get("record_type") in RECORD_CONTRACTS:
                contract = RECORD_CONTRACTS[kind]
                valid_fingerprint = value.get(
                    "record_fingerprint") == record_fingerprint(value)
                field = "record_fingerprint"
            else:
                contract, field = contract_by_kind[kind]
                valid_fingerprint = value.get(field) == \
                    _artifact_fingerprint(value, field)
            if (not accepted(validate(contract, value)) or
                    value.get("job_id") != self.job_id or
                    not valid_fingerprint):
                raise ProjectToolError(
                    "STALE_EVIDENCE", "authoritative repair record is stale")
            records.append({
                "path": relative, "record_type": kind,
                "record_fingerprint": value[field], "record": value,
            })
        records.sort(key=lambda item: (
            item["record"].get("sequence", 0), item["path"]))
        for sequence, record in enumerate(records, 1):
            record["sequence"] = sequence
        return records

    def _load_plan_receipt_history(
            self, plan_path: Path, receipt_path: Path
            ) -> list[dict[str, Any]]:
        for path in (plan_path, receipt_path):
            if not path.is_file() or path.is_symlink():
                raise ProjectToolError(
                    "STALE_EVIDENCE", "repair history path is invalid")
        try:
            plan = load_document(plan_path)
            receipt = load_document(receipt_path)
        except Exception as error:
            raise ProjectToolError(
                "STALE_EVIDENCE", "repair history record is malformed") \
                from error
        if (not accepted(validate("project_repair_plan", plan)) or
                plan.get("job_id") != self.job_id or
                not accepted(validate("project_router_receipt", receipt)) or
                receipt.get("receipt_fingerprint") !=
                    _artifact_fingerprint(receipt, "receipt_fingerprint") or
                receipt.get("job_id") != self.job_id or
                receipt.get("plan_id") != plan.get("plan_id") or
                receipt.get("plan_fingerprint") !=
                    plan.get("plan_fingerprint")):
            raise ProjectToolError(
                "STALE_EVIDENCE",
                "repair plan and Router receipt lineage is stale")

        plan_relative = plan_path.relative_to(self.job_root).as_posix()
        receipt_relative = receipt_path.relative_to(self.job_root).as_posix()
        receipt_record = {
            "path": receipt_relative,
            "record_type": "ROUTER_RECEIPT",
            "record_fingerprint": receipt["receipt_fingerprint"],
            "record": receipt,
        }
        if receipt["status"] == "ACCEPTED":
            if (receipt["diagnostic"]["code"] != "ACCEPTED" or
                    receipt["formal_dispatch_id"] == "NONE" or
                    plan.get("plan_fingerprint") !=
                        _artifact_fingerprint(plan, "plan_fingerprint")):
                raise ProjectToolError(
                    "STALE_EVIDENCE", "accepted repair plan authority is stale")
            return [{
                "path": plan_relative,
                "record_type": "REPAIR_PLAN",
                "record_fingerprint": plan["plan_fingerprint"],
                "authority_status": "ACCEPTED_FORMAL_PLAN",
                "record": plan,
            }, receipt_record]

        diagnostic = receipt["diagnostic"]["code"]
        if (receipt["formal_dispatch_id"] != "NONE" or
                diagnostic not in REJECTED_PLAN_DIAGNOSTICS):
            raise ProjectToolError(
                "STALE_EVIDENCE", "rejected repair diagnostic is unexpected")
        transcript = self._validate_rejected_plan_transcript(plan, receipt)
        return [{
            "path": plan_relative,
            "record_type": "REJECTED_REPAIR_PLAN",
            "record_fingerprint": canonical_hash(plan),
            "authority_status": "REJECTED_UNTRUSTED_PLAN",
            "rejection_evidence": {
                "router_receipt_path": receipt_relative,
                "router_receipt_fingerprint": receipt["receipt_fingerprint"],
                **transcript,
            },
            "record": plan,
        }, receipt_record]

    def _validate_rejected_plan_transcript(
            self, plan: Mapping[str, Any], receipt: Mapping[str, Any]
            ) -> dict[str, str]:
        session_id = plan.get("planning_session_id")
        if not isinstance(session_id, str) or not re.fullmatch(
                r"PLANNING\.[A-Z0-9_.-]+", session_id):
            raise ProjectToolError(
                "STALE_EVIDENCE", "rejected plan session identity is invalid")
        relative = "transcripts/orchestrator/{}/manifest.json".format(
            session_id)
        manifest_path = _safe_relative(self.job_root, relative)
        try:
            if not manifest_path.is_file() or manifest_path.is_symlink():
                raise OSError("transcript manifest is unavailable")
            manifest = load_document(manifest_path)
        except Exception as error:
            raise ProjectToolError(
                "STALE_EVIDENCE",
                "rejected plan transcript is unavailable or malformed") \
                from error
        if (not accepted(validate("project_transcript_manifest", manifest)) or
                manifest.get("job_id") != self.job_id or
                manifest.get("role") != "ORCHESTRATOR" or
                manifest.get("session_id") != session_id or
                manifest.get("terminal", {}).get("status") != "COMPLETED"):
            raise ProjectToolError(
                "STALE_EVIDENCE", "rejected plan transcript lineage is stale")

        values = []
        session_root = manifest_path.parent
        for sequence, entry in enumerate(manifest["entries"], 1):
            expected_name = "{:04d}.{}.json".format(
                sequence, entry["kind"].lower())
            event_path = session_root / entry["path"]
            try:
                if (entry["path"] != expected_name or
                        not event_path.is_file() or event_path.is_symlink() or
                        hashlib.sha256(event_path.read_bytes()).hexdigest() !=
                            entry["content_fingerprint"]):
                    raise OSError("transcript event is stale")
                values.append(load_document(event_path))
            except Exception as error:
                raise ProjectToolError(
                    "STALE_EVIDENCE", "rejected plan transcript event is stale") \
                    from error

        result_sequence = manifest["terminal"].get("result_sequence")
        if (not isinstance(result_sequence, int) or result_sequence < 2 or
                result_sequence != len(values) or
                manifest["entries"][result_sequence - 2]["kind"] !=
                    "TOOL_CALL" or
                manifest["entries"][result_sequence - 1]["kind"] !=
                    "TOOL_RESULT"):
            raise ProjectToolError(
                "STALE_EVIDENCE",
                "rejected plan transcript terminal sequence is invalid")
        call = values[result_sequence - 2]
        result = values[result_sequence - 1]
        if (call.get("name") != "submit_repair_plan" or result != {
                    "status": "REJECTED",
                    "receipt": receipt,
                    "dispatch": None,
                }):
            raise ProjectToolError(
                "STALE_EVIDENCE",
                "rejected plan transcript does not bind exact evidence")
        arguments = call.get("arguments")
        binding_kind = "LEGACY_FORMAL_PLAN"
        if arguments != plan:
            semantic_candidate = {
                "schema_version": "1.0",
                "status": plan.get("status"),
                "repairs": plan.get("repairs"),
            }
            if (arguments != semantic_candidate or result_sequence < 4 or
                    manifest["entries"][result_sequence - 4]["kind"] !=
                        "REQUEST" or
                    manifest["entries"][result_sequence - 3]["kind"] !=
                        "RESPONSE"):
                raise ProjectToolError(
                    "STALE_EVIDENCE",
                    "rejected plan transcript submission is not exact")
            request = values[result_sequence - 4]
            response = values[result_sequence - 3]
            orchestrator = plan.get("orchestrator", {})
            provider = response.get("provider_metadata", {})
            if (request.get("request_id") !=
                    orchestrator.get("request_id") or
                    response.get("request_id") != request.get("request_id") or
                    provider.get("response_id") !=
                        orchestrator.get("response_id") or
                    provider.get("provider_id") !=
                        orchestrator.get("provider_id") or
                    response.get("model_id") != orchestrator.get("model_id")):
                raise ProjectToolError(
                    "STALE_EVIDENCE",
                    "rejected formal plan session lineage is stale")
            binding_kind = "SEMANTIC_CANDIDATE_FORMALIZATION"
        return {
            "transcript_manifest_path": relative,
            "transcript_manifest_fingerprint":
                manifest["manifest_fingerprint"],
            "transcript_binding_kind": binding_kind,
        }

    def handlers(
            self, allowlist: Iterable[str] = READ_TOOL_NAMES
            ) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        names = sorted(set(allowlist))
        if set(names) - set(READ_TOOL_NAMES):
            raise ProjectToolError(
                "TOOL_PERMISSION_DENIED", "read tool allow-list is invalid")
        return {name: (lambda arguments, selected=name:
                       self.call(selected, arguments)) for name in names}

    def call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name not in READ_TOOL_NAMES:
            raise ProjectToolError(
                "TOOL_PERMISSION_DENIED", "read tool is not registered")
        errors = validate_schema(
            arguments, _READ_TOOL_INPUT_SCHEMAS[tool_name], "arguments")
        if errors:
            raise ProjectToolError(
                "INVALID_TOOL_CALL", "invalid {} arguments: {}".format(
                    tool_name, "; ".join(errors)))
        method = getattr(self, "_" + tool_name)
        items, diagnostics = method(copy.deepcopy(arguments))
        result = {
            "schema_version": "1.0", "tool_name": tool_name,
            "job_id": self.job_id, "artifact_root": self.artifact_root,
            "items": items, "diagnostics": diagnostics,
            "result_fingerprint": "0" * 64,
        }
        result["result_fingerprint"] = _artifact_fingerprint(
            result, "result_fingerprint")
        if not accepted(validate("project_read_tool_result", result)):
            raise ProjectToolError(
                "INVALID_SCHEMA", "read tool generated an invalid result")
        return result

    @staticmethod
    def _not_found(item_key: str, message: str) -> dict[str, str]:
        return {"code": "NOT_FOUND", "item_key": item_key,
                "message": message}

    @staticmethod
    def _strings(arguments: dict[str, Any], field: str) -> list[str]:
        return sorted(arguments[field])

    def _get_issue(self, arguments):
        findings = {item["issue_id"]: item for item in self.report["findings"]}
        items, diagnostics = [], []
        for issue_id in self._strings(arguments, "issue_ids"):
            finding = findings.get(issue_id)
            if finding is None:
                diagnostics.append(self._not_found(
                    issue_id, "current Reviewer finding was not found"))
                continue
            items.append({
                "issue_id": issue_id,
                "finding": copy.deepcopy(finding),
                "report_id": self.report["report_id"],
                "report_fingerprint": self.report["report_fingerprint"],
                "artifact_roots": copy.deepcopy(self.artifact_roots),
            })
        return items, diagnostics

    def _get_unit(self, arguments):
        items, diagnostics = [], []
        for unit_id in self._strings(arguments, "unit_ids"):
            unit = self.units.get(unit_id)
            if unit is None:
                diagnostics.append(self._not_found(
                    unit_id, "current semantic unit was not found"))
            else:
                items.append(copy.deepcopy(unit))
        return items, diagnostics

    def _dependency_item(self, dependency: dict[str, Any]) -> dict[str, Any]:
        target = self._units_by_lineage.get((
            dependency["kind"], dependency["identity"]))
        return {
            **copy.deepcopy(dependency),
            "revision": target["revision"] if target is not None else None,
            "artifact_fingerprint": (
                target["artifact_fingerprint"] if target is not None else None),
        }

    def _get_direct_dependencies(self, arguments):
        items, diagnostics = [], []
        for unit_id in self._strings(arguments, "unit_ids"):
            unit = self.units.get(unit_id)
            if unit is None:
                diagnostics.append(self._not_found(
                    unit_id, "current semantic unit was not found"))
                continue
            dependencies = sorted(
                [self._dependency_item(item)
                 for item in unit["dependency_fingerprints"]],
                key=lambda item: (item["kind"], item["identity"]))
            items.append({
                "unit_id": unit_id, "unit_kind": unit["unit_kind"],
                "revision": unit["revision"],
                "artifact_fingerprint": unit["artifact_fingerprint"],
                "dependencies": dependencies,
            })
        return items, diagnostics

    def _reverse_dependencies(self) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {}
        for unit in self.units.values():
            for dependency in unit["dependency_fingerprints"]:
                target = self._units_by_lineage.get((
                    dependency["kind"], dependency["identity"]))
                if target is not None:
                    result.setdefault(target["unit_id"], []).append(unit)
        for values in result.values():
            values.sort(key=lambda item: (item["unit_kind"], item["unit_id"]))
        return result

    def _get_dependents(self, arguments):
        reverse = self._reverse_dependencies()
        items, diagnostics = [], []
        for unit_id in self._strings(arguments, "unit_ids"):
            unit = self.units.get(unit_id)
            if unit is None:
                diagnostics.append(self._not_found(
                    unit_id, "current semantic unit was not found"))
                continue
            items.append({
                "unit_id": unit_id, "unit_kind": unit["unit_kind"],
                "revision": unit["revision"],
                "artifact_fingerprint": unit["artifact_fingerprint"],
                "dependents": [{
                    "unit_id": item["unit_id"],
                    "unit_kind": item["unit_kind"],
                    "revision": item["revision"],
                    "content_fingerprint": item["content_fingerprint"],
                    "dependency_fingerprint": item["dependency_fingerprint"],
                    "artifact_fingerprint": item["artifact_fingerprint"],
                } for item in reverse.get(unit_id, [])],
            })
        return items, diagnostics

    def _get_spec_evidence(self, arguments):
        refs = sorted(arguments["evidence_refs"], key=self._evidence_key)
        items, diagnostics = [], []
        for reference in refs:
            key = self._evidence_key(reference)
            value = self.evidence.get(key)
            item_key = "{}:{}:{}:{}".format(*key)
            if value is None:
                diagnostics.append(self._not_found(
                    item_key, "authorized current Spec evidence was not found"))
            else:
                items.append(copy.deepcopy(value))
        return items, diagnostics

    def _get_repair_history(self, arguments):
        items, diagnostics = [], []
        for identity in self._strings(arguments, "identities"):
            matches = [record for record in self.history
                       if _contains_identity(record["record"], identity)]
            if not matches:
                diagnostics.append(self._not_found(
                    identity, "matching repair history was not found"))
                continue
            for match in matches:
                item = copy.deepcopy(match)
                item["matched_identity"] = identity
                items.append(item)
        items.sort(key=lambda item: (
            item["sequence"], item["matched_identity"], item["path"]))
        return items, diagnostics

    def _revision_unit(self, unit_id: str, revision: int) -> dict[str, Any] | None:
        matches = []
        pattern = "staging/units/*/index.current.r{:03d}.json".format(revision)
        for path in sorted(self.job_root.glob(pattern)):
            relative = path.relative_to(self.job_root).as_posix()
            index, units = load_index(
                self.job_root, relative, ProjectToolError,
                expected_job_id=self.job_id)
            if (index["input_fingerprint"] != self.input_fingerprint or
                    index["spec_fingerprint"] != self.spec_fingerprint or
                    index["policy_fingerprint"] != self.policy_fingerprint or
                    index["owner_scope_fingerprint"] !=
                        self.owner_scope_fingerprint):
                raise ProjectToolError(
                    "STALE_EVIDENCE", "historical unit authority is stale")
            if unit_id in units:
                matches.append(units[unit_id])
        if len(matches) > 1:
            raise ProjectToolError(
                "STALE_EVIDENCE", "historical unit identity collides")
        return matches[0] if matches else None

    def _compare_unit_revisions(self, arguments):
        comparisons = sorted(arguments["comparisons"], key=lambda item: (
            item["unit_id"], item["from_revision"], item["to_revision"]))
        items, diagnostics = [], []
        for comparison in comparisons:
            unit_id = comparison["unit_id"]
            before = self._revision_unit(unit_id, comparison["from_revision"])
            after = self._revision_unit(unit_id, comparison["to_revision"])
            key = "{}:{}:{}".format(
                unit_id, comparison["from_revision"], comparison["to_revision"])
            if before is None or after is None:
                diagnostics.append(self._not_found(
                    key, "one or both exact unit revisions were not found"))
                continue
            items.append({
                **copy.deepcopy(comparison),
                "from_unit": copy.deepcopy(before),
                "to_unit": copy.deepcopy(after),
                "changes": _json_diff(before, after),
            })
        return items, diagnostics

    def _estimate_repair_impact(self, arguments):
        targets = self._strings(arguments, "target_ids")
        diagnostics = [self._not_found(
            target, "current repair target was not found")
            for target in targets if target not in self.units]
        valid = {target for target in targets if target in self.units}
        reverse = self._reverse_dependencies()
        dirty = set(valid)
        pending = list(sorted(valid))
        while pending:
            current = pending.pop(0)
            for dependent in reverse.get(current, []):
                identity = dependent["unit_id"]
                if identity not in dirty:
                    dirty.add(identity)
                    pending.append(identity)
        item = {
            "target_ids": sorted(valid),
            "dirty_units": [{
                "unit_id": identity,
                "unit_kind": self.units[identity]["unit_kind"],
                "revision": self.units[identity]["revision"],
                "artifact_fingerprint":
                    self.units[identity]["artifact_fingerprint"],
            } for identity in sorted(dirty)],
            "reused_units": [{
                "unit_id": identity,
                "unit_kind": self.units[identity]["unit_kind"],
                "revision": self.units[identity]["revision"],
                "artifact_fingerprint":
                    self.units[identity]["artifact_fingerprint"],
            } for identity in sorted(set(self.units) - dirty)],
            "artifact_roots": copy.deepcopy(self.artifact_roots),
            "unit_roots": {
                name: index["root_fingerprint"]
                for name, index in sorted(self.indexes.items())},
        }
        return [item], diagnostics

    def _get_budget_status(self, arguments):
        raw = self._budget_status() if callable(self._budget_status) \
            else self._budget_status
        allowed = (
            "calls_used", "calls_remaining", "elapsed_seconds",
            "sessions_used", "sessions_remaining")
        item = {key: copy.deepcopy(raw[key]) for key in allowed if key in raw}
        item["token_capacity_mode"] = "NOT_MODELED_IN_OCHES002"
        return [item], []


__all__ = [
    "ORCHESTRATOR_READ_TOOLS", "ProjectReadModel", "ProjectToolError",
    "READ_TOOL_NAMES", "STAGE_READ_TOOLS", "read_tool_definitions",
]
