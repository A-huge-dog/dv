"""OCHES002 end-to-end planning and scoped Stage session runtime."""
from __future__ import annotations

import copy
import json
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from contracts.validator import load_schema
from core.atomic_artifact import publish_immutable_bytes
from core.project_agent_profile import binding_lineage
from core.project_repair import (
    build_failure_feedback, formalize_repair_plan, validate_repair_plan,
)
from core.project_scoped_repair import (
    artifact_fingerprint,
    formalize_scoped_replacement,
    validate_current_dispatch_authority,
    validate_scoped_replacement,
)
from core.project_tools import (
    ORCHESTRATOR_READ_TOOLS, ProjectReadModel, ProjectToolError,
    STAGE_READ_TOOLS, read_tool_definitions,
)
from core.project_oches003 import (
    RepairRecordStore, build_prompt_contract, canonical_repair_groups,
)
from infrastructure.persistence.transcript_store import create_transcript_store
from runtime.agent_loop import AgentLoop, AgentLoopError, AgentLoopPolicy
from scripts.dvlib import canonical_hash


_SUBMISSION_TOOLS = {
    "STAGE_1": (
        "submit_stage1_replacement",
        "project_stage1_replacement_candidate"),
    "STAGE_2": (
        "submit_stage2_replacement",
        "project_stage2_replacement_candidate"),
    "STAGE_3": (
        "submit_stage3_replacement",
        "project_stage3_replacement_candidate"),
}


def _submission_tool(name: str, contract: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": "Submit one typed candidate for Framework formalization.",
        "input_schema": load_schema(contract),
    }


def _json_message(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ProjectRepairRuntime:
    """Generate and validate one scoped replacement; never commit it."""

    def __init__(
            self, *, job_root: Path, checkpoint: Mapping[str, Any],
            project_input: Mapping[str, Any],
            error: Callable[[str, str], Exception],
            authority_checkpoint_path: str =
                "audit/oches002_awaiting_scoped_replacement.json",
            validated_checkpoint_path: str =
                "audit/oches002_scoped_replacement_validated.json"):
        self.job_root = Path(job_root)
        self.checkpoint = copy.deepcopy(dict(checkpoint))
        self.project_input = copy.deepcopy(dict(project_input))
        self.error = error
        self.authority_checkpoint_path = authority_checkpoint_path
        self.validated_checkpoint_path = validated_checkpoint_path
        self.model = ProjectReadModel.from_checkpoint(
            self.job_root, self.checkpoint)
        if (self.project_input.get("job_id") != self.model.job_id or
                self.project_input.get("input_fingerprint") !=
                    self.model.input_fingerprint):
            raise error(
                "CROSS_JOB_ARTIFACT", "runtime input and checkpoint differ")

    @property
    def inventory(self) -> dict[str, set[str]]:
        kind_map = {
            "SCENARIO": {"SCENARIO"},
            "ACCEPTANCE_CRITERION": {"ACCEPTANCE_CRITERION"},
            "TESTCASE": {"LOGICAL_TESTCASE"},
            "CODE_UNIT": {"CODE_SHARED", "CODE_TESTCASE"},
        }
        return {
            target_kind: {
                unit["unit_id"] for unit in self.model.units.values()
                if unit["unit_kind"] in unit_kinds}
            for target_kind, unit_kinds in kind_map.items()
        }

    def _persist(self, relative: str, value: Mapping[str, Any]) -> str:
        pure = PurePosixPath(relative)
        if (pure.is_absolute() or not pure.parts or
                any(part in {"", ".", ".."} or part.startswith(".")
                    for part in pure.parts)):
            raise self.error("TOOL_PERMISSION_DENIED", "unsafe runtime path")
        path = self.job_root.joinpath(*pure.parts)
        encoded = (_json_message(value) + "\n").encode("utf-8")
        publish_immutable_bytes(
            path, encoded,
            lambda message: self.error("CONFLICTING_REPLAY", message),
            "runtime artifact replay conflicts")
        return relative

    def _load_authority_path(
            self, relative: Any,
            expected_parts: tuple[str, ...]) -> dict[str, Any]:
        if not isinstance(relative, str):
            raise self.error("STALE_DISPATCH", "authority path is missing")
        pure = PurePosixPath(relative)
        if (pure.is_absolute() or
                pure.parts[:len(expected_parts)] != expected_parts or
                len(pure.parts) != len(expected_parts) + 1 or
                any(part in {"", ".", ".."} or part.startswith(".")
                    for part in pure.parts)):
            raise self.error("STALE_DISPATCH", "authority path is outside scope")
        path = self.job_root.joinpath(*pure.parts)
        try:
            if (not path.is_file() or path.is_symlink() or
                    self.job_root.resolve() not in path.resolve().parents):
                raise OSError("authority path is unavailable")
            value = json.loads(path.read_text(encoding="utf-8"))
        except Exception as caught:
            raise self.error(
                "STALE_DISPATCH", "authority artifact is unavailable") \
                from caught
        if not isinstance(value, dict):
            raise self.error("STALE_DISPATCH", "authority artifact is malformed")
        return value

    def validate_stage_authority(
            self, dispatch: Mapping[str, Any]) -> dict[str, Any]:
        """Validate dispatch, plan, receipt and checkpoint before Provider use."""
        waiting_path = self.job_root / self.authority_checkpoint_path
        try:
            if not waiting_path.is_file() or waiting_path.is_symlink():
                raise OSError("waiting checkpoint is unavailable")
            checkpoint = json.loads(waiting_path.read_text(encoding="utf-8"))
        except Exception as caught:
            raise self.error(
                "STALE_DISPATCH", "scoped replacement checkpoint is unavailable") \
                from caught
        persisted_dispatch = self._load_authority_path(
            checkpoint.get("dispatch_path"), ("staging", "dispatch"))
        plan = self._load_authority_path(
            checkpoint.get("plan_path"), ("staging", "orchestrator"))
        receipt = self._load_authority_path(
            checkpoint.get("router_receipt_path"), ("audit",))
        if persisted_dispatch != dict(dispatch):
            raise self.error(
                "STALE_DISPATCH", "runtime dispatch differs from its checkpoint")
        stage = str(dispatch.get("stage"))
        if stage not in _SUBMISSION_TOOLS:
            raise self.error("STALE_DISPATCH", "dispatch Stage is invalid")
        binding = self._role_binding(
            "repair", stage.replace("STAGE_", "stage"),
            "STAGE_AGENT", "PROFILED")
        return validate_current_dispatch_authority(
            dispatch, self.model, binding, self.error,
            plan=plan, receipt=receipt, checkpoint=checkpoint)

    def _role_binding(self, section: str, role: str,
                      runtime_role: str, model_class: str) -> dict[str, Any]:
        return {
            "runtime_role": runtime_role,
            "model_class": model_class,
            **binding_lineage(self.project_input, section, role),
        }

    def _records(self) -> RepairRecordStore:
        return RepairRecordStore(
            self.job_root, job_id=self.model.job_id,
            input_fingerprint=self.model.input_fingerprint,
            spec_fingerprint=self.model.spec_fingerprint,
            policy_fingerprint=self.model.policy_fingerprint)

    def _prompt(self, role: str, role_binding: Mapping[str, Any], *,
                tool_allow_list: list[str], final_output: str,
                formal_scope: Mapping[str, Any],
                dependencies: list[Mapping[str, Any]]) -> dict[str, Any]:
        prompt = build_prompt_contract(
            role=role, job_id=self.model.job_id,
            input_fingerprint=self.model.input_fingerprint,
            spec_fingerprint=self.model.spec_fingerprint,
            policy_fingerprint=self.model.policy_fingerprint,
            artifact_roots=self.model.artifact_roots,
            unit_roots={name: index["root_fingerprint"]
                        for name, index in sorted(self.model.indexes.items())},
            dependencies=dependencies,
            provider_id=role_binding["provider_id"],
            model_id=role_binding["model_id"],
            role_fingerprint=canonical_hash(role_binding),
            tool_allow_list=tool_allow_list, final_output=final_output,
            formal_scope=formal_scope)
        self._persist(
            "staging/prompts/{}.{}.json".format(
                role.casefold(), prompt["prompt_fingerprint"][:16]), prompt)
        return prompt

    @staticmethod
    def _provider_identity(value: Mapping[str, Any]) -> dict[str, str]:
        return {
            "provider_id": value["provider_id"],
            "model_id": value["model_id"],
        }

    def run_orchestrator(
            self, provider: Any, session_id: str,
            cancel_requested: Callable[[], bool] | None = None
            ) -> dict[str, Any]:
        report = self.model.report
        request = self.model.review_request
        tools = read_tool_definitions(ORCHESTRATOR_READ_TOOLS)
        tools.append(_submission_tool(
            "submit_repair_plan", "project_repair_plan_candidate"))

        def submit(
                candidate: dict[str, Any], context: dict[str, Any]
                ) -> dict[str, Any]:
            plan = formalize_repair_plan(
                candidate, job=self.project_input, report=report,
                roots=self.model.artifact_roots,
                scope_fingerprint=request["coverage_scope"][
                    "scope_fingerprint"],
                planning_session_id=session_id,
                retrieval_rounds=context["retrieval_rounds"],
                orchestrator_binding=binding_lineage(
                    self.project_input, "repair", "orchestrator"),
                request_id=context["request"]["request_id"],
                response_id=context["response"]["provider_metadata"][
                    "response_id"],
                error=self.error)
            receipt, dispatch = validate_repair_plan(
                plan, self.project_input, report, self.model.artifact_roots,
                request["coverage_scope"]["scope_fingerprint"],
                self.inventory, self.model,
                self._role_binding(
                    "repair", "orchestrator", "ORCHESTRATOR", "PROFILED"),
                {
                    stage: self._role_binding(
                        "repair", stage.replace("STAGE_", "stage"),
                        "STAGE_AGENT", "PROFILED")
                    for stage in ("STAGE_1", "STAGE_2", "STAGE_3")
                },
                self.error)
            token = canonical_hash({
                "session_id": session_id,
                "plan_fingerprint": plan.get("plan_fingerprint"),
            })[:24]
            plan_path = self._persist(
                "staging/orchestrator/repair_plan.{}.json".format(token), plan)
            receipt_path = self._persist(
                "audit/router_receipt.{}.json".format(token), receipt)
            dispatch_path = None
            if dispatch is not None:
                dispatch_path = self._persist(
                    "staging/dispatch/repair_dispatch.{}.json".format(
                        dispatch["group_id"].split(".")[-1].casefold()),
                    dispatch)
                feedback = build_failure_feedback(
                    self.project_input, report, self.model.artifact_roots,
                    request["coverage_scope"]["scope_fingerprint"],
                    dispatch["issue_ids"])
                feedback_path = self._persist(
                    "staging/dispatch/failure_feedback.{}.json".format(
                        dispatch["group_id"].split(".")[-1].casefold()),
                    feedback)
                checkpoint = {
                    **copy.deepcopy(self.checkpoint),
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
                self._persist(
                    "audit/oches002_awaiting_scoped_replacement.json",
                    checkpoint)
            records = self._records()
            groups = canonical_repair_groups(
                plan["repairs"],
                policy_fingerprint=self.model.policy_fingerprint,
                units=self.model.units)
            records.append("ORCHESTRATOR_PLAN", {
                "initial_report_path": self.checkpoint["review_report_path"],
                "ordered_groups": groups,
                "submission_response_fingerprint": canonical_hash(
                    context["response"]),
            }, producer_role="ORCHESTRATOR")
            records.append("ROUTER_RECEIPT", {
                "plan_id": plan["plan_id"],
                "group_id": dispatch["group_id"] if dispatch else "NONE",
                "current_roots": self.model.artifact_roots,
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
            return {
                "status": receipt["status"], "receipt": receipt,
                "dispatch": dispatch,
            }

        orchestrator_binding = self._role_binding(
            "repair", "orchestrator", "ORCHESTRATOR", "PROFILED")
        prompt = self._prompt(
            "ORCHESTRATOR", orchestrator_binding,
            tool_allow_list=sorted(ORCHESTRATOR_READ_TOOLS),
            final_output="project_repair_plan_candidate",
            formal_scope=request["coverage_scope"], dependencies=[])
        messages = [{
            "role": "SYSTEM",
            "content": prompt["instructions"] + "\n" + _json_message(prompt),
        }, {
            "role": "USER",
            "content": _json_message({
                "job_id": self.model.job_id,
                "input_fingerprint": self.model.input_fingerprint,
                "spec_fingerprint": self.model.spec_fingerprint,
                "policy_fingerprint": self.model.policy_fingerprint,
                "artifact_roots": self.model.artifact_roots,
                "unit_roots": {
                    name: index["root_fingerprint"]
                    for name, index in sorted(self.model.indexes.items())},
                "coverage_scope": request["coverage_scope"],
                "review_report": report,
            }),
        }]
        lineage = {
                "input_fingerprint": self.model.input_fingerprint,
                "source_report_fingerprint": report["report_fingerprint"],
                "artifact_root": self.model.artifact_root,
                **self._role_binding(
                    "repair", "orchestrator", "ORCHESTRATOR", "PROFILED"),
            }
        session = AgentLoop(
            provider=provider,
            transcript_store=create_transcript_store(
                job_root=self.job_root, job_id=self.model.job_id,
                role="ORCHESTRATOR", session_id=session_id, lineage=lineage),
            job_id=self.model.job_id, session_id=session_id,
            initial_messages=messages, tools=tools,
            retrieval_handlers=self.model.handlers(ORCHESTRATOR_READ_TOOLS),
            submission_handlers={"submit_repair_plan": submit},
            provider_binding=self._provider_identity(self._role_binding(
                "repair", "orchestrator", "ORCHESTRATOR", "PROFILED")),
            policy=AgentLoopPolicy(
                role="ORCHESTRATOR",
                retrieval_tools=frozenset(ORCHESTRATOR_READ_TOOLS),
                submission_tools=frozenset({"submit_repair_plan"})),
            cancel_requested=cancel_requested)
        return session.run()

    def _scoped_handlers(
            self, dispatch: Mapping[str, Any]
            ) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        dispatch = self.validate_stage_authority(dispatch)
        targets = {item["unit_id"] for item in dispatch["target_units"]}
        dependency_units = {
            item["identity"]
            for group in dispatch["direct_dependencies"]
            for item in group["dependencies"]
            if item["revision"] is not None}
        readable_units = targets | dependency_units
        issue_ids = set(dispatch["issue_ids"])
        evidence = {
            tuple(item[key] for key in (
                "path", "line_start", "line_end", "snippet_fingerprint"))
            for item in dispatch["authorized_spec_evidence"]}
        history_identities = (
            readable_units | issue_ids | {
                dispatch["dispatch_id"], dispatch["group_id"],
                dispatch["plan_id"]})

        def ensure(name: str, arguments: Mapping[str, Any]) -> None:
            valid = False
            if name == "get_issue":
                valid = set(arguments.get("issue_ids", [])) <= issue_ids
            elif name == "get_unit":
                valid = set(arguments.get("unit_ids", [])) <= readable_units
            elif name == "get_direct_dependencies":
                valid = set(arguments.get("unit_ids", [])) <= targets
            elif name == "get_spec_evidence":
                requested = {
                    tuple(item.get(key) for key in (
                        "path", "line_start", "line_end",
                        "snippet_fingerprint"))
                    for item in arguments.get("evidence_refs", [])}
                valid = requested <= evidence
            elif name == "get_repair_history":
                valid = set(arguments.get("identities", [])) <= \
                    history_identities
            elif name == "compare_unit_revisions":
                valid = {
                    item.get("unit_id")
                    for item in arguments.get("comparisons", [])} <= \
                    readable_units
            if not valid:
                raise ProjectToolError(
                    "SCOPE_EXPANSION",
                    "Stage read request exceeds formal dispatch scope")

        handlers = self.model.handlers(STAGE_READ_TOOLS)
        return {
            name: (lambda arguments, selected=name: (
                ensure(selected, arguments), handlers[selected](arguments)
            )[1])
            for name in STAGE_READ_TOOLS
        }

    def run_stage(
            self, provider: Any, dispatch: Mapping[str, Any],
            session_id: str,
            cancel_requested: Callable[[], bool] | None = None
            ) -> dict[str, Any]:
        dispatch = self.validate_stage_authority(dispatch)
        stage = str(dispatch["stage"])
        submit_name, contract = _SUBMISSION_TOOLS[stage]
        tools = read_tool_definitions(STAGE_READ_TOOLS)
        tools.append(_submission_tool(submit_name, contract))

        def submit(
                candidate: dict[str, Any], context: dict[str, Any]
                ) -> dict[str, Any]:
            current_dispatch = self.validate_stage_authority(dispatch)
            replacement = formalize_scoped_replacement(
                candidate, current_dispatch, self.model, session_id,
                context["request"], context["response"], self.error)
            value = validate_scoped_replacement(
                replacement, current_dispatch, self.model, self.error,
                session_id=session_id, request=context["request"],
                response=context["response"])
            token = replacement["replacement_fingerprint"][:24]
            path = self._persist(
                "staging/scoped_replacements/{}.json".format(token), value)
            waiting_path = self.job_root / self.authority_checkpoint_path
            if not waiting_path.is_file() or waiting_path.is_symlink():
                raise self.error(
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
            validated_checkpoint["checkpoint_fingerprint"] = \
                artifact_fingerprint(
                    validated_checkpoint, "checkpoint_fingerprint")
            checkpoint_path = self._persist(
                self.validated_checkpoint_path, validated_checkpoint)
            self._records().append("SCOPED_REPLACEMENT", {
                "group_id": current_dispatch["group_id"],
                "dispatch_fingerprint": current_dispatch[
                    "dispatch_fingerprint"],
                "base_roots": value["base_artifact_roots"],
                "replacements": value["replacements"],
                "stage_response_fingerprint": value[
                    "response_fingerprint"],
            }, producer_role="STAGE_AGENT")
            return {
                "status": "VALIDATED", "replacement": value,
                "replacement_path": path,
                "checkpoint_path": checkpoint_path,
                "checkpoint_fingerprint":
                    validated_checkpoint["checkpoint_fingerprint"],
                "current_state_modified": False,
            }

        findings = [
            item for item in self.model.report["findings"]
            if item["issue_id"] in set(dispatch["issue_ids"])]
        stage_binding = self._role_binding(
            "repair", stage.replace("STAGE_", "stage"),
            "STAGE_AGENT", "PROFILED")
        prompt = self._prompt(
            stage, stage_binding,
            tool_allow_list=sorted(STAGE_READ_TOOLS), final_output=contract,
            formal_scope={
                "group_id": dispatch["group_id"],
                "scope_fingerprint": dispatch["scope_fingerprint"],
                "target_ids": sorted(item["unit_id"]
                                     for item in dispatch["target_units"]),
            }, dependencies=dispatch["direct_dependencies"])
        messages = [{
            "role": "SYSTEM",
            "content": prompt["instructions"] + "\n" + _json_message(prompt),
        }, {
            "role": "USER", "content": _json_message({
                "formal_dispatch": dispatch, "findings": findings,
            }),
        }]
        lineage = {
                "dispatch_id": dispatch["dispatch_id"],
                "dispatch_fingerprint": dispatch["dispatch_fingerprint"],
                "scope_fingerprint": dispatch["scope_fingerprint"],
                "artifact_root": self.model.artifact_root,
                **self._role_binding(
                    "repair", stage.replace("STAGE_", "stage"),
                    "STAGE_AGENT", "PROFILED"),
            }
        session = AgentLoop(
            provider=provider,
            transcript_store=create_transcript_store(
                job_root=self.job_root, job_id=self.model.job_id,
                role=stage, session_id=session_id, lineage=lineage),
            job_id=self.model.job_id, session_id=session_id,
            initial_messages=messages, tools=tools,
            retrieval_handlers=self._scoped_handlers(dispatch),
            submission_handlers={submit_name: submit},
            provider_binding=self._provider_identity(self._role_binding(
                "repair", stage.replace("STAGE_", "stage"),
                "STAGE_AGENT", "PROFILED")),
            policy=AgentLoopPolicy(
                role=stage, retrieval_tools=frozenset(STAGE_READ_TOOLS),
                submission_tools=frozenset({submit_name})),
            cancel_requested=cancel_requested)
        try:
            return session.run()
        except Exception as caught:
            if isinstance(caught, AgentLoopError) and caught.code in {
                    "TOOL_PROTOCOL_VIOLATION", "MALFORMED_MODEL_OUTPUT",
                    "CONTENT_FILTERED", "MODEL_REFUSAL",
                    "OUTPUT_LIMIT_EXCEEDED", "PROVIDER_UNAVAILABLE",
                    "CANCELLED"}:
                records = self._records()
                episodes = [record for _, record in records.records()
                            if record["record_type"] == "REPAIR_EPISODE" and
                            record["payload"].get("group_id") ==
                                dispatch["group_id"]]
                if not episodes:
                    validation_path, validation = records.append(
                        "VALIDATION_RESULT", {
                            "group_id": dispatch["group_id"],
                            "replacement_fingerprint": "0" * 64,
                            "status": "FAIL", "diagnostics": [caught.code],
                            "unexecuted_checks": [
                                "SCOPE", "SCHEMA", "LINEAGE", "DEPENDENCY",
                                "CONTENT", "COMPILE"],
                        })
                    commit_path, commit = records.append("GROUP_COMMIT", {
                        "group_id": dispatch["group_id"],
                        "status": "NOT_COMMITTED",
                        "before_roots": dispatch["artifact_roots"],
                        "current_roots": dispatch["artifact_roots"],
                        "target_revisions": [],
                        "validation_fingerprint": validation[
                            "record_fingerprint"],
                    })
                    records.append("REPAIR_EPISODE", {
                        "group_id": dispatch["group_id"],
                        "ordered_links": [{
                            "record_type": "VALIDATION_RESULT",
                            "path": validation_path,
                            "fingerprint": validation[
                                "record_fingerprint"],
                        }, {
                            "record_type": "GROUP_COMMIT", "path": commit_path,
                            "fingerprint": commit["record_fingerprint"],
                        }], "terminal_status": caught.code,
                    })
            raise

    def run(
            self, *, orchestrator_provider: Any,
            stage_provider_factory: Callable[[Mapping[str, Any]], Any],
            planning_session_id: str, stage_session_id: str,
            cancel_requested: Callable[[], bool] | None = None
            ) -> dict[str, Any]:
        """Run the OCHES002 boundary through validated, uncommitted output."""
        cancelled = cancel_requested or (lambda: False)
        planned = self.run_orchestrator(
            orchestrator_provider, planning_session_id, cancelled)
        if planned["status"] != "ACCEPTED" or planned["dispatch"] is None:
            return planned
        if cancelled():
            raise AgentLoopError(
                "CANCELLED", "Job was cancelled before Stage session")
        self.validate_stage_authority(planned["dispatch"])
        stage_result = self.run_stage(
            stage_provider_factory(planned["dispatch"]),
            planned["dispatch"], stage_session_id, cancelled)
        return {
            "status": "VALIDATED", "receipt": planned["receipt"],
            "dispatch": planned["dispatch"],
            **stage_result,
        }


__all__ = ["ProjectRepairRuntime"]
