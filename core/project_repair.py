"""OCHES001 repair-control contracts and deterministic Router.

This module owns no semantic routing.  It verifies an Orchestrator's exact
plan against the current immutable Job bundle and either emits one formal
dispatch or a typed rejection.
"""
from __future__ import annotations

import copy
import re
from datetime import datetime, timezone
from typing import Any, Callable

from contracts.validator import accepted, validate
from core.project_scoped_repair import (
    build_scoped_dispatch, validate_orchestrator_binding,
)
from core.project_tools import ProjectReadModel
from core.project_oches003 import canonical_repair_groups
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


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def artifact_fingerprint(value: dict[str, Any], field: str) -> str:
    projected = copy.deepcopy(value)
    projected.pop(field, None)
    return canonical_hash(projected)


def current_roots(map1: dict[str, Any], map2: dict[str, Any],
                  candidate: dict[str, Any]) -> dict[str, str]:
    return {
        "scenario_ac_map": map1["artifact_fingerprint"],
        "ac_testcase_map": map2["artifact_fingerprint"],
        "testcase": candidate["candidate_fingerprint"],
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
        read_model: ProjectReadModel,
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
        report: dict[str, Any], read_model: ProjectReadModel,
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
        model: ProjectReadModel) -> dict[str, str]:
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


__all__ = [
    "INVALIDATED_STAGES", "STAGE_TARGET_KINDS", "build_failure_feedback",
    "current_inventory", "current_roots", "dispatch_canonical_group",
    "formalize_repair_plan", "validate_repair_plan",
]
