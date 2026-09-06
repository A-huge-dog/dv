#!/usr/bin/env python3
"""Start or resume the Verilator-first Project Job workflow."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path, PurePosixPath

import yaml

# Support the documented direct-script entry without requiring PYTHONPATH.
DV_ROOT = Path(__file__).resolve().parents[1]
if str(DV_ROOT) not in sys.path:
    sys.path.insert(0, str(DV_ROOT))

from adapters.llm import OpenAICompatibleProvider, ProviderConfigError
from adapters.eda import XceliumAdapter, XceliumRunConfiguration
from contracts.validator import load_document
from runtime.errors import ProjectJobError
from runtime.project_job import (
    ProjectJobWorkflow,
    validate_project_submission,
)
from runtime.recovery import stop_result
from runtime.project_loop import ProjectLoop, ProjectLoopRequest


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


def authorized_xcelium(job_root: Path, manifest: dict, authorization: dict):
    """Resolve only the environment named by an explicit DV_OWNER authority."""
    return XceliumAdapter.from_preloaded_environment(
        workspace_root=ROOT,
        result_root=ROOT / "result",
        job_id=manifest["job_id"],
        environment_identity=authorization["environment_identity"],
        timeout_seconds=authorization["constraints"]["timeout_seconds"],
    )


def generation_xcelium(manifest: dict, job_root: Path, request: dict):
    """Compile and elaborate one exact Job-local UVM overlay before Stage 3."""
    adapter = XceliumAdapter.from_preloaded_environment(
        workspace_root=ROOT,
        result_root=ROOT / "result",
        job_id=manifest["job_id"],
        environment_identity="XCELIUMENV.PROJECT.GENERATION.V1",
        timeout_seconds=manifest["eda"]["timeout_seconds"],
    )
    source_records = [
        *request["sources"], *request["framework_sources"]]
    uvm_sources = [
        "result/jobs/{}/{}".format(manifest["job_id"], item["path"])
        for item in source_records]
    include_dirs = set()
    for item, source in zip(source_records, uvm_sources):
        source_path = Path(source)

        # Generated packages commonly include headers relative to their own
        # directory, for example "transactions/coral_types.svh".  Keep that
        # directory in addition to the overlay root so both package-relative
        # and full logical-path includes resolve correctly.
        include_dirs.add(source_path.parent.as_posix())

        root = source_path
        for _ in PurePosixPath(item["logical_path"]).parts:
            root = root.parent
        include_dirs.add(root.as_posix())
    execution_id = "UVM.{}.ATTEMPT{:03d}.RUN{:03d}".format(
        request["cycle_id"].upper().replace("-", "."),
        request["attempt"], request["tool_run"])
    return adapter.build_only(execution_id, XceliumRunConfiguration(
        sources=tuple(uvm_sources),
        include_dirs=tuple(sorted(include_dirs)),
        top=request["top"], uvm=True,
        timeout_seconds=manifest["eda"]["timeout_seconds"],
    ))


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
    parser.add_argument(
        "--execution-authorization", type=Path,
        help="separate DV_OWNER Project execution authorization JSON")
    args = parser.parse_args()
    try:
        if args.retry_blocked_review and (
                args.resume or args.decision or args.scenario_routing or
                args.execution_authorization):
            raise ProjectJobError(
                "INVALID_INPUT",
                "--retry-blocked-review cannot be combined with "
                "--resume, --decision, or --scenario-routing")
        if args.scenario_routing and (args.resume or args.decision):
            raise ProjectJobError(
                "INVALID_INPUT",
                "--scenario-routing cannot be combined with "
                "--resume or --decision")
        if args.decision and args.execution_authorization:
            raise ProjectJobError(
                "INVALID_INPUT",
                "testcase decision and execution authorization must be "
                "submitted separately")
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

        # CLI only wires dependencies. Bootstrap and every workflow
        # transition are owned by the single ProjectLoop.
        workflow = ProjectJobWorkflow(
            ROOT, ROOT / "result", project_input_root=args.project_input.parent)
        loop = ProjectLoop(
            workflow, provider_factory=snapshot_provider,
            eda_adapter_factory=authorized_xcelium,
            uvm_build_runner=generation_xcelium)
        result = loop.run_until_pause(ProjectLoopRequest(
            submission=project_submission,
            submission_bytes=submission_bytes,
            human_decision=(load_document(args.decision)
                            if args.decision is not None else None),
            execution_authorization=(
                load_document(args.execution_authorization)
                if args.execution_authorization is not None else None),
            scenario_routing=(load_document(args.scenario_routing)
                              if args.scenario_routing is not None else None),
            retry_blocked_review=args.retry_blocked_review,
        ))
        print(json.dumps(
            result, sort_keys=True, indent=2, ensure_ascii=False))
        state = result.get("state") or result.get("status")
        return 0 if state in {
            "AWAITING_SCENARIO_ROUTING", "AWAITING_HUMAN_REVIEW",
            "AWAITING_EXECUTION_AUTHORIZATION", "SCOPED_REPLACEMENT_VALIDATED",
            "SPEC_ISSUES_RECORDED", "PAUSED_BY_HUMAN",
            "EXECUTION_PASS", "EXECUTION_FAIL", "EXECUTION_BLOCKED",
            "PAUSED_BUDGET", "PAUSED_RETRYABLE", "PAUSED_RECOVERY_REQUIRED",
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
