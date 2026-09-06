"""Repair-plan and scoped-replacement application handlers."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from contracts.validator import accepted, validate
from domain.agent_binding import binding_lineage
from domain.artifacts import artifact_fingerprint
from domain.repair import (
    build_failure_feedback,
    canonical_repair_groups,
    formalize_repair_plan,
    formalize_scoped_replacement,
    validate_repair_plan,
    validate_scoped_replacement,
)
from scripts.dvlib import canonical_hash


@dataclass(frozen=True)
class CreateRepairPlanInput:
    candidate: dict[str, Any]
    agent_context: dict[str, Any]
    project_input: dict[str, Any]
    checkpoint: dict[str, Any]
    report: dict[str, Any]
    review_request: dict[str, Any]
    artifact_roots: dict[str, str]
    inventory: dict[str, set[str]]
    read_model: Any
    planning_session_id: str
    orchestrator_binding: dict[str, Any]
    stage_bindings: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class RepairPlanResult:
    status: str
    receipt: dict[str, Any]
    dispatch: dict[str, Any] | None
    output_references: tuple[str, ...]

    @property
    def tool_result(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "receipt": copy.deepcopy(self.receipt),
            "dispatch": copy.deepcopy(self.dispatch),
        }


@dataclass(frozen=True)
class CreateRepairPlanDependencies:
    error: type[Exception]
    persist: Callable[[str, Mapping[str, Any]], str]
    records: Callable[[], Any]


class CreateRepairPlanHandler:
    """Formalize, validate, and persist one exact Orchestrator submission."""

    def __init__(self, dependencies: CreateRepairPlanDependencies):
        self.dependencies = dependencies

    def handle(self, command: CreateRepairPlanInput) -> RepairPlanResult:
        deps = self.dependencies
        context = command.agent_context
        scope = command.review_request["coverage_scope"]["scope_fingerprint"]
        plan = formalize_repair_plan(
            command.candidate,
            job=command.project_input,
            report=command.report,
            roots=command.artifact_roots,
            scope_fingerprint=scope,
            planning_session_id=command.planning_session_id,
            retrieval_rounds=context["retrieval_rounds"],
            orchestrator_binding=binding_lineage(
                command.project_input, "repair", "orchestrator"),
            request_id=context["request"]["request_id"],
            response_id=context["response"]["provider_metadata"]["response_id"],
            error=deps.error,
        )
        receipt, dispatch = validate_repair_plan(
            plan, command.project_input, command.report,
            command.artifact_roots, scope, command.inventory,
            command.read_model, command.orchestrator_binding,
            command.stage_bindings, deps.error)
        token = canonical_hash({
            "session_id": command.planning_session_id,
            "plan_fingerprint": plan.get("plan_fingerprint"),
        })[:24]
        plan_path = deps.persist(
            "staging/orchestrator/repair_plan.{}.json".format(token), plan)
        receipt_path = deps.persist(
            "audit/router_receipt.{}.json".format(token), receipt)
        output_references = [plan_path, receipt_path]
        if dispatch is not None:
            dispatch_path = deps.persist(
                "staging/dispatch/repair_dispatch.{}.json".format(
                    dispatch["group_id"].split(".")[-1].casefold()),
                dispatch)
            feedback = build_failure_feedback(
                command.project_input, command.report,
                command.artifact_roots, scope, dispatch["issue_ids"])
            if not accepted(validate("project_failure_feedback", feedback)):
                raise deps.error(
                    "INVALID_SCHEMA", "repair feedback contract is invalid")
            feedback_path = deps.persist(
                "staging/dispatch/failure_feedback.{}.json".format(
                    dispatch["group_id"].split(".")[-1].casefold()),
                feedback)
            checkpoint = {
                **copy.deepcopy(command.checkpoint),
                "state": "AWAITING_SCOPED_REPLACEMENT",
                "plan_path": plan_path,
                "router_receipt_path": receipt_path,
                "dispatch_path": dispatch_path,
                "dispatch_fingerprint": dispatch["dispatch_fingerprint"],
                "scope_fingerprint": dispatch["scope_fingerprint"],
                "failure_feedback_path": feedback_path,
                "checkpoint_fingerprint": "0" * 64,
            }
            checkpoint["checkpoint_fingerprint"] = artifact_fingerprint(
                checkpoint, "checkpoint_fingerprint")
            checkpoint_path = deps.persist(
                "audit/oches002_awaiting_scoped_replacement.json", checkpoint)
            output_references.extend(
                [dispatch_path, feedback_path, checkpoint_path])
        records = deps.records()
        groups = canonical_repair_groups(
            plan["repairs"],
            policy_fingerprint=command.read_model.policy_fingerprint,
            units=command.read_model.units)
        records.append("ORCHESTRATOR_PLAN", {
            "initial_report_path": command.checkpoint["review_report_path"],
            "ordered_groups": groups,
            "submission_response_fingerprint": canonical_hash(
                context["response"]),
        }, producer_role="ORCHESTRATOR")
        records.append("ROUTER_RECEIPT", {
            "plan_id": plan["plan_id"],
            "group_id": dispatch["group_id"] if dispatch else "NONE",
            "current_roots": command.artifact_roots,
            "status": receipt["status"],
            "diagnostics": [receipt["diagnostic"]],
        }, producer_role="ROUTER")
        if dispatch is not None:
            records.append("FORMAL_DISPATCH", {
                "plan_id": plan["plan_id"],
                "group_id": dispatch["group_id"],
                "stage": dispatch["stage"],
                "targets": dispatch["targets"],
                "issue_ids": dispatch["issue_ids"],
                "base_roots": dispatch["artifact_roots"],
                "dependencies": dispatch["direct_dependencies"],
                "spec_identities": dispatch["authorized_spec_evidence"],
                "tool_allow_list": dispatch["tool_allow_list"],
                "retrieval_call_limit": dispatch["retrieval_call_limit"],
            })
        return RepairPlanResult(
            receipt["status"], receipt, dispatch, tuple(output_references))


@dataclass(frozen=True)
class ValidateRepairPlanInput:
    plan: dict[str, Any]
    project_input: dict[str, Any]
    source_checkpoint: dict[str, Any]
    report: dict[str, Any]
    review_request: dict[str, Any]
    artifact_roots: dict[str, str]
    inventory: dict[str, set[str]]
    read_model: Any
    orchestrator_binding: dict[str, Any]
    stage_bindings: dict[str, dict[str, Any]]
    existing_plan: dict[str, Any] | None = None


class ValidateRepairPlanHandler:
    """Validate and dispatch one already-formalized exact repair plan."""

    def __init__(self, dependencies: CreateRepairPlanDependencies):
        self.dependencies = dependencies

    def handle(self, command: ValidateRepairPlanInput) -> RepairPlanResult:
        deps = self.dependencies
        scope = command.review_request["coverage_scope"]["scope_fingerprint"]
        receipt, dispatch = validate_repair_plan(
            command.plan, command.project_input, command.report,
            command.artifact_roots, scope, command.inventory,
            command.read_model, command.orchestrator_binding,
            command.stage_bindings, deps.error)
        session_id = command.plan.get("planning_session_id")
        plan_token = canonical_hash({
            "session": session_id,
            "plan": command.plan.get("plan_id"),
        })[:24]
        plan_path = "staging/orchestrator/repair_plan.{}.json".format(plan_token)
        receipt_path = "audit/router_receipt.{}.json".format(plan_token)
        if command.existing_plan is None:
            plan_path = deps.persist(plan_path, command.plan)
        receipt_path = deps.persist(receipt_path, receipt)
        references = [plan_path, receipt_path]
        if dispatch is None:
            return RepairPlanResult(
                receipt["status"], receipt, None, tuple(references))
        dispatch_path = deps.persist(
            "staging/dispatch/repair_dispatch.r001.json", dispatch)
        feedback = build_failure_feedback(
            command.project_input, command.report, command.artifact_roots,
            scope, dispatch["issue_ids"])
        if not accepted(validate("project_failure_feedback", feedback)):
            raise deps.error(
                "INVALID_SCHEMA", "repair feedback contract is invalid")
        feedback_path = deps.persist(
            "staging/dispatch/failure_feedback.r001.json", feedback)
        checkpoint = {
            **copy.deepcopy(command.source_checkpoint),
            "state": "AWAITING_SCOPED_REPLACEMENT",
            "plan_path": plan_path,
            "router_receipt_path": receipt_path,
            "dispatch_path": dispatch_path,
            "dispatch_fingerprint": dispatch["dispatch_fingerprint"],
            "scope_fingerprint": dispatch["scope_fingerprint"],
            "failure_feedback_path": feedback_path,
            "checkpoint_fingerprint": "0" * 64,
        }
        checkpoint["checkpoint_fingerprint"] = artifact_fingerprint(
            checkpoint, "checkpoint_fingerprint")
        checkpoint_path = deps.persist(
            "audit/oches002_awaiting_scoped_replacement.json", checkpoint)
        references.extend([dispatch_path, feedback_path, checkpoint_path])
        return RepairPlanResult(
            receipt["status"], receipt, dispatch, tuple(references))


@dataclass(frozen=True)
class ScopedReplacementInput:
    candidate: dict[str, Any]
    agent_context: dict[str, Any]
    dispatch: dict[str, Any]
    read_model: Any
    session_id: str
    job_root: Path
    authority_checkpoint_path: str
    validated_checkpoint_path: str


@dataclass(frozen=True)
class ScopedReplacementResult:
    replacement: dict[str, Any]
    replacement_path: str
    checkpoint_path: str
    checkpoint_fingerprint: str

    @property
    def tool_result(self) -> dict[str, Any]:
        return {
            "status": "VALIDATED",
            "replacement": copy.deepcopy(self.replacement),
            "replacement_path": self.replacement_path,
            "checkpoint_path": self.checkpoint_path,
            "checkpoint_fingerprint": self.checkpoint_fingerprint,
            "current_state_modified": False,
        }


@dataclass(frozen=True)
class ScopedReplacementDependencies:
    error: type[Exception]
    validate_authority: Callable[[Mapping[str, Any]], dict[str, Any]]
    persist: Callable[[str, Mapping[str, Any]], str]
    records: Callable[[], Any]


class ScopedReplacementHandler:
    """Validate and persist one Stage Agent replacement without commit."""

    def __init__(self, dependencies: ScopedReplacementDependencies):
        self.dependencies = dependencies

    def handle(self, command: ScopedReplacementInput) -> ScopedReplacementResult:
        deps = self.dependencies
        dispatch = deps.validate_authority(command.dispatch)
        context = command.agent_context
        replacement = formalize_scoped_replacement(
            command.candidate, dispatch, command.read_model,
            command.session_id, context["request"], context["response"],
            deps.error)
        value = validate_scoped_replacement(
            replacement, dispatch, command.read_model, deps.error,
            session_id=command.session_id, request=context["request"],
            response=context["response"])
        token = replacement["replacement_fingerprint"][:24]
        path = deps.persist(
            "staging/scoped_replacements/{}.json".format(token), value)
        waiting_path = command.job_root / command.authority_checkpoint_path
        if not waiting_path.is_file() or waiting_path.is_symlink():
            raise deps.error(
                "STALE_EVIDENCE",
                "scoped-replacement checkpoint is unavailable")
        waiting = json.loads(waiting_path.read_text(encoding="utf-8"))
        validated_checkpoint = {
            **waiting,
            "state": "SCOPED_REPLACEMENT_VALIDATED",
            "replacement_path": path,
            "replacement_fingerprint": value["replacement_fingerprint"],
            "checkpoint_fingerprint": "0" * 64,
        }
        validated_checkpoint["checkpoint_fingerprint"] = artifact_fingerprint(
            validated_checkpoint, "checkpoint_fingerprint")
        checkpoint_path = deps.persist(
            command.validated_checkpoint_path, validated_checkpoint)
        deps.records().append("SCOPED_REPLACEMENT", {
            "group_id": dispatch["group_id"],
            "dispatch_fingerprint": dispatch["dispatch_fingerprint"],
            "base_roots": value["base_artifact_roots"],
            "replacements": value["replacements"],
            "stage_response_fingerprint": value["response_fingerprint"],
        }, producer_role="STAGE_AGENT")
        return ScopedReplacementResult(
            value, path, checkpoint_path,
            validated_checkpoint["checkpoint_fingerprint"])
