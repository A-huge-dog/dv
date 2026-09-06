"""Pure repair planning, routing, replacement, grouping, and impact rules."""
from __future__ import annotations

import copy
import re
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from contracts.validator import accepted, validate
from domain.artifacts import (
    IMPACT_CONTRACT_VERSION, _ZERO, _lineage_fingerprint,
    _producer_dependency, artifact_fingerprint,
)
from scripts.dvlib import canonical_hash


STAGE_TARGET_KINDS = {
    "STAGE_1": {"SCENARIO", "ACCEPTANCE_CRITERION"},
    "STAGE_2": {"ACCEPTANCE_CRITERION", "TESTCASE"},
    "STAGE_3": {"TESTCASE", "CODE_UNIT"},
}
INVALIDATED_STAGES = {
    "STAGE_1": ["STAGE_1", "STAGE_2", "STAGE_3", "REVIEW"],
    "STAGE_2": ["STAGE_2", "STAGE_3", "REVIEW"],
    "STAGE_3": ["STAGE_3", "REVIEW"],
}
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
_STAGE_ORDER = {"STAGE_1": 1, "STAGE_2": 2, "STAGE_3": 3}
STAGE_READ_TOOLS = frozenset({
    "get_issue", "get_unit", "get_direct_dependencies",
    "get_spec_evidence", "get_repair_history", "compare_unit_revisions",
})


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
        stage: str, targets: list[dict[str, str]], model: Any,
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

def _current_unit_roots(model: Any) -> dict[str, str]:
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
        base: Mapping[str, Any], model: Any,
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
        dispatch: Mapping[str, Any], model: Any,
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
        model: Any, session_id: str,
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
        model: Any,
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

def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def current_roots(map1: dict[str, Any], map2: dict[str, Any],
                  candidate: dict[str, Any]) -> dict[str, str]:
    return {
        "scenario_ac_map": map1["artifact_fingerprint"],
        "ac_testcase_map": map2["artifact_fingerprint"],
        "testcase": candidate["candidate_fingerprint"],
        "effective_uvm": candidate["effective_uvm_root"],
    }

def current_inventory(map1: dict[str, Any], map2: dict[str, Any],
                      testcases: list[dict[str, Any]],
                      candidate: dict[str, Any]) -> dict[str, set[str]]:
    return {
        "SCENARIO": {item["scenario_id"] for item in map1["scenarios"]},
        "ACCEPTANCE_CRITERION": {
            item["ac_id"] for item in map1["acceptance_criteria"]},
        "TESTCASE": {item["testcase_id"] for item in testcases},
        "CODE_UNIT": {
            item["code_unit_id"] for item in candidate.get("code_units", [])},
    }

def formalize_repair_plan(
        candidate: dict[str, Any], *, job: dict[str, Any],
        report: dict[str, Any], roots: dict[str, str],
        scope_fingerprint: str, planning_session_id: str,
        retrieval_rounds: int, orchestrator_binding: dict[str, Any],
        request_id: str, response_id: str,
        error: Callable[[str, str], Exception]) -> dict[str, Any]:
    """Turn a model semantic decision into a Framework-owned formal plan."""
    if not accepted(validate("project_repair_plan_candidate", candidate)):
        raise error(
            "INVALID_REPAIR_PLAN",
            "Orchestrator candidate must contain only status and repairs")
    if not 0 <= retrieval_rounds <= 3:
        raise error(
            "INVALID_REPAIR_PLAN", "retrieval round count is invalid")
    if candidate["status"] == "INSUFFICIENT_EVIDENCE":
        if candidate["repairs"] or retrieval_rounds != 3:
            raise error(
                "INVALID_REPAIR_PLAN",
                "insufficient evidence requires three retrievals and no repairs")
    elif not candidate["repairs"]:
        raise error(
            "INVALID_REPAIR_PLAN", "a ready candidate requires repairs")
    binding = {
        "runtime_role": "ORCHESTRATOR",
        "model_class": "PROFILED",
        **copy.deepcopy(orchestrator_binding),
        "request_id": request_id,
        "response_id": response_id,
    }
    seed = canonical_hash({
        "job_id": job["job_id"],
        "planning_session_id": planning_session_id,
        "source_report_fingerprint": report["report_fingerprint"],
        "candidate": candidate,
    })[:16].upper()
    plan = {
        "schema_version": "1.0",
        "plan_id": "REPAIRPLAN.{}".format(seed),
        "planning_session_id": planning_session_id,
        "job_id": job["job_id"],
        "input_fingerprint": job["input_fingerprint"],
        "source_report_id": report["report_id"],
        "source_report_fingerprint": report["report_fingerprint"],
        "artifact_roots": copy.deepcopy(roots),
        "scope_fingerprint": scope_fingerprint,
        "retrieval_rounds": retrieval_rounds,
        "status": candidate["status"],
        "orchestrator": binding,
        "repairs": copy.deepcopy(candidate["repairs"]),
        "plan_fingerprint": "0" * 64,
    }
    plan["plan_fingerprint"] = artifact_fingerprint(
        plan, "plan_fingerprint")
    if not accepted(validate("project_repair_plan", plan)):
        raise error(
            "INVALID_SCHEMA", "Framework generated an invalid repair plan")
    return plan

def _receipt(plan: dict[str, Any], status: str, code: str, message: str,
             dispatch_id: str = "NONE") -> dict[str, Any]:
    raw_fingerprint = plan.get("plan_fingerprint")
    plan_fingerprint = raw_fingerprint if isinstance(
        raw_fingerprint, str) and re.fullmatch(
            r"[0-9a-f]{64}", raw_fingerprint) else canonical_hash(plan)
    raw_job_id = plan.get("job_id")
    job_id = raw_job_id if isinstance(raw_job_id, str) and re.fullmatch(
        r"JOB\.PROJECT\.[A-Z0-9_.-]+", raw_job_id) else "JOB.PROJECT.INVALID"
    raw_plan_id = plan.get("plan_id")
    plan_id = raw_plan_id if isinstance(raw_plan_id, str) and re.fullmatch(
        r"REPAIRPLAN\.[A-Z0-9_.-]+", raw_plan_id) else "REPAIRPLAN.INVALID"
    seed = canonical_hash({
        "plan": plan_fingerprint,
        "status": status,
        "code": code,
    })[:16].upper()
    value = {
        "schema_version": "1.0",
        "receipt_id": "ROUTERRECEIPT.{}".format(seed),
        "job_id": job_id,
        "plan_id": plan_id,
        "plan_fingerprint": plan_fingerprint,
        "status": status,
        "diagnostic": {"code": code, "message": message[:1024]},
        "formal_dispatch_id": dispatch_id,
        "receipt_fingerprint": "0" * 64,
    }
    value["receipt_fingerprint"] = artifact_fingerprint(
        value, "receipt_fingerprint")
    return value

def validate_repair_plan(
        plan: dict[str, Any], job: dict[str, Any], report: dict[str, Any],
        roots: dict[str, str], scope_fingerprint: str,
        inventory: dict[str, set[str]],
        read_model: Any,
        orchestrator_binding: dict[str, Any],
        stage_bindings: dict[str, dict[str, Any]],
        error: Callable[[str, str], Exception]
        ) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Accept or reject without changing the Orchestrator's plan."""
    if not accepted(validate("project_repair_plan", plan)):
        receipt = _receipt(
            plan, "REJECTED", "INVALID_REPAIR_PLAN",
            "repair plan does not match the versioned contract")
        return receipt, None
    if plan["plan_fingerprint"] != artifact_fingerprint(
            plan, "plan_fingerprint"):
        return _receipt(
            plan, "REJECTED", "STALE_EVIDENCE",
            "repair plan fingerprint is stale"), None
    try:
        validate_orchestrator_binding(plan, orchestrator_binding, error)
    except Exception as caught:
        if getattr(caught, "code", None) != "INVALID_AGENT_BINDING":
            raise
        return _receipt(
            plan, "REJECTED", "INVALID_AGENT_BINDING",
            "Orchestrator does not bind the Job's profiled role"), None
    if (plan["job_id"] != job["job_id"] or
            plan["input_fingerprint"] != job["input_fingerprint"] or
            plan["source_report_id"] != report["report_id"] or
            plan["source_report_fingerprint"] !=
                report["report_fingerprint"] or
            plan["artifact_roots"] != roots):
        return _receipt(
            plan, "REJECTED", "STALE_EVIDENCE",
            "plan does not bind the current Job, report, or roots"), None
    if plan["scope_fingerprint"] != scope_fingerprint:
        return _receipt(
            plan, "REJECTED", "SCOPE_EXPANSION",
            "plan scope differs from the current Owner-routed scope"), None
    if plan["status"] == "INSUFFICIENT_EVIDENCE":
        if plan["repairs"] or plan["retrieval_rounds"] != 3:
            return _receipt(
                plan, "REJECTED", "INVALID_REPAIR_PLAN",
                "insufficient evidence requires three rounds and no repairs"), None
        return _receipt(
            plan, "REJECTED", "INSUFFICIENT_EVIDENCE",
            "Orchestrator stopped after three bounded retrieval rounds"), None
    if not plan["repairs"]:
        return _receipt(
            plan, "REJECTED", "INVALID_REPAIR_PLAN",
            "a ready repair plan must contain repairs"), None

    findings = {item["issue_id"]: item for item in report["findings"]}
    error_ids = {
        item["issue_id"] for item in report["findings"]
        if item["severity"] == "ERROR" and
        item["suspected_origin_stage"] != "SPEC"}
    seen_issues: set[str] = set()
    all_targets: list[dict[str, str]] = []
    stages: set[str] = set()
    for repair in plan["repairs"]:
        stage = repair["stage"]
        stages.add(stage)
        for issue_id in repair["issue_ids"]:
            if issue_id not in findings or issue_id not in error_ids:
                return _receipt(
                    plan, "REJECTED", "UNKNOWN_OR_UNREPAIRABLE_ISSUE",
                    "plan references a warning, Spec issue, or unknown finding"), None
            if issue_id in seen_issues:
                return _receipt(
                    plan, "REJECTED", "DUPLICATE_ISSUE_ROUTE",
                    "one finding may be routed only once"), None
            if (findings[issue_id]["suspected_origin_stage"] == "STAGE_3" and
                    stage != "STAGE_2"):
                return _receipt(
                    plan, "REJECTED", "WRONG_STAGE_TARGET_KIND",
                    "testcase findings must route to STAGE_2; Framework "
                    "automatically runs UVM Generation and Stage 3"), None
            seen_issues.add(issue_id)
        for target in repair["targets"]:
            kind, identity = target["kind"], target["id"]
            if kind not in STAGE_TARGET_KINDS[stage]:
                return _receipt(
                    plan, "REJECTED", "WRONG_STAGE_TARGET_KIND",
                    "target kind is not legal for the selected Stage"), None
            if identity not in inventory[kind]:
                return _receipt(
                    plan, "REJECTED", "UNKNOWN_TARGET",
                    "repair target does not exist in the current Job"), None
            all_targets.append(copy.deepcopy(target))
    if seen_issues != error_ids:
        return _receipt(
            plan, "REJECTED", "INCOMPLETE_ERROR_ROUTING",
            "every repairable ERROR must share the one regeneration round"), None

    # Preserve the complete Orchestrator plan.  Only the next canonical group
    # receives a just-in-time dispatch; downstream groups are reconsidered
    # after the current roots and dependency closure have been recomputed.
    groups = canonical_repair_groups(
        plan["repairs"], policy_fingerprint=read_model.policy_fingerprint,
        units=read_model.units)
    if not groups:
        return _receipt(
            plan, "REJECTED", "EMPTY_REPAIR_SCOPE",
            "repair plan has no canonical groups"), None
    next_group = groups[0]
    earliest = next_group["stage"]
    issue_ids = next_group["issue_ids"]
    targets = next_group["targets"]
    token = canonical_hash({
        "plan": plan["plan_fingerprint"], "group": next_group["group_id"],
        "roots": roots,
    })[:16].upper()
    base_dispatch = {
        "dispatch_id": "REPAIRDISPATCH.{}".format(token),
        "job_id": job["job_id"],
        "input_fingerprint": job["input_fingerprint"],
        "spec_fingerprint": read_model.spec_fingerprint,
        "policy_fingerprint": read_model.policy_fingerprint,
        "owner_scope_fingerprint": read_model.owner_scope_fingerprint,
        "plan_id": plan["plan_id"],
        "plan_fingerprint": plan["plan_fingerprint"],
        "source_report_fingerprint": report["report_fingerprint"],
        "regeneration_round": 1,
        "stage": earliest,
        "targets": targets,
        "issue_ids": issue_ids,
        "artifact_roots": copy.deepcopy(roots),
        "invalidated_stages": INVALIDATED_STAGES[earliest],
        "created_at": _utc(),
    }
    try:
        dispatch = build_scoped_dispatch(
            base_dispatch, read_model, stage_bindings[earliest], error)
    except Exception as caught:
        code = getattr(caught, "code", None)
        if code not in {"EMPTY_REPAIR_SCOPE", "STALE_EVIDENCE"}:
            raise
        return _receipt(
            plan, "REJECTED", code,
            "formal targets do not resolve to a current editable scope"), None
    receipt = _receipt(
        plan, "ACCEPTED", "ACCEPTED",
        "plan passed deterministic current-Job validation",
        dispatch["dispatch_id"])
    if not accepted(validate("project_router_receipt", receipt)):
        raise error("INVALID_SCHEMA", "Router generated invalid receipt")
    return receipt, dispatch

def dispatch_canonical_group(
        plan: dict[str, Any], group: dict[str, Any], job: dict[str, Any],
        report: dict[str, Any], read_model: Any,
        stage_binding: dict[str, Any],
        error: Callable[[str, str], Exception]
        ) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Create one just-in-time dispatch against exact post-commit roots."""
    groups = canonical_repair_groups(
        plan.get("repairs", []),
        policy_fingerprint=read_model.policy_fingerprint,
        units=read_model.units)
    current = next((item for item in groups
                    if item["group_id"] == group.get("group_id")), None)
    if current != group:
        return _receipt(
            plan, "REJECTED", "REPLAN_REQUIRED",
            "planned group no longer resolves in the current dependency graph"), None
    token = canonical_hash({
        "plan": plan["plan_fingerprint"], "group": group["group_id"],
        "roots": read_model.artifact_roots,
        "unit_roots": _current_unit_roots_for_dispatch(read_model),
    })[:16].upper()
    base = {
        "dispatch_id": "REPAIRDISPATCH.{}".format(token),
        "job_id": job["job_id"],
        "input_fingerprint": job["input_fingerprint"],
        "spec_fingerprint": read_model.spec_fingerprint,
        "policy_fingerprint": read_model.policy_fingerprint,
        "owner_scope_fingerprint": read_model.owner_scope_fingerprint,
        "plan_id": plan["plan_id"],
        "plan_fingerprint": plan["plan_fingerprint"],
        "source_report_fingerprint": report["report_fingerprint"],
        "regeneration_round": 1, "stage": group["stage"],
        "targets": copy.deepcopy(group["targets"]),
        "issue_ids": copy.deepcopy(group["issue_ids"]),
        "artifact_roots": copy.deepcopy(read_model.artifact_roots),
        "invalidated_stages": INVALIDATED_STAGES[group["stage"]],
        "created_at": _utc(),
    }
    try:
        dispatch = build_scoped_dispatch(base, read_model, stage_binding, error)
    except Exception as caught:
        if getattr(caught, "code", None) in {
                "EMPTY_REPAIR_SCOPE", "STALE_EVIDENCE", "REPLAN_REQUIRED"}:
            return _receipt(
                plan, "REJECTED", "REPLAN_REQUIRED",
                "planned group requires new scope or no longer exists"), None
        raise
    receipt = _receipt(
        plan, "ACCEPTED", "ACCEPTED",
        "canonical group passed just-in-time current-root validation",
        dispatch["dispatch_id"])
    return receipt, dispatch

def _current_unit_roots_for_dispatch(
        model: Any) -> dict[str, str]:
    return {name: model.indexes[name]["root_fingerprint"]
            for name in ("stage1", "stage2", "stage3", "review")}

def build_failure_feedback(
        job: dict[str, Any], report: dict[str, Any], roots: dict[str, str],
        scope_fingerprint: str, issue_ids: list[str]) -> dict[str, Any]:
    selected = [
        copy.deepcopy(item) for item in report["findings"]
        if item["issue_id"] in set(issue_ids)]
    value = {
        "schema_version": "1.0",
        "job_id": job["job_id"],
        "regeneration_round": 1,
        "source_report_id": report["report_id"],
        "source_report_fingerprint": report["report_fingerprint"],
        "artifact_roots": copy.deepcopy(roots),
        "scope_fingerprint": scope_fingerprint,
        "findings": selected,
        "feedback_fingerprint": "0" * 64,
    }
    value["feedback_fingerprint"] = artifact_fingerprint(
        value, "feedback_fingerprint")
    return value

def canonical_repair_groups(
        repairs: Iterable[Mapping[str, Any]], *, policy_fingerprint: str,
        units: Mapping[str, Mapping[str, Any]] | None = None
        ) -> list[dict[str, Any]]:
    """Group same-Stage repairs by overlap/direct dependency, deterministically."""
    normalized = []
    for repair in repairs:
        stage = str(repair.get("stage"))
        if stage not in _STAGE_ORDER:
            raise ValueError("invalid repair Stage")
        targets = {
            (str(item["kind"]), str(item["id"]))
            for item in repair.get("targets", [])
        }
        issues = {str(item) for item in repair.get("issue_ids", [])}
        if not targets or not issues:
            raise ValueError("repair must contain targets and issues")
        normalized.append({"stage": stage, "targets": targets, "issues": issues})

    unit_map = dict(units or {})

    def dependency_ids(target_ids: set[str]) -> set[str]:
        result = set()
        for identity in target_ids:
            unit = unit_map.get(identity, {})
            for item in unit.get("dependency_fingerprints", []):
                dependency = str(item.get("identity", ""))
                if dependency:
                    result.add(dependency)
        return result

    groups: list[dict[str, Any]] = []
    for stage in sorted(_STAGE_ORDER, key=_STAGE_ORDER.get):
        candidates = [item for item in normalized if item["stage"] == stage]
        components: list[dict[str, Any]] = []
        # Sorting first makes union/merge independent of Provider array order.
        candidates.sort(key=lambda item: canonical_hash({
            "targets": sorted(item["targets"]), "issues": sorted(item["issues"])}))
        for item in candidates:
            target_ids = {identity for _, identity in item["targets"]}
            closure = target_ids | dependency_ids(target_ids)
            overlaps = []
            for index, component in enumerate(components):
                if (target_ids & component["target_ids"] or
                        closure & component["closure"] or
                        target_ids & component["closure"] or
                        component["target_ids"] & closure):
                    overlaps.append(index)
            merged = {
                "targets": set(item["targets"]), "issues": set(item["issues"]),
                "target_ids": target_ids, "closure": closure,
            }
            for index in reversed(overlaps):
                other = components.pop(index)
                for key in ("targets", "issues", "target_ids", "closure"):
                    merged[key].update(other[key])
            components.append(merged)
        for component in components:
            targets = [
                {"kind": kind, "id": identity}
                for kind, identity in sorted(component["targets"])
            ]
            issues = sorted(component["issues"])
            target_ids = sorted(component["target_ids"])
            group_id = "REPAIRGROUP.{}".format(canonical_hash({
                "stage": stage, "target_ids": target_ids,
                "issue_ids": issues, "policy_fingerprint": policy_fingerprint,
            })[:16].upper())
            groups.append({
                "group_id": group_id, "stage": stage, "targets": targets,
                "target_ids": target_ids, "issue_ids": issues,
            })
    return sorted(groups, key=lambda item: (
        _STAGE_ORDER[item["stage"]], item["group_id"]))

def evaluate_impact(
        old_indexes: dict[str, tuple[dict[str, Any], dict[str, dict[str, Any]]]],
        new_indexes: dict[str, tuple[dict[str, Any], dict[str, dict[str, Any]]]],
        error: Callable[[str, str], Exception], *,
        allow_stale_stages: Iterable[str] = ()) -> dict[str, Any]:
    """Compare validated direct-lineage units without executing any repair."""
    if not old_indexes or not new_indexes:
        raise error("PARTIAL_ARTIFACT", "impact analysis requires old and new indexes")
    ordered_old = [old_indexes[key][0] for key in sorted(old_indexes)]
    ordered_new = [new_indexes[key][0] for key in sorted(new_indexes)]
    authority_fields = (
        "job_id", "input_fingerprint", "spec_fingerprint",
        "policy_fingerprint", "owner_scope_fingerprint",
    )
    authority = {field: ordered_new[0][field] for field in authority_fields}
    for index in [*ordered_old, *ordered_new]:
        if any(index[field] != authority[field] for field in authority_fields):
            code = "CROSS_JOB_ARTIFACT" if index["job_id"] != authority["job_id"] \
                else "AUTHORITY_DRIFT"
            raise error(code, "impact comparison authority is not exact")
    def flatten(
            values: dict[str, tuple[dict[str, Any], dict[str, dict[str, Any]]]]
            ) -> dict[str, dict[str, Any]]:
        result = {}
        for _, (index, units) in sorted(values.items()):
            for unit in units.values():
                key = "{}:{}:{}".format(
                    unit["stage"], unit["unit_kind"], unit["unit_id"])
                if key in result:
                    raise error("INDEX_COLLISION", "impact unit identity collides")
                result[key] = unit
        return result
    old_units = flatten(old_indexes)
    new_units = flatten(new_indexes)
    current_lineage = {
        (unit["unit_kind"], unit["unit_id"]): _lineage_fingerprint(unit)
        for unit in new_units.values()
    }
    allowed_stale = set(allow_stale_stages)
    for unit in new_units.values():
        if unit.get("stage") in allowed_stale:
            continue
        for dependency in unit["dependency_fingerprints"]:
            target = current_lineage.get((
                dependency["kind"], dependency["identity"]))
            if target is not None and target != dependency["fingerprint"]:
                raise error(
                    "UNDECLARED_DEPENDENCY",
                    "unit direct dependency does not bind the current child")
    dirty: list[dict[str, Any]] = []
    reused: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    dirty_lineages: set[tuple[str, str]] = set()
    pending: dict[str, list[str]] = {}
    for key, unit in new_units.items():
        prior = old_units.get(key)
        reasons = []
        if prior is None:
            reasons.append("ADDED")
        else:
            if prior["content_fingerprint"] != unit["content_fingerprint"]:
                reasons.append("CONTENT_CHANGED")
            if prior["dependency_fingerprint"] != unit["dependency_fingerprint"]:
                reasons.append("DIRECT_DEPENDENCY_CHANGED")
            if _producer_dependency(prior["producer"]) != _producer_dependency(unit["producer"]):
                if "DIRECT_DEPENDENCY_CHANGED" not in reasons:
                    reasons.append("DIRECT_DEPENDENCY_CHANGED")
        pending[key] = reasons
        if reasons:
            dirty_lineages.add((unit["unit_kind"], unit["unit_id"]))
    changed = True
    while changed:
        changed = False
        for key, unit in new_units.items():
            if pending[key]:
                continue
            if any((dependency["kind"], dependency["identity"]) in dirty_lineages
                   for dependency in unit["dependency_fingerprints"]):
                pending[key].append("TRANSITIVE_DEPENDENCY_CHANGED")
                dirty_lineages.add((unit["unit_kind"], unit["unit_id"]))
                changed = True
    def change_record(
            key: str, old: dict[str, Any] | None,
            new: dict[str, Any] | None, reasons: list[str]) -> dict[str, Any]:
        source = new or old
        assert source is not None
        return {
            "unit_key": key,
            "unit_id": source["unit_id"],
            "stage": source["stage"],
            "unit_kind": source["unit_kind"],
            "old_fingerprint": old["artifact_fingerprint"] if old else None,
            "new_fingerprint": new["artifact_fingerprint"] if new else None,
            "reasons": sorted(set(reasons)),
        }
    for key in sorted(new_units):
        record = change_record(
            key, old_units.get(key), new_units[key], pending[key] or
            ["UNCHANGED"])
        if pending[key]:
            dirty.append(record)
        else:
            reused.append(record)
    for key in sorted(set(old_units) - set(new_units)):
        removed.append(change_record(
            key, old_units[key], None, ["REMOVED"]))
    old_roots = {key: value[0]["root_fingerprint"]
                 for key, value in sorted(old_indexes.items())}
    new_roots = {key: value[0]["root_fingerprint"]
                 for key, value in sorted(new_indexes.items())}
    seed = {
        "job_id": authority["job_id"],
        "old_roots": old_roots, "new_roots": new_roots,
        "dirty": dirty, "reused": reused, "removed": removed,
    }
    manifest = {
        "schema_version": IMPACT_CONTRACT_VERSION,
        "artifact_kind": "PROJECT_INCREMENTAL_IMPACT",
        "impact_id": "IMPACT.{}".format(canonical_hash(seed)[:16].upper()),
        **authority,
        "old_roots": old_roots,
        "new_roots": new_roots,
        "dirty_units": dirty,
        "reused_units": reused,
        "removed_units": removed,
        "impact_fingerprint": _ZERO,
    }
    manifest["impact_fingerprint"] = artifact_fingerprint(
        manifest, "impact_fingerprint")
    if not accepted(validate("project_impact_manifest", manifest)):
        raise error("INVALID_SCHEMA", "incremental impact manifest is invalid")
    return manifest
