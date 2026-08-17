#!/usr/bin/env python3
"""Start or resume the Verilator-first Project Job workflow."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

# Support the documented direct-script entry without requiring PYTHONPATH.
DV_ROOT = Path(__file__).resolve().parents[1]
if str(DV_ROOT) not in sys.path:
    sys.path.insert(0, str(DV_ROOT))

from adapters.llm import OpenAICompatibleProvider, ProviderConfigError
from contracts.validator import load_document
from core.project_job import (
    ProjectJobError,
    ProjectJobWorkflow,
    validate_project_submission,
)
from core.project_agent_profile import ROLE_PATHS
from core.project_job_runtime import (
    ProjectJobRuntimeIntegration, REPAIR_RUNTIME_STATES,
)
from core.project_commit_runtime import ProjectCommitRuntime
from core.recovery import stop_result


ROOT = Path(__file__).resolve().parents[2]


def configured_provider(path: Path) -> OpenAICompatibleProvider:
    """Build the configured API adapter without constraining model identity."""
    return OpenAICompatibleProvider(load_document(path))


def snapshot_provider(
        job_root: Path, manifest: dict, profile_role: str
        ) -> OpenAICompatibleProvider:
    """Create one role Provider only from this Job's immutable snapshot."""
    section, role = profile_role.split(".", 1)
    relative = manifest["agent_profile"]["bindings"][section][role][
        "baseline_path"]
    return configured_provider(job_root / relative)


def advance_repair_runtime(
        result: dict, workflow: ProjectJobWorkflow,
        provider_factory=snapshot_provider) -> dict:
    """Continue repairable Reviewer ERRORs without a second public input."""
    if result.get("state") not in REPAIR_RUNTIME_STATES:
        return result
    integration = ProjectJobRuntimeIntegration(
        workspace_root=workflow.workspace_root,
        result_root=workflow.result_root,
        provider_factory=provider_factory)
    return integration.advance(result["job_id"])


def advance_commit_runtime(
        result: dict, workflow: ProjectJobWorkflow,
        provider_factory=snapshot_provider,
        compile_runner_factory=None) -> dict:
    """Advance or recover a formally planned repair through OCHES003."""
    state = result.get("state")
    if state == "AWAITING_HUMAN_REVIEW":
        record_root = (
            workflow.result_root / "jobs" / str(result.get("job_id", "")) /
            "audit/repair_records")
        plan_paths = sorted(record_root.glob(
            "*.orchestrator_plan.*.json"))
        if not plan_paths:
            return result
        for path in plan_paths:
            if not path.is_file() or path.is_symlink():
                raise ProjectJobError(
                    "STALE_EVIDENCE", "Project repair plan record is unsafe")
            record = load_document(path)
            if (record.get("record_type") != "ORCHESTRATOR_PLAN" or
                    record.get("job_id") != result.get("job_id") or
                    record.get("input_fingerprint") !=
                        result.get("input_fingerprint")):
                raise ProjectJobError(
                    "STALE_EVIDENCE", "Project repair plan record is stale")
    elif state != "SCOPED_REPLACEMENT_VALIDATED":
        return result
    runtime = ProjectCommitRuntime(
        workspace_root=workflow.workspace_root,
        result_root=workflow.result_root,
        provider_factory=provider_factory,
        compile_runner_factory=compile_runner_factory)
    return runtime.advance(result["job_id"])


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Bootstrap or resume a Project Job from one user-authored YAML"))
    parser.add_argument("--project-input", type=Path, required=True)
    parser.add_argument("--job-id")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--retry-blocked-review", action="store_true")
    parser.add_argument(
        "--scenario-routing", type=Path,
        help="completed DV Owner direct-review JSON form")
    parser.add_argument("--decision", type=Path)
    args = parser.parse_args()
    try:
        if args.retry_blocked_review and (
                args.resume or args.decision or args.scenario_routing):
            raise ProjectJobError(
                "INVALID_INPUT",
                "--retry-blocked-review cannot be combined with "
                "--resume, --decision, or --scenario-routing")
        if args.scenario_routing and (args.resume or args.decision):
            raise ProjectJobError(
                "INVALID_INPUT",
                "--scenario-routing cannot be combined with "
                "--resume or --decision")
        if args.resume and args.decision is None:
            raise ProjectJobError(
                "MISSING_HUMAN_DECISION",
                "--resume requires --decision")
        try:
            submission_bytes = args.project_input.read_bytes()
        except FileNotFoundError as error:
            raise ProjectJobError(
                "BLOCKED_INPUT",
                "Project submission YAML is missing") from error
        try:
            project_submission = yaml.safe_load(
                submission_bytes.decode("utf-8"))
        except (UnicodeError, yaml.YAMLError) as error:
            raise ProjectJobError(
                "INVALID_SCHEMA",
                "Project submission YAML is invalid") from error
        project_submission = validate_project_submission(
            project_submission)
        if args.job_id and project_submission.get("job_id") != args.job_id:
            raise ProjectJobError(
                "STALE_EVIDENCE",
                "--job-id does not match Project submission")

        bootstrapper = ProjectJobWorkflow(ROOT, ROOT / "result")
        manifest = bootstrapper.bootstrap(
            project_submission, submission_bytes,
            create=not (
                args.resume or args.decision or
                args.retry_blocked_review or args.scenario_routing))

        job_root = ROOT / "result/jobs" / manifest["job_id"]
        role_providers = {
            "{}.{}".format(section, role): configured_provider(
                job_root / manifest["agent_profile"]["bindings"][section][
                    role]["baseline_path"])
            for section, role in ROLE_PATHS
        }
        workflow = ProjectJobWorkflow(
            ROOT, ROOT / "result", role_providers=role_providers)
        if args.retry_blocked_review:
            result = workflow.retry_blocked_review(
                project_submission, submission_bytes)
        elif args.scenario_routing:
            result = workflow.route_scenarios(
                project_submission,
                load_document(args.scenario_routing), submission_bytes)
        elif args.resume or args.decision:
            if args.decision is None:
                raise ProjectJobError(
                    "MISSING_HUMAN_DECISION",
                    "--resume requires --decision")
            result = workflow.resume(
                project_submission, load_document(args.decision),
                submission_bytes)
        else:
            result = workflow.start(
                project_submission, submission_bytes)
        result = advance_repair_runtime(result, workflow)
        result = advance_commit_runtime(result, workflow)
        print(json.dumps(
            result, sort_keys=True, indent=2, ensure_ascii=False))
        state = result.get("state") or result.get("status")
        return 0 if state in {
            "AWAITING_SCENARIO_ROUTING", "AWAITING_TESTCASE_APPROVAL",
            "AWAITING_HUMAN_REVIEW", "SCOPED_REPLACEMENT_VALIDATED",
            "SPEC_ISSUES_RECORDED", "COMPLETE", "PAUSED_BY_HUMAN",
            "PAUSED_RETRYABLE", "PAUSED_RECOVERY_REQUIRED",
            "PAUSED_COMPILE_REPAIR_REQUIRED",
            "OCHES003_FINAL_REVIEW_COMPLETE",
        } else 1
    except ProjectJobError as error:
        print(json.dumps(
            stop_result(error.code, str(error)), sort_keys=True),
            file=sys.stderr)
        return 2
    except ProviderConfigError:
        print(json.dumps(stop_result(
            "INVALID_PROVIDER_CONFIG",
            "Project Job production provider configuration is invalid"),
            sort_keys=True), file=sys.stderr)
        return 2
    except (OSError, ValueError, yaml.YAMLError):
        print(json.dumps(stop_result(
            "BLOCKED_INPUT",
            "Project Job input file could not be loaded safely"),
            sort_keys=True), file=sys.stderr)
        return 2
    except Exception:
        print(json.dumps(stop_result(
            "INTERNAL_ERROR",
            "Project Job failed without exposing private values"),
            sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
