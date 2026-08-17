"""OCHES003 serial commit, impact, and final-review runtime."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from adapters.eda import ProjectVerilatorRunner
from contracts.validator import accepted, load_document, validate
from core.atomic_artifact import publish_immutable_bytes, publish_immutable_text
from core.project_agent_profile import binding, binding_lineage
from core.project_incremental import (
    IncrementalArtifactStore, evaluate_impact, load_index, persist_impact,
)
from core.project_job import (
    INTERNAL_MANIFEST_PATH, ProjectJobError, ProjectJobWorkflow,
    validate_project_input,
)
from core.project_scoped_repair import (
    artifact_fingerprint, validate_scoped_replacement_lineage,
)
from core.project_repair import build_failure_feedback, dispatch_canonical_group
from core.project_repair_runtime import ProjectRepairRuntime
from core.project_staged import (
    WORKFLOW_VERSION, StagedProjectWorkflow, build_reviewer_repair_lineage,
    validate_ac_testcase_map, validate_scenario_ac_map,
)
from core.project_tools import ProjectReadModel
from core.tool_session import ToolSessionError, load_terminal_transcript_events
from core.project_oches003 import (
    RepairRecordStore, build_prompt_contract, canonical_repair_groups,
)
from scripts.dvlib import canonical_hash


COMMIT_PATH = "audit/oches003_commit_manifest.001.json"
FINAL_PATH = "audit/oches003_final_review_checkpoint.json"
RECOVERED_FINAL_PATH = "audit/oches004_final_review_checkpoint.json"
SOURCE_CHECKPOINT_PATH = "audit/oches002_scoped_replacement_validated.json"

CompileRunnerFactory = Callable[[Path, Path, Mapping[str, Any]], Any]
ProviderFactory = Callable[[Path, Mapping[str, Any], str], Any]


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _regular(job_root: Path, relative: str, error: Callable[..., Exception]
             ) -> Path:
    pure = PurePosixPath(relative)
    if (pure.is_absolute() or not pure.parts or
            any(part in {"", ".", ".."} or part.startswith(".")
                for part in pure.parts)):
        raise error("PATH_ESCAPE", "OCHES003 authority path is unsafe")
    path = job_root.joinpath(*pure.parts)
    try:
        if (not path.is_file() or path.is_symlink() or
                job_root.resolve() not in path.resolve().parents):
            raise OSError("authority path is unavailable")
    except OSError as caught:
        raise error("STALE_EVIDENCE", "OCHES003 authority file is unavailable") \
            from caught
    return path


def _persist_json(job_root: Path, relative: str, value: Mapping[str, Any]) -> str:
    encoded = (json.dumps(
        value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode(
            "utf-8")
    publish_immutable_bytes(
        job_root / relative, encoded,
        lambda message: ProjectJobError("CONFLICTING_REPLAY", message),
        "OCHES003 immutable artifact conflicts")
    return relative


def _persist_text(job_root: Path, relative: str, value: str) -> str:
    publish_immutable_text(
        job_root / relative, value,
        lambda message: ProjectJobError("CONFLICTING_REPLAY", message),
        "OCHES003 immutable source conflicts")
    return relative


def _provider_identity(
        replacement: Mapping[str, Any], response: Mapping[str, Any]
        ) -> dict[str, Any]:
    usage = response.get("usage", {})
    return {
        "provider_id": replacement["stage_agent"]["provider_id"],
        "model_id": replacement["stage_agent"]["model_id"],
        "request_id": replacement["stage_agent"]["request_id"],
        "response_id": replacement["stage_agent"]["response_id"],
        "input_tokens": int(usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
    }


def _current_index_path(stage: str, revision: int) -> str:
    return "staging/units/{}/index.current.r{:03d}.json".format(
        stage, revision)


def _next_unoccupied_revision(
        job_root: Path, pattern: str, current: int) -> int:
    revisions = [current]
    for path in job_root.glob(pattern):
        match = re.search(r"\.r([0-9]{3})\.", path.name)
        if match:
            revisions.append(int(match.group(1)))
    return max(revisions) + 1


def _artifact_paths(
        map1: Mapping[str, Any], map2: Mapping[str, Any],
        candidate: Mapping[str, Any]) -> dict[str, str]:
    return {
        "scenario_ac_map":
            "staging/mappings/scenario_ac_map.checked.r{:03d}.json".format(
                map1["revision"]),
        "ac_testcase_map":
            "staging/mappings/ac_testcase_map.r{:03d}.json".format(
                map2["revision"]),
        "testcase":
            "staging/generated/portable_sv/testcase.r{:03d}.json".format(
                candidate["revision"]),
    }


def _artifact_roots(
        map1: Mapping[str, Any], map2: Mapping[str, Any],
        candidate: Mapping[str, Any]) -> dict[str, str]:
    return {
        "scenario_ac_map": map1["artifact_fingerprint"],
        "ac_testcase_map": map2["artifact_fingerprint"],
        "testcase": candidate["candidate_fingerprint"],
    }


def _rebuild_stage1(
        source: Mapping[str, Any], replacements: Mapping[str, Mapping[str, Any]],
        provider: Mapping[str, Any], revision: int) -> dict[str, Any]:
    value = copy.deepcopy(dict(source))
    value["revision"] = revision
    value["provider"] = copy.deepcopy(dict(provider))
    for collection, identity in (
            ("scenarios", "scenario_id"),
            ("acceptance_criteria", "ac_id")):
        for item in value[collection]:
            replacement = replacements.get(item[identity])
            if replacement is not None:
                item.update(copy.deepcopy(replacement["semantic_body"]))
            item["item_fingerprint"] = artifact_fingerprint(
                item, "item_fingerprint")
    value["map_id"] = "SCENARIOACMAP.{}".format(canonical_hash({
        "job": value["job_id"], "revision": revision,
        "scenarios": value["scenarios"],
        "acceptance_criteria": value["acceptance_criteria"],
    })[:16].upper())
    value["artifact_fingerprint"] = artifact_fingerprint(
        value, "artifact_fingerprint")
    return value


def _rebuild_stage2(
        source: Mapping[str, Any], logical_testcases: list[dict[str, Any]],
        replacements: Mapping[str, Mapping[str, Any]],
        map1: Mapping[str, Any], provider: Mapping[str, Any], revision: int
        ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    testcases = copy.deepcopy(logical_testcases)
    for testcase in testcases:
        replacement = replacements.get(testcase["testcase_id"])
        if replacement is not None:
            body = copy.deepcopy(replacement["semantic_body"])
            body.pop("reason", None)
            testcase.update(body)
        testcase["testcase_fingerprint"] = artifact_fingerprint(
            testcase, "testcase_fingerprint")
    coverage = []
    for ac_id in sorted(
            item["ac_id"] for item in map1["acceptance_criteria"]):
        item = {
            "ac_id": ac_id,
            "testcase_ids": sorted(
                testcase["testcase_id"] for testcase in testcases
                if ac_id in testcase["ac_ids"]),
            "coverage_fingerprint": "0" * 64,
        }
        item["coverage_fingerprint"] = artifact_fingerprint(
            item, "coverage_fingerprint")
        coverage.append(item)
    value = copy.deepcopy(dict(source))
    value.update({
        "revision": revision,
        "map_id": "ACTESTCASEMAP.{}".format(canonical_hash({
            "job": source["job_id"], "revision": revision,
            "map1": map1["artifact_fingerprint"],
            "testcases": testcases,
        })[:16].upper()),
        "scenario_ac_map_fingerprint": map1["artifact_fingerprint"],
        "upstream_fingerprints": {
            "input": source["input_fingerprint"],
            "spec": source["spec_fingerprint"],
            "scenario_ac_map": map1["artifact_fingerprint"],
        },
        "storage": "INLINE",
        "logical_testcases": testcases,
        "shards": [],
        "ac_coverage": coverage,
        "completeness": {
            **copy.deepcopy(source["completeness"]),
            "ac_ids": sorted(item["ac_id"] for item in
                             map1["acceptance_criteria"]),
            "testcase_ids": sorted(item["testcase_id"] for item in testcases),
        },
        "provider": copy.deepcopy(dict(provider)),
        "artifact_fingerprint": "0" * 64,
    })
    value["artifact_fingerprint"] = artifact_fingerprint(
        value, "artifact_fingerprint")
    return value, testcases


def _validate_committed_candidate(value: Mapping[str, Any]) -> None:
    if (not accepted(validate("project_committed_testcase", value)) or
            value["candidate_fingerprint"] != artifact_fingerprint(
                value, "candidate_fingerprint") or
            value["content_fingerprint"] != _sha_text(value["content"])):
        raise ProjectJobError(
            "INVALID_SCHEMA", "committed testcase contract is invalid")
    by_id = {item["code_unit_id"]: item for item in value["code_units"]}
    if (len(by_id) != len(value["code_units"]) or
            set(by_id) != set(value["assembly_manifest"]) or
            "".join(by_id[item]["content"]
                    for item in value["assembly_manifest"]) != value["content"] or
            any(item["content_fingerprint"] != _sha_text(item["content"])
                for item in value["code_units"])):
        raise ProjectJobError(
            "ASSEMBLY_MISMATCH", "committed testcase assembly is invalid")


def _rebuild_stage3(
        source: Mapping[str, Any], replacements: Mapping[str, Mapping[str, Any]],
        map1: Mapping[str, Any], map2: Mapping[str, Any],
        provider: Mapping[str, Any], revision: int) -> dict[str, Any]:
    code_units = copy.deepcopy(source["code_units"])
    for unit in code_units:
        replacement = replacements.get(unit["code_unit_id"])
        if replacement is not None:
            unit["content"] = replacement["semantic_body"]["segments"][0]
        unit["content_fingerprint"] = _sha_text(unit["content"])
    by_id = {item["code_unit_id"]: item for item in code_units}
    content = "".join(
        by_id[unit_id]["content"] for unit_id in source["assembly_manifest"])
    content_fp = _sha_text(content)
    checks = sorted([
        "ASSEMBLY_COMPLETE", "CONTENT_FINGERPRINT", "FRAMEWORK_FORMALIZED",
        "FINAL_REVIEW_TRACEABILITY_REQUIRED",
    ])
    validation = {
        "status": "PASS", "checks": checks,
        "validation_fingerprint": canonical_hash({
            "content_fingerprint": content_fp,
            "input_fingerprint": source["input_fingerprint"],
            "scenario_ac_map_fingerprint": map1["artifact_fingerprint"],
            "ac_testcase_map_fingerprint": map2["artifact_fingerprint"],
            "checks": checks,
        }),
    }
    value = {
        "schema_version": "1.0",
        "artifact_kind": "COMMITTED_PORTABLE_SV_TESTCASE",
        "candidate_id": "PROJECTTESTCOMMIT.{}".format(canonical_hash({
            "job": source["job_id"], "revision": revision,
            "content": content_fp,
        })[:16].upper()),
        "job_id": source["job_id"],
        "revision": revision,
        "state": "COMMIT_PREPARED",
        "output_path":
            "staging/generated/portable_sv/testcase.r{:03d}.sv".format(
                revision),
        "top": source["top"],
        "content": content,
        "content_fingerprint": content_fp,
        "input_fingerprint": source["input_fingerprint"],
        "spec_fingerprint": source["spec_fingerprint"],
        "scenario_ac_map_fingerprint": map1["artifact_fingerprint"],
        "ac_testcase_map_fingerprint": map2["artifact_fingerprint"],
        "upstream_fingerprints": {
            "input": source["input_fingerprint"],
            "spec": source["spec_fingerprint"],
            "scenario_ac_map": map1["artifact_fingerprint"],
            "ac_testcase_map": map2["artifact_fingerprint"],
        },
        "policy_fingerprint": source["policy_fingerprint"],
        "code_units": sorted(code_units, key=lambda item: item["code_unit_id"]),
        "assembly_manifest": copy.deepcopy(source["assembly_manifest"]),
        "implemented_testcase_ids": copy.deepcopy(
            source["implemented_testcase_ids"]),
        "traceability_status": "FINAL_REVIEW_REQUIRED",
        "provider": copy.deepcopy(dict(provider)),
        "validation": validation,
        "candidate_fingerprint": "0" * 64,
    }
    value["candidate_fingerprint"] = artifact_fingerprint(
        value, "candidate_fingerprint")
    _validate_committed_candidate(value)
    return value


def validate_compile_validation(
        value: Mapping[str, Any], *, job_id: str,
        checkpoint_fingerprint: str, replacement_fingerprint: str,
        candidate_fingerprint: str) -> dict[str, Any]:
    if (not accepted(validate("project_compile_validation", value)) or
            value.get("validation_fingerprint") != artifact_fingerprint(
                value, "validation_fingerprint") or
            value.get("job_id") != job_id or
            value.get("source_checkpoint_fingerprint") !=
                checkpoint_fingerprint or
            value.get("replacement_fingerprint") != replacement_fingerprint or
            value.get("candidate_fingerprint") != candidate_fingerprint or
            not accepted(validate("eda_probe_request", value.get("request"))) or
            not accepted(validate("eda_probe_evidence", value.get("evidence"))) or
            value["evidence"].get("request_fingerprint") !=
                value["request"].get("request_fingerprint") or
            value.get("status") != value["evidence"].get("execution_status")):
        raise ProjectJobError(
            "STALE_EVIDENCE", "precommit compile validation is stale")
    return copy.deepcopy(dict(value))


def validate_commit_manifest(
        job_root: Path, manifest: Mapping[str, Any],
        project_input: Mapping[str, Any]) -> dict[str, Any]:
    if (not accepted(validate("project_commit_manifest", manifest)) or
            manifest.get("commit_fingerprint") != artifact_fingerprint(
                manifest, "commit_fingerprint") or
            manifest.get("job_id") != project_input["job_id"] or
            manifest.get("input_fingerprint") !=
                project_input["input_fingerprint"]):
        raise ProjectJobError("STALE_EVIDENCE", "commit manifest is stale")
    checkpoint = load_document(_regular(
        job_root, manifest["source_checkpoint_path"], ProjectJobError))
    if (checkpoint.get("checkpoint_fingerprint") !=
            manifest["source_checkpoint_fingerprint"] or
            checkpoint.get("replacement_path") != manifest["replacement_path"] or
            checkpoint.get("replacement_fingerprint") !=
                manifest["replacement_fingerprint"] or
            checkpoint.get("dispatch_fingerprint") !=
                manifest["dispatch_fingerprint"]):
        raise ProjectJobError("STALE_EVIDENCE", "commit source checkpoint differs")
    if (manifest["old_artifact_paths"] != {
            "scenario_ac_map": checkpoint["scenario_ac_map_path"],
            "ac_testcase_map": checkpoint["ac_testcase_map_path"],
            "testcase": checkpoint["candidate_metadata_path"],
            } or manifest["old_artifact_roots"] !=
                checkpoint["artifact_roots"] or
            manifest["prior_review_index_path"] !=
                checkpoint["review_unit_index_path"]):
        raise ProjectJobError("STALE_EVIDENCE", "commit base authority differs")
    replacement = load_document(_regular(
        job_root, manifest["replacement_path"], ProjectJobError))
    if (replacement.get("replacement_fingerprint") !=
            artifact_fingerprint(replacement, "replacement_fingerprint") or
            replacement.get("replacement_fingerprint") !=
                manifest["replacement_fingerprint"] or
            replacement.get("dispatch_fingerprint") !=
                manifest["dispatch_fingerprint"] or
            replacement.get("job_id") != manifest["job_id"]):
        raise ProjectJobError("STALE_EVIDENCE", "commit replacement lineage is stale")
    dispatch = load_document(_regular(
        job_root, checkpoint["dispatch_path"], ProjectJobError))
    if (dispatch.get("dispatch_fingerprint") !=
            artifact_fingerprint(dispatch, "dispatch_fingerprint") or
            dispatch.get("dispatch_fingerprint") !=
                manifest["dispatch_fingerprint"] or
            dispatch.get("job_id") != manifest["job_id"] or
            dispatch.get("input_fingerprint") != manifest["input_fingerprint"] or
            dispatch.get("stage") != manifest["stage"] or
            dispatch.get("spec_fingerprint") != manifest["spec_fingerprint"] or
            dispatch.get("policy_fingerprint") !=
                manifest["policy_fingerprint"] or
            dispatch.get("owner_scope_fingerprint") !=
                manifest["owner_scope_fingerprint"]):
        raise ProjectJobError("STALE_EVIDENCE", "commit dispatch lineage is stale")
    compile_value = load_document(_regular(
        job_root, manifest["compile_validation_path"], ProjectJobError))
    validate_compile_validation(
        compile_value, job_id=manifest["job_id"],
        checkpoint_fingerprint=manifest["source_checkpoint_fingerprint"],
        replacement_fingerprint=manifest["replacement_fingerprint"],
        candidate_fingerprint=manifest["new_artifact_roots"]["testcase"])
    if (compile_value["status"] != "PASS" or
            compile_value["validation_fingerprint"] !=
                manifest["compile_validation_fingerprint"]):
        raise ProjectJobError("STALE_EVIDENCE", "commit lacks passing compile evidence")
    impact = load_document(_regular(
        job_root, manifest["impact_manifest_path"], ProjectJobError))
    if (not accepted(validate("project_impact_manifest", impact)) or
            impact.get("impact_fingerprint") != artifact_fingerprint(
                impact, "impact_fingerprint") or
            impact.get("impact_fingerprint") != manifest["impact_fingerprint"] or
            impact.get("source_checkpoint_fingerprint") !=
                manifest["source_checkpoint_fingerprint"] or
            impact.get("replacement_fingerprint") !=
                manifest["replacement_fingerprint"] or
            impact.get("commit_id") != manifest["commit_id"]):
        raise ProjectJobError("STALE_EVIDENCE", "commit impact lineage is stale")
    for stage in ("stage1", "stage2", "stage3"):
        old_index, _ = load_index(
            job_root, manifest["old_index_paths"][stage], ProjectJobError)
        new_index, _ = load_index(
            job_root, manifest["new_index_paths"][stage], ProjectJobError)
        if (old_index["root_fingerprint"] != manifest["old_unit_roots"][stage] or
                new_index["root_fingerprint"] !=
                    manifest["new_unit_roots"][stage]):
            raise ProjectJobError("STALE_EVIDENCE", "commit unit root is stale")
    for key, field in (
            ("scenario_ac_map", "artifact_fingerprint"),
            ("ac_testcase_map", "artifact_fingerprint"),
            ("testcase", "candidate_fingerprint")):
        value = load_document(_regular(
            job_root, manifest["new_artifact_paths"][key], ProjectJobError))
        if value.get(field) != manifest["new_artifact_roots"][key]:
            raise ProjectJobError("STALE_EVIDENCE", "committed aggregate root is stale")
    candidate = load_document(
        job_root / manifest["new_artifact_paths"]["testcase"])
    _validate_committed_candidate(candidate)
    if candidate["content_fingerprint"] != compile_value["content_fingerprint"]:
        raise ProjectJobError(
            "STALE_EVIDENCE", "compile evidence binds different source bytes")
    content_path = _regular(job_root, candidate["output_path"], ProjectJobError)
    if content_path.read_text(encoding="utf-8") != candidate["content"]:
        raise ProjectJobError("STALE_EVIDENCE", "committed testcase bytes differ")
    return copy.deepcopy(dict(manifest))


class ProjectCommitRuntime:
    """Advance one validated replacement through OCHES003 exactly once."""

    def __init__(
            self, *, workspace_root: Path, result_root: Path,
            provider_factory: ProviderFactory,
            compile_runner_factory: CompileRunnerFactory | None = None,
            checkpoint_hook: Callable[[str, Mapping[str, Any]], None] | None = None):
        self.workspace_root = Path(workspace_root).resolve()
        self.result_root = Path(result_root).resolve()
        self.provider_factory = provider_factory
        self.compile_runner_factory = compile_runner_factory
        self.checkpoint_hook = checkpoint_hook

    def _job_root(self, job_id: str) -> Path:
        if not re.fullmatch(r"JOB\.PROJECT\.[A-Z0-9_.-]+", job_id):
            raise ProjectJobError("INVALID_INPUT", "invalid Project Job ID")
        return self.result_root / "jobs" / job_id

    def _manifest(self, job_root: Path) -> dict[str, Any]:
        return validate_project_input(
            load_document(job_root / INTERNAL_MANIFEST_PATH),
            self.workspace_root)

    def _entry(
            self, job_root: Path, project_input: Mapping[str, Any],
            source_checkpoint_path: str = SOURCE_CHECKPOINT_PATH,
            ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any],
                       ProjectReadModel, dict[str, Any]]:
        checkpoint = load_document(_regular(
            job_root, source_checkpoint_path, ProjectJobError))
        model = ProjectReadModel.from_checkpoint(job_root, checkpoint)
        dispatch = load_document(job_root / checkpoint["dispatch_path"])
        role = dispatch["stage"].replace("STAGE_", "stage")
        expected_binding = {
            "runtime_role": "STAGE_AGENT", "model_class": "PROFILED",
            **binding_lineage(project_input, "repair", role),
        }
        replacement = validate_scoped_replacement_lineage(
            job_root, checkpoint, model, expected_binding, ProjectJobError)
        lineage = {
            "dispatch_id": dispatch["dispatch_id"],
            "dispatch_fingerprint": dispatch["dispatch_fingerprint"],
            "scope_fingerprint": dispatch["scope_fingerprint"],
            "artifact_root": model.artifact_root,
            **expected_binding,
        }
        transcript = load_terminal_transcript_events(
            job_root=job_root, job_id=model.job_id, role=dispatch["stage"],
            session_id=replacement["session_id"], lineage=lineage)
        matches = [
            event["value"] for event in transcript["events"]
            if event["kind"] == "RESPONSE" and
            event["value"].get("provider_metadata", {}).get("response_id") ==
                replacement["stage_agent"]["response_id"]]
        if len(matches) != 1 or canonical_hash(matches[0]) != \
                replacement["response_fingerprint"]:
            raise ProjectJobError(
                "STALE_EVIDENCE", "replacement response lineage is stale")
        return checkpoint, dispatch, replacement, model, matches[0]

    def _load_source_bundle(
            self, job_root: Path, checkpoint: Mapping[str, Any],
            staged: StagedProjectWorkflow
            ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]],
                       list[dict[str, Any]], dict[str, Any]]:
        map1 = load_document(job_root / checkpoint["scenario_ac_map_path"])
        map2 = load_document(job_root / checkpoint["ac_testcase_map_path"])
        candidate = load_document(job_root / checkpoint["candidate_metadata_path"])
        testcases, shards = staged._load_shards(job_root, map2)
        return map1, map2, testcases, shards, candidate

    def _compile(
            self, job_root: Path, project_input: Mapping[str, Any],
            checkpoint: Mapping[str, Any], replacement: Mapping[str, Any],
            candidate: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        token = canonical_hash({
            "checkpoint": checkpoint["checkpoint_fingerprint"],
            "replacement": replacement["replacement_fingerprint"],
            "candidate": candidate["candidate_fingerprint"],
        })[:24]
        relative = "staging/validations/oches003_compile.{}.json".format(token)
        path = job_root / relative
        if path.exists():
            value = load_document(path)
            return relative, validate_compile_validation(
                value, job_id=project_input["job_id"],
                checkpoint_fingerprint=checkpoint["checkpoint_fingerprint"],
                replacement_fingerprint=replacement["replacement_fingerprint"],
                candidate_fingerprint=candidate["candidate_fingerprint"])
        if self.compile_runner_factory is None:
            runner = ProjectVerilatorRunner(
                self.workspace_root, self.result_root, project_input["job_id"],
                project_input["eda"]["environment_fingerprint"],
                project_input["eda"]["timeout_seconds"])
        else:
            runner = self.compile_runner_factory(
                self.workspace_root, self.result_root, project_input)
        source_paths = [
            item["baseline_path"] for item in project_input["rtl"]["sources"]]
        source_paths.append(
            (job_root / candidate["output_path"]).relative_to(
                self.workspace_root).as_posix())
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
            "source_checkpoint_fingerprint":
                checkpoint["checkpoint_fingerprint"],
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
            candidate_fingerprint=candidate["candidate_fingerprint"])
        _persist_json(job_root, relative, value)
        return relative, value

    @staticmethod
    def _index_paths_for(
            map1: Mapping[str, Any], map2: Mapping[str, Any],
            candidate: Mapping[str, Any], review_path: str) -> dict[str, str]:
        return {
            "stage1": _current_index_path("stage1", int(map1["revision"])),
            "stage2": _current_index_path("stage2", int(map2["revision"])),
            "stage3": _current_index_path("stage3", int(candidate["revision"])),
            "review": review_path,
        }

    def _authority_checkpoint(
            self, job_root: Path, initial: Mapping[str, Any],
            paths: Mapping[str, str]) -> dict[str, Any]:
        """Build the restart read model from committed existing artifacts.

        The durable authority switch is the existing GROUP_COMMIT record.  This
        object is deliberately in-memory only; it does not introduce a new
        checkpoint schema or persisted artifact kind.
        """
        map1 = load_document(_regular(
            job_root, paths["scenario_ac_map"], ProjectJobError))
        map2 = load_document(_regular(
            job_root, paths["ac_testcase_map"], ProjectJobError))
        candidate = load_document(_regular(
            job_root, paths["testcase"], ProjectJobError))
        roots = _artifact_roots(map1, map2, candidate)
        checkpoint = copy.deepcopy(dict(initial))
        checkpoint.update({
            "state": "SCOPED_REPLACEMENT_VALIDATED",
            "scenario_ac_map_path": paths["scenario_ac_map"],
            "ac_testcase_map_path": paths["ac_testcase_map"],
            "candidate_metadata_path": paths["testcase"],
            "artifact_roots": roots,
            "artifact_unit_index_paths": self._index_paths_for(
                map1, map2, candidate, initial["review_unit_index_path"]),
            "prior_review_artifact_roots": copy.deepcopy(
                load_document(job_root / initial["review_report_path"])[
                    "artifact_roots"]),
            "checkpoint_fingerprint": "0" * 64,
        })
        checkpoint["checkpoint_fingerprint"] = artifact_fingerprint(
            checkpoint, "checkpoint_fingerprint")
        ProjectReadModel.from_checkpoint(job_root, checkpoint)
        return checkpoint

    def _prepare_stage2(
            self, job_root: Path, project_input: Mapping[str, Any],
            authority: Mapping[str, Any],
            entries: list[tuple[Mapping[str, Any], Mapping[str, Any],
                                Mapping[str, Any]]],
            staged: StagedProjectWorkflow) -> dict[str, Any]:
        map1, map2, testcases, shards, candidate = self._load_source_bundle(
            job_root, authority, staged)
        del shards
        spec_evidence, sources, spec_fp = staged._spec(dict(project_input))
        del spec_evidence
        validate_scenario_ac_map(
            map1, dict(project_input), sources, spec_fp,
            staged.policy_fingerprint, ProjectJobError)
        validate_ac_testcase_map(
            map2, map1, dict(project_input), sources, spec_fp,
            staged.policy_fingerprint, testcases, ProjectJobError)
        replacements: dict[str, Mapping[str, Any]] = {}
        for _, replacement, _ in entries:
            for item in replacement["replacements"]:
                prior = replacements.get(item["unit_id"])
                if prior is not None and prior != item:
                    raise ProjectJobError(
                        "CONFLICTING_REPLAY",
                        "Stage 2 groups contain conflicting replacements")
                replacements[item["unit_id"]] = item
        provider = _provider_identity(entries[-1][1], entries[-1][2])
        revision = _next_unoccupied_revision(
            job_root, "staging/mappings/ac_testcase_map.r*.json",
            int(map2["revision"]))
        new_map2, new_testcases = _rebuild_stage2(
            map2, testcases, replacements, map1, provider,
            revision)
        validate_ac_testcase_map(
            new_map2, map1, dict(project_input), sources, spec_fp,
            staged.policy_fingerprint, new_testcases, ProjectJobError)
        new_paths = {
            "scenario_ac_map": authority["scenario_ac_map_path"],
            "ac_testcase_map": _artifact_paths(map1, new_map2, candidate)[
                "ac_testcase_map"],
            "testcase": authority["candidate_metadata_path"],
        }
        _persist_json(job_root, new_paths["ac_testcase_map"], new_map2)
        store = IncrementalArtifactStore(
            job_root, staged.max_file_bytes, ProjectJobError)
        old_indexes = {
            key: authority["artifact_unit_index_paths"][key]
            for key in ("stage1", "stage2", "stage3")
        }
        _, stage1_units = load_index(
            job_root, old_indexes["stage1"], ProjectJobError)
        stage2_result = store.persist_stage2(
            new_map2, new_testcases, stage1_units,
            staged._owner_scope_fingerprint(job_root, dict(project_input)))
        new_indexes = {
            **old_indexes,
            "stage2": stage2_result["path"],
        }
        return {
            "stage": "STAGE_2", "map1": map1, "map2": new_map2,
            "candidate": candidate, "testcases": new_testcases,
            "old_artifact_paths": {
                "scenario_ac_map": authority["scenario_ac_map_path"],
                "ac_testcase_map": authority["ac_testcase_map_path"],
                "testcase": authority["candidate_metadata_path"],
            },
            "new_artifact_paths": new_paths,
            "old_artifact_roots": copy.deepcopy(authority["artifact_roots"]),
            "new_artifact_roots": _artifact_roots(
                map1, new_map2, candidate),
            "old_index_paths": old_indexes,
            "new_index_paths": new_indexes,
        }

    def _prepare_stage3(
            self, job_root: Path, project_input: Mapping[str, Any],
            authority: Mapping[str, Any],
            entries: list[tuple[Mapping[str, Any], Mapping[str, Any],
                                Mapping[str, Any]]],
            fallback_entry: tuple[Mapping[str, Any], Mapping[str, Any],
                                  Mapping[str, Any]],
            staged: StagedProjectWorkflow) -> dict[str, Any]:
        map1, map2, testcases, shards, candidate = self._load_source_bundle(
            job_root, authority, staged)
        del testcases, shards
        replacements: dict[str, Mapping[str, Any]] = {}
        for _, replacement, _ in entries:
            for item in replacement["replacements"]:
                prior = replacements.get(item["unit_id"])
                if prior is not None and prior != item:
                    raise ProjectJobError(
                        "CONFLICTING_REPLAY",
                        "Stage 3 groups contain conflicting replacements")
                replacements[item["unit_id"]] = item
        provider_entry = entries[-1] if entries else fallback_entry
        provider = _provider_identity(provider_entry[1], provider_entry[2])
        replay_candidate_fps = set()
        for validation_path in job_root.glob(
                "staging/validations/oches003_compile.*.json"):
            value = load_document(validation_path)
            if (value.get("source_checkpoint_fingerprint") ==
                    authority["checkpoint_fingerprint"] and
                    value.get("replacement_fingerprint") ==
                    provider_entry[1]["replacement_fingerprint"]):
                replay_candidate_fps.add(value.get("candidate_fingerprint"))
        new_candidate = None
        for candidate_path in sorted(job_root.glob(
                "staging/generated/portable_sv/testcase.r*.json")):
            existing = load_document(candidate_path)
            if (existing.get("candidate_fingerprint") not in
                    replay_candidate_fps or
                    int(existing.get("revision", -1)) <=
                        int(candidate["revision"])):
                continue
            expected = _rebuild_stage3(
                candidate, replacements, map1, map2, provider,
                int(existing["revision"]))
            if existing == expected:
                new_candidate = existing
                break
        if new_candidate is None:
            revision = _next_unoccupied_revision(
                job_root, "staging/generated/portable_sv/testcase.r*.json",
                int(candidate["revision"]))
            new_candidate = _rebuild_stage3(
                candidate, replacements, map1, map2, provider,
                revision)
        new_paths = {
            "scenario_ac_map": authority["scenario_ac_map_path"],
            "ac_testcase_map": authority["ac_testcase_map_path"],
            "testcase": _artifact_paths(map1, map2, new_candidate)["testcase"],
        }
        _persist_text(
            job_root, new_candidate["output_path"], new_candidate["content"])
        _persist_json(job_root, new_paths["testcase"], new_candidate)
        store = IncrementalArtifactStore(
            job_root, staged.max_file_bytes, ProjectJobError)
        old_indexes = {
            key: authority["artifact_unit_index_paths"][key]
            for key in ("stage1", "stage2", "stage3")
        }
        _, stage1_units = load_index(
            job_root, old_indexes["stage1"], ProjectJobError)
        _, stage2_units = load_index(
            job_root, old_indexes["stage2"], ProjectJobError)
        candidate_for_units = {**new_candidate, "schema_version": "6.0"}
        stage3_result = store.persist_stage3(
            candidate_for_units, stage1_units, stage2_units,
            staged._owner_scope_fingerprint(job_root, dict(project_input)))
        new_indexes = {**old_indexes, "stage3": stage3_result["path"]}
        return {
            "stage": "STAGE_3", "map1": map1, "map2": map2,
            "candidate": new_candidate,
            "old_artifact_paths": {
                "scenario_ac_map": authority["scenario_ac_map_path"],
                "ac_testcase_map": authority["ac_testcase_map_path"],
                "testcase": authority["candidate_metadata_path"],
            },
            "new_artifact_paths": new_paths,
            "old_artifact_roots": copy.deepcopy(authority["artifact_roots"]),
            "new_artifact_roots": _artifact_roots(
                map1, map2, new_candidate),
            "old_index_paths": old_indexes,
            "new_index_paths": new_indexes,
        }

    @staticmethod
    def _target_revisions(
            job_root: Path, prepared: Mapping[str, Any],
            replacement: Mapping[str, Any] | None) -> list[dict[str, Any]]:
        old_units = {
            unit_id: unit for path in prepared["old_index_paths"].values()
            for unit_id, unit in load_index(
                job_root, path, ProjectJobError)[1].items()
        }
        new_units = {
            unit_id: unit for path in prepared["new_index_paths"].values()
            for unit_id, unit in load_index(
                job_root, path, ProjectJobError)[1].items()
        }
        requested = ({item["unit_id"] for item in replacement["replacements"]}
                     if replacement is not None else set())
        if not requested:
            requested = {
                unit_id for unit_id in old_units.keys() & new_units.keys()
                if old_units[unit_id]["artifact_fingerprint"] !=
                    new_units[unit_id]["artifact_fingerprint"]
            }
        result = []
        for unit_id in sorted(requested):
            old, new = old_units[unit_id], new_units[unit_id]
            result.append({
                "unit_id": unit_id,
                "unit_kind": new["unit_kind"],
                "old_revision": old["revision"],
                "new_revision": new["revision"],
                "old_artifact_fingerprint": old["artifact_fingerprint"],
                "new_artifact_fingerprint": new["artifact_fingerprint"],
            })
        return result

    def _record_stage_success(
            self, job_root: Path, project_input: Mapping[str, Any],
            dispatch: Mapping[str, Any], replacement: Mapping[str, Any] | None,
            prepared: Mapping[str, Any]) -> dict[str, Any]:
        store = RepairRecordStore(
            job_root, job_id=project_input["job_id"],
            input_fingerprint=project_input["input_fingerprint"],
            spec_fingerprint=dispatch["spec_fingerprint"],
            policy_fingerprint=dispatch["policy_fingerprint"])
        group_id = dispatch["group_id"]
        matching = [record for _, record in store.records()
                    if record["record_type"] == "GROUP_COMMIT" and
                    record["payload"].get("group_id") == group_id and
                    record["payload"].get("status") == "COMMITTED" and
                    record["payload"].get("current_roots") ==
                        prepared["new_artifact_roots"]]
        if matching:
            if len(matching) != 1:
                raise ProjectJobError(
                    "CONFLICTING_REPLAY", "stage commit is ambiguous")
            return matching[0]
        replacement_fp = (replacement["replacement_fingerprint"]
                          if replacement is not None else
                          dispatch["dispatch_fingerprint"])
        validation_path, validation = store.append("VALIDATION_RESULT", {
            "group_id": group_id,
            "replacement_fingerprint": replacement_fp,
            "status": "PASS", "diagnostics": [], "unexecuted_checks": [],
        })
        commit_path, commit = store.append("GROUP_COMMIT", {
            "group_id": group_id, "status": "COMMITTED",
            "before_roots": prepared["old_artifact_roots"],
            "current_roots": prepared["new_artifact_roots"],
            "target_revisions": self._target_revisions(
                job_root, prepared, replacement),
            "validation_fingerprint": validation["record_fingerprint"],
        })
        old_indexes = {
            key: load_index(job_root, path, ProjectJobError)
            for key, path in prepared["old_index_paths"].items()}
        new_indexes = {
            key: load_index(job_root, path, ProjectJobError)
            for key, path in prepared["new_index_paths"].items()}
        impact = evaluate_impact(
            old_indexes, new_indexes, ProjectJobError,
            allow_stale_stages=(
                {"STAGE3"} if prepared["stage"] == "STAGE_2" else set()))
        impact_path, impact_record = store.append("IMPACT_RESULT", {
            "group_id": group_id,
            "commit_fingerprint": commit["record_fingerprint"],
            "dirty_units": impact["dirty_units"],
            "reused_units": impact["reused_units"],
            "direct_dependency_closure": sorted({
                item["unit_id"] for item in impact["dirty_units"]}),
            "new_roots": prepared["new_artifact_roots"],
        })
        store.append("REPAIR_EPISODE", {
            "group_id": group_id,
            "ordered_links": [{
                "record_type": "VALIDATION_RESULT", "path": validation_path,
                "fingerprint": validation["record_fingerprint"],
            }, {
                "record_type": "GROUP_COMMIT", "path": commit_path,
                "fingerprint": commit["record_fingerprint"],
            }, {
                "record_type": "IMPACT_RESULT", "path": impact_path,
                "fingerprint": impact_record["record_fingerprint"],
            }],
            "terminal_status": "COMMITTED",
        })
        store.rebuild_indexes()
        return commit

    def _record_stage_failure(
            self, job_root: Path, project_input: Mapping[str, Any],
            dispatch: Mapping[str, Any], replacement: Mapping[str, Any],
            compile_value: Mapping[str, Any]) -> None:
        store = RepairRecordStore(
            job_root, job_id=project_input["job_id"],
            input_fingerprint=project_input["input_fingerprint"],
            spec_fingerprint=dispatch["spec_fingerprint"],
            policy_fingerprint=dispatch["policy_fingerprint"])
        diagnostics = copy.deepcopy(
            compile_value["evidence"].get("diagnostic_codes", []))
        prior_failures = [record for _, record in store.records()
                          if record["record_type"] == "VALIDATION_RESULT" and
                          record["payload"].get("group_id") ==
                              dispatch["group_id"] and
                          record["payload"].get("replacement_fingerprint") ==
                              replacement["replacement_fingerprint"] and
                          record["payload"].get("status") == "FAIL" and
                          record["payload"].get("diagnostics") == diagnostics]
        if prior_failures:
            return
        validation_path, validation = store.append("VALIDATION_RESULT", {
            "group_id": dispatch["group_id"],
            "replacement_fingerprint": replacement[
                "replacement_fingerprint"],
            "status": "FAIL",
            "diagnostics": diagnostics,
            "unexecuted_checks": [],
        })
        commit_path, commit = store.append("GROUP_COMMIT", {
            "group_id": dispatch["group_id"], "status": "NOT_COMMITTED",
            "before_roots": compile_value["base_artifact_roots"],
            "current_roots": compile_value["base_artifact_roots"],
            "target_revisions": [],
            "validation_fingerprint": validation["record_fingerprint"],
        })
        store.append("REPAIR_EPISODE", {
            "group_id": dispatch["group_id"],
            "ordered_links": [{
                "record_type": "VALIDATION_RESULT", "path": validation_path,
                "fingerprint": validation["record_fingerprint"],
            }, {
                "record_type": "GROUP_COMMIT", "path": commit_path,
                "fingerprint": commit["record_fingerprint"],
            }],
            "terminal_status": "VALIDATION_FAILED_NOT_COMMITTED",
        })
        store.rebuild_indexes()

    def _provider(
            self, job_root: Path, project_input: Mapping[str, Any]) -> Any:
        expected = binding(project_input, "review", "final")
        try:
            provider = self.provider_factory(
                job_root, copy.deepcopy(dict(project_input)), "review.final")
        except Exception as caught:
            raise ProjectJobError(
                "INVALID_AGENT_BINDING", "final Reviewer Provider is unavailable") \
                from caught
        if provider is None or (
                getattr(provider, "provider_id", expected["provider_id"]) !=
                    expected["provider_id"] or
                getattr(provider, "model_id", expected["model_id"]) !=
                    expected["model_id"]):
            raise ProjectJobError(
                "INVALID_AGENT_BINDING", "final Reviewer identity is stale")
        return provider

    @staticmethod
    def _planned_groups(records: RepairRecordStore) -> list[dict[str, Any]]:
        plans = [record for _, record in records.records()
                 if record["record_type"] == "ORCHESTRATOR_PLAN"]
        if len(plans) != 1:
            raise ProjectJobError(
                "STALE_EVIDENCE", "canonical repair plan record is ambiguous")
        return copy.deepcopy(plans[0]["payload"]["ordered_groups"])

    @staticmethod
    def _stage_session_id(
            dispatch: Mapping[str, Any], roots: Mapping[str, Any],
            attempt: int) -> str:
        """Derive one replay-stable identity for each Provider attempt."""
        if attempt < 1:
            raise ValueError("Provider attempt must be positive")
        if attempt == 1:
            seed = {
                "group": dispatch["group_id"],
                "roots": copy.deepcopy(dict(roots)),
            }
        else:
            seed = {
                "runtime": "OCHES003_PROVIDER_RETRY_V1",
                "group": dispatch["group_id"],
                "roots": copy.deepcopy(dict(roots)),
                "dispatch_fingerprint": dispatch["dispatch_fingerprint"],
                "attempt": attempt,
            }
        return "STAGESESSION.OCHES003.{}".format(
            canonical_hash(seed)[:20].upper())

    @staticmethod
    def _stage_lineage(
            runtime: ProjectRepairRuntime,
            dispatch: Mapping[str, Any]) -> dict[str, Any]:
        stage_role = dispatch["stage"].replace("STAGE_", "stage")
        return {
            "dispatch_id": dispatch["dispatch_id"],
            "dispatch_fingerprint": dispatch["dispatch_fingerprint"],
            "scope_fingerprint": dispatch["scope_fingerprint"],
            "artifact_root": runtime.model.artifact_root,
            **runtime._role_binding(
                "repair", stage_role, "STAGE_AGENT", "PROFILED"),
        }

    def _next_stage_session_id(
            self, job_root: Path, runtime: ProjectRepairRuntime,
            dispatch: Mapping[str, Any]) -> str:
        """Resume an open attempt or advance past request-only failures."""
        role = str(dispatch["stage"])
        role_directory = role.replace("STAGE_", "stage")
        lineage = self._stage_lineage(runtime, dispatch)
        attempt = 1
        while True:
            session_id = self._stage_session_id(
                dispatch, runtime.model.artifact_roots, attempt)
            session_dir = job_root / "transcripts" / role_directory / session_id
            if not session_dir.exists():
                return session_id
            if (not session_dir.is_dir() or session_dir.is_symlink()):
                raise ProjectJobError(
                    "STALE_EVIDENCE", "Stage Provider attempt path is unsafe")
            if not (session_dir / "manifest.json").exists():
                # SequentialToolSession validates and resumes every existing
                # raw event before it invokes the Provider again.
                return session_id
            try:
                transcript = load_terminal_transcript_events(
                    job_root=job_root, job_id=runtime.model.job_id,
                    role=role, session_id=session_id, lineage=lineage)
            except ToolSessionError as caught:
                raise ProjectJobError(caught.code, caught.message) from caught
            manifest = transcript["manifest"]
            terminal = manifest["terminal"]
            if terminal.get("status") == "COMPLETED":
                raise ProjectJobError(
                    "STALE_EVIDENCE",
                    "completed Stage Provider session lacks its checkpoint")
            if terminal != {
                    "status": "FAILED", "code": "PROVIDER_UNAVAILABLE",
                    "result_sequence": None,
                    }:
                raise ProjectJobError(
                    "INVALID_RETRY_STATE",
                    "Stage Provider session is not retryable")
            entries = manifest["entries"]
            if (len(entries) % 4 != 1 or
                    any(item["sequence"] != index + 1 or
                        item["kind"] != (
                            "REQUEST", "RESPONSE", "TOOL_CALL", "TOOL_RESULT"
                        )[index % 4]
                        for index, item in enumerate(entries))):
                raise ProjectJobError(
                    "INVALID_RETRY_STATE",
                    "Stage Provider failure is not request-only")
            request = transcript["events"][-1]["value"]
            if (not accepted(validate("provider_request", request)) or
                    request.get("metadata", {}).get("job_id") !=
                        runtime.model.job_id or
                    request.get("metadata", {}).get("role") != role or
                    request.get("metadata", {}).get("session_id") !=
                        session_id):
                raise ProjectJobError(
                    "STALE_EVIDENCE",
                    "Stage Provider request authority is stale")
            attempt += 1

    @staticmethod
    def _expected_group_record_payloads(
            plan: Mapping[str, Any], receipt: Mapping[str, Any],
            dispatch: Mapping[str, Any], roots: Mapping[str, Any]
            ) -> tuple[dict[str, Any], dict[str, Any]]:
        return ({
            "plan_id": plan["plan_id"], "group_id": dispatch["group_id"],
            "current_roots": copy.deepcopy(dict(roots)),
            "status": receipt["status"],
            "diagnostics": [copy.deepcopy(receipt["diagnostic"])],
        }, {
            "plan_id": plan["plan_id"], "group_id": dispatch["group_id"],
            "stage": dispatch["stage"], "targets": dispatch["targets"],
            "issue_ids": dispatch["issue_ids"],
            "base_roots": dispatch["artifact_roots"],
            "dependencies": dispatch["direct_dependencies"],
            "spec_identities": dispatch["authorized_spec_evidence"],
            "tool_allow_list": dispatch["tool_allow_list"],
            "retrieval_call_limit": dispatch["retrieval_call_limit"],
        })

    def _validate_existing_group_records(
            self, records: RepairRecordStore, plan: Mapping[str, Any],
            receipt: Mapping[str, Any], dispatch: Mapping[str, Any],
            roots: Mapping[str, Any]) -> None:
        """Accept identical historical Router duplicates, but no divergence."""
        router_payload, formal_payload = self._expected_group_record_payloads(
            plan, receipt, dispatch, roots)
        group_id = dispatch["group_id"]
        history = [value for _, value in records.records()]
        routers = [
            value for value in history
            if value["record_type"] == "ROUTER_RECEIPT" and
            value["payload"].get("group_id") == group_id]
        formals = [
            value for value in history
            if value["record_type"] == "FORMAL_DISPATCH" and
            value["payload"].get("group_id") == group_id]
        if (not routers or any(value["payload"] != router_payload
                               for value in routers)):
            raise ProjectJobError(
                "STALE_EVIDENCE", "existing Router history is divergent")
        if (len(formals) != 1 or formals[0]["payload"] != formal_payload):
            raise ProjectJobError(
                "STALE_EVIDENCE", "existing Formal dispatch history is stale")

    def _run_stage_provider(
            self, job_root: Path, project_input: Mapping[str, Any],
            runtime: ProjectRepairRuntime, dispatch: Mapping[str, Any],
            validated_path: str) -> tuple[str | None, str]:
        session_id = self._next_stage_session_id(job_root, runtime, dispatch)
        stage_role = dispatch["stage"].replace("STAGE_", "stage")
        profile_role = "repair.{}".format(stage_role)
        expected = binding(project_input, "repair", stage_role)
        try:
            provider = self.provider_factory(
                job_root, copy.deepcopy(dict(project_input)), profile_role)
        except ProjectJobError:
            raise
        except Exception as caught:
            raise ProjectJobError(
                "INVALID_AGENT_BINDING",
                "Stage Provider could not be created from the Job snapshot") \
                from caught
        if provider is None:
            raise ProjectJobError(
                "BLOCKED_TOOL", "Stage Provider is unavailable")
        if (getattr(provider, "provider_id", None) !=
                expected["provider_id"] or
                getattr(provider, "model_id", None) != expected["model_id"]):
            raise ProjectJobError(
                "INVALID_AGENT_BINDING",
                "Stage Provider identity differs from the Job snapshot")
        # A snapshot factory returns a new process-local adapter. Restore an
        # existing PASS probe, or run and persist the role's first probe,
        # before the request reaches OpenAICompatibleProvider._execute().
        probe_workflow = ProjectJobWorkflow(
            self.workspace_root, self.result_root,
            role_providers={profile_role: provider})
        probe_workflow._probe_provider(job_root, profile_role)
        try:
            runtime.run_stage(provider, dispatch, session_id)
        except ToolSessionError as caught:
            if caught.code == "PROVIDER_UNAVAILABLE":
                return None, "PROVIDER_UNAVAILABLE"
            raise ProjectJobError(caught.code, caught.message) from caught
        return validated_path, "VALIDATED"

    def _resume_existing_group(
            self, job_root: Path, project_input: Mapping[str, Any],
            checkpoint: Mapping[str, Any], plan: Mapping[str, Any],
            pending: Mapping[str, Any], records: RepairRecordStore,
            token: str) -> tuple[str | None, str] | None:
        receipt_path = "audit/router_receipt.oches003.{}.json".format(token)
        dispatch_path = "staging/dispatch/repair_dispatch.{}.json".format(token)
        feedback_path = "staging/dispatch/failure_feedback.{}.json".format(token)
        waiting_path = "audit/oches003_groups/{}.awaiting.json".format(token)
        validated_path = "audit/oches003_groups/{}.validated.json".format(token)
        required = [receipt_path, dispatch_path, feedback_path, waiting_path]
        present = [bool((job_root / path).exists()) for path in required]
        if not any(present) and not (job_root / validated_path).exists():
            return None
        if not all(present):
            raise ProjectJobError(
                "STALE_EVIDENCE",
                "pending group has an incomplete formal authority graph")
        waiting = load_document(_regular(
            job_root, waiting_path, ProjectJobError))
        if (waiting.get("checkpoint_fingerprint") != artifact_fingerprint(
                waiting, "checkpoint_fingerprint") or
                waiting.get("state") != "AWAITING_SCOPED_REPLACEMENT"):
            raise ProjectJobError(
                "STALE_EVIDENCE", "pending group checkpoint is stale")
        for key, value in checkpoint.items():
            if key != "scope_fingerprint" and waiting.get(key) != value:
                raise ProjectJobError(
                    "STALE_EVIDENCE",
                    "pending group checkpoint no longer binds current roots")
        if (waiting.get("router_receipt_path") != receipt_path or
                waiting.get("dispatch_path") != dispatch_path or
                waiting.get("failure_feedback_path") != feedback_path):
            raise ProjectJobError(
                "STALE_EVIDENCE", "pending group authority paths are stale")
        dispatch = load_document(_regular(
            job_root, dispatch_path, ProjectJobError))
        receipt = load_document(_regular(
            job_root, receipt_path, ProjectJobError))
        if (dispatch.get("group_id") != pending["group_id"] or
                dispatch.get("stage") != pending["stage"] or
                dispatch.get("targets") != pending["targets"] or
                dispatch.get("issue_ids") != pending["issue_ids"]):
            raise ProjectJobError(
                "STALE_EVIDENCE", "pending group dispatch scope is stale")
        runtime = ProjectRepairRuntime(
            job_root=job_root, checkpoint=waiting,
            project_input=project_input, error=ProjectJobError,
            authority_checkpoint_path=waiting_path,
            validated_checkpoint_path=validated_path)
        runtime.validate_stage_authority(dispatch)
        self._validate_existing_group_records(
            records, plan, receipt, dispatch, runtime.model.artifact_roots)
        if (job_root / validated_path).exists():
            _, validated_dispatch, _, _, _ = self._entry(
                job_root, project_input, validated_path)
            if validated_dispatch != dispatch:
                raise ProjectJobError(
                    "STALE_EVIDENCE",
                    "validated replacement dispatch differs from authority")
            return validated_path, "VALIDATED"
        return self._run_stage_provider(
            job_root, project_input, runtime, dispatch, validated_path)

    def _next_group_replacement(
            self, job_root: Path, project_input: Mapping[str, Any],
            source_checkpoint: Mapping[str, Any],
            last_commit: Mapping[str, Any], completed_group_ids: list[str],
            groups: list[dict[str, Any]]) -> tuple[str | None, str]:
        pending = next((group for group in groups
                        if group["group_id"] not in set(completed_group_ids)), None)
        if pending is None:
            return None, "ALL_GROUPS_TERMINAL"
        initial_report = load_document(
            job_root / source_checkpoint["review_report_path"])
        checkpoint = {
            "schema_version": "1.0", "workflow_version": WORKFLOW_VERSION,
            "state": "AWAITING_SCOPED_REPLACEMENT",
            "job_id": project_input["job_id"],
            "input_fingerprint": project_input["input_fingerprint"],
            "scenario_ac_map_path": last_commit["new_artifact_paths"][
                "scenario_ac_map"],
            "ac_testcase_map_path": last_commit["new_artifact_paths"][
                "ac_testcase_map"],
            "candidate_metadata_path": last_commit["new_artifact_paths"][
                "testcase"],
            "review_request_path": source_checkpoint["review_request_path"],
            "review_report_path": source_checkpoint["review_report_path"],
            "review_validation_path": source_checkpoint[
                "review_validation_path"],
            "review_unit_index_path": source_checkpoint["review_unit_index_path"],
            "artifact_roots": copy.deepcopy(last_commit["new_artifact_roots"]),
            "artifact_unit_index_paths": {
                **copy.deepcopy(last_commit["new_index_paths"]),
                "review": source_checkpoint["review_unit_index_path"],
            },
            "prior_review_artifact_roots": copy.deepcopy(
                initial_report["artifact_roots"]),
            "scope_fingerprint": source_checkpoint["scope_fingerprint"],
            "source_report_fingerprint": source_checkpoint[
                "source_report_fingerprint"],
            "plan_path": source_checkpoint["plan_path"],
            "canonical_groups": copy.deepcopy(groups),
            "completed_group_ids": copy.deepcopy(completed_group_ids),
        }
        model = ProjectReadModel.from_checkpoint(job_root, checkpoint)
        plan = load_document(job_root / checkpoint["plan_path"])
        stage_role = pending["stage"].replace("STAGE_", "stage")
        token = pending["group_id"].split(".")[-1].casefold()
        records = RepairRecordStore(
            job_root, job_id=project_input["job_id"],
            input_fingerprint=project_input["input_fingerprint"],
            spec_fingerprint=model.spec_fingerprint,
            policy_fingerprint=model.policy_fingerprint)
        resumed = self._resume_existing_group(
            job_root, project_input, checkpoint, plan, pending, records, token)
        if resumed is not None:
            return resumed
        receipt, dispatch = dispatch_canonical_group(
            plan, pending, dict(project_input), initial_report, model,
            {
                "runtime_role": "STAGE_AGENT", "model_class": "PROFILED",
                **binding_lineage(project_input, "repair", stage_role),
            }, ProjectJobError)
        receipt_path = _persist_json(
            job_root, "audit/router_receipt.oches003.{}.json".format(token),
            receipt)
        records.append("ROUTER_RECEIPT", {
            "plan_id": plan["plan_id"], "group_id": pending["group_id"],
            "current_roots": model.artifact_roots,
            "status": receipt["status"],
            "diagnostics": [receipt["diagnostic"]],
        }, producer_role="ROUTER")
        if dispatch is None:
            records.append("REPAIR_EPISODE", {
                "group_id": pending["group_id"], "ordered_links": [],
                "terminal_status": "REPLAN_REQUIRED",
            })
            return None, "REPLAN_REQUIRED"
        dispatch_path = _persist_json(
            job_root, "staging/dispatch/repair_dispatch.{}.json".format(token),
            dispatch)
        feedback = build_failure_feedback(
            dict(project_input), initial_report, model.artifact_roots,
            load_document(job_root / checkpoint["review_request_path"])[
                "coverage_scope"]["scope_fingerprint"],
            dispatch["issue_ids"])
        feedback_path = _persist_json(
            job_root, "staging/dispatch/failure_feedback.{}.json".format(token),
            feedback)
        records.append("FORMAL_DISPATCH", {
            "plan_id": plan["plan_id"], "group_id": dispatch["group_id"],
            "stage": dispatch["stage"], "targets": dispatch["targets"],
            "issue_ids": dispatch["issue_ids"],
            "base_roots": dispatch["artifact_roots"],
            "dependencies": dispatch["direct_dependencies"],
            "spec_identities": dispatch["authorized_spec_evidence"],
            "tool_allow_list": dispatch["tool_allow_list"],
            "retrieval_call_limit": dispatch["retrieval_call_limit"],
        })
        checkpoint.update({
            "router_receipt_path": receipt_path,
            "dispatch_path": dispatch_path,
            "dispatch_fingerprint": dispatch["dispatch_fingerprint"],
            "scope_fingerprint": dispatch["scope_fingerprint"],
            "failure_feedback_path": feedback_path,
            "checkpoint_fingerprint": "0" * 64,
        })
        checkpoint["checkpoint_fingerprint"] = artifact_fingerprint(
            checkpoint, "checkpoint_fingerprint")
        waiting_path = "audit/oches003_groups/{}.awaiting.json".format(token)
        validated_path = "audit/oches003_groups/{}.validated.json".format(token)
        _persist_json(job_root, waiting_path, checkpoint)
        runtime = ProjectRepairRuntime(
            job_root=job_root, checkpoint=checkpoint,
            project_input=project_input, error=ProjectJobError,
            authority_checkpoint_path=waiting_path,
            validated_checkpoint_path=validated_path)
        return self._run_stage_provider(
            job_root, project_input, runtime, dispatch, validated_path)

    def _final_review_current_authority(
            self, job_root: Path, project_input: Mapping[str, Any],
            checkpoint: Mapping[str, Any],
            staged: StagedProjectWorkflow) -> dict[str, Any]:
        map1, map2, testcases, shards, candidate = self._load_source_bundle(
            job_root, checkpoint, staged)
        del testcases
        previous_report = load_document(job_root / checkpoint[
            "review_report_path"])
        prior_request = load_document(job_root / checkpoint[
            "review_request_path"])
        routing_summary = {
            "owner_review_submission_path":
                prior_request["owner_routing_decision"]["path"],
            "owner_review_submission_fingerprint":
                prior_request["owner_routing_decision"]["submission"][
                    "submission_fingerprint"],
            "spec_issues": "staging/mappings/scenario_spec_issues.r000.json",
        }
        model = ProjectReadModel.from_checkpoint(job_root, dict(checkpoint))
        reviewer_binding = {
            "runtime_role": "REVIEWER", "model_class": "PROFILED",
            **binding_lineage(project_input, "review", "final"),
        }
        prompt = build_prompt_contract(
            role="REVIEWER", job_id=project_input["job_id"],
            input_fingerprint=project_input["input_fingerprint"],
            spec_fingerprint=model.spec_fingerprint,
            policy_fingerprint=model.policy_fingerprint,
            artifact_roots=model.artifact_roots,
            unit_roots={name: value["root_fingerprint"]
                        for name, value in model.indexes.items()},
            dependencies=[], provider_id=reviewer_binding["provider_id"],
            model_id=reviewer_binding["model_id"],
            role_fingerprint=canonical_hash(reviewer_binding),
            tool_allow_list=[], final_output="project_testcase_review_candidate",
            formal_scope=prior_request["coverage_scope"])
        _persist_json(
            job_root, "staging/prompts/reviewer.{}.json".format(
                prompt["prompt_fingerprint"][:16]), prompt)
        provider = self._provider(job_root, project_input)
        probe_workflow = ProjectJobWorkflow(
            self.workspace_root, self.result_root,
            role_providers={"review.final": provider})
        reviewer_probe = probe_workflow._probe_provider(job_root, "review.final")
        review_workflow = StagedProjectWorkflow(probe_workflow)
        spec_evidence, sources, spec_fp = review_workflow._spec(
            dict(project_input))
        lineage = build_reviewer_repair_lineage(
            job_root, project_input["job_id"], ProjectJobError)
        report, validation, paths = review_workflow._review(
            dict(project_input), job_root, spec_evidence, sources, spec_fp,
            map1, map2, shards, candidate, reviewer_probe, 2,
            review_workflow._existing_usage(job_root), routing_summary,
            previous_report, lineage)
        human_paths = {
            "map1": checkpoint["scenario_ac_map_path"],
            "map2": checkpoint["ac_testcase_map_path"],
            "candidate": checkpoint["candidate_metadata_path"],
            **paths,
        }
        human = review_workflow._human_review_gate(
            job_root, dict(project_input), map1, map2, candidate, report,
            validation, human_paths,
            review_workflow._existing_usage(job_root), {
                **routing_summary,
                "has_spec_issues": bool(routing_summary.get("spec_issues")),
            })
        dispatch = load_document(job_root / checkpoint["dispatch_path"])
        records = RepairRecordStore(
            job_root, job_id=project_input["job_id"],
            input_fingerprint=project_input["input_fingerprint"],
            spec_fingerprint=dispatch["spec_fingerprint"],
            policy_fingerprint=dispatch["policy_fingerprint"])
        episode_refs = [{"path": path, "fingerprint": value[
            "record_fingerprint"]} for path, value in records.records()
                        if value["record_type"] == "REPAIR_EPISODE"]
        records.append("REVIEW_LINK", {
            "initial_report_path": checkpoint["review_report_path"],
            "repair_episodes": episode_refs,
            "final_request_path": paths["review_request"],
            "final_report_path": paths["review_report"],
        }, producer_role="REVIEWER")
        _persist_json(job_root, FINAL_PATH, human)
        records.rebuild_indexes()
        return human

    def _final_review_recovered(
            self, job_root: Path, project_input: Mapping[str, Any],
            checkpoint: Mapping[str, Any],
            staged: StagedProjectWorkflow) -> dict[str, Any]:
        """Append a new round-2 certification instance for legacy R3 state.

        Review contracts intentionally remain at review_round=2.  A
        deterministic filename suffix and a later existing review-index
        revision keep the old premature certification byte-identical.
        """
        map1, map2, testcases, shards, candidate = self._load_source_bundle(
            job_root, checkpoint, staged)
        del testcases
        previous_report = load_document(
            job_root / checkpoint["review_report_path"])
        prior_request = load_document(
            job_root / checkpoint["review_request_path"])
        routing_summary = {
            "owner_review_submission_path":
                prior_request["owner_routing_decision"]["path"],
            "owner_review_submission_fingerprint":
                prior_request["owner_routing_decision"]["submission"][
                    "submission_fingerprint"],
            "spec_issues": "staging/mappings/scenario_spec_issues.r000.json",
        }
        model = ProjectReadModel.from_checkpoint(job_root, dict(checkpoint))
        reviewer_binding = {
            "runtime_role": "REVIEWER", "model_class": "PROFILED",
            **binding_lineage(project_input, "review", "final"),
        }
        prompt = build_prompt_contract(
            role="REVIEWER", job_id=project_input["job_id"],
            input_fingerprint=project_input["input_fingerprint"],
            spec_fingerprint=model.spec_fingerprint,
            policy_fingerprint=model.policy_fingerprint,
            artifact_roots=model.artifact_roots,
            unit_roots={name: value["root_fingerprint"]
                        for name, value in model.indexes.items()},
            dependencies=[], provider_id=reviewer_binding["provider_id"],
            model_id=reviewer_binding["model_id"],
            role_fingerprint=canonical_hash(reviewer_binding),
            tool_allow_list=[], final_output="project_testcase_review_candidate",
            formal_scope=prior_request["coverage_scope"])
        _persist_json(
            job_root, "staging/prompts/reviewer.{}.json".format(
                prompt["prompt_fingerprint"][:16]), prompt)
        provider = self._provider(job_root, project_input)
        probe_workflow = ProjectJobWorkflow(
            self.workspace_root, self.result_root,
            role_providers={"review.final": provider})
        reviewer_probe = probe_workflow._probe_provider(job_root, "review.final")
        review_workflow = StagedProjectWorkflow(probe_workflow)
        spec_evidence, sources, spec_fp = review_workflow._spec(
            dict(project_input))
        lineage = build_reviewer_repair_lineage(
            job_root, project_input["job_id"], ProjectJobError)
        repair_loop = sum(
            item.get("record_type") == "ORCHESTRATOR_PLAN"
            for item in lineage)
        if repair_loop != 1:
            raise ProjectJobError(
                "STALE_EVIDENCE",
                "recovered Final Review requires the first formal repair "
                "loop")
        token = canonical_hash(checkpoint["artifact_roots"])[:16]
        suffix = ".loop{:03d}".format(repair_loop)
        legacy_suffix = ".repair.{}".format(token)
        legacy_request_path = (
            "staging/reviews/review_request.r002{}.json".format(
                legacy_suffix))
        legacy_provider_tag = "review.r002{}".format(legacy_suffix)
        legacy_response_path = (
            job_root /
            "audit/pj002_provider_response.{}.json".format(
                legacy_provider_tag))
        legacy_transcript_path = (
            job_root / "transcripts/reviewer" /
            "REVIEWSESSION.R002.07C98F69D1A11EAC.ATTEMPT000" /
            "manifest.json")
        legacy_parts = (
            job_root / legacy_request_path,
            legacy_response_path,
            legacy_transcript_path,
        )
        legacy_present = [path.exists() for path in legacy_parts]
        if any(legacy_present) and not all(legacy_present):
            raise ProjectJobError(
                "PARTIAL_ARTIFACT",
                "legacy Reviewer attempt-zero evidence is incomplete")
        recover_attempt_zero = all(legacy_present)
        if recover_attempt_zero:
            legacy_request = load_document(legacy_parts[0])
            legacy_terminal = load_document(legacy_transcript_path).get(
                "terminal")
            if (
                legacy_request.get("request_fingerprint") !=
                    "07c98f69d1a11eacb0ba76cca080f33656ae53678c72b9952c02858e8691dda3" or
                legacy_terminal != {
                    "status": "FAILED",
                    "code": "REVIEW_COVERAGE_MISMATCH",
                    "result_sequence": None,
                }
            ):
                raise ProjectJobError(
                    "STALE_EVIDENCE",
                    "legacy Reviewer attempt-zero evidence is not the exact "
                    "recoverable R3 failure")
        review_revisions = [
            int(match.group(1))
            for path in job_root.glob(
                "staging/units/review/index.current.r*.json")
            if (match := re.fullmatch(
                r"index\.current\.r([0-9]{3})\.json", path.name))]
        storage_revision = repair_loop + 1
        if (not {0, 1}.issubset(set(review_revisions)) or
                any(item > storage_revision for item in review_revisions)):
            raise ProjectJobError(
                "STALE_EVIDENCE",
                "recovered Final Review unit lineage is not the exact "
                "loop001 state")
        report, validation, paths = review_workflow._review(
            dict(project_input), job_root, spec_evidence, sources, spec_fp,
            map1, map2, shards, candidate, reviewer_probe, 2,
            review_workflow._existing_usage(job_root), routing_summary,
            previous_report, lineage, artifact_suffix=suffix,
            review_storage_revision=storage_revision,
            attempt_zero_request_path=(
                legacy_request_path if recover_attempt_zero else None),
            attempt_zero_provider_tag=(
                legacy_provider_tag if recover_attempt_zero else None))
        final_paths = {
            "map1": checkpoint["scenario_ac_map_path"],
            "map2": checkpoint["ac_testcase_map_path"],
            "candidate": checkpoint["candidate_metadata_path"],
            **paths,
        }
        repairable_errors = [
            item for item in report["findings"]
            if item["severity"] == "ERROR" and
            item["suspected_origin_stage"] != "SPEC"]
        if repairable_errors:
            return review_workflow._awaiting_repair_plan(
                job_root, dict(project_input), map1, map2, candidate,
                report, validation, final_paths,
                artifact_suffix=suffix)
        human = review_workflow._human_review_gate(
            job_root, dict(project_input), map1, map2, candidate, report,
            validation, final_paths,
            review_workflow._existing_usage(job_root), {
                **routing_summary,
                "has_spec_issues": bool(routing_summary.get("spec_issues")),
            }, artifact_suffix=suffix, allow_recertification=True,
            review_storage_revision=storage_revision)
        dispatch = load_document(job_root / checkpoint["dispatch_path"])
        records = RepairRecordStore(
            job_root, job_id=project_input["job_id"],
            input_fingerprint=project_input["input_fingerprint"],
            spec_fingerprint=dispatch["spec_fingerprint"],
            policy_fingerprint=dispatch["policy_fingerprint"])
        episode_refs = [{
            "path": path, "fingerprint": value["record_fingerprint"],
        } for path, value in records.records()
            if value["record_type"] == "REPAIR_EPISODE"]
        records.append("REVIEW_LINK", {
            "initial_report_path": checkpoint["review_report_path"],
            "repair_episodes": episode_refs,
            "final_request_path": paths["review_request"],
            "final_report_path": paths["review_report"],
        }, producer_role="REVIEWER")
        _persist_json(job_root, RECOVERED_FINAL_PATH, human)
        records.rebuild_indexes()
        return human

    @staticmethod
    def _paths_for_roots(
            job_root: Path, roots: Mapping[str, Any],
            fallback: Mapping[str, str]) -> dict[str, str]:
        specs = {
            "scenario_ac_map": (
                ["staging/mappings/scenario_ac_map.checked.r*.json",
                 "staging/mappings/scenario_ac_map.r*.json"],
                "artifact_fingerprint"),
            "ac_testcase_map": (
                ["staging/mappings/ac_testcase_map.r*.json"],
                "artifact_fingerprint"),
            "testcase": (
                ["staging/generated/portable_sv/testcase.r*.json"],
                "candidate_fingerprint"),
        }
        result: dict[str, str] = {}
        for key, (patterns, field) in specs.items():
            matches = []
            for pattern in patterns:
                for path in sorted(job_root.glob(pattern)):
                    if path.is_file() and not path.is_symlink():
                        value = load_document(path)
                        if value.get(field) == roots[key]:
                            matches.append(path.relative_to(job_root).as_posix())
            matches = sorted(set(matches))
            if fallback.get(key) in matches:
                result[key] = fallback[key]
            elif len(matches) == 1:
                result[key] = matches[0]
            elif matches:
                # Checked Scenario/AC maps are the current authority form.
                checked = [item for item in matches if ".checked." in item]
                if key == "scenario_ac_map" and len(checked) == 1:
                    result[key] = checked[0]
                else:
                    raise ProjectJobError(
                        "STALE_EVIDENCE", "committed root path is ambiguous")
            else:
                raise ProjectJobError(
                    "STALE_EVIDENCE", "committed root artifact is missing")
        return result

    @staticmethod
    def _authority_stub(checkpoint: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "new_artifact_paths": {
                "scenario_ac_map": checkpoint["scenario_ac_map_path"],
                "ac_testcase_map": checkpoint["ac_testcase_map_path"],
                "testcase": checkpoint["candidate_metadata_path"],
            },
            "new_artifact_roots": copy.deepcopy(checkpoint["artifact_roots"]),
            "new_index_paths": {
                key: checkpoint["artifact_unit_index_paths"][key]
                for key in ("stage1", "stage2", "stage3")
            },
        }

    @staticmethod
    def _final_binds_authority(
            value: Mapping[str, Any], roots: Mapping[str, Any]) -> bool:
        bundle = value.get("bundle_fingerprints", {})
        return all(bundle.get(key) == roots.get(key) for key in (
            "scenario_ac_map", "ac_testcase_map", "testcase"))

    def advance(self, job_id: str) -> dict[str, Any]:
        job_root = self._job_root(job_id)
        project_input = self._manifest(job_root)
        workflow = ProjectJobWorkflow(self.workspace_root, self.result_root)
        staged = StagedProjectWorkflow(workflow)
        initial_checkpoint, initial_dispatch, initial_replacement, _, \
            initial_response = self._entry(job_root, project_input)
        states = staged._regeneration_states(job_root, dict(project_input))
        if not states or not states[-1]["regeneration_used"]:
            staged._append_regeneration_state(
                job_root, dict(project_input), "REGENERATION_STARTED",
                initial_checkpoint["source_report_fingerprint"])
        record_store = RepairRecordStore(
            job_root, job_id=project_input["job_id"],
            input_fingerprint=project_input["input_fingerprint"],
            spec_fingerprint=initial_checkpoint.get(
                "spec_fingerprint", load_document(
                    job_root / initial_checkpoint["dispatch_path"])[
                        "spec_fingerprint"]),
            policy_fingerprint=load_document(
                job_root / initial_checkpoint["dispatch_path"])[
                    "policy_fingerprint"])
        groups = self._planned_groups(record_store)
        records = record_store.records()
        committed = [record for _, record in records
                     if record["record_type"] == "GROUP_COMMIT" and
                     record["payload"].get("status") == "COMMITTED"]
        initial_paths = {
            "scenario_ac_map": initial_checkpoint["scenario_ac_map_path"],
            "ac_testcase_map": initial_checkpoint["ac_testcase_map_path"],
            "testcase": initial_checkpoint["candidate_metadata_path"],
        }
        if committed:
            authority_paths = self._paths_for_roots(
                job_root, committed[-1]["payload"]["current_roots"],
                initial_paths)
        else:
            authority_paths = initial_paths
        authority = self._authority_checkpoint(
            job_root, initial_checkpoint, authority_paths)
        committed_ids = {
            record["payload"]["group_id"] for record in committed}

        stage2_groups = [item for item in groups if item["stage"] == "STAGE_2"]
        current_map2 = load_document(job_root / authority["ac_testcase_map_path"])
        initial_map2 = load_document(
            job_root / initial_checkpoint["ac_testcase_map_path"])
        stage2_current = int(current_map2["revision"]) > int(
            initial_map2["revision"])
        pending_stage2 = ([] if stage2_current else [
            item for item in stage2_groups
            if item["group_id"] not in committed_ids])
        fallback_entry = (
            initial_dispatch, initial_replacement, initial_response)
        if pending_stage2:
            entries = []
            dispatched = set(committed_ids)
            for group in pending_stage2:
                if (not entries and initial_dispatch["stage"] == "STAGE_2" and
                        (group["group_id"] == initial_dispatch["group_id"] or
                         group["targets"] == initial_dispatch["targets"])):
                    entry = fallback_entry
                else:
                    source_path, status = self._next_group_replacement(
                        job_root, project_input, initial_checkpoint,
                        self._authority_stub(authority), sorted(dispatched), groups)
                    if source_path is None:
                        result = {
                            "state": status, "job_id": project_input["job_id"],
                            "artifact_roots": authority["artifact_roots"],
                        }
                        if status == "PROVIDER_UNAVAILABLE":
                            result.update({
                                "state": "PAUSED_RETRYABLE",
                                "diagnostic": {
                                    "code": "PROVIDER_UNAVAILABLE",
                                    "message": (
                                        "Stage Provider attempt failed; rerun "
                                        "the same Job for a fresh attempt"),
                                },
                            })
                        return result
                    _, dispatch, replacement, _, response = self._entry(
                        job_root, project_input, source_path)
                    entry = (dispatch, replacement, response)
                if (entry[0]["group_id"] != group["group_id"] and
                        entry[0]["targets"] != group["targets"]):
                    raise ProjectJobError(
                        "STALE_EVIDENCE", "Stage 2 dispatch order is stale")
                entries.append(entry)
                dispatched.add(group["group_id"])
            prepared2 = self._prepare_stage2(
                job_root, project_input, authority, entries, staged)
            for dispatch, replacement, _ in entries:
                self._record_stage_success(
                    job_root, project_input, dispatch, replacement, prepared2)
            authority = self._authority_checkpoint(
                job_root, initial_checkpoint, prepared2["new_artifact_paths"])
            committed_ids.update(item[0]["group_id"] for item in entries)
            if self.checkpoint_hook is not None:
                self.checkpoint_hook("STAGE_2_COMMITTED", authority)

        map2 = load_document(job_root / authority["ac_testcase_map_path"])
        candidate = load_document(job_root / authority["candidate_metadata_path"])
        stage3_current = (
            candidate.get("ac_testcase_map_fingerprint") ==
                map2.get("artifact_fingerprint") and
            int(candidate.get("revision", 0)) > int(load_document(
                job_root / initial_checkpoint["candidate_metadata_path"])[
                    "revision"]))
        stage3_groups = [item for item in groups if item["stage"] == "STAGE_3"]
        if not stage3_current:
            entries3 = []
            dispatched = set(committed_ids)
            for group in stage3_groups:
                if group["group_id"] in committed_ids:
                    continue
                if (not entries3 and initial_dispatch["stage"] == "STAGE_3" and
                        (group["group_id"] == initial_dispatch["group_id"] or
                         group["targets"] == initial_dispatch["targets"])):
                    entry = fallback_entry
                else:
                    source_path, status = self._next_group_replacement(
                        job_root, project_input, initial_checkpoint,
                        self._authority_stub(authority), sorted(dispatched), groups)
                    if source_path is None:
                        result = {
                            "state": status, "job_id": project_input["job_id"],
                            "artifact_roots": authority["artifact_roots"],
                        }
                        if status == "PROVIDER_UNAVAILABLE":
                            result.update({
                                "state": "PAUSED_RETRYABLE",
                                "diagnostic": {
                                    "code": "PROVIDER_UNAVAILABLE",
                                    "message": (
                                        "Stage Provider attempt failed; rerun "
                                        "the same Job for a fresh attempt"),
                                },
                            })
                        return result
                    _, dispatch, replacement, _, response = self._entry(
                        job_root, project_input, source_path)
                    entry = (dispatch, replacement, response)
                if (entry[0]["group_id"] != group["group_id"] and
                        entry[0]["targets"] != group["targets"]):
                    raise ProjectJobError(
                        "STALE_EVIDENCE", "Stage 3 dispatch order is stale")
                entries3.append(entry)
                dispatched.add(group["group_id"])
            prepared3 = self._prepare_stage3(
                job_root, project_input, authority, entries3,
                fallback_entry, staged)
            compile_entry = entries3[-1] if entries3 else fallback_entry
            compile_path, compile_value = self._compile(
                job_root, project_input, authority, compile_entry[1],
                prepared3["candidate"])
            if compile_value["status"] != "PASS":
                failure_value = {
                    **compile_value,
                    "base_artifact_roots": copy.deepcopy(
                        authority["artifact_roots"]),
                }
                self._record_stage_failure(
                    job_root, project_input, compile_entry[0],
                    compile_entry[1], failure_value)
                return {
                    "schema_version": "1.0",
                    "workflow_version": WORKFLOW_VERSION,
                    "state": "PAUSED_COMPILE_REPAIR_REQUIRED",
                    "job_id": project_input["job_id"],
                    "input_fingerprint": project_input["input_fingerprint"],
                    "scenario_ac_map_path": authority[
                        "scenario_ac_map_path"],
                    "ac_testcase_map_path": authority[
                        "ac_testcase_map_path"],
                    "candidate_metadata_path": authority[
                        "candidate_metadata_path"],
                    "pending_candidate_metadata_path": prepared3[
                        "new_artifact_paths"]["testcase"],
                    "compile_validation_path": compile_path,
                    "artifact_roots": copy.deepcopy(authority["artifact_roots"]),
                    "checkpoint_fingerprint": compile_value[
                        "validation_fingerprint"],
                }
            stage3_commits = entries3 or [fallback_entry]
            for dispatch, replacement, _ in stage3_commits:
                self._record_stage_success(
                    job_root, project_input, dispatch, replacement, prepared3)
            authority = self._authority_checkpoint(
                job_root, initial_checkpoint, prepared3["new_artifact_paths"])
            if self.checkpoint_hook is not None:
                self.checkpoint_hook("STAGE_3_COMMITTED", authority)

        for relative in (RECOVERED_FINAL_PATH, FINAL_PATH):
            path = job_root / relative
            if path.exists():
                value = load_document(path)
                if self._final_binds_authority(
                        value, authority["artifact_roots"]):
                    return value
        try:
            if (job_root / FINAL_PATH).exists():
                return self._final_review_recovered(
                    job_root, project_input, authority, staged)
            return self._final_review_current_authority(
                job_root, project_input, authority, staged)
        except ProjectJobError as caught:
            if caught.code != "ATTEMPT_PAUSED":
                raise
            return {
                "schema_version": "1.0",
                "workflow_version": WORKFLOW_VERSION,
                "state": "PAUSED_RETRYABLE",
                "job_id": project_input["job_id"],
                "input_fingerprint": project_input["input_fingerprint"],
                "scenario_ac_map_path": authority[
                    "scenario_ac_map_path"],
                "ac_testcase_map_path": authority[
                    "ac_testcase_map_path"],
                "candidate_metadata_path": authority[
                    "candidate_metadata_path"],
                "artifact_roots": copy.deepcopy(authority["artifact_roots"]),
                "diagnostic": {
                    "code": "ATTEMPT_PAUSED",
                    "message": (
                        "Reviewer candidate attempt failed; rerun the same "
                        "Job for a fresh correction attempt"),
                },
            }


def checkpoint_map1_revision(checkpoint: Mapping[str, Any], job_root: Path) -> int:
    return int(load_document(job_root / checkpoint["scenario_ac_map_path"])[
        "revision"])


def checkpoint_map2_revision(checkpoint: Mapping[str, Any], job_root: Path) -> int:
    return int(load_document(job_root / checkpoint["ac_testcase_map_path"])[
        "revision"])


def checkpoint_candidate_revision(
        checkpoint: Mapping[str, Any], job_root: Path) -> int:
    return int(load_document(job_root / checkpoint["candidate_metadata_path"])[
        "revision"])


__all__ = [
    "COMMIT_PATH", "FINAL_PATH", "RECOVERED_FINAL_PATH",
    "ProjectCommitRuntime",
    "validate_commit_manifest", "validate_compile_validation",
]
