"""Single-stage generation handlers with explicit artifact inputs."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from domain.artifacts import STAGE1 as UNIT_STAGE1, STAGE2 as UNIT_STAGE2
from domain.artifacts import unrouted_owner_scope
from domain.stage1 import enrich_stage1
from domain.stage2 import enrich_stage2
from domain.stage3 import enrich_stage3
from domain.uvm_testcase import build_manifest, validate_generated_tests
from scripts.dvlib import canonical_hash


STAGE1 = "SCENARIO_AC_MAP"
STAGE2 = "AC_TESTCASE_MAP"
STAGE3 = "TESTCASE"


@dataclass(frozen=True)
class GenerateStage1Input:
    project_input: dict[str, Any]
    job_root: Path
    spec_evidence: list[dict[str, Any]]
    sources: dict[str, str]
    spec_fingerprint: str
    revision: int
    budget: dict[str, Any]
    prior: dict[str, Any] | None = None
    issues: list[dict[str, Any]] | None = None
    executable_scope: dict[str, list[str]] | None = None


@dataclass(frozen=True)
class GenerateStage2Input:
    project_input: dict[str, Any]
    job_root: Path
    spec_evidence: list[dict[str, Any]]
    sources: dict[str, str]
    spec_fingerprint: str
    scenario_ac_map: dict[str, Any]
    revision: int
    budget: dict[str, Any]
    prior: dict[str, Any] | None = None
    issues: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class GenerateStage3Input:
    project_input: dict[str, Any]
    job_root: Path
    spec_evidence: list[dict[str, Any]]
    spec_fingerprint: str
    scenario_ac_map: dict[str, Any]
    ac_testcase_map: dict[str, Any]
    testcases: list[dict[str, Any]]
    shards: list[dict[str, Any]]
    revision: int
    budget: dict[str, Any]
    prior: dict[str, Any] | None = None
    issues: list[dict[str, Any]] | None = None
    execution_job_id: str | None = None
    effective_uvm_files: tuple[dict[str, str], ...] = ()
    effective_uvm_root: str | None = None
    profile_section: str = "initial"


@dataclass(frozen=True)
class GenerationResult:
    artifact: dict[str, Any]
    output_references: tuple[str, ...]
    replayed: bool
    testcases: tuple[dict[str, Any], ...] = ()
    shards: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class GenerationDependencies:
    error: type[Exception]
    root: Path
    max_items: int
    max_per_shard: int
    max_file_bytes: int
    policy_fingerprint: str
    request: Callable[..., dict[str, Any]]
    invoke: Callable[..., dict[str, Any]]
    response_candidate: Callable[[dict[str, Any]], dict[str, Any]]
    raw_generation: Callable[..., dict[str, Any]]
    candidate_correction: Callable[..., Any]
    inspect_no_rtl: Callable[..., None]
    incremental_store: Callable[[Path], Any]
    persist_artifact: Callable[[Path, str, dict[str, Any]], None]
    immutable_json: Callable[[Path, dict[str, Any]], None]
    immutable_text: Callable[[Path, str], None]
    owner_scope_fingerprint: Callable[..., str]
    compile_stage3_candidate: Callable[..., None]
    persist_stage3_rejection: Callable[..., Any]


class GenerateStage1Handler:
    def __init__(self, dependencies: GenerationDependencies):
        self.dependencies = dependencies

    def handle(self, command: GenerateStage1Input) -> GenerationResult:
        deps = self.dependencies
        value, job_root = command.project_input, command.job_root
        request = deps.request(
            value, STAGE1, command.revision, command.spec_evidence,
            command.spec_fingerprint, prior=command.prior,
            issues=command.issues)
        if command.executable_scope is not None:
            payload = json.loads(request["messages"][1]["content"])
            payload["owner_routed_executable_scope"] = copy.deepcopy(
                command.executable_scope)
            request["messages"][1]["content"] = json.dumps(
                payload, sort_keys=True, ensure_ascii=False)
            request["messages"][0]["content"] += (
                " This is a post-routing Stage 1 repair. Preserve exactly "
                "owner_routed_executable_scope; do not add, remove, or "
                "restore any Scenario or AC outside it.")
            deps.inspect_no_rtl(request, value, deps.root, deps.error)
        path = "staging/mappings/scenario_ac_map.r{:03d}.json".format(
            command.revision)
        replayed = (job_root / path).exists()
        response = deps.invoke(
            value, job_root, "GENERATOR", request, command.budget,
            "stage1.r{:03d}".format(command.revision))

        def validate_candidate(candidate: dict[str, Any],
                               candidate_response: dict[str, Any]
                               ) -> dict[str, Any]:
            raw = deps.raw_generation(candidate_response, STAGE1, deps.error)
            if raw != candidate:
                raise deps.error(
                    "CONFLICTING_REPLAY", "Stage 1 response candidate changed")
            return enrich_stage1(
                raw, value, command.sources, command.spec_fingerprint,
                command.revision, candidate_response,
                max_items=deps.max_items,
                policy_fingerprint=deps.policy_fingerprint,
                error=deps.error)

        try:
            artifact = validate_candidate(
                deps.response_candidate(response), response)
        except deps.error as caught:
            if (command.revision != 0 or command.prior is not None
                    or command.executable_scope is not None):
                raise
            artifact = deps.candidate_correction(
                value=value, job_root=job_root, stage=STAGE1,
                revision=command.revision, base_request=request,
                base_tag="stage1.r{:03d}".format(command.revision),
                response=response, caught=caught, budget=command.budget,
                validate_candidate=validate_candidate)
        if command.executable_scope is not None and (
                sorted(item["scenario_id"] for item in artifact["scenarios"])
                != command.executable_scope["scenario_ids"] or
                sorted(item["ac_id"] for item in
                       artifact["acceptance_criteria"])
                != command.executable_scope["ac_ids"]):
            raise deps.error(
                "OWNER_ROUTING_VIOLATION",
                "Stage 1 repair attempted to change Owner-routed executable scope")
        deps.incremental_store(job_root).persist_stage1(
            artifact,
            unrouted_owner_scope(value["job_id"], value["input_fingerprint"]),
            "PROVIDER")
        deps.persist_artifact(job_root, path, artifact)
        return GenerationResult(artifact, (path,), replayed)


class GenerateStage2Handler:
    def __init__(self, dependencies: GenerationDependencies):
        self.dependencies = dependencies

    def handle(self, command: GenerateStage2Input) -> GenerationResult:
        deps = self.dependencies
        value, job_root, map1 = (
            command.project_input, command.job_root, command.scenario_ac_map)
        request = deps.request(
            value, STAGE2, command.revision, command.spec_evidence,
            command.spec_fingerprint, map1=map1, prior=command.prior,
            issues=command.issues)
        path = "staging/mappings/ac_testcase_map.r{:03d}.json".format(
            command.revision)
        replayed = (job_root / path).exists()
        response = deps.invoke(
            value, job_root, "GENERATOR", request, command.budget,
            "stage2.r{:03d}".format(command.revision))

        def validate_candidate(candidate: dict[str, Any],
                               candidate_response: dict[str, Any]):
            raw = deps.raw_generation(candidate_response, STAGE2, deps.error)
            if raw != candidate:
                raise deps.error(
                    "CONFLICTING_REPLAY", "Stage 2 response candidate changed")
            artifact_value, testcases, shards = enrich_stage2(
                raw, value, map1, command.sources, command.spec_fingerprint,
                command.revision, candidate_response,
                max_items=deps.max_items,
                max_per_shard=deps.max_per_shard,
                max_file_bytes=deps.max_file_bytes,
                policy_fingerprint=deps.policy_fingerprint,
                error=deps.error)
            for reference, shard in zip(artifact_value["shards"], shards):
                deps.immutable_json(job_root / reference["path"], shard)
            return artifact_value, testcases, shards

        try:
            artifact, testcases, shards = validate_candidate(
                deps.response_candidate(response), response)
        except deps.error as caught:
            if command.revision != 0 or command.prior is not None:
                raise
            artifact, testcases, shards = deps.candidate_correction(
                value=value, job_root=job_root, stage=STAGE2,
                revision=command.revision, base_request=request,
                base_tag="stage2.r{:03d}".format(command.revision),
                response=response, caught=caught, budget=command.budget,
                validate_candidate=validate_candidate)
        deps.persist_artifact(job_root, path, artifact)
        store = deps.incremental_store(job_root)
        _, stage1_units = store.load(
            UNIT_STAGE1, "CURRENT", map1["revision"], value["job_id"])
        store.persist_stage2(
            artifact, testcases, stage1_units,
            deps.owner_scope_fingerprint(job_root, value))
        references = (path, *(item["path"] for item in artifact["shards"]))
        return GenerationResult(
            artifact, tuple(references), replayed,
            tuple(testcases), tuple(shards))


class GenerateStage3Handler:
    def __init__(self, dependencies: GenerationDependencies):
        self.dependencies = dependencies

    def handle(self, command: GenerateStage3Input) -> GenerationResult:
        deps = self.dependencies
        value, job_root = command.project_input, command.job_root
        map1, map2 = command.scenario_ac_map, command.ac_testcase_map
        base_request = deps.request(
            value, STAGE3, command.revision, command.spec_evidence,
            command.spec_fingerprint, map1=map1,
            map2_bundle={"index": map2, "shards": command.shards},
            prior=command.prior, issues=command.issues,
            effective_uvm_files=command.effective_uvm_files,
            effective_uvm_root=command.effective_uvm_root)
        if command.profile_section not in {"initial", "repair"}:
            raise deps.error(
                "INVALID_AGENT_BINDING",
                "Stage 3 Provider profile section is invalid")
        base_request["metadata"]["agent_profile_section"] = \
            command.profile_section
        if command.execution_job_id is not None:
            execution_tag = canonical_hash({
                "execution_job_id": command.execution_job_id,
                "source_job_id": value["job_id"],
            })[:16].upper()
            base_request["request_id"] += ".TEST.{}".format(execution_tag)
            base_request["metadata"]["execution_job_id"] = (
                command.execution_job_id)
        tag = "stage3.r{:03d}".format(command.revision)
        path = (
            "staging/generated/portable_sv/"
            "testcase.r{:03d}.json".format(command.revision))
        replayed = (job_root / path).exists()
        response = deps.invoke(
            value, job_root, "GENERATOR", copy.deepcopy(base_request),
            command.budget, tag)

        def validate_candidate(candidate_value: dict[str, Any],
                               candidate_response: dict[str, Any]
                               ) -> dict[str, Any]:
            raw_value = deps.raw_generation(
                candidate_response, STAGE3, deps.error)
            if raw_value != candidate_value:
                raise deps.error(
                    "CONFLICTING_REPLAY", "Stage 3 response candidate changed")
            effective_value = copy.deepcopy(value)
            baseline_by_path = {
                item["logical_path"]: item for item in
                value["uvm_testcase_context"]["files"]}
            effective_value["uvm_testcase_context"]["files"] = [{
                **copy.deepcopy(baseline_by_path[item["logical_path"]]),
                "content": item["content"],
                "fingerprint": item["fingerprint"],
            } for item in command.effective_uvm_files]
            artifact_value = enrich_stage3(
                candidate_value, effective_value, map1, map2, command.testcases,
                command.spec_fingerprint, command.revision,
                candidate_response, deps.policy_fingerprint,
                str(command.effective_uvm_root), deps.error)
            # UVM code is compiled only after DV_OWNER approval inside the
            # deployment-owned Xcelium platform.  A standalone pre-approval
            # build would require freezing or guessing that platform.
            return artifact_value

        try:
            artifact = validate_candidate(
                deps.response_candidate(response), response)
        except deps.error as caught:
            prior_candidate = deps.response_candidate(response)
            deps.persist_stage3_rejection(
                job_root, value, tag, command.revision, 0,
                command.spec_fingerprint, map1, map2, response,
                prior_candidate, caught)
            if command.revision != 0 or command.prior is not None:
                raise
            artifact = deps.candidate_correction(
                value=value, job_root=job_root, stage=STAGE3,
                revision=command.revision, base_request=base_request,
                base_tag=tag, response=response, caught=caught,
                budget=command.budget, validate_candidate=validate_candidate)
        deps.immutable_text(job_root / artifact["output_path"], artifact["content"])
        # This manifest is framework-derived, not model-authored.
        uvm_manifest = build_manifest(
            command.testcases,
            implemented_testcase_ids=artifact["implemented_testcase_ids"],
            skipped_testcases=artifact["skipped_testcases"])
        validate_generated_tests(artifact["content"], uvm_manifest, deps.error)
        manifest_path = (
            "staging/generated/uvm/generated_tests_manifest.r{:03d}.json".format(
                command.revision))
        deps.persist_artifact(job_root, manifest_path, uvm_manifest)
        deps.persist_artifact(job_root, path, artifact)
        store = deps.incremental_store(job_root)
        effective_job_id = command.execution_job_id or value["job_id"]
        owner_scope = deps.owner_scope_fingerprint(
            job_root, value, map1 if command.execution_job_id is not None else None)
        try:
            _, stage1_units = store.load(
                UNIT_STAGE1, "CURRENT", map1["revision"], effective_job_id)
            _, stage2_units = store.load(
                UNIT_STAGE2, "CURRENT", map2["revision"], effective_job_id)
        except deps.error as caught:
            if command.execution_job_id is None or caught.code not in {
                    "PARTIAL_ARTIFACT", "INVALID_SCHEMA"}:
                raise
            stage1_result = store.persist_stage1(
                map1, owner_scope, "CURRENT", command.execution_job_id)
            stage1_units = stage1_result["units"]
            stage2_result = store.persist_stage2(
                map2, command.testcases, stage1_units, owner_scope,
                command.execution_job_id)
            stage2_units = stage2_result["units"]
        store.persist_stage3(
            artifact, stage1_units, stage2_units, owner_scope,
            effective_job_id)
        return GenerationResult(
            artifact, (artifact["output_path"], manifest_path, path), replayed)
