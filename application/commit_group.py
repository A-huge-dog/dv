"""Commit one validated repair group without computing downstream impact."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class CommitGroupInput:
    job_root: Path
    project_input: Mapping[str, Any]
    dispatch: Mapping[str, Any]
    replacement: Mapping[str, Any] | None
    prepared: Mapping[str, Any]


@dataclass(frozen=True)
class CommitGroupFailureInput:
    job_root: Path
    project_input: Mapping[str, Any]
    dispatch: Mapping[str, Any]
    replacement: Mapping[str, Any]
    compile_validation: Mapping[str, Any]


@dataclass(frozen=True)
class CommitGroupResult:
    validation_path: str
    validation: dict[str, Any]
    commit_path: str
    commit: dict[str, Any]
    replayed: bool


@dataclass(frozen=True)
class CommitGroupDependencies:
    error: type[Exception]
    record_store: Callable[..., Any]
    target_revisions: Callable[..., list[dict[str, Any]]]


class CommitGroupHandler:
    def __init__(self, dependencies: CommitGroupDependencies):
        self.dependencies = dependencies

    def _store(self, command: Any) -> Any:
        return self.dependencies.record_store(
            command.job_root,
            job_id=command.project_input["job_id"],
            input_fingerprint=command.project_input["input_fingerprint"],
            spec_fingerprint=command.dispatch["spec_fingerprint"],
            policy_fingerprint=command.dispatch["policy_fingerprint"])

    def handle(self, command: CommitGroupInput) -> CommitGroupResult:
        deps = self.dependencies
        store = self._store(command)
        group_id = command.dispatch["group_id"]
        matching = [
            (path, record) for path, record in store.records()
            if record["record_type"] == "GROUP_COMMIT"
            and record["payload"].get("group_id") == group_id
            and record["payload"].get("status") == "COMMITTED"
            and record["payload"].get("current_roots") ==
                command.prepared["new_artifact_roots"]
        ]
        if matching:
            if len(matching) != 1:
                raise deps.error("CONFLICTING_REPLAY", "stage commit is ambiguous")
            commit_path, commit = matching[0]
            validation_matches = [
                (path, record) for path, record in store.records()
                if record["record_type"] == "VALIDATION_RESULT"
                and record["record_fingerprint"] ==
                    commit["payload"]["validation_fingerprint"]
            ]
            if len(validation_matches) != 1:
                raise deps.error(
                    "STALE_EVIDENCE", "stage commit validation lineage is stale")
            validation_path, validation = validation_matches[0]
            return CommitGroupResult(
                validation_path, validation, commit_path, commit, True)
        replacement_fp = (
            command.replacement["replacement_fingerprint"]
            if command.replacement is not None
            else command.dispatch["dispatch_fingerprint"])
        validation_path, validation = store.append("VALIDATION_RESULT", {
            "group_id": group_id,
            "replacement_fingerprint": replacement_fp,
            "status": "PASS",
            "diagnostics": [],
            "unexecuted_checks": [],
        })
        commit_path, commit = store.append("GROUP_COMMIT", {
            "group_id": group_id,
            "status": "COMMITTED",
            "before_roots": command.prepared["old_artifact_roots"],
            "current_roots": command.prepared["new_artifact_roots"],
            "target_revisions": deps.target_revisions(
                command.job_root, command.prepared, command.replacement),
            "validation_fingerprint": validation["record_fingerprint"],
        })
        return CommitGroupResult(
            validation_path, validation, commit_path, commit, False)

    def handle_failure(
            self, command: CommitGroupFailureInput) -> CommitGroupResult | None:
        store = self._store(command)
        diagnostics = copy.deepcopy(
            command.compile_validation["evidence"].get(
                "diagnostic_codes", []))
        prior = [
            (path, record) for path, record in store.records()
            if record["record_type"] == "VALIDATION_RESULT"
            and record["payload"].get("group_id") ==
                command.dispatch["group_id"]
            and record["payload"].get("replacement_fingerprint") ==
                command.replacement["replacement_fingerprint"]
            and record["payload"].get("status") == "FAIL"
            and record["payload"].get("diagnostics") == diagnostics
        ]
        if prior:
            if len(prior) != 1:
                raise self.dependencies.error(
                    "CONFLICTING_REPLAY", "compile failure replay is ambiguous")
            return None
        validation_path, validation = store.append("VALIDATION_RESULT", {
            "group_id": command.dispatch["group_id"],
            "replacement_fingerprint": command.replacement[
                "replacement_fingerprint"],
            "status": "FAIL",
            "diagnostics": diagnostics,
            "unexecuted_checks": [],
        })
        commit_path, commit = store.append("GROUP_COMMIT", {
            "group_id": command.dispatch["group_id"],
            "status": "NOT_COMMITTED",
            "before_roots": command.compile_validation[
                "base_artifact_roots"],
            "current_roots": command.compile_validation[
                "base_artifact_roots"],
            "target_revisions": [],
            "validation_fingerprint": validation["record_fingerprint"],
        })
        store.append("REPAIR_EPISODE", {
            "group_id": command.dispatch["group_id"],
            "ordered_links": [{
                "record_type": "VALIDATION_RESULT",
                "path": validation_path,
                "fingerprint": validation["record_fingerprint"],
            }, {
                "record_type": "GROUP_COMMIT",
                "path": commit_path,
                "fingerprint": commit["record_fingerprint"],
            }],
            "terminal_status": "VALIDATION_FAILED_NOT_COMMITTED",
        })
        store.rebuild_indexes()
        return CommitGroupResult(
            validation_path, validation, commit_path, commit, False)

