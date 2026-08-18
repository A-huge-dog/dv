"""OCHES002 scope-rich dispatch and immutable-identity replacement checks."""
from __future__ import annotations

import copy
import json
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from contracts.validator import accepted, validate
from core.project_tools import ProjectReadModel, STAGE_READ_TOOLS
from scripts.dvlib import canonical_hash


_STAGE_CONTRACTS = {
    "STAGE_1": "project_stage1_replacement",
    "STAGE_2": "project_stage2_replacement",
    "STAGE_3": "project_stage3_replacement",
}
_STAGE_CANDIDATE_CONTRACTS = {
    "STAGE_1": "project_stage1_replacement_candidate",
    "STAGE_2": "project_stage2_replacement_candidate",
    "STAGE_3": "project_stage3_replacement_candidate",
}
_ACTUAL_TARGET_KIND = {
    "SCENARIO": "SCENARIO",
    "ACCEPTANCE_CRITERION": "ACCEPTANCE_CRITERION",
    "LOGICAL_TESTCASE": "TESTCASE",
    "CODE_SHARED": "CODE_UNIT",
    "CODE_TESTCASE": "CODE_UNIT",
}
_STAGE_UNIT_KINDS = {
    "STAGE_1": {"SCENARIO", "ACCEPTANCE_CRITERION"},
    "STAGE_2": {"LOGICAL_TESTCASE"},
    "STAGE_3": {"CODE_SHARED", "CODE_TESTCASE"},
}
_STAGE1_KEYS = {
    "SCENARIO": {"objective", "verification_level", "status", "reason"},
    "ACCEPTANCE_CRITERION": {
        "behavior", "verification_level", "status", "reason"},
}
_STAGE2_KEYS = {
    "objective", "preconditions", "stimulus", "transaction_sequence",
    "timing_intent", "checker", "expected_result", "failure_condition",
    "timeout_cycles", "status", "reason", "scenario_ids", "ac_ids",
}


def artifact_fingerprint(value: Mapping[str, Any], field: str) -> str:
    projected = copy.deepcopy(dict(value))
    projected.pop(field, None)
    return canonical_hash(projected)


def validate_orchestrator_binding(
        plan: Mapping[str, Any], expected_binding: Mapping[str, Any],
        error: Callable[[str, str], Exception]) -> None:
    expected = {
        "runtime_role": "ORCHESTRATOR",
        **copy.deepcopy(dict(expected_binding)),
    }
    actual = plan.get("orchestrator", {})
    if any(actual.get(key) != value for key, value in expected.items()):
        raise error(
            "INVALID_AGENT_BINDING",
            "Orchestrator must bind its resolved profile role")


def _dependency_matches(unit: Mapping[str, Any], kind: str,
                        identity: str) -> bool:
    return any(
        item["kind"] == kind and item["identity"] == identity
        for item in unit["dependency_fingerprints"])


def _resolve_target_units(
        stage: str, targets: list[dict[str, str]], model: ProjectReadModel,
        error: Callable[[str, str], Exception]) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for target in targets:
        kind, identity = target["kind"], target["id"]
        if stage == "STAGE_1":
            unit = model.units.get(identity)
            candidates = [unit] if unit is not None else []
        elif stage == "STAGE_2" and kind == "TESTCASE":
            unit = model.units.get(identity)
            candidates = [unit] if unit is not None else []
        elif stage == "STAGE_2" and kind == "ACCEPTANCE_CRITERION":
            candidates = [
                unit for unit in model.units.values()
                if unit["unit_kind"] == "LOGICAL_TESTCASE" and
                _dependency_matches(unit, "ACCEPTANCE_CRITERION", identity)]
        elif stage == "STAGE_3" and kind == "CODE_UNIT":
            unit = model.units.get(identity)
            candidates = [unit] if unit is not None else []
        elif stage == "STAGE_3" and kind == "TESTCASE":
            candidates = [
                unit for unit in model.units.values()
                if unit["unit_kind"] == "CODE_TESTCASE" and
                _dependency_matches(unit, "LOGICAL_TESTCASE", identity)]
        else:
            candidates = []
        candidates = [item for item in candidates if item is not None and
                      item["unit_kind"] in _STAGE_UNIT_KINDS[stage]]
        if not candidates:
            raise error(
                "EMPTY_REPAIR_SCOPE",
                "formal target resolves to no editable unit in its Stage")
        for unit in candidates:
            selected[unit["unit_id"]] = unit
    return [selected[key] for key in sorted(selected)]


def _evidence_ref(item: Mapping[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(item[key]) for key in (
        "path", "line_start", "line_end", "snippet_fingerprint")}


def _evidence_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(item[key] for key in (
        "path", "line_start", "line_end", "snippet_fingerprint"))


def _current_unit_roots(model: ProjectReadModel) -> dict[str, str]:
    return {
        name: model.indexes[name]["root_fingerprint"]
        for name in ("stage1", "stage2", "stage3", "review")}


def _replacement_id(
        stage: str, session_id: str, dispatch_id: str,
        request_id: str, response_id: str, response_fingerprint: str,
        replacements: list[dict[str, Any]]) -> str:
    token = canonical_hash({
        "stage": stage, "session_id": session_id,
        "dispatch_id": dispatch_id, "request_id": request_id,
        "response_id": response_id,
        "response_fingerprint": response_fingerprint,
        "replacements": replacements,
    })[:24].upper()
    return "REPLACEMENT.STAGE{}.{}".format(stage[-1], token)


def build_scoped_dispatch(
        base: Mapping[str, Any], model: ProjectReadModel,
        stage_binding: Mapping[str, Any],
        error: Callable[[str, str], Exception]) -> dict[str, Any]:
    """Resolve formal plan targets to exact current editable units."""
    if (base["job_id"] != model.job_id or
            base["input_fingerprint"] != model.input_fingerprint or
            base["spec_fingerprint"] != model.spec_fingerprint or
            base["policy_fingerprint"] != model.policy_fingerprint or
            base["owner_scope_fingerprint"] != model.owner_scope_fingerprint or
            base["artifact_roots"] != model.artifact_roots):
        raise error("STALE_EVIDENCE", "dispatch authority is not current")

    units = _resolve_target_units(
        base["stage"], base["targets"], model, error)
    target_units = [{
        "target_kind": _ACTUAL_TARGET_KIND[unit["unit_kind"]],
        "unit_id": unit["unit_id"], "unit_kind": unit["unit_kind"],
        "revision": unit["revision"],
        "repair_scope": "SHARED" if unit["unit_kind"] == "CODE_SHARED"
        else "LOCAL",
        "content_fingerprint": unit["content_fingerprint"],
        "dependency_fingerprint": unit["dependency_fingerprint"],
        "artifact_fingerprint": unit["artifact_fingerprint"],
    } for unit in units]
    dependencies = [{
        "target_id": unit["unit_id"],
        "dependencies": sorted([
            model._dependency_item(item)
            for item in unit["dependency_fingerprints"]],
            key=lambda item: (item["kind"], item["identity"])),
    } for unit in units]

    evidence: dict[tuple[Any, ...], dict[str, Any]] = {}
    evidence_units = {unit["unit_id"]: unit for unit in units}
    for unit in units:
        for dependency in unit["dependency_fingerprints"]:
            upstream = model._units_by_lineage.get((
                dependency["kind"], dependency["identity"]))
            if upstream is not None:
                evidence_units[upstream["unit_id"]] = upstream
    for unit in evidence_units.values():
        for item in unit["spec_evidence"]:
            ref = _evidence_ref(item)
            evidence[_evidence_key(ref)] = ref
    selected_issues = set(base["issue_ids"])
    for finding in model.report["findings"]:
        if finding["issue_id"] in selected_issues:
            for item in finding["spec_evidence"]:
                ref = _evidence_ref(item)
                evidence[_evidence_key(ref)] = ref
    authorized_evidence = [evidence[key] for key in sorted(evidence)]
    if authorized_evidence:
        result = model.call("get_spec_evidence", {
            "evidence_refs": authorized_evidence})
        if result["diagnostics"] or len(result["items"]) != len(
                authorized_evidence):
            raise error("STALE_EVIDENCE", "dispatch Spec evidence is stale")

    unit_roots = {
        name: model.indexes[name]["root_fingerprint"]
        for name in ("stage1", "stage2", "stage3", "review")}
    group_seed = {
        "stage": base["stage"],
        "target_ids": sorted(item["unit_id"] for item in target_units),
        "issue_ids": sorted(base["issue_ids"]),
        "policy_fingerprint": model.policy_fingerprint,
    }
    scope_seed = {
        "job_id": model.job_id,
        "input_fingerprint": model.input_fingerprint,
        "spec_fingerprint": model.spec_fingerprint,
        "policy_fingerprint": model.policy_fingerprint,
        "owner_scope_fingerprint": model.owner_scope_fingerprint,
        "artifact_roots": copy.deepcopy(model.artifact_roots),
        "unit_roots": unit_roots,
        "stage": base["stage"],
        "target_units": target_units,
        "direct_dependencies": dependencies,
        "authorized_spec_evidence": authorized_evidence,
        "issue_ids": sorted(base["issue_ids"]),
    }
    value = {
        "schema_version": "2.0",
        "dispatch_id": base["dispatch_id"],
        "group_id": "REPAIRGROUP.{}".format(
            canonical_hash(group_seed)[:16].upper()),
        "job_id": model.job_id,
        "input_fingerprint": model.input_fingerprint,
        "spec_fingerprint": model.spec_fingerprint,
        "policy_fingerprint": model.policy_fingerprint,
        "owner_scope_fingerprint": model.owner_scope_fingerprint,
        "plan_id": base["plan_id"],
        "plan_fingerprint": base["plan_fingerprint"],
        "source_report_fingerprint": base["source_report_fingerprint"],
        "regeneration_round": 1,
        "stage": base["stage"],
        "targets": copy.deepcopy(base["targets"]),
        "target_units": target_units,
        "issue_ids": sorted(base["issue_ids"]),
        "artifact_roots": copy.deepcopy(model.artifact_roots),
        "unit_roots": unit_roots,
        "direct_dependencies": dependencies,
        "authorized_spec_evidence": authorized_evidence,
        "tool_allow_list": sorted(STAGE_READ_TOOLS),
        "retrieval_call_limit": 3,
        "stage_agent": {
            "runtime_role": "STAGE_AGENT",
            **copy.deepcopy(dict(stage_binding)),
        },
        "scope_fingerprint": canonical_hash(scope_seed),
        "invalidated_stages": copy.deepcopy(base["invalidated_stages"]),
        "created_at": base["created_at"],
        "dispatch_fingerprint": "0" * 64,
    }
    value["dispatch_fingerprint"] = artifact_fingerprint(
        value, "dispatch_fingerprint")
    if not accepted(validate("project_formal_dispatch", value)):
        raise error("INVALID_SCHEMA", "Router generated invalid scoped dispatch")
    return value


def _same_json_shape(before: Any, after: Any) -> bool:
    if isinstance(before, dict):
        return isinstance(after, dict) and set(before) == set(after) and all(
            _same_json_shape(before[key], after[key]) for key in before)
    if isinstance(before, list):
        return isinstance(after, list) and (
            not before or all(
                _same_json_shape(before[0], item) for item in after))
    if isinstance(before, bool):
        return isinstance(after, bool)
    if isinstance(before, int):
        return isinstance(after, int) and not isinstance(after, bool)
    return isinstance(after, type(before))


def _validate_semantic_body(
        stage: str, unit: Mapping[str, Any], body: Any,
        error: Callable[[str, str], Exception]) -> None:
    expected_keys = (
        _STAGE1_KEYS[unit["unit_kind"]] if stage == "STAGE_1" else
        _STAGE2_KEYS if stage == "STAGE_2" else {"segments"})
    if (not isinstance(body, dict) or set(body) != expected_keys or
            not _same_json_shape(unit["semantic_body"], body)):
        raise error(
            "INVALID_REPLACEMENT_CONTENT",
            "replacement must preserve the exact semantic object shape")
    if stage == "STAGE_1":
        text_key = "objective" if unit["unit_kind"] == "SCENARIO" else "behavior"
        if (not body[text_key] or body["verification_level"] not in {
                "UNIT", "SUBSYSTEM", "SYSTEM", "OBSERVATION"} or
                body["status"] not in {
                "CHECKABLE", "OBSERVATION_ONLY", "BLOCKED_CONTRACT",
                "SPEC_AMBIGUITY"}):
            raise error("INVALID_REPLACEMENT_CONTENT", "Stage 1 content is invalid")
    elif stage == "STAGE_2":
        if (body["scenario_ids"] != unit["semantic_body"]["scenario_ids"] or
                body["ac_ids"] != unit["semantic_body"]["ac_ids"]):
            raise error(
                "IDENTITY_MUTATION",
                "testcase Scenario/AC relationships are immutable in replacement")
        if (not body["objective"] or not body["preconditions"] or
                not body["timing_intent"] or body["timeout_cycles"] < 1 or
                body["status"] not in {
                "PLANNED", "CHECKABLE", "OBSERVATION_ONLY",
                "BLOCKED_CONTRACT", "SPEC_AMBIGUITY"}):
            raise error("INVALID_REPLACEMENT_CONTENT", "Stage 2 content is invalid")
    elif not body["segments"] or len(body["segments"]) != 1 or not body[
            "segments"][0]:
        raise error("INVALID_REPLACEMENT_CONTENT", "Stage 3 content is invalid")


def validate_current_dispatch_authority(
        dispatch: Mapping[str, Any], model: ProjectReadModel,
        expected_stage_binding: Mapping[str, Any],
        error: Callable[[str, str], Exception], *,
        plan: Mapping[str, Any], receipt: Mapping[str, Any],
        checkpoint: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild and verify the exact current dispatch authority graph."""
    if (not accepted(validate("project_formal_dispatch", dispatch)) or
            dispatch.get("dispatch_fingerprint") !=
                artifact_fingerprint(dispatch, "dispatch_fingerprint")):
        raise error("STALE_DISPATCH", "formal dispatch is invalid or stale")
    if (not accepted(validate("project_repair_plan", plan)) or
            plan.get("plan_fingerprint") !=
                artifact_fingerprint(plan, "plan_fingerprint")):
        raise error("STALE_DISPATCH", "dispatch repair plan is invalid or stale")
    if (not accepted(validate("project_router_receipt", receipt)) or
            receipt.get("receipt_fingerprint") !=
                artifact_fingerprint(receipt, "receipt_fingerprint")):
        raise error("STALE_DISPATCH", "dispatch Router receipt is invalid")
    if (checkpoint.get("state") not in {
                "AWAITING_SCOPED_REPLACEMENT",
                "SCOPED_REPLACEMENT_VALIDATED"} or
            checkpoint.get("checkpoint_fingerprint") !=
                artifact_fingerprint(checkpoint, "checkpoint_fingerprint")):
        raise error("STALE_DISPATCH", "dispatch checkpoint is invalid")

    stage = dispatch["stage"]
    expected_binding = {
        "runtime_role": "STAGE_AGENT",
        **copy.deepcopy(dict(expected_stage_binding)),
    }
    if dispatch.get("stage_agent") != expected_binding:
        raise error(
            "INVALID_AGENT_BINDING",
            "dispatch Stage Agent differs from the current Job binding")

    report = model.report
    repairable = {
        item["issue_id"] for item in report["findings"]
        if item["severity"] == "ERROR" and
        item["suspected_origin_stage"] != "SPEC"}
    from core.project_oches003 import canonical_repair_groups
    plan_issues = [issue_id for repair in plan.get("repairs", [])
                   for issue_id in repair["issue_ids"]]
    groups = canonical_repair_groups(
        plan.get("repairs", []), policy_fingerprint=model.policy_fingerprint,
        units=model.units)
    completed_groups = set(checkpoint.get("completed_group_ids", []))
    planned_groups = checkpoint.get("canonical_groups", groups)
    expected = next((group for group in planned_groups
                     if group["group_id"] not in completed_groups), None)
    current_group = next((group for group in groups
                          if expected is not None and
                          group["group_id"] == expected["group_id"]), None)
    if expected is not None and current_group != expected:
        raise error(
            "REPLAN_REQUIRED",
            "current targets or dependencies changed canonical grouping")
    expected_stage = expected["stage"] if expected else None
    expected_issues = expected["issue_ids"] if expected else []
    expected_targets = expected["targets"] if expected else []
    if (set(plan_issues) != repairable or
            len(plan_issues) != len(set(plan_issues)) or
            stage != expected_stage or dispatch["targets"] != expected_targets or
            dispatch["issue_ids"] != expected_issues):
        raise error(
            "SCOPE_EXPANSION",
            "dispatch scope differs from the accepted current repair plan")

    if (dispatch["job_id"] != model.job_id or
            dispatch["input_fingerprint"] != model.input_fingerprint or
            dispatch["spec_fingerprint"] != model.spec_fingerprint or
            dispatch["policy_fingerprint"] != model.policy_fingerprint or
            dispatch["owner_scope_fingerprint"] !=
                model.owner_scope_fingerprint or
            dispatch["artifact_roots"] != model.artifact_roots or
            dispatch["unit_roots"] != _current_unit_roots(model) or
            dispatch["source_report_fingerprint"] !=
                report["report_fingerprint"] or
            dispatch["plan_id"] != plan["plan_id"] or
            dispatch["plan_fingerprint"] != plan["plan_fingerprint"]):
        raise error(
            "STALE_DISPATCH", "formal dispatch no longer binds current authority")
    if (receipt.get("job_id") != model.job_id or
            receipt.get("status") != "ACCEPTED" or
            receipt.get("formal_dispatch_id") != dispatch["dispatch_id"] or
            receipt.get("plan_id") != plan["plan_id"] or
            receipt.get("plan_fingerprint") != plan["plan_fingerprint"]):
        raise error(
            "STALE_DISPATCH", "Router receipt does not authorize this dispatch")
    if (checkpoint.get("job_id") != model.job_id or
            checkpoint.get("input_fingerprint") != model.input_fingerprint or
            checkpoint.get("dispatch_fingerprint") !=
                dispatch["dispatch_fingerprint"] or
            checkpoint.get("scope_fingerprint") !=
                dispatch["scope_fingerprint"]):
        raise error(
            "STALE_DISPATCH", "checkpoint does not authorize this dispatch")

    base = {key: copy.deepcopy(dispatch[key]) for key in (
        "dispatch_id", "job_id", "input_fingerprint", "spec_fingerprint",
        "policy_fingerprint", "owner_scope_fingerprint", "plan_id",
        "plan_fingerprint", "source_report_fingerprint",
        "regeneration_round", "stage", "targets", "issue_ids",
        "artifact_roots", "invalidated_stages", "created_at")}
    rebuilt = build_scoped_dispatch(
        base, model, expected_stage_binding, error)
    if rebuilt != dict(dispatch):
        raise error(
            "SCOPE_EXPANSION",
            "dispatch content differs from deterministic current scope")
    return copy.deepcopy(dict(dispatch))


def formalize_scoped_replacement(
        candidate: Mapping[str, Any], dispatch: Mapping[str, Any],
        model: ProjectReadModel, session_id: str,
        request: Mapping[str, Any], response: Mapping[str, Any],
        error: Callable[[str, str], Exception]) -> dict[str, Any]:
    """Turn semantic-only Agent output into one formal replacement."""
    stage = dispatch.get("stage")
    candidate_contract = _STAGE_CANDIDATE_CONTRACTS.get(stage)
    if (candidate_contract is None or
            not accepted(validate(candidate_contract, candidate))):
        raise error(
            "INVALID_REPLACEMENT_CONTENT",
            "Stage Agent must submit only unit_id and semantic_body")
    if (request.get("metadata", {}).get("session_id") != session_id or
            request.get("metadata", {}).get("job_id") != model.job_id or
            request.get("metadata", {}).get("role") != stage or
            response.get("request_id") != request.get("request_id") or
            response.get("provider_metadata", {}).get("provider_id") !=
                dispatch["stage_agent"]["provider_id"] or
            response.get("model_id") != dispatch["stage_agent"]["model_id"]):
        raise error(
            "INVALID_AGENT_BINDING",
            "submission turn does not bind the current Stage session")
    response_id = response.get("provider_metadata", {}).get("response_id")
    if not isinstance(response_id, str) or not response_id:
        raise error(
            "INVALID_AGENT_BINDING", "submission response identity is missing")

    expected = {item["unit_id"]: item for item in dispatch["target_units"]}
    submitted = candidate["replacements"]
    submitted_ids = [item["unit_id"] for item in submitted]
    if (len(submitted_ids) != len(set(submitted_ids)) or
            set(submitted_ids) != set(expected)):
        raise error(
            "IDENTITY_MUTATION",
            "semantic candidate IDs must exactly equal dispatched units")
    replacements = []
    by_id = {item["unit_id"]: item for item in submitted}
    for unit_id in sorted(by_id):
        authority = expected[unit_id]
        unit = model.units.get(unit_id)
        if (unit is None or
                unit["artifact_fingerprint"] !=
                    authority["artifact_fingerprint"]):
            raise error("STALE_DISPATCH", "target unit is no longer current")
        body = copy.deepcopy(by_id[unit_id]["semantic_body"])
        _validate_semantic_body(stage, unit, body, error)
        replacements.append({
            "unit_id": unit_id,
            "unit_kind": unit["unit_kind"],
            "base_revision": unit["revision"],
            "base_artifact_fingerprint": unit["artifact_fingerprint"],
            "semantic_body": body,
            "spec_evidence": copy.deepcopy(unit["spec_evidence"]),
        })
    response_fingerprint = canonical_hash(response)
    request_id = str(request["request_id"])
    value = {
        "schema_version": "1.0",
        "replacement_id": _replacement_id(
            str(stage), session_id, dispatch["dispatch_id"], request_id,
            response_id, response_fingerprint, replacements),
        "session_id": session_id,
        "dispatch_id": dispatch["dispatch_id"],
        "dispatch_fingerprint": dispatch["dispatch_fingerprint"],
        "job_id": dispatch["job_id"],
        "stage": stage,
        "base_artifact_roots": copy.deepcopy(dispatch["artifact_roots"]),
        "base_unit_roots": copy.deepcopy(dispatch["unit_roots"]),
        "scope_fingerprint": dispatch["scope_fingerprint"],
        "stage_agent": {
            **copy.deepcopy(dispatch["stage_agent"]),
            "request_id": request_id,
            "response_id": response_id,
        },
        "replacements": replacements,
        "response_fingerprint": response_fingerprint,
        "replacement_fingerprint": "0" * 64,
    }
    value["replacement_fingerprint"] = artifact_fingerprint(
        value, "replacement_fingerprint")
    return value


def validate_scoped_replacement(
        replacement: Mapping[str, Any], dispatch: Mapping[str, Any],
        model: ProjectReadModel,
        error: Callable[[str, str], Exception], *, session_id: str,
        request: Mapping[str, Any], response: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a full replacement without committing any current artifact."""
    stage = dispatch.get("stage")
    contract = _STAGE_CONTRACTS.get(stage)
    if contract is None or not accepted(validate(contract, replacement)):
        raise error("INVALID_SCHEMA", "Stage replacement contract is invalid")
    if (dispatch.get("dispatch_fingerprint") !=
            artifact_fingerprint(dispatch, "dispatch_fingerprint") or
            not accepted(validate("project_formal_dispatch", dispatch))):
        raise error("STALE_DISPATCH", "formal dispatch is invalid or stale")
    current_unit_roots = _current_unit_roots(model)
    if (dispatch["job_id"] != model.job_id or
            dispatch["input_fingerprint"] != model.input_fingerprint or
            dispatch["spec_fingerprint"] != model.spec_fingerprint or
            dispatch["policy_fingerprint"] != model.policy_fingerprint or
            dispatch["owner_scope_fingerprint"] !=
                model.owner_scope_fingerprint or
            dispatch["artifact_roots"] != model.artifact_roots or
            dispatch["unit_roots"] != current_unit_roots):
        raise error("STALE_DISPATCH", "formal dispatch no longer binds current roots")
    if (replacement["dispatch_id"] != dispatch["dispatch_id"] or
            replacement["dispatch_fingerprint"] !=
                dispatch["dispatch_fingerprint"] or
            replacement["job_id"] != dispatch["job_id"] or
            replacement["stage"] != stage or
            replacement["base_artifact_roots"] !=
                dispatch["artifact_roots"] or
            replacement["base_unit_roots"] != dispatch["unit_roots"] or
            replacement["scope_fingerprint"] != dispatch["scope_fingerprint"]):
        raise error("STALE_DISPATCH", "replacement does not bind its dispatch")
    expected_agent = copy.deepcopy(dispatch["stage_agent"])
    if any(replacement["stage_agent"].get(key) != value
           for key, value in expected_agent.items()):
        raise error(
            "INVALID_AGENT_BINDING",
            "Stage replacement must bind the resolved profile role")
    if (replacement["session_id"] != session_id or
            request.get("request_id") !=
                replacement["stage_agent"]["request_id"] or
            request.get("metadata", {}).get("session_id") != session_id or
            request.get("metadata", {}).get("job_id") != model.job_id or
            request.get("metadata", {}).get("role") != stage or
            response.get("request_id") != request.get("request_id") or
            response.get("provider_metadata", {}).get("response_id") !=
                replacement["stage_agent"]["response_id"] or
            response.get("provider_metadata", {}).get("provider_id") !=
                replacement["stage_agent"]["provider_id"] or
            response.get("model_id") != replacement["stage_agent"]["model_id"] or
            replacement["response_fingerprint"] != canonical_hash(response)):
        raise error(
            "INVALID_AGENT_BINDING",
            "replacement does not bind the exact submission turn")
    if replacement["replacement_fingerprint"] != artifact_fingerprint(
            replacement, "replacement_fingerprint"):
        raise error("STALE_EVIDENCE", "replacement fingerprint is stale")

    expected = {item["unit_id"]: item for item in dispatch["target_units"]}
    submitted = replacement["replacements"]
    submitted_ids = [item["unit_id"] for item in submitted]
    if len(submitted_ids) != len(set(submitted_ids)) or set(
            submitted_ids) != set(expected):
        raise error(
            "IDENTITY_MUTATION",
            "replacement IDs and count must exactly equal dispatched units")
    changed = False
    for item in submitted:
        authority = expected[item["unit_id"]]
        unit = model.units.get(item["unit_id"])
        if (unit is None or item["unit_kind"] != authority["unit_kind"] or
                item["base_revision"] != authority["revision"] or
                item["base_artifact_fingerprint"] !=
                    authority["artifact_fingerprint"] or
                unit["artifact_fingerprint"] !=
                    authority["artifact_fingerprint"]):
            raise error(
                "IDENTITY_MUTATION",
                "replacement kind or exact base lineage was changed")
        _validate_semantic_body(stage, unit, item["semantic_body"], error)
        if item["spec_evidence"] != unit["spec_evidence"]:
            raise error(
                "SCOPE_EXPANSION",
                "replacement must preserve the exact Spec evidence list")
        changed = changed or item["semantic_body"] != unit["semantic_body"]
    if not changed:
        raise error("NO_SEMANTIC_CHANGE", "replacement changes no target content")
    expected_replacement_id = _replacement_id(
        str(stage), session_id, dispatch["dispatch_id"],
        replacement["stage_agent"]["request_id"],
        replacement["stage_agent"]["response_id"],
        replacement["response_fingerprint"], replacement["replacements"])
    if replacement["replacement_id"] != expected_replacement_id:
        raise error(
            "STALE_EVIDENCE", "replacement identity is not deterministic")
    return copy.deepcopy(dict(replacement))


def _load_explicit_json(
        job_root: Path, relative: Any, expected_parts: tuple[str, ...],
        error: Callable[[str, str], Exception]) -> dict[str, Any]:
    if not isinstance(relative, str):
        raise error("STALE_EVIDENCE", "authority path is missing")
    pure = PurePosixPath(relative)
    if (pure.is_absolute() or pure.parts[:len(expected_parts)] !=
            expected_parts or len(pure.parts) != len(expected_parts) + 1 or
            any(part in {"", ".", ".."} or part.startswith(".")
                for part in pure.parts)):
        raise error("STALE_EVIDENCE", "authority path is outside its directory")
    path = Path(job_root).joinpath(*pure.parts)
    try:
        if (not path.is_file() or path.is_symlink() or
                Path(job_root).resolve() not in path.resolve().parents):
            raise OSError("authority file is unavailable")
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as caught:
        raise error("STALE_EVIDENCE", "authority file is unavailable") from caught
    if not isinstance(value, dict):
        raise error("STALE_EVIDENCE", "authority file is malformed")
    return value


def validate_scoped_replacement_lineage(
        job_root: Path, checkpoint: Mapping[str, Any],
        model: ProjectReadModel, expected_stage_binding: Mapping[str, Any],
        error: Callable[[str, str], Exception]) -> dict[str, Any]:
    """Replay checkpoint → replacement → dispatch → exact response."""
    if (checkpoint.get("state") != "SCOPED_REPLACEMENT_VALIDATED" or
            checkpoint.get("job_id") != model.job_id or
            checkpoint.get("input_fingerprint") != model.input_fingerprint or
            checkpoint.get("checkpoint_fingerprint") !=
                artifact_fingerprint(checkpoint, "checkpoint_fingerprint")):
        raise error("STALE_EVIDENCE", "validated replacement checkpoint is stale")
    replacement = _load_explicit_json(
        job_root, checkpoint.get("replacement_path"),
        ("staging", "scoped_replacements"), error)
    dispatch = _load_explicit_json(
        job_root, checkpoint.get("dispatch_path"),
        ("staging", "dispatch"), error)
    plan = _load_explicit_json(
        job_root, checkpoint.get("plan_path"),
        ("staging", "orchestrator"), error)
    receipt = _load_explicit_json(
        job_root, checkpoint.get("router_receipt_path"), ("audit",), error)
    validate_current_dispatch_authority(
        dispatch, model, expected_stage_binding, error,
        plan=plan, receipt=receipt, checkpoint=checkpoint)
    if (replacement.get("replacement_fingerprint") !=
            checkpoint.get("replacement_fingerprint") or
            checkpoint.get("replacement_path") !=
                "staging/scoped_replacements/{}.json".format(
                    str(checkpoint.get("replacement_fingerprint"))[:24])):
        raise error(
            "STALE_EVIDENCE", "checkpoint does not reference exact replacement")

    stage = dispatch["stage"]
    session_id = replacement.get("session_id")
    if not isinstance(session_id, str):
        raise error("STALE_EVIDENCE", "replacement session identity is missing")
    lineage = {
        "dispatch_id": dispatch["dispatch_id"],
        "dispatch_fingerprint": dispatch["dispatch_fingerprint"],
        "scope_fingerprint": dispatch["scope_fingerprint"],
        "artifact_root": model.artifact_root,
        "runtime_role": "STAGE_AGENT",
        **copy.deepcopy(dict(expected_stage_binding)),
    }
    try:
        from infrastructure.persistence.transcript_store import (
            load_terminal_transcript_events,
        )
        transcript = load_terminal_transcript_events(
            job_root=Path(job_root), job_id=model.job_id, role=stage,
            session_id=session_id, lineage=lineage)
    except Exception as caught:
        raise error(
            "STALE_EVIDENCE", "replacement transcript is unavailable") \
            from caught
    manifest = transcript["manifest"]
    events = transcript["events"]
    if manifest.get("terminal") != {
            "status": "COMPLETED", "code": "COMPLETED",
            "result_sequence": len(manifest["entries"])}:
        raise error("STALE_EVIDENCE", "replacement transcript is not complete")
    request_id = replacement.get("stage_agent", {}).get("request_id")
    response_id = replacement.get("stage_agent", {}).get("response_id")
    request = response = tool_call = None
    for index, event in enumerate(events):
        if event["kind"] != "RESPONSE":
            continue
        value = event["value"]
        if value.get("provider_metadata", {}).get("response_id") == response_id:
            if index == 0 or events[index - 1]["kind"] != "REQUEST":
                raise error("STALE_EVIDENCE", "response has no exact request")
            request = events[index - 1]["value"]
            response = value
            if (index + 1 >= len(events) or
                    events[index + 1]["kind"] != "TOOL_CALL"):
                raise error("STALE_EVIDENCE", "response has no submission call")
            tool_call = events[index + 1]["value"]
            break
    submit_name = "submit_stage{}_replacement".format(stage[-1])
    if (request is None or response is None or tool_call is None or
            request.get("request_id") != request_id or
            tool_call.get("name") != submit_name or
            not isinstance(tool_call.get("arguments"), dict)):
        raise error(
            "STALE_EVIDENCE", "replacement does not bind exact submission turn")
    rebuilt = formalize_scoped_replacement(
        tool_call["arguments"], dispatch, model, session_id,
        request, response, error)
    if rebuilt != replacement:
        raise error(
            "STALE_EVIDENCE", "replacement differs from formalized transcript")
    return validate_scoped_replacement(
        replacement, dispatch, model, error, session_id=session_id,
        request=request, response=response)


__all__ = [
    "artifact_fingerprint", "build_scoped_dispatch",
    "formalize_scoped_replacement", "validate_current_dispatch_authority",
    "validate_orchestrator_binding", "validate_scoped_replacement",
    "validate_scoped_replacement_lineage",
]
