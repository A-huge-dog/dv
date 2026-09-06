"""The single Project-level control loop for a persisted DV Job.

This module deliberately does not implement generation, review, repair,
compile, commit, or Agent protocols.  Those actions remain owned by their
REF-004 application handlers and existing runtime integrations.  The loop
only resolves one authoritative checkpoint, executes one transition, and
continues while the resulting state is automatic.
"""
from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping

from application.bootstrap import BootstrapResult
from application.project_execution import (
    APPROVED_TESTCASE_PATH,
    EXECUTION_AUTHORIZATION_PATH,
    EXECUTION_BUNDLE_PATH,
    EXECUTION_EVIDENCE_PATH,
    EXECUTION_REQUEST_PATH,
    EXECUTION_RESULT_PATH,
    BindApprovedBundleHandler,
    BindApprovedBundleInput,
    ExecuteApprovedTestcaseHandler,
    ExecuteApprovedTestcaseInput,
    RecordExecutionAuthorizationHandler,
    RecordExecutionAuthorizationInput,
    RecordHumanDecisionHandler,
    RecordHumanDecisionInput,
)
from contracts.validator import accepted, load_document, validate
from agents.profile import ROLE_PATHS
from domain.artifacts import artifact_fingerprint
from runtime.errors import ProjectJobError
from runtime.project_job import ProjectJobWorkflow
from scripts.dvlib import canonical_hash


def _tree_digest(path: Path) -> tuple[str, int, int]:
    """Verify one explicitly named adapter output root, including extras."""
    if not path.is_dir() or path.is_symlink():
        raise ProjectJobError(
            "STALE_EVIDENCE", "adapter output root is missing or unsafe")
    inventory = []
    size = 0
    for child in sorted(path.rglob("*")):
        if child.is_symlink():
            try:
                target = child.readlink()
                resolved = child.resolve(strict=True)
                resolved.relative_to(path.resolve())
            except (OSError, ValueError) as caught:
                raise ProjectJobError(
                    "STALE_EVIDENCE",
                    "adapter output symlink escapes the output tree") from caught
            if target.is_absolute() or not (resolved.is_file() or
                                            resolved.is_dir()):
                raise ProjectJobError(
                    "STALE_EVIDENCE", "adapter output symlink is unsafe")
            inventory.append({
                "path": child.relative_to(path).as_posix(),
                "symlink_target": str(target),
            })
            continue
        if child.is_file():
            content = child.read_bytes()
            size += len(content)
            inventory.append({
                "path": child.relative_to(path).as_posix(),
                "fingerprint": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            })
    if not inventory:
        raise ProjectJobError("STALE_EVIDENCE", "adapter output tree is empty")
    return canonical_hash(inventory), size, len(inventory)


class WorkflowState(str, Enum):
    """Persisted business states understood by the Project loop."""

    BOOTSTRAP = "BOOTSTRAP"
    INITIAL_GENERATION = "INITIAL_GENERATION"
    AWAITING_SCENARIO_ROUTING = "AWAITING_SCENARIO_ROUTING"
    AWAITING_REPAIR_PLAN = "AWAITING_REPAIR_PLAN"
    AWAITING_SCOPED_REPLACEMENT = "AWAITING_SCOPED_REPLACEMENT"
    SCOPED_REPLACEMENT_VALIDATED = "SCOPED_REPLACEMENT_VALIDATED"
    AWAITING_HUMAN_REVIEW = "AWAITING_HUMAN_REVIEW"
    AWAITING_EXECUTION_AUTHORIZATION = "AWAITING_EXECUTION_AUTHORIZATION"
    READY_FOR_BINDING = "READY_FOR_BINDING"
    READY_FOR_EXECUTION = "READY_FOR_EXECUTION"
    EXECUTION_PASS = "EXECUTION_PASS"
    EXECUTION_FAIL = "EXECUTION_FAIL"
    EXECUTION_BLOCKED = "EXECUTION_BLOCKED"
    PAUSED_BUDGET = "PAUSED_BUDGET"
    PAUSED_RETRYABLE = "PAUSED_RETRYABLE"
    PAUSED_RECOVERY_REQUIRED = "PAUSED_RECOVERY_REQUIRED"
    BLOCKED_INPUT = "BLOCKED_INPUT"
    BLOCKED_TOOL = "BLOCKED_TOOL"
    CANCELLED = "CANCELLED"
    FAILED_POLICY = "FAILED_POLICY"
    FAILED_INTERNAL = "FAILED_INTERNAL"
    PAUSED_COMPILE_REPAIR_REQUIRED = "PAUSED_COMPILE_REPAIR_REQUIRED"
    PAUSED_BY_HUMAN = "PAUSED_BY_HUMAN"
    CANDIDATE_ATTEMPT_PAUSED = "CANDIDATE_ATTEMPT_PAUSED"
    REJECTED_CANDIDATE_CORRECTABLE = "REJECTED_CANDIDATE_CORRECTABLE"
    REJECTED_FAIL_CLOSED = "REJECTED_FAIL_CLOSED"
    SPEC_ISSUES_RECORDED = "SPEC_ISSUES_RECORDED"
    REVIEW_REVISION_REQUIRED = "REVIEW_REVISION_REQUIRED"
    REVIEW_SPEC_AMBIGUITY = "REVIEW_SPEC_AMBIGUITY"
    TERMINAL = "TERMINAL"


class ResumePolicy(str, Enum):
    """Who is allowed to move forward from a Project business state."""

    AUTO = "AUTO"
    HUMAN = "HUMAN"
    OPERATOR = "OPERATOR"
    RETRY = "RETRY"
    TERMINAL = "TERMINAL"


@dataclass(frozen=True)
class Checkpoint:
    """One verified view of the already-authoritative Job state."""

    state: WorkflowState
    policy: ResumePolicy
    value: dict[str, Any]
    source_path: str | None


@dataclass(frozen=True)
class ProjectLoopRequest:
    """CLI-owned input and an optional explicit Human submission."""

    submission: dict[str, Any]
    submission_bytes: bytes | None = None
    human_decision: dict[str, Any] | None = None
    execution_authorization: dict[str, Any] | None = None
    scenario_routing: dict[str, Any] | None = None
    retry_blocked_review: bool = False


@dataclass(frozen=True)
class Transition:
    """State-to-handler ownership map; execution is exactly one action."""

    state: WorkflowState
    policy: ResumePolicy
    handler: str
    execute: Callable[[ProjectLoopRequest, Mapping[str, Any]], dict[str, Any]]


class CheckpointRepository:
    """Read and publish *existing* checkpoint authorities in one place.

    It uses only fixed, schema-owned paths.  In particular, it never scans
    for a newest Job, revision, artifact, or checkpoint.  Publication is
    intentionally validation-only: action handlers remain the sole writers
    of their own immutable artifacts.
    """

    _PATHS = {
        "AWAITING_HUMAN_REVIEW": "audit/oches001_human_review_checkpoint.json",
        "AWAITING_EXECUTION_AUTHORIZATION":
            "audit/pj003_awaiting_execution_authorization.json",
        "READY_FOR_BINDING": "audit/pj003_ready_for_binding.json",
        "READY_FOR_EXECUTION": "audit/pj003_ready_for_execution.json",
        "EXECUTION_PASS": EXECUTION_RESULT_PATH,
        "EXECUTION_FAIL": EXECUTION_RESULT_PATH,
        "EXECUTION_BLOCKED": EXECUTION_RESULT_PATH,
        "PAUSED_BY_HUMAN": "audit/pj003_paused_by_human.json",
        "SCOPED_REPLACEMENT_VALIDATED": (
            "audit/oches002_scoped_replacement_validated.json"),
        "AWAITING_SCOPED_REPLACEMENT": (
            "audit/oches002_awaiting_scoped_replacement.json"),
        "AWAITING_REPAIR_PLAN": "audit/oches001_awaiting_repair_plan.json",
        "REVIEW_REVISION_REQUIRED": "audit/pj002_review_fail_closed.json",
        "REVIEW_SPEC_AMBIGUITY": "audit/pj002_review_fail_closed.json",
    }

    def __init__(self, workflow: ProjectJobWorkflow):
        self.workflow = workflow

    @staticmethod
    def _state(value: Mapping[str, Any]) -> WorkflowState:
        raw = value.get("state")
        try:
            return WorkflowState(str(raw))
        except ValueError as caught:
            raise ProjectJobError(
                "INVALID_TRANSITION",
                "Project checkpoint has an unsupported workflow state",
            ) from caught

    @staticmethod
    def policy_for(state: WorkflowState) -> ResumePolicy:
        return {
            WorkflowState.BOOTSTRAP: ResumePolicy.AUTO,
            WorkflowState.INITIAL_GENERATION: ResumePolicy.AUTO,
            WorkflowState.AWAITING_REPAIR_PLAN: ResumePolicy.AUTO,
            WorkflowState.AWAITING_SCOPED_REPLACEMENT: ResumePolicy.AUTO,
            WorkflowState.SCOPED_REPLACEMENT_VALIDATED: ResumePolicy.AUTO,
            WorkflowState.AWAITING_SCENARIO_ROUTING: ResumePolicy.HUMAN,
            WorkflowState.AWAITING_HUMAN_REVIEW: ResumePolicy.HUMAN,
            WorkflowState.AWAITING_EXECUTION_AUTHORIZATION: ResumePolicy.HUMAN,
            WorkflowState.READY_FOR_BINDING: ResumePolicy.AUTO,
            WorkflowState.READY_FOR_EXECUTION: ResumePolicy.AUTO,
            WorkflowState.EXECUTION_PASS: ResumePolicy.TERMINAL,
            WorkflowState.EXECUTION_FAIL: ResumePolicy.TERMINAL,
            WorkflowState.EXECUTION_BLOCKED: ResumePolicy.TERMINAL,
            WorkflowState.PAUSED_BY_HUMAN: ResumePolicy.HUMAN,
            WorkflowState.PAUSED_BUDGET: ResumePolicy.RETRY,
            WorkflowState.PAUSED_RETRYABLE: ResumePolicy.RETRY,
            WorkflowState.PAUSED_RECOVERY_REQUIRED: ResumePolicy.OPERATOR,
            WorkflowState.BLOCKED_INPUT: ResumePolicy.OPERATOR,
            WorkflowState.BLOCKED_TOOL: ResumePolicy.OPERATOR,
            WorkflowState.CANCELLED: ResumePolicy.TERMINAL,
            WorkflowState.FAILED_POLICY: ResumePolicy.TERMINAL,
            WorkflowState.FAILED_INTERNAL: ResumePolicy.TERMINAL,
            WorkflowState.PAUSED_COMPILE_REPAIR_REQUIRED: ResumePolicy.RETRY,
            WorkflowState.SPEC_ISSUES_RECORDED: ResumePolicy.OPERATOR,
            WorkflowState.REVIEW_REVISION_REQUIRED: ResumePolicy.OPERATOR,
            WorkflowState.REVIEW_SPEC_AMBIGUITY: ResumePolicy.OPERATOR,
            WorkflowState.CANDIDATE_ATTEMPT_PAUSED: ResumePolicy.RETRY,
            WorkflowState.REJECTED_CANDIDATE_CORRECTABLE: ResumePolicy.RETRY,
            WorkflowState.REJECTED_FAIL_CLOSED: ResumePolicy.OPERATOR,
            WorkflowState.TERMINAL: ResumePolicy.TERMINAL,
        }[state]

    @staticmethod
    def _load_fixed(path: Path, description: str) -> dict[str, Any]:
        if not path.is_file() or path.is_symlink():
            raise ProjectJobError(
                "STALE_EVIDENCE", "{} is missing or unsafe".format(description))
        try:
            return load_document(path)
        except Exception as caught:
            raise ProjectJobError(
                "STALE_EVIDENCE", "{} is malformed".format(description)) \
                from caught

    def _read_pj003(
            self, job_root: Path, manifest: Mapping[str, Any]
            ) -> Checkpoint | None:
        execution_children = [
            (job_root / item).exists() for item in (
                EXECUTION_REQUEST_PATH, EXECUTION_EVIDENCE_PATH)]
        if any(execution_children) and not (
                all(execution_children) and
                (job_root / EXECUTION_RESULT_PATH).is_file()):
            raise ProjectJobError(
                "PARTIAL_ARTIFACT",
                "Project execution request/evidence/checkpoint is incomplete")
        ordered = (
            (EXECUTION_RESULT_PATH, {
                WorkflowState.EXECUTION_PASS,
                WorkflowState.EXECUTION_FAIL,
                WorkflowState.EXECUTION_BLOCKED,
            }),
            ("audit/pj003_ready_for_execution.json",
             {WorkflowState.READY_FOR_EXECUTION}),
            ("audit/pj003_ready_for_binding.json",
             {WorkflowState.READY_FOR_BINDING}),
            ("audit/pj003_awaiting_execution_authorization.json",
             {WorkflowState.AWAITING_EXECUTION_AUTHORIZATION}),
            ("audit/pj003_paused_by_human.json",
             {WorkflowState.PAUSED_BY_HUMAN}),
        )
        for relative, expected_states in ordered:
            path = job_root / relative
            if not path.exists():
                continue
            value = self._load_fixed(path, "PJ-003 checkpoint")
            state = self._state(value)
            if state not in expected_states or \
                    value.get("job_id") != manifest["job_id"] or \
                    value.get("input_fingerprint") != manifest["input_fingerprint"] or \
                    value.get("checkpoint_fingerprint") != artifact_fingerprint(
                        value, "checkpoint_fingerprint"):
                raise ProjectJobError(
                    "STALE_EVIDENCE", "PJ-003 checkpoint is stale or cross-Job")
            if not accepted(validate("project_execution_checkpoint", value)):
                raise ProjectJobError(
                    "INVALID_SCHEMA", "PJ-003 checkpoint contract is invalid")
            authority = self._load_fixed(
                job_root / value["authority_path"], "PJ-003 authority")
            field = {
                WorkflowState.AWAITING_EXECUTION_AUTHORIZATION:
                    "authority_fingerprint",
                WorkflowState.READY_FOR_BINDING: "authorization_fingerprint",
                WorkflowState.READY_FOR_EXECUTION: "binding_fingerprint",
                WorkflowState.EXECUTION_PASS: "evidence_fingerprint",
                WorkflowState.EXECUTION_FAIL: "evidence_fingerprint",
                WorkflowState.EXECUTION_BLOCKED: "evidence_fingerprint",
            }.get(state, "decision_fingerprint")
            actual_authority = (
                canonical_hash(authority)
                if state is WorkflowState.PAUSED_BY_HUMAN
                else authority.get(field))
            valid_authority = (
                actual_authority == canonical_hash(authority)
                if state is WorkflowState.PAUSED_BY_HUMAN
                else actual_authority == artifact_fingerprint(authority, field))
            if actual_authority != value.get("authority_fingerprint") or \
                    not valid_authority:
                raise ProjectJobError(
                    "STALE_EVIDENCE", "PJ-003 checkpoint authority is stale")
            if state in {
                    WorkflowState.EXECUTION_PASS,
                    WorkflowState.EXECUTION_FAIL,
                    WorkflowState.EXECUTION_BLOCKED}:
                self._validate_execution_replay(job_root, authority, state)
            return Checkpoint(state, self.policy_for(state), value, relative)
        return None

    def _validate_execution_replay(
            self, job_root: Path, evidence: Mapping[str, Any],
            state: WorkflowState) -> None:
        if (not accepted(validate("project_execution_evidence", dict(evidence))) or
                evidence.get("execution_status") !=
                    state.value.removeprefix("EXECUTION_")):
            raise ProjectJobError(
                "STALE_EVIDENCE", "Project execution result is inconsistent")
        request = self._load_fixed(
            job_root / EXECUTION_REQUEST_PATH, "Project execution request")
        if (not accepted(validate("project_execution_request", request)) or
                request.get("request_fingerprint") != artifact_fingerprint(
                    request, "request_fingerprint") or
                evidence.get("request_fingerprint") !=
                    request.get("request_fingerprint")):
            raise ProjectJobError(
                "STALE_EVIDENCE", "Project execution request is stale")
        for summary in (evidence["build"], evidence.get("run")):
            if summary is None:
                continue
            adapter_request = self._load_fixed(
                job_root / summary["request_path"], "adapter request")
            adapter_evidence = self._load_fixed(
                job_root / summary["evidence_path"], "adapter evidence")
            if (not accepted(validate(
                    "xcelium_execution_request", adapter_request)) or
                    not accepted(validate(
                        "xcelium_execution_evidence", adapter_evidence)) or
                    adapter_request.get("request_fingerprint") !=
                        summary["request_fingerprint"] or
                    adapter_request.get("request_fingerprint") !=
                        artifact_fingerprint(
                            adapter_request, "request_fingerprint") or
                    adapter_evidence.get("evidence_fingerprint") !=
                        summary["evidence_fingerprint"] or
                    adapter_evidence.get("evidence_fingerprint") !=
                        artifact_fingerprint(
                            adapter_evidence, "evidence_fingerprint") or
                    adapter_evidence.get("request_fingerprint") !=
                        adapter_request.get("request_fingerprint")):
                raise ProjectJobError(
                    "STALE_EVIDENCE", "adapter execution evidence is stale")
            fingerprint, size, count = _tree_digest(
                job_root / summary["output_subdir"])
            if (fingerprint != summary["output_tree_fingerprint"] or
                    size != summary["output_tree_size_bytes"] or
                    count != summary["output_tree_file_count"]):
                raise ProjectJobError(
                    "STALE_EVIDENCE", "adapter output tree has drifted")

    def _manifest(self, request: ProjectLoopRequest) -> dict[str, Any] | None:
        job_root = self.workflow._job_root(request.submission)
        path = job_root / "input_baseline/project_input_manifest.json"
        if not path.exists():
            return None
        if not path.is_file() or path.is_symlink():
            raise ProjectJobError("STALE_EVIDENCE", "Project manifest is unsafe")
        return self.workflow.bootstrap_handler.handle(
            request.submission, request.submission_bytes, create=False).manifest

    def read(self, request: ProjectLoopRequest) -> Checkpoint:
        """Return the current state from a verified authoritative checkpoint."""
        manifest = self._manifest(request)
        if manifest is None:
            return Checkpoint(
                WorkflowState.BOOTSTRAP, ResumePolicy.AUTO, {}, None)
        job_root = self.workflow._job_root(manifest)
        pj003 = self._read_pj003(job_root, manifest)
        if pj003 is not None:
            return pj003
        # StagedProjectWorkflow is the existing authoritative checkpoint
        # validator.  The repository centralizes access to it for Project-loop
        # callers; it does not reinterpret or replace its evidence format.
        # Keep this import local: StagedProjectWorkflow reaches AgentLoop via
        # persistence, while this module is exported by ``runtime``.
        from runtime.staged_workflow import StagedProjectWorkflow
        terminal = StagedProjectWorkflow(self.workflow)._load_terminal(
            job_root, manifest)
        if terminal is None:
            # Owner routing predates the terminal-checkpoint family, but its
            # form and append-only submission have fixed schema-owned paths.
            # Reading those exact paths is not a directory scan or a new
            # authority; it makes restart resume the same Human boundary.
            owner_form = (
                job_root / "staging/validations/scenario_owner_review.json")
            owner_submission = (
                job_root / "audit/scenario_owner_review_submission.json")
            if owner_form.exists() and not owner_submission.exists():
                if (not owner_form.is_file() or owner_form.is_symlink()):
                    raise ProjectJobError(
                        "STALE_EVIDENCE", "Scenario Owner review form is unsafe")
                form = load_document(owner_form)
                if form.get("job_id") != manifest["job_id"]:
                    raise ProjectJobError(
                        "STALE_EVIDENCE", "Scenario Owner review form is cross-Job")
                value = {
                    "state": WorkflowState.AWAITING_SCENARIO_ROUTING.value,
                    "job_id": manifest["job_id"],
                    "input_fingerprint": manifest["input_fingerprint"],
                    "owner_review_path": (
                        "staging/validations/scenario_owner_review.json"),
                }
                return Checkpoint(
                    WorkflowState.AWAITING_SCENARIO_ROUTING,
                    ResumePolicy.HUMAN, value, value["owner_review_path"])
            return Checkpoint(
                WorkflowState.INITIAL_GENERATION, ResumePolicy.AUTO,
                copy.deepcopy(manifest), None)
        state = self._state(terminal)
        return Checkpoint(
            state, self.policy_for(state), copy.deepcopy(terminal),
            self._PATHS.get(state.value))

    def publish(self, value: Mapping[str, Any]) -> Checkpoint:
        """Validate a handler-produced checkpoint without writing new authority."""
        state = self._state(value)
        return Checkpoint(
            state, self.policy_for(state), copy.deepcopy(dict(value)),
            self._PATHS.get(state.value))


class ProjectLoop:
    """Run a Project Job through automatic transitions until a pause."""

    def __init__(
            self, workflow: ProjectJobWorkflow, *,
            provider_factory: Callable[[Path, Mapping[str, Any], str], Any],
            compile_runner_factory: Callable[[Path, Path, Mapping[str, Any]], Any]
            | None = None,
            checkpoints: CheckpointRepository | None = None,
            eda_adapter_factory: Callable[
                [Path, Mapping[str, Any], Mapping[str, Any]], Any] | None = None,
            uvm_build_runner: Callable[
                [Mapping[str, Any], Path, Mapping[str, Any]],
                Mapping[str, Any]] | None = None):
        self.workflow = workflow
        self.provider_factory = provider_factory
        self.compile_runner_factory = compile_runner_factory
        self.checkpoints = checkpoints or CheckpointRepository(workflow)
        self.eda_adapter_factory = eda_adapter_factory
        if uvm_build_runner is not None:
            self.workflow.uvm_build_runner = uvm_build_runner
        self.transitions = self._transitions()

    def _transitions(self) -> dict[WorkflowState, Transition]:
        return {
            WorkflowState.BOOTSTRAP: Transition(
                WorkflowState.BOOTSTRAP, ResumePolicy.AUTO, "BootstrapHandler",
                self._bootstrap),
            WorkflowState.INITIAL_GENERATION: Transition(
                WorkflowState.INITIAL_GENERATION, ResumePolicy.AUTO,
                "GenerateStage1Handler", self._start),
            WorkflowState.AWAITING_REPAIR_PLAN: Transition(
                WorkflowState.AWAITING_REPAIR_PLAN, ResumePolicy.AUTO,
                "CreateRepairPlanHandler", self._advance_repair),
            WorkflowState.AWAITING_SCOPED_REPLACEMENT: Transition(
                WorkflowState.AWAITING_SCOPED_REPLACEMENT, ResumePolicy.AUTO,
                "ScopedReplacementHandler", self._advance_repair),
            WorkflowState.SCOPED_REPLACEMENT_VALIDATED: Transition(
                WorkflowState.SCOPED_REPLACEMENT_VALIDATED, ResumePolicy.AUTO,
                "CompileCandidateHandler", self._advance_commit),
            WorkflowState.AWAITING_SCENARIO_ROUTING: Transition(
                WorkflowState.AWAITING_SCENARIO_ROUTING, ResumePolicy.HUMAN,
                "GenerateStage1Handler", self._route_scenarios),
            WorkflowState.AWAITING_HUMAN_REVIEW: Transition(
                WorkflowState.AWAITING_HUMAN_REVIEW, ResumePolicy.HUMAN,
                "RecordHumanDecisionHandler", self._record_human_decision),
            WorkflowState.AWAITING_EXECUTION_AUTHORIZATION: Transition(
                WorkflowState.AWAITING_EXECUTION_AUTHORIZATION,
                ResumePolicy.HUMAN,
                "RecordExecutionAuthorizationHandler",
                self._record_execution_authorization),
            WorkflowState.READY_FOR_BINDING: Transition(
                WorkflowState.READY_FOR_BINDING, ResumePolicy.AUTO,
                "BindApprovedBundleHandler", self._bind),
            WorkflowState.READY_FOR_EXECUTION: Transition(
                WorkflowState.READY_FOR_EXECUTION, ResumePolicy.AUTO,
                "ExecuteApprovedTestcaseHandler", self._execute),
        }

    def _bootstrap(
            self, request: ProjectLoopRequest, _: Mapping[str, Any]
            ) -> dict[str, Any]:
        result: BootstrapResult = self.workflow.bootstrap_handler.handle(
            request.submission, request.submission_bytes, create=True)
        self._configure_role_providers(result.manifest)
        # The manifest is existing authority, but it is not a business pause.
        return {"state": WorkflowState.INITIAL_GENERATION.value, **result}

    def _configure_role_providers(self, manifest: Mapping[str, Any]) -> None:
        """Bind providers from this Job's immutable profile snapshots only."""
        job_root = self.workflow._job_root(dict(manifest))
        for section, role in ROLE_PATHS:
            profile_role = "{}.{}".format(section, role)
            if profile_role not in self.workflow.role_providers:
                self.workflow.role_providers[profile_role] = self.provider_factory(
                    job_root, manifest, profile_role)

    def _start(
            self, request: ProjectLoopRequest, _: Mapping[str, Any]
            ) -> dict[str, Any]:
        return self.workflow.start(request.submission, request.submission_bytes)

    def _advance_repair(
            self, _: ProjectLoopRequest, checkpoint: Mapping[str, Any]
            ) -> dict[str, Any]:
        # Import lazily: repair runtime itself depends on AgentLoop, while the
        # Project loop is exported from the runtime package.
        from runtime.job_runtime import ProjectJobRuntimeIntegration
        integration = ProjectJobRuntimeIntegration(
            workspace_root=self.workflow.workspace_root,
            result_root=self.workflow.result_root,
            provider_factory=self.provider_factory)
        return integration.advance(str(checkpoint["job_id"]))

    def _advance_commit(
            self, _: ProjectLoopRequest, checkpoint: Mapping[str, Any]
            ) -> dict[str, Any]:
        from runtime.commit_runtime import ProjectCommitRuntime
        runtime = ProjectCommitRuntime(
            workspace_root=self.workflow.workspace_root,
            result_root=self.workflow.result_root,
            provider_factory=self.provider_factory,
            compile_runner_factory=self.compile_runner_factory,
            uvm_build_runner=self.workflow.uvm_build_runner)
        return runtime.advance(str(checkpoint["job_id"]))

    def _route_scenarios(
            self, request: ProjectLoopRequest, _: Mapping[str, Any]
            ) -> dict[str, Any]:
        if request.scenario_routing is None:
            raise ProjectJobError(
                "MISSING_HUMAN_SUBMISSION",
                "Scenario routing requires an explicit Human submission")
        return self.workflow.route_scenarios(
            request.submission, request.scenario_routing,
            request.submission_bytes)

    def _record_human_decision(
            self, request: ProjectLoopRequest, checkpoint: Mapping[str, Any]
            ) -> dict[str, Any]:
        if request.human_decision is None:
            raise ProjectJobError(
                "MISSING_HUMAN_DECISION",
                "Human review requires an explicit Human decision")
        manifest = self.workflow.bootstrap_handler.handle(
            request.submission, request.submission_bytes, create=False).manifest
        job_root = self.workflow._job_root(manifest)
        approval = load_document(job_root / checkpoint["approval_request_path"])
        return RecordHumanDecisionHandler(ProjectJobError).handle(
            RecordHumanDecisionInput(
                job_root, manifest, dict(checkpoint), approval,
                request.human_decision)).checkpoint

    def _record_execution_authorization(
            self, request: ProjectLoopRequest, checkpoint: Mapping[str, Any]
            ) -> dict[str, Any]:
        if request.execution_authorization is None:
            raise ProjectJobError(
                "MISSING_EXECUTION_AUTHORIZATION",
                "execution requires a separate explicit DV_OWNER authorization")
        manifest = self.workflow.bootstrap_handler.handle(
            request.submission, request.submission_bytes, create=False).manifest
        job_root = self.workflow._job_root(manifest)
        return RecordExecutionAuthorizationHandler(ProjectJobError).handle(
            RecordExecutionAuthorizationInput(
                job_root, manifest, dict(checkpoint),
                request.execution_authorization)).checkpoint

    def _bind(
            self, request: ProjectLoopRequest, checkpoint: Mapping[str, Any]
            ) -> dict[str, Any]:
        manifest = self.workflow.bootstrap_handler.handle(
            request.submission, request.submission_bytes, create=False).manifest
        job_root = self.workflow._job_root(manifest)
        return BindApprovedBundleHandler(ProjectJobError).handle(
            BindApprovedBundleInput(
                job_root, self.workflow.workspace_root, manifest,
                dict(checkpoint))).checkpoint

    def _execute(
            self, request: ProjectLoopRequest, checkpoint: Mapping[str, Any]
            ) -> dict[str, Any]:
        if self.eda_adapter_factory is None:
            raise ProjectJobError(
                "BLOCKED_TOOL", "trusted PJ-003 EDA adapter is not configured")
        manifest = self.workflow.bootstrap_handler.handle(
            request.submission, request.submission_bytes, create=False).manifest
        job_root = self.workflow._job_root(manifest)
        authorization = load_document(job_root / EXECUTION_AUTHORIZATION_PATH)
        adapter_cache: list[Any] = []

        def trusted_adapter():
            if not adapter_cache:
                candidate = self.eda_adapter_factory(
                    job_root, manifest, authorization)
                for attribute, expected in (
                        ("job_id", manifest["job_id"]),
                        ("environment_identity",
                         authorization["environment_identity"]),
                        ("environment_fingerprint",
                         authorization["environment_fingerprint"])):
                    if getattr(candidate, attribute, None) != expected:
                        raise ProjectJobError(
                            "INVALID_APPROVAL_PROVENANCE",
                            "trusted EDA adapter does not match execution authority")
                adapter_cache.append(candidate)
            return adapter_cache[0]

        def configuration(value: Mapping[str, Any]):
            from adapters.eda.xcelium import XceliumRunConfiguration
            return XceliumRunConfiguration(
                sources=tuple(value["sources"]), top=str(value["top"]),
                seed=int(value["seed"]), uvm=bool(value["uvm"]),
                coverage=bool(value["coverage"]), waves=bool(value["waves"]),
                pass_marker=str(value["pass_marker"]))

        return ExecuteApprovedTestcaseHandler(ProjectJobError).handle(
            ExecuteApprovedTestcaseInput(
                job_root, self.workflow.workspace_root, manifest,
                dict(checkpoint),
                lambda execution_id, value: trusted_adapter().build_only(
                    execution_id, configuration(value)),
                lambda execution_id, value: trusted_adapter().run(
                    execution_id, configuration(value)),
            )).checkpoint

    @staticmethod
    def _pause(checkpoint: Checkpoint) -> dict[str, Any]:
        return {
            **copy.deepcopy(checkpoint.value),
            "state": checkpoint.state.value,
            "resume_policy": checkpoint.policy.value,
            "current_state_modified": False,
        }

    def run_until_pause(self, request: ProjectLoopRequest) -> dict[str, Any]:
        """Execute one transition at a time until the first non-AUTO state."""
        checkpoint = self.checkpoints.read(request)
        if request.human_decision is not None and \
                request.execution_authorization is not None:
            raise ProjectJobError(
                "INVALID_INPUT",
                "testcase decision and execution authorization are separate submissions")
        if request.human_decision is not None and \
                checkpoint.state is not WorkflowState.AWAITING_HUMAN_REVIEW:
            raise ProjectJobError(
                "INVALID_TRANSITION",
                "testcase decision requires the existing Human-review checkpoint")
        if request.execution_authorization is not None and checkpoint.state is not \
                WorkflowState.AWAITING_EXECUTION_AUTHORIZATION:
            raise ProjectJobError(
                "INVALID_TRANSITION",
                "execution authorization requires its existing checkpoint")
        if request.retry_blocked_review:
            # This is an explicit retry request, never an automatic replay of
            # a failed Provider attempt.  The staged workflow validates the
            # exact retry boundary and reuses all completed artifacts.
            result = self.workflow.retry_blocked_review(
                request.submission, request.submission_bytes)
            checkpoint = self.checkpoints.publish(result)
        if checkpoint.state is not WorkflowState.BOOTSTRAP:
            manifest = self.workflow.bootstrap_handler.handle(
                request.submission, request.submission_bytes, create=False)
            self._configure_role_providers(manifest.manifest)
        while True:
            has_human_submission = (
                checkpoint.state is WorkflowState.AWAITING_SCENARIO_ROUTING
                and request.scenario_routing is not None
            ) or (
                checkpoint.state is WorkflowState.AWAITING_HUMAN_REVIEW
                and request.human_decision is not None
            ) or (
                checkpoint.state is
                    WorkflowState.AWAITING_EXECUTION_AUTHORIZATION
                and request.execution_authorization is not None
            )
            if checkpoint.policy is not ResumePolicy.AUTO and \
                    not has_human_submission:
                break
            transition = self.transitions.get(checkpoint.state)
            if transition is None:
                raise ProjectJobError(
                    "INVALID_TRANSITION",
                    "Project loop has no transition for the current AUTO state")
            result = transition.execute(request, checkpoint.value)
            checkpoint = self.checkpoints.publish(result)
            # All subsequent state reads must go through the repository, so
            # restart uses persisted authority and never a guessed "latest".
            if checkpoint.policy is ResumePolicy.AUTO:
                persisted = self.checkpoints.read(request)
                if persisted.state != WorkflowState.INITIAL_GENERATION or \
                        checkpoint.state == WorkflowState.INITIAL_GENERATION:
                    checkpoint = persisted
        return self._pause(checkpoint)


__all__ = [
    "Checkpoint", "CheckpointRepository", "ProjectLoop",
    "ProjectLoopRequest", "ResumePolicy", "Transition", "WorkflowState",
]
