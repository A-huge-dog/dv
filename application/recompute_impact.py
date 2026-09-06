"""Recompute and persist impact for one exact committed repair group."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from domain.repair import evaluate_impact

from application.commit_group import CommitGroupResult


@dataclass(frozen=True)
class RecomputeImpactInput:
    job_root: Path
    project_input: Mapping[str, Any]
    dispatch: Mapping[str, Any]
    prepared: Mapping[str, Any]
    commit_result: CommitGroupResult


@dataclass(frozen=True)
class RecomputeImpactResult:
    impact_path: str
    impact: dict[str, Any]
    replayed: bool


@dataclass(frozen=True)
class RecomputeImpactDependencies:
    error: type[Exception]
    record_store: Callable[..., Any]
    load_index: Callable[..., tuple[dict[str, Any], dict[str, Any]]]


class RecomputeImpactHandler:
    def __init__(self, dependencies: RecomputeImpactDependencies):
        self.dependencies = dependencies

    def handle(self, command: RecomputeImpactInput) -> RecomputeImpactResult:
        deps = self.dependencies
        store = deps.record_store(
            command.job_root,
            job_id=command.project_input["job_id"],
            input_fingerprint=command.project_input["input_fingerprint"],
            spec_fingerprint=command.dispatch["spec_fingerprint"],
            policy_fingerprint=command.dispatch["policy_fingerprint"])
        existing = [
            (path, record) for path, record in store.records()
            if record["record_type"] == "IMPACT_RESULT"
            and record["payload"].get("group_id") ==
                command.dispatch["group_id"]
            and record["payload"].get("commit_fingerprint") ==
                command.commit_result.commit["record_fingerprint"]
        ]
        if existing:
            if len(existing) != 1:
                raise deps.error("CONFLICTING_REPLAY", "group impact is ambiguous")
            path, impact = existing[0]
            return RecomputeImpactResult(path, impact, True)
        old_indexes = {
            key: deps.load_index(command.job_root, path, deps.error)
            for key, path in command.prepared["old_index_paths"].items()
        }
        new_indexes = {
            key: deps.load_index(command.job_root, path, deps.error)
            for key, path in command.prepared["new_index_paths"].items()
        }
        impact_value = evaluate_impact(
            old_indexes, new_indexes, deps.error,
            allow_stale_stages=(
                {"STAGE3"} if command.prepared["stage"] == "STAGE_2"
                else set()))
        impact_path, impact = store.append("IMPACT_RESULT", {
            "group_id": command.dispatch["group_id"],
            "commit_fingerprint": command.commit_result.commit[
                "record_fingerprint"],
            "dirty_units": impact_value["dirty_units"],
            "reused_units": impact_value["reused_units"],
            "direct_dependency_closure": sorted({
                item["unit_id"] for item in impact_value["dirty_units"]}),
            "new_roots": command.prepared["new_artifact_roots"],
        })
        store.append("REPAIR_EPISODE", {
            "group_id": command.dispatch["group_id"],
            "ordered_links": [{
                "record_type": "VALIDATION_RESULT",
                "path": command.commit_result.validation_path,
                "fingerprint": command.commit_result.validation[
                    "record_fingerprint"],
            }, {
                "record_type": "GROUP_COMMIT",
                "path": command.commit_result.commit_path,
                "fingerprint": command.commit_result.commit[
                    "record_fingerprint"],
            }, {
                "record_type": "IMPACT_RESULT",
                "path": impact_path,
                "fingerprint": impact["record_fingerprint"],
            }],
            "terminal_status": "COMMITTED",
        })
        store.rebuild_indexes()
        return RecomputeImpactResult(impact_path, impact, False)
