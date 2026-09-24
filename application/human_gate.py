"""Publish execution preparation from an exact reviewed artifact bundle."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from contracts.validator import load_document
from domain.review import WORKFLOW_VERSION
from scripts.dvlib import canonical_hash


@dataclass(frozen=True)
class CreateHumanGateInput:
    job_root: Path
    project_input: dict[str, Any]
    scenario_ac_map: dict[str, Any]
    ac_testcase_map: dict[str, Any]
    candidate: dict[str, Any]
    review_report: dict[str, Any]
    review_validation: dict[str, Any]
    artifact_paths: dict[str, str]
    budget: dict[str, Any]
    routing_summary: dict[str, Any]
    artifact_suffix: str = ""
    allow_recertification: bool = False
    review_storage_revision: int | None = None


@dataclass(frozen=True)
class HumanGateResult:
    checkpoint: dict[str, Any]
    output_references: tuple[str, ...]
    replayed: bool


@dataclass(frozen=True)
class HumanGateDependencies:
    error: type[Exception]
    regeneration_states: Callable[..., list[dict[str, Any]]]
    append_regeneration_state: Callable[..., dict[str, Any]]
    incremental_roots: Callable[..., dict[str, str]]
    checkpoint_fingerprint: Callable[[dict[str, Any]], str]
    immutable_json: Callable[[Path, dict[str, Any]], None]
    write_traceability: Callable[..., None]


class CreateHumanGateHandler:
    def __init__(self, dependencies: HumanGateDependencies):
        self.dependencies = dependencies

    def handle(self, command: CreateHumanGateInput) -> HumanGateResult:
        deps = self.dependencies
        suffix = command.artifact_suffix
        if suffix and not re.fullmatch(r"\.[a-z0-9.-]+", suffix):
            raise deps.error("INVALID_INPUT", "Human artifact suffix is unsafe")
        states = deps.regeneration_states(
            command.job_root, command.project_input)
        if not states or not states[-1]["final_review_done"]:
            state = deps.append_regeneration_state(
                command.job_root, command.project_input, "FINAL_REVIEW_DONE",
                command.review_report["report_fingerprint"])
        else:
            state = states[-1]
            if (state["source_report_fingerprint"] !=
                    command.review_report["report_fingerprint"] and
                    not command.allow_recertification):
                raise deps.error(
                    "STALE_EVIDENCE",
                    "final review state binds a different report")
        roots = deps.incremental_roots(
            command.job_root, command.project_input,
            command.scenario_ac_map, command.ac_testcase_map,
            command.candidate, command.review_report,
            review_storage_revision=command.review_storage_revision)
        seed = {
            "input": command.project_input["input_fingerprint"],
            **command.review_report["artifact_roots"],
            "review": command.review_report["report_fingerprint"],
            "review_validation": command.review_validation[
                "validation_fingerprint"],
            **roots,
        }
        checkpoint_id = "CHECKPOINT.PROJECT.REVIEW.{}".format(
            canonical_hash(seed)[:16].upper())
        paths = command.artifact_paths
        map1, map2 = command.scenario_ac_map, command.ac_testcase_map
        candidate, report = command.candidate, command.review_report
        issue_path = command.routing_summary.get("spec_issues")
        has_spec_issues = (bool(load_document(command.job_root / issue_path).get("scenario_ids"))
                           if issue_path else command.routing_summary.get("has_spec_issues", False))
        checkpoint = {
            "schema_version": "1.0",
            "workflow_version": WORKFLOW_VERSION,
            "state": "READY_FOR_EXECUTION_PREPARATION",
            "source_checkpoint_path": "audit/project_review_complete{}.json".format(suffix),
            "job_id": command.project_input["job_id"],
            "input_fingerprint": command.project_input["input_fingerprint"],
            "scenario_ac_map_path": paths["map1"],
            "ac_testcase_map_path": paths["map2"],
            "candidate_metadata_path": paths["candidate"],
            "review_request_path": paths["review_request"],
            "review_report_path": paths["review_report"],
            "review_validation_path": paths["review_validation"],
            "review_unit_index_path": paths["review_units"],
            "checkpoint_id": checkpoint_id,
            "artifact_unit_index_paths": {
                "stage1": (
                    "staging/units/stage1/index.current.r{:03d}.json".format(
                        map1["revision"])),
                "stage2": (
                    "staging/units/stage2/index.current.r{:03d}.json".format(
                        map2["revision"])),
                "stage3": (
                    "staging/units/stage3/index.current.r{:03d}.json".format(
                        candidate["revision"])),
                "review": paths["review_units"],
            },
            "review_verdict": report["verdict"],
            "error_count": sum(
                item["severity"] == "ERROR" for item in report["findings"]),
            "warning_count": sum(
                item["severity"] == "WARNING" for item in report["findings"]),
            "regeneration_state_fingerprint": state["state_fingerprint"],
            "bundle_fingerprints": {**seed, "checkpoint": "0" * 64},
            "resource_usage": {
                "provider_calls": command.budget["calls"],
                "tokens": command.budget["tokens"],
            },
            "generation_complete": True,
            "review_complete": True,
            "implemented_testcase_count": len(candidate["implemented_testcase_ids"]),
            "skipped_testcase_count": len(candidate["skipped_testcases"]),
            "full_spec_coverage_complete":
                (not has_spec_issues
                 and not candidate["skipped_testcases"]
                 and all(row["status"] == "COVERED" for row in report["ac_reviews"])),
            "scenario_partition_paths": {
                key: command.routing_summary[key]
                for key in ("checked", "commented", "spec_issues")
                if key in command.routing_summary
            },
            "owner_review_submission_path":
                command.routing_summary.get("owner_review_submission_path"),
            "owner_review_submission_fingerprint":
                command.routing_summary.get(
                    "owner_review_submission_fingerprint"),
            "checkpoint_fingerprint": "0" * 64,
        }
        checkpoint["checkpoint_fingerprint"] = (
            deps.checkpoint_fingerprint(checkpoint))
        checkpoint["bundle_fingerprints"]["checkpoint"] = (
            checkpoint["checkpoint_fingerprint"])
        checkpoint_relative = "audit/project_review_complete{}.json".format(suffix)
        replayed = (command.job_root / checkpoint_relative).exists()
        deps.immutable_json(command.job_root / checkpoint_relative, checkpoint)
        if not suffix:
            deps.write_traceability(
                command.job_root, map1, map2, report)
        return HumanGateResult(
            checkpoint, (checkpoint_relative,), replayed)
