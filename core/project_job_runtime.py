"""Wire one-YAML Project Jobs into the OCHES002 repair runtime."""
from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path
from typing import Any, Callable, Mapping

from contracts.validator import accepted, load_document, validate
from core.project_agent_profile import binding as agent_binding
from core.project_job import (
    INTERNAL_MANIFEST_PATH,
    ProjectJobError,
    ProjectJobWorkflow,
    validate_project_input,
)
from core.project_repair_runtime import ProjectRepairRuntime
from core.recovery import NON_RECOVERABLE_CODES, stop_status
from core.project_scoped_repair import artifact_fingerprint
from core.project_staged import StagedProjectWorkflow
from core.project_tools import ProjectReadModel, ProjectToolError
from core.session_scheduler import SerialSessionScheduler, SessionSchedulerError
from core.tool_session import ToolSessionError, load_terminal_transcript
from scripts.dvlib import canonical_hash


REPAIR_RUNTIME_STATES = {
    "AWAITING_REPAIR_PLAN",
    "AWAITING_SCOPED_REPLACEMENT",
    "SCOPED_REPLACEMENT_VALIDATED",
}
RUNTIME_PROTOCOL = "OCHES002_PLAN_CANDIDATE_V1"
PROVIDER_RETRY_RUNTIME_PROTOCOL = "OCHES002_PLAN_CANDIDATE_V2"
LEGACY_RUNTIME_PROTOCOL = "OCHES002"

ProviderFactory = Callable[[Path, Mapping[str, Any], str], Any]


def repair_checkpoint_id(checkpoint: Mapping[str, Any]) -> str:
    """Give old and new OCHES001 checkpoints one stable queue identity."""
    return "CHECKPOINT.PROJECT.OCHES001.{}".format(
        str(checkpoint["checkpoint_fingerprint"])[:16].upper())


class ProjectJobRuntimeIntegration:
    """Advance FIFO Jobs to one validated, uncommitted replacement."""

    def __init__(
            self, *, workspace_root: Path, result_root: Path,
            provider_factory: ProviderFactory):
        self.workspace_root = Path(workspace_root).resolve()
        self.result_root = Path(result_root).resolve()
        if self.result_root != self.workspace_root / "result":
            raise ProjectJobError(
                "TOOL_PERMISSION_DENIED",
                "Project repair result root must be workspace/result")
        self.provider_factory = provider_factory
        try:
            self.scheduler = SerialSessionScheduler(
                self.result_root, self._checkpoint_authority)
        except SessionSchedulerError as caught:
            raise ProjectJobError(caught.code, caught.message) from caught

    def _job_root(self, job_id: str) -> Path:
        if not re.fullmatch(r"JOB\.PROJECT\.[A-Z0-9_.-]+", job_id):
            raise ProjectJobError(
                "INVALID_INPUT", "Project repair Job identity is invalid")
        root = self.result_root / "jobs" / job_id
        try:
            if (root.is_symlink() or root.parent.resolve() !=
                    self.result_root / "jobs"):
                raise ValueError("unsafe Job root")
        except (OSError, ValueError) as caught:
            raise ProjectJobError(
                "TOOL_PERMISSION_DENIED",
                "Project repair Job root is invalid") from caught
        return root

    def _manifest(self, job_id: str) -> dict[str, Any]:
        job_root = self._job_root(job_id)
        path = job_root / INTERNAL_MANIFEST_PATH
        try:
            if not path.is_file() or path.is_symlink():
                raise OSError("manifest is unavailable")
            manifest = load_document(path)
            return validate_project_input(manifest, self.workspace_root)
        except ProjectJobError:
            raise
        except Exception as caught:
            raise ProjectJobError(
                "STALE_EVIDENCE",
                "Project repair manifest is unavailable or malformed") \
                from caught

    def _source_checkpoint(
            self, job_id: str,
            manifest: Mapping[str, Any] | None = None) -> dict[str, Any]:
        value = dict(manifest or self._manifest(job_id))
        path = (
            self._job_root(job_id) /
            "audit/oches001_awaiting_repair_plan.json")
        try:
            if not path.is_file() or path.is_symlink():
                raise OSError("checkpoint is unavailable")
            checkpoint = load_document(path)
        except Exception as caught:
            raise ProjectJobError(
                "STALE_EVIDENCE",
                "OCHES001 repair checkpoint is unavailable") from caught
        if (checkpoint.get("state") != "AWAITING_REPAIR_PLAN" or
                checkpoint.get("job_id") != job_id or
                checkpoint.get("input_fingerprint") !=
                    value.get("input_fingerprint") or
                checkpoint.get("checkpoint_fingerprint") !=
                    artifact_fingerprint(
                        checkpoint, "checkpoint_fingerprint")):
            raise ProjectJobError(
                "STALE_EVIDENCE",
                "OCHES001 repair checkpoint authority is stale")
        return checkpoint

    def _checkpoint_authority(self, job_id: str) -> tuple[str, str]:
        checkpoint = self._source_checkpoint(job_id)
        return (
            repair_checkpoint_id(checkpoint),
            checkpoint["checkpoint_fingerprint"],
        )

    def _terminal(
            self, job_root: Path, manifest: Mapping[str, Any]
            ) -> dict[str, Any] | None:
        workflow = ProjectJobWorkflow(self.workspace_root, self.result_root)
        return StagedProjectWorkflow(workflow)._load_terminal(
            job_root, dict(manifest))

    def _provider(
            self, job_root: Path, manifest: Mapping[str, Any],
            profile_role: str) -> Any:
        try:
            section, role = profile_role.split(".", 1)
            expected = agent_binding(manifest, section, role)
            provider = self.provider_factory(
                job_root, copy.deepcopy(dict(manifest)), profile_role)
        except ProjectJobError:
            raise
        except Exception as caught:
            raise ProjectJobError(
                "INVALID_AGENT_BINDING",
                "repair Provider could not be created from the Job snapshot") \
                from caught
        if provider is None:
            raise ProjectJobError(
                "BLOCKED_TOOL", "repair Provider is unavailable")
        provider_id = getattr(provider, "provider_id", None)
        model_id = getattr(provider, "model_id", None)
        if (provider_id is not None and
                provider_id != expected["provider_id"]) or (
                model_id is not None and model_id != expected["model_id"]):
            raise ProjectJobError(
                "INVALID_AGENT_BINDING",
                "repair Provider identity differs from the Job snapshot")
        probe_workflow = ProjectJobWorkflow(
            self.workspace_root, self.result_root,
            role_providers={profile_role: provider})
        probe_workflow._probe_provider(job_root, profile_role)
        return provider

    @staticmethod
    def _session_ids(
            job_id: str, checkpoint_fingerprint: str,
            runtime_protocol: str = RUNTIME_PROTOCOL,
            ) -> tuple[str, str]:
        token = canonical_hash({
            "job_id": job_id,
            "checkpoint_fingerprint": checkpoint_fingerprint,
            "runtime": runtime_protocol,
        })[:24].upper()
        return (
            "PLANNING.OCHES002.{}".format(token),
            "STAGESESSION.OCHES002.{}".format(token),
        )

    @staticmethod
    def _legacy_planning_session_id(
            job_id: str, checkpoint_fingerprint: str) -> str:
        token = canonical_hash({
            "job_id": job_id,
            "checkpoint_fingerprint": checkpoint_fingerprint,
            "runtime": LEGACY_RUNTIME_PROTOCOL,
        })[:24].upper()
        return "PLANNING.OCHES002.{}".format(token)

    def _retry_legacy_rejected_plan(
            self, job_root: Path, job_id: str, checkpoint_id: str,
            checkpoint_fingerprint: str,
            state: Mapping[str, Any]) -> bool:
        if (state.get("state") != "FAILED" or
                state.get("attempt") != 1 or
                state.get("event", {}).get("diagnostic_code") !=
                    "REPAIR_PLAN_REJECTED"):
            return False
        legacy = self._legacy_planning_session_id(
            job_id, checkpoint_fingerprint)
        current, _ = self._session_ids(job_id, checkpoint_fingerprint)
        current_dir = job_root / "transcripts/orchestrator" / current
        if current_dir.exists():
            return False
        try:
            model = ProjectReadModel.from_checkpoint(
                job_root, self._source_checkpoint(job_id))
        except Exception as caught:
            raise ProjectJobError(
                "STALE_EVIDENCE",
                "legacy rejected-plan evidence is malformed") from caught
        rejected = [
            item for item in model.history
            if item.get("record_type") == "REJECTED_REPAIR_PLAN" and
            item.get("record", {}).get("planning_session_id") == legacy and
            item.get("record", {}).get("plan_fingerprint") !=
                artifact_fingerprint(item["record"], "plan_fingerprint")
        ]
        if len(rejected) != 1:
            raise ProjectJobError(
                "STALE_EVIDENCE",
                "legacy retry does not bind the rejected formal plan")
        matching_receipts = [
            item["record"] for item in model.history
            if item.get("record_type") == "ROUTER_RECEIPT" and
            item.get("record", {}).get("plan_id") ==
                rejected[0]["record"].get("plan_id") and
            item.get("record", {}).get("plan_fingerprint") ==
                rejected[0]["record"].get("plan_fingerprint")
        ]
        if (len(matching_receipts) != 1 or
                matching_receipts[0]["diagnostic"]["code"] !=
                    "STALE_EVIDENCE"):
            raise ProjectJobError(
                "STALE_EVIDENCE",
                "legacy retry diagnostic does not match the rejected plan")
        self.scheduler.retry_failed(
            job_id=job_id, checkpoint_id=checkpoint_id,
            checkpoint_fingerprint=checkpoint_fingerprint,
            diagnostic_code="REPAIR_PLAN_REJECTED")
        return True

    def _retry_rejected_history_read_model_failure(
            self, job_root: Path, job_id: str, checkpoint_id: str,
            checkpoint_fingerprint: str,
            state: Mapping[str, Any]) -> bool:
        if (state.get("state") != "FAILED" or
                state.get("attempt") != 2 or
                state.get("event", {}).get("diagnostic_code") !=
                    "STALE_EVIDENCE"):
            return False
        expected = [
            ("ENQUEUED", "NONE"),
            ("STARTED", "NONE"),
            ("FAILED", "REPAIR_PLAN_REJECTED"),
            ("REQUEUED", "REPAIR_PLAN_REJECTED"),
            ("STARTED", "NONE"),
            ("FAILED", "STALE_EVIDENCE"),
        ]
        events = self.scheduler.events_for_job(job_id)
        if [(item["event"], item["diagnostic_code"])
                for item in events] != expected:
            raise ProjectJobError(
                "STALE_EVIDENCE",
                "rejected-plan recovery queue history is not exact")
        current, _ = self._session_ids(job_id, checkpoint_fingerprint)
        if (job_root / "transcripts/orchestrator" / current).exists():
            raise ProjectJobError(
                "STALE_EVIDENCE",
                "attempt 2 reached the new Orchestrator unexpectedly")
        try:
            model = ProjectReadModel.from_checkpoint(
                job_root, self._source_checkpoint(job_id))
        except ProjectToolError as caught:
            raise ProjectJobError(caught.code, caught.message) from caught
        rejected = [
            item for item in model.history
            if item.get("record_type") == "REJECTED_REPAIR_PLAN" and
            item.get("authority_status") == "REJECTED_UNTRUSTED_PLAN" and
            item.get("record", {}).get("plan_fingerprint") !=
                artifact_fingerprint(item["record"], "plan_fingerprint")
        ]
        if len(rejected) != 1:
            raise ProjectJobError(
                "STALE_EVIDENCE",
                "attempt 2 failure does not bind one rejected legacy plan")
        self.scheduler.retry_failed(
            job_id=job_id, checkpoint_id=checkpoint_id,
            checkpoint_fingerprint=checkpoint_fingerprint,
            diagnostic_code="STALE_EVIDENCE")
        return True

    @staticmethod
    def _provider_retry_used(events: list[Mapping[str, Any]]) -> bool:
        return any(
            item.get("event") == "REQUEUED" and
            item.get("diagnostic_code") == "PROVIDER_UNAVAILABLE"
            for item in events)

    def _retry_provider_contract_failure(
            self, job_root: Path, job_id: str, checkpoint_id: str,
            checkpoint_fingerprint: str, state: Mapping[str, Any]) -> bool:
        """Requeue one request-only Provider failure under a fresh session."""
        if (state.get("state") != "FAILED" or
                state.get("event", {}).get("diagnostic_code") !=
                    "PROVIDER_UNAVAILABLE"):
            return False
        events = self.scheduler.events_for_job(job_id)
        if self._provider_retry_used(events):
            return False
        if (len(events) < 2 or
                [(item["event"], item["diagnostic_code"])
                 for item in events[-2:]] != [
                    ("STARTED", "NONE"),
                    ("FAILED", "PROVIDER_UNAVAILABLE"),
                ]):
            raise ProjectJobError(
                "STALE_EVIDENCE",
                "Provider recovery queue history is not exact")

        manifest = self._manifest(job_id)
        source = self._source_checkpoint(job_id, manifest)
        runtime = ProjectRepairRuntime(
            job_root=job_root, checkpoint=source,
            project_input=manifest, error=ProjectJobError)
        terminal = self._terminal(job_root, manifest)
        old_planning, old_stage = self._session_ids(
            job_id, checkpoint_fingerprint)
        new_planning, new_stage = self._session_ids(
            job_id, checkpoint_fingerprint,
            PROVIDER_RETRY_RUNTIME_PROTOCOL)

        if terminal is not None and terminal.get("state") == \
                "AWAITING_REPAIR_PLAN":
            role = "ORCHESTRATOR"
            old_session, new_session = old_planning, new_planning
            lineage = {
                "input_fingerprint": runtime.model.input_fingerprint,
                "source_report_fingerprint":
                    runtime.model.report["report_fingerprint"],
                "artifact_root": runtime.model.artifact_root,
                **runtime._role_binding(
                    "repair", "orchestrator", "ORCHESTRATOR", "PROFILED"),
            }
            for path in job_root.glob(
                    "staging/orchestrator/repair_plan.*.json"):
                try:
                    if load_document(path).get("planning_session_id") == \
                            old_session:
                        raise ProjectJobError(
                            "INVALID_RETRY_STATE",
                            "Provider failure already produced a repair plan")
                except ProjectJobError:
                    raise
                except Exception as caught:
                    raise ProjectJobError(
                        "STALE_EVIDENCE",
                        "repair plan evidence is malformed") from caught
        elif terminal is not None and terminal.get("state") == \
                "AWAITING_SCOPED_REPLACEMENT":
            try:
                dispatch = load_document(job_root / terminal["dispatch_path"])
                receipt = load_document(
                    job_root / terminal["router_receipt_path"])
            except Exception as caught:
                raise ProjectJobError(
                    "STALE_EVIDENCE",
                    "scoped dispatch or Router receipt is unavailable") \
                    from caught
            if (not accepted(validate("project_formal_dispatch", dispatch)) or
                    dispatch.get("dispatch_fingerprint") !=
                        artifact_fingerprint(
                            dispatch, "dispatch_fingerprint") or
                    terminal.get("dispatch_fingerprint") !=
                        dispatch.get("dispatch_fingerprint") or
                    dispatch.get("job_id") != job_id or
                    not accepted(validate("project_router_receipt", receipt)) or
                    receipt.get("receipt_fingerprint") !=
                        artifact_fingerprint(
                            receipt, "receipt_fingerprint") or
                    receipt.get("job_id") != job_id or
                    receipt.get("status") != "ACCEPTED" or
                    receipt.get("formal_dispatch_id") !=
                        dispatch.get("dispatch_id") or
                    receipt.get("plan_id") != dispatch.get("plan_id") or
                    receipt.get("plan_fingerprint") !=
                        dispatch.get("plan_fingerprint")):
                raise ProjectJobError(
                    "STALE_EVIDENCE",
                    "scoped dispatch or Router receipt lineage is stale")
            stage = dispatch.get("stage")
            role_name = {
                "STAGE_1": "stage1", "STAGE_2": "stage2",
                "STAGE_3": "stage3",
            }.get(stage)
            if role_name is None:
                raise ProjectJobError(
                    "STALE_EVIDENCE", "scoped dispatch Stage is invalid")
            role = str(stage)
            old_session, new_session = old_stage, new_stage
            lineage = {
                "dispatch_id": dispatch["dispatch_id"],
                "dispatch_fingerprint": dispatch["dispatch_fingerprint"],
                "scope_fingerprint": dispatch["scope_fingerprint"],
                "artifact_root": runtime.model.artifact_root,
                **runtime._role_binding(
                    "repair", role_name, "STAGE_AGENT", "PROFILED"),
            }
            runtime.validate_stage_authority(dispatch)
        else:
            raise ProjectJobError(
                "INVALID_RETRY_STATE",
                "Provider failure is outside a recoverable repair state")

        try:
            transcript = load_terminal_transcript(
                job_root=job_root, job_id=job_id, role=role,
                session_id=old_session, lineage=lineage)
        except ToolSessionError as caught:
            raise ProjectJobError(caught.code, caught.message) from caught
        if transcript["terminal"] != {
                "status": "FAILED", "code": "PROVIDER_UNAVAILABLE",
                "result_sequence": None,
                }:
            raise ProjectJobError(
                "INVALID_RETRY_STATE",
                "transcript is not the exact Provider contract failure")
        entries = transcript["entries"]
        if len(entries) % 4 != 1 or any(
                item["sequence"] != index + 1 or
                item["kind"] != (
                    "REQUEST", "RESPONSE", "TOOL_CALL", "TOOL_RESULT"
                )[index % 4]
                for index, item in enumerate(entries)):
            raise ProjectJobError(
                "INVALID_RETRY_STATE",
                "Provider failure is not request-only")
        request_entry = entries[-1]
        role_directory = {
            "ORCHESTRATOR": "orchestrator", "STAGE_1": "stage1",
            "STAGE_2": "stage2", "STAGE_3": "stage3",
        }[role]
        request_path = (
            job_root / "transcripts" / role_directory / old_session /
            request_entry["path"])
        try:
            request_bytes = request_path.read_bytes()
            request = load_document(request_path)
        except Exception as caught:
            raise ProjectJobError(
                "STALE_EVIDENCE", "Provider request evidence is malformed") \
                from caught
        if (request_path.is_symlink() or
                hashlib.sha256(request_bytes).hexdigest() !=
                    request_entry["content_fingerprint"] or
                not accepted(validate("provider_request", request)) or
                request.get("metadata", {}).get("job_id") != job_id or
                request.get("metadata", {}).get("role") != role or
                request.get("metadata", {}).get("session_id") != old_session):
            raise ProjectJobError(
                "STALE_EVIDENCE", "Provider request authority is stale")

        if (job_root / "transcripts" / role_directory / new_session).exists():
            raise ProjectJobError(
                "STALE_EVIDENCE",
                "Provider retry session already contains evidence")
        self.scheduler.retry_failed(
            job_id=job_id, checkpoint_id=checkpoint_id,
            checkpoint_fingerprint=checkpoint_fingerprint,
            diagnostic_code="PROVIDER_UNAVAILABLE")
        return True

    def _execute(
            self, job_id: str, checkpoint_id: str,
            cancel_requested: Callable[[], bool]) -> dict[str, Any]:
        if cancel_requested():
            from core.tool_session import ToolSessionError
            raise ToolSessionError(
                "CANCELLED", "Job was cancelled before runtime recovery")
        manifest = self._manifest(job_id)
        source = self._source_checkpoint(job_id, manifest)
        if checkpoint_id != repair_checkpoint_id(source):
            raise ProjectJobError(
                "STALE_EVIDENCE",
                "FIFO execution does not bind the exact repair checkpoint")
        job_root = self._job_root(job_id)
        runtime = ProjectRepairRuntime(
            job_root=job_root, checkpoint=source,
            project_input=manifest, error=ProjectJobError)
        events = self.scheduler.events_for_job(job_id)
        state = self.scheduler.state()[job_id]
        attempt = int(state["attempt"])
        provider_retry_ids = self._session_ids(
            job_id, source["checkpoint_fingerprint"],
            PROVIDER_RETRY_RUNTIME_PROTOCOL)
        provider_retry_unused = (
            self._provider_retry_used(events) and
            not any((job_root / "transcripts" / role / session).exists()
                    for role, session in (
                        ("orchestrator", provider_retry_ids[0]),
                        ("stage1", provider_retry_ids[1]),
                        ("stage2", provider_retry_ids[1]),
                        ("stage3", provider_retry_ids[1]))))
        if attempt == 1:
            runtime_protocol = RUNTIME_PROTOCOL
        elif provider_retry_unused:
            # Preserve the already-authorized R1 recovery identity. Every
            # later attempt receives a unique protocol/session identity.
            runtime_protocol = PROVIDER_RETRY_RUNTIME_PROTOCOL
        else:
            runtime_protocol = "OCHES002_RECOVERY_ATTEMPT_{:06d}".format(
                attempt)
        planning_session_id, stage_session_id = self._session_ids(
            job_id, source["checkpoint_fingerprint"], runtime_protocol)
        terminal = self._terminal(job_root, manifest)
        if terminal is None or terminal.get("state") not in \
                REPAIR_RUNTIME_STATES:
            raise ProjectJobError(
                "INVALID_RETRY_STATE",
                "Job is not at an OCHES002 repair runtime checkpoint")
        if terminal["state"] == "SCOPED_REPLACEMENT_VALIDATED":
            return {
                "status": "VALIDATED",
                "checkpoint": terminal,
                "current_state_modified": False,
            }

        dispatch = None
        receipt = None
        if terminal["state"] == "AWAITING_REPAIR_PLAN":
            orchestrator = self._provider(
                job_root, manifest, "repair.orchestrator")
            planned = runtime.run_orchestrator(
                orchestrator, planning_session_id, cancel_requested)
            receipt = planned.get("receipt")
            dispatch = planned.get("dispatch")
            if planned.get("status") != "ACCEPTED" or dispatch is None:
                raise ProjectJobError(
                    "REPAIR_PLAN_REJECTED",
                    "Orchestrator plan did not produce an accepted dispatch")
        else:
            dispatch = load_document(job_root / terminal["dispatch_path"])
            if terminal.get("dispatch_fingerprint") != \
                    dispatch.get("dispatch_fingerprint"):
                raise ProjectJobError(
                    "STALE_EVIDENCE", "scoped dispatch lineage is stale")
            receipt = load_document(
                job_root / terminal["router_receipt_path"])
            if (not accepted(validate("project_router_receipt", receipt)) or
                    receipt.get("receipt_fingerprint") !=
                        artifact_fingerprint(
                            receipt, "receipt_fingerprint") or
                    receipt.get("job_id") != job_id or
                    receipt.get("status") != "ACCEPTED" or
                    receipt.get("formal_dispatch_id") !=
                        dispatch.get("dispatch_id") or
                    receipt.get("plan_id") != dispatch.get("plan_id") or
                    receipt.get("plan_fingerprint") !=
                        dispatch.get("plan_fingerprint")):
                raise ProjectJobError(
                    "STALE_EVIDENCE", "Router receipt lineage is stale")

        if cancel_requested():
            from core.tool_session import ToolSessionError
            raise ToolSessionError(
                "CANCELLED", "Job was cancelled before the Stage session")
        stage = str(dispatch.get("stage"))
        profile_role = {
            "STAGE_1": "repair.stage1",
            "STAGE_2": "repair.stage2",
            "STAGE_3": "repair.stage3",
        }.get(stage)
        if profile_role is None:
            raise ProjectJobError(
                "STALE_DISPATCH", "formal dispatch Stage is invalid")
        runtime.validate_stage_authority(dispatch)
        stage_provider = self._provider(
            job_root, manifest, profile_role)
        result = runtime.run_stage(
            stage_provider, dispatch, stage_session_id, cancel_requested)
        return {
            "status": "VALIDATED",
            "receipt": receipt,
            "dispatch": dispatch,
            **result,
        }

    def _advance(self, job_id: str) -> dict[str, Any]:
        manifest = self._manifest(job_id)
        source = self._source_checkpoint(job_id, manifest)
        checkpoint_id = repair_checkpoint_id(source)
        self.scheduler.enqueue(
            job_id, checkpoint_id, source["checkpoint_fingerprint"])

        initial = self.scheduler.state()[job_id]
        if initial["state"] in {"FAILED", "CANCELLED"}:
            diagnostic = initial["event"].get(
                "diagnostic_code", initial["state"])
            if diagnostic in NON_RECOVERABLE_CODES:
                raise ProjectJobError(
                    diagnostic,
                    "Project repair Job reached a non-recoverable terminal")
            self.scheduler.retry_failed(
                job_id=job_id, checkpoint_id=checkpoint_id,
                checkpoint_fingerprint=source["checkpoint_fingerprint"],
                diagnostic_code=diagnostic)

        while True:
            state = self.scheduler.state()[job_id]
            if state["state"] == "COMPLETED":
                terminal = self._terminal(self._job_root(job_id), manifest)
                if (terminal is None or terminal.get("state") !=
                        "SCOPED_REPLACEMENT_VALIDATED"):
                    raise ProjectJobError(
                        "STALE_EVIDENCE",
                        "completed FIFO Job lacks validated replacement")
                return terminal
            if state["state"] == "FAILED":
                code = state["event"].get(
                    "diagnostic_code", "SESSION_FAILED")
                return {
                    "schema_version": "1.0",
                    "state": stop_status(code),
                    "job_id": job_id,
                    "checkpoint_id": checkpoint_id,
                    "checkpoint_fingerprint":
                        source["checkpoint_fingerprint"],
                    "failed_attempt": state["attempt"],
                    "diagnostic": {
                        "code": code,
                        "message": (
                            "repair attempt failed; rerun the same Job to "
                            "start a fresh session attempt"),
                    },
                    "current_state_modified": False,
                }
            if state["state"] == "CANCELLED":
                return {
                    "schema_version": "1.0",
                    "state": "PAUSED_BY_HUMAN",
                    "job_id": job_id,
                    "checkpoint_id": checkpoint_id,
                    "checkpoint_fingerprint":
                        source["checkpoint_fingerprint"],
                    "current_state_modified": False,
                }
            event = self.scheduler.run_next(self._execute)
            if event is None:
                raise ProjectJobError(
                    "INVALID_TRANSITION",
                    "FIFO could not advance the queued repair Job")

    def advance(self, job_id: str) -> dict[str, Any]:
        """Enqueue a Job and drain FIFO until this Job is terminal."""
        try:
            return self._advance(job_id)
        except SessionSchedulerError as caught:
            raise ProjectJobError(caught.code, caught.message) from caught

    def recover_rejected_plan_failure(self, job_id: str) -> dict[str, Any]:
        """Append only the narrowly scoped attempt-2 recovery event."""
        try:
            manifest = self._manifest(job_id)
            source = self._source_checkpoint(job_id, manifest)
            checkpoint_id = repair_checkpoint_id(source)
            state = self.scheduler.state().get(job_id)
            if state is None or not self._retry_rejected_history_read_model_failure(
                    self._job_root(job_id), job_id, checkpoint_id,
                    source["checkpoint_fingerprint"], state):
                raise ProjectJobError(
                    "INVALID_RETRY_STATE",
                    "Job is not the exact recoverable attempt-2 failure")
            return self.scheduler.state()[job_id]
        except SessionSchedulerError as caught:
            raise ProjectJobError(caught.code, caught.message) from caught

    def recover_provider_failure(self, job_id: str) -> dict[str, Any]:
        """Append one exact request-only Provider failure recovery event."""
        try:
            manifest = self._manifest(job_id)
            source = self._source_checkpoint(job_id, manifest)
            checkpoint_id = repair_checkpoint_id(source)
            state = self.scheduler.state().get(job_id)
            if state is None or not self._retry_provider_contract_failure(
                    self._job_root(job_id), job_id, checkpoint_id,
                    source["checkpoint_fingerprint"], state):
                raise ProjectJobError(
                    "INVALID_RETRY_STATE",
                    "Job is not an exact recoverable Provider failure")
            return self.scheduler.state()[job_id]
        except SessionSchedulerError as caught:
            raise ProjectJobError(caught.code, caught.message) from caught


__all__ = [
    "ProjectJobRuntimeIntegration", "REPAIR_RUNTIME_STATES",
    "ProviderFactory", "PROVIDER_RETRY_RUNTIME_PROTOCOL",
    "RUNTIME_PROTOCOL", "repair_checkpoint_id",
]
