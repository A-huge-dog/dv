"""Compile one exact candidate and persist its validation evidence."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from adapters.eda import ProjectVerilatorRunner
from contracts.validator import load_document
from domain.artifacts import artifact_fingerprint, validate_compile_validation
from scripts.dvlib import canonical_hash


@dataclass(frozen=True)
class CompileCandidateInput:
    job_root: Path
    project_input: Mapping[str, Any]
    source_checkpoint: Mapping[str, Any]
    replacement: Mapping[str, Any]
    candidate: Mapping[str, Any]


@dataclass(frozen=True)
class CompileCandidateResult:
    validation: dict[str, Any]
    output_reference: str
    replayed: bool


@dataclass(frozen=True)
class CompileCandidateDependencies:
    workspace_root: Path
    result_root: Path
    error: type[Exception]
    persist_json: Callable[[Path, str, Mapping[str, Any]], None]
    runner_factory: Callable[..., Any] | None = None


class CompileCandidateHandler:
    def __init__(self, dependencies: CompileCandidateDependencies):
        self.dependencies = dependencies

    def handle(self, command: CompileCandidateInput) -> CompileCandidateResult:
        deps = self.dependencies
        project_input = command.project_input
        checkpoint = command.source_checkpoint
        replacement = command.replacement
        candidate = command.candidate
        token = canonical_hash({
            "checkpoint": checkpoint["checkpoint_fingerprint"],
            "replacement": replacement["replacement_fingerprint"],
            "candidate": candidate["candidate_fingerprint"],
        })[:24]
        relative = "staging/validations/oches003_compile.{}.json".format(token)
        path = command.job_root / relative
        if path.exists():
            value = load_document(path)
            validated = validate_compile_validation(
                value, job_id=project_input["job_id"],
                checkpoint_fingerprint=checkpoint["checkpoint_fingerprint"],
                replacement_fingerprint=replacement["replacement_fingerprint"],
                candidate_fingerprint=candidate["candidate_fingerprint"],
                error=deps.error)
            return CompileCandidateResult(validated, relative, True)
        if deps.runner_factory is None:
            runner = ProjectVerilatorRunner(
                deps.workspace_root, deps.result_root, project_input["job_id"],
                project_input["eda"]["environment_fingerprint"],
                project_input["eda"]["timeout_seconds"])
        else:
            runner = deps.runner_factory(
                deps.workspace_root, deps.result_root, project_input)
        source_paths = [
            item["baseline_path"] for item in project_input["rtl"]["sources"]]
        source_paths.append(
            (command.job_root / candidate["output_path"]).relative_to(
                deps.workspace_root).as_posix())
        bundle = runner.build_only(
            source_paths, candidate["top"],
            project_input["eda"]["approval_ref"], token)
        evidence = bundle["evidence"]
        value = {
            "schema_version": "1.0",
            "artifact_kind": "PROJECT_PRECOMMIT_COMPILE_VALIDATION",
            "validation_id": "COMPILEVALIDATION.{}".format(token.upper()),
            "job_id": project_input["job_id"],
            "input_fingerprint": project_input["input_fingerprint"],
            "source_checkpoint_fingerprint": checkpoint["checkpoint_fingerprint"],
            "replacement_fingerprint": replacement["replacement_fingerprint"],
            "candidate_fingerprint": candidate["candidate_fingerprint"],
            "content_fingerprint": candidate["content_fingerprint"],
            "request": copy.deepcopy(bundle["request"]),
            "evidence": copy.deepcopy(evidence),
            "status": evidence["execution_status"],
            "validation_fingerprint": "0" * 64,
        }
        value["validation_fingerprint"] = artifact_fingerprint(
            value, "validation_fingerprint")
        validate_compile_validation(
            value, job_id=project_input["job_id"],
            checkpoint_fingerprint=checkpoint["checkpoint_fingerprint"],
            replacement_fingerprint=replacement["replacement_fingerprint"],
            candidate_fingerprint=candidate["candidate_fingerprint"],
            error=deps.error)
        deps.persist_json(command.job_root, relative, value)
        return CompileCandidateResult(value, relative, False)
