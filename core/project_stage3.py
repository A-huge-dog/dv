"""Standalone, test-only Stage 3 generation from an immutable source Job."""
from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path
from typing import Any

import yaml

from contracts.validator import accepted, load_document, validate
from core.atomic_artifact import is_pending_artifact
from core.project_job import ProjectJobError, ProjectJobWorkflow
from core.project_staged import (
    StagedProjectWorkflow, artifact_fingerprint,
    validate_ac_testcase_map, validate_scenario_ac_map,
    validate_testcase_candidate,
)
from scripts.dvlib import canonical_hash


STAGE3_SUBMISSION_PATH = "input_baseline/stage3_job_submission.yaml"
TEST_MANIFEST_PATH = "input_baseline/stage3_job_manifest.json"
TEST_RESULT_PATH = "reports/stage3_test_result.json"
TEST_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
SOURCE_JOB_ID = re.compile(r"JOB\.PROJECT\.[A-Z0-9_.-]+")


def validate_stage3_submission(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not accepted(validate(
            "project_stage3_submission", value)):
        raise ProjectJobError(
            "INVALID_SCHEMA", "standalone Stage 3 YAML is invalid")
    return copy.deepcopy(value)


def load_stage3_provider_config(
        workspace_root: Path, relative: str) -> dict[str, Any]:
    path = Path(relative)
    if (path.is_absolute() or ".." in path.parts or
            any(part.startswith(".") for part in path.parts)):
        raise ProjectJobError(
            "TOOL_PERMISSION_DENIED",
            "Stage 3 provider config path is unsafe")
    root = Path(workspace_root).resolve()
    unresolved = root / path
    cursor = root
    for part in path.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ProjectJobError(
                "TOOL_PERMISSION_DENIED",
                "Stage 3 provider config contains a symlink")
    resolved = unresolved.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as caught:
        raise ProjectJobError(
            "TOOL_PERMISSION_DENIED",
            "Stage 3 provider config escapes the workspace") from caught
    if (not resolved.is_file() or unresolved.is_symlink() or
            not unresolved.is_file()):
        raise ProjectJobError(
            "INVALID_PROVIDER_CONFIG",
            "Stage 3 provider config is missing or unsafe")
    config = load_document(resolved)
    if not accepted(validate("provider_config", config)):
        raise ProjectJobError(
            "INVALID_PROVIDER_CONFIG",
            "Stage 3 provider config contract is invalid")
    return config


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class StandaloneStage3Workflow:
    """Run only Stage 3 without mutating the source Project Job."""

    def __init__(self, workflow: ProjectJobWorkflow):
        self.workflow = workflow
        self.root = workflow.workspace_root
        self.result_root = workflow.result_root
        self.staged = StagedProjectWorkflow(workflow)

    def _job_root(self, test_job_id: str) -> Path:
        if (not isinstance(test_job_id, str) or
                not TEST_JOB_ID.fullmatch(test_job_id) or
                test_job_id.startswith(".") or ".." in test_job_id):
            raise ProjectJobError(
                "INVALID_INPUT", "Stage 3 test Job ID is unsafe")
        root = self.result_root / "jobs" / test_job_id
        jobs_root = (self.result_root / "jobs").resolve()
        if root.is_symlink() or root.parent.is_symlink() or \
                root.parent.resolve() != jobs_root:
            raise ProjectJobError(
                "TOOL_PERMISSION_DENIED",
                "Stage 3 test Job escapes the approved result root")
        return root

    @staticmethod
    def _regular(path: Path, code: str, message: str) -> Path:
        if not path.is_file() or path.is_symlink():
            raise ProjectJobError(code, message)
        return path

    def _load_source(
            self, source: dict[str, str]
            ) -> tuple[
                dict[str, Any], Path, list[dict[str, Any]],
                dict[str, str], str, dict[str, Any], dict[str, Any],
                list[dict[str, Any]], list[dict[str, Any]],
                dict[str, str]]:
        source_job_id = source["project_job_id"]
        if (not isinstance(source_job_id, str) or
                not SOURCE_JOB_ID.fullmatch(source_job_id)):
            raise ProjectJobError(
                "INVALID_INPUT", "source Project Job ID is invalid")
        source_root = self.result_root / "jobs" / source_job_id
        if source_root.is_symlink() or not source_root.is_dir() or \
                source_root.parent.resolve() != \
                    (self.result_root / "jobs").resolve():
            raise ProjectJobError(
                "BLOCKED_INPUT", "source Project Job is missing or unsafe")
        submission_path = self._regular(
            source_root / "input_baseline/project_job_submission.yaml",
            "BLOCKED_INPUT", "source Project submission is missing")
        submission_bytes = submission_path.read_bytes()
        try:
            submission = yaml.safe_load(submission_bytes.decode("utf-8"))
        except (UnicodeError, yaml.YAMLError) as caught:
            raise ProjectJobError(
                "INVALID_SCHEMA", "source Project submission is invalid") \
                from caught
        value = self.workflow.bootstrap(
            submission, submission_bytes, create=False)
        if value["job_id"] != source_job_id:
            raise ProjectJobError(
                "CROSS_JOB_EVIDENCE",
                "source manifest does not match the requested Project Job")
        spec_evidence, sources, spec_fp = self.staged._spec(value)

        def selected(relative: str, kind: str) -> Path:
            path = source_root / relative
            if (".." in Path(relative).parts or Path(relative).is_absolute() or
                    not path.is_file() or path.is_symlink() or
                    path.resolve().parent !=
                        (source_root / "staging/mappings").resolve()):
                raise ProjectJobError(
                    "TOOL_PERMISSION_DENIED",
                    "selected source {} mapping is unsafe".format(kind))
            return path

        map1_path = selected(source["scenario_ac_map"], "Stage 1")
        map2_path = selected(source["ac_testcase_map"], "Stage 2")
        map1 = load_document(map1_path)
        source_policy_fingerprint = map1.get("policy_fingerprint")
        if source_policy_fingerprint != self.staged.policy_fingerprint:
            raise ProjectJobError(
                "STALE_EVIDENCE",
                "source Stage 1 mapping policy is unsupported")
        validate_scenario_ac_map(
            map1, value, sources, spec_fp,
            source_policy_fingerprint, ProjectJobError)
        map2 = load_document(map2_path)
        if map2.get("policy_fingerprint") != source_policy_fingerprint:
            raise ProjectJobError(
                "STALE_EVIDENCE",
                "source Stage 1/2 mapping policies conflict")
        testcases, shards = self.staged._load_shards(source_root, map2)
        validate_ac_testcase_map(
            map2, map1, value, sources, spec_fp,
            source_policy_fingerprint, testcases, ProjectJobError)
        if any(item["status"] == "SPEC_AMBIGUITY" for item in testcases):
            raise ProjectJobError(
                "SPEC_AMBIGUITY",
                "source Stage 2 contains unresolved Spec ambiguity")
        source_paths = {
            "submission": submission_path.relative_to(
                source_root).as_posix(),
            "manifest": "input_baseline/project_input_manifest.json",
            "scenario_ac_map": source["scenario_ac_map"],
            "ac_testcase_map": source["ac_testcase_map"],
        }
        for index, shard in enumerate(map2.get("shards", [])):
            relative = shard.get("path", "")
            shard_path = source_root / relative
            if (not isinstance(relative, str) or
                    not shard_path.is_file() or shard_path.is_symlink() or
                    ".." in Path(relative).parts or
                    not shard_path.resolve().is_relative_to(
                        source_root.resolve())):
                raise ProjectJobError(
                    "STALE_EVIDENCE", "source Stage 2 shard is unsafe")
            source_paths["ac_testcase_shard_{:03d}".format(index)] = relative
        self._regular(
            source_root / source_paths["manifest"],
            "STALE_EVIDENCE", "source Project manifest is missing")
        return (
            value, source_root, spec_evidence, sources, spec_fp,
            map1, map2, testcases, shards, source_paths)

    def _manifest(
            self, submission: dict[str, Any], submission_bytes: bytes,
            generator: dict[str, str], source_job_id: str,
            source_root: Path, value: dict[str, Any], spec_fp: str,
            map1: dict[str, Any], map2: dict[str, Any],
            source_paths: dict[str, str]) -> dict[str, Any]:
        source_artifacts = {
            name: {
                "path": path,
                "fingerprint": _sha256(source_root / path),
            }
            for name, path in sorted(source_paths.items())
        }
        manifest = {
            "schema_version": "2.0",
            "artifact_kind": "STAGE3_JOB_MANIFEST",
            "job_id": submission["job_id"],
            "submission": {
                "baseline_path": STAGE3_SUBMISSION_PATH,
                "byte_fingerprint": hashlib.sha256(
                    submission_bytes).hexdigest(),
                "document_fingerprint": canonical_hash(submission),
            },
            "source_project_job_id": source_job_id,
            "source_input_fingerprint": value["input_fingerprint"],
            "source_spec_fingerprint": spec_fp,
            "source_scenario_ac_map_fingerprint":
                map1["artifact_fingerprint"],
            "source_ac_testcase_map_fingerprint":
                map2["artifact_fingerprint"],
            "source_artifacts": source_artifacts,
            "generator": copy.deepcopy(generator),
            "input_authority": copy.deepcopy(
                submission["input_authority"]),
            "input_authority_fingerprint": canonical_hash(
                submission["input_authority"]),
            "stage3_policy_fingerprint": canonical_hash({
                "base_policy_fingerprint": self.staged.policy_fingerprint,
                "provider_attempts": 1,
                "authority": "SPEC_ONLY_SOURCE_JOB",
                "qualification": "TEST_ONLY_NO_PROMOTION",
            }),
            "provider_attempts": 1,
            "qualification_scope": "TEST_ONLY_NO_PROMOTION_OR_EDA",
            "manifest_fingerprint": "0" * 64,
        }
        manifest["manifest_fingerprint"] = artifact_fingerprint(
            manifest, "manifest_fingerprint")
        return manifest

    def _persist_or_validate_manifest(
            self, job_root: Path, manifest: dict[str, Any],
            submission_bytes: bytes) -> None:
        path = job_root / TEST_MANIFEST_PATH
        submission_path = job_root / STAGE3_SUBMISSION_PATH
        if path.exists():
            if (not path.is_file() or path.is_symlink() or
                    load_document(path) != manifest or
                    not submission_path.is_file() or
                    submission_path.is_symlink() or
                    submission_path.read_bytes() != submission_bytes):
                raise ProjectJobError(
                    "STALE_EVIDENCE",
                    "Stage 3 test Job conflicts with its immutable source")
            return
        if job_root.exists() and any(
                not is_pending_artifact(path) for path in job_root.rglob("*")):
            raise ProjectJobError(
                "PARTIAL_ARTIFACT",
                "Stage 3 test Job has partial unbound artifacts")
        self.staged._immutable_text(
            submission_path, submission_bytes.decode("utf-8"))
        self.staged._immutable_json(path, manifest)

    def _probe(
            self, job_root: Path, source_root: Path,
            expected: dict[str, Any]) -> None:
        path = job_root / "audit/provider_probe.json"
        provider = self.workflow.provider
        if provider is None:
            raise ProjectJobError(
                "BLOCKED_TOOL", "Generator provider is unavailable")
        if path.exists():
            probe = load_document(self._regular(
                path, "STALE_EVIDENCE",
                "Stage 3 provider probe evidence is unsafe"))
            if (not accepted(validate("provider_probe", probe)) or
                    probe.get("provider_id") != expected["provider_id"] or
                    probe.get("model_id") != expected["model_id"] or
                    probe.get("status") != "PASS" or
                    not probe.get("tool_call_capable")):
                raise ProjectJobError(
                    "STALE_EVIDENCE",
                    "Stage 3 provider probe identity is stale")
            restore = getattr(provider, "restore_probe", None)
            if callable(restore):
                try:
                    restore(probe)
                except Exception as caught:
                    raise ProjectJobError(
                        "STALE_EVIDENCE",
                        "persisted Stage 3 provider probe cannot activate "
                        "the configured provider") from caught
            return
        source_probe_path = source_root / "audit/provider_probe.json"
        if source_probe_path.is_file() and not source_probe_path.is_symlink():
            source_probe = load_document(source_probe_path)
            if (accepted(validate("provider_probe", source_probe)) and
                    source_probe.get("provider_id") ==
                        expected["provider_id"] and
                    source_probe.get("model_id") == expected["model_id"] and
                    source_probe.get("status") == "PASS" and
                    source_probe.get("tool_call_capable")):
                restore = getattr(provider, "restore_probe", None)
                if callable(restore):
                    try:
                        restore(source_probe)
                    except Exception as caught:
                        raise ProjectJobError(
                            "STALE_EVIDENCE",
                            "source provider probe cannot activate the "
                            "configured provider") from caught
                self.staged._immutable_json(path, source_probe)
                return
        try:
            probe = provider.probe()
        except Exception as caught:
            raise ProjectJobError(
                "BLOCKED_TOOL", "Generator provider probe failed safely") \
                from caught
        if (not accepted(validate("provider_probe", probe)) or
                probe.get("provider_id") != expected["provider_id"] or
                probe.get("model_id") != expected["model_id"] or
                probe.get("status") != "PASS" or
                not probe.get("tool_call_capable")):
            raise ProjectJobError(
                "BLOCKED_TOOL", "Generator provider probe failed")
        self.staged._immutable_json(path, probe)

    def _load_result(
            self, job_root: Path, manifest: dict[str, Any],
            value: dict[str, Any], spec_fp: str,
            map1: dict[str, Any], map2: dict[str, Any],
            testcases: list[dict[str, Any]]) -> dict[str, Any] | None:
        path = job_root / TEST_RESULT_PATH
        if not path.exists():
            return None
        result = load_document(self._regular(
            path, "STALE_EVIDENCE", "Stage 3 test result is unsafe"))
        if (result.get("result_fingerprint") != artifact_fingerprint(
                result, "result_fingerprint") or
                result.get("manifest_fingerprint") !=
                    manifest["manifest_fingerprint"] or
                result.get("job_id") != manifest["job_id"] or
                result.get("source_project_job_id") != value["job_id"]):
            raise ProjectJobError(
                "STALE_EVIDENCE", "Stage 3 test result lineage is stale")
        candidate_path = job_root / result.get("candidate_path", "")
        candidate = load_document(self._regular(
            candidate_path, "PARTIAL_ARTIFACT",
            "Stage 3 test candidate metadata is missing"))
        content_path = job_root / candidate.get("output_path", "")
        if (not content_path.is_file() or content_path.is_symlink() or
                content_path.read_text(encoding="utf-8") !=
                    candidate.get("content")):
            raise ProjectJobError(
                "PARTIAL_ARTIFACT",
                "Stage 3 test candidate content is missing or stale")
        validate_testcase_candidate(
            candidate, map1, map2, testcases, value, spec_fp,
            self.staged.policy_fingerprint, ProjectJobError)
        if candidate["candidate_fingerprint"] != \
                result.get("candidate_fingerprint"):
            raise ProjectJobError(
                "STALE_EVIDENCE", "Stage 3 test candidate lineage is stale")
        store = self.staged._incremental_store(job_root)
        stage1_index, _ = store.load(
            "STAGE1", "CURRENT", map1["revision"], manifest["job_id"])
        stage2_index, _ = store.load(
            "STAGE2", "CURRENT", map2["revision"], manifest["job_id"])
        stage3_index, _, assembly = store.load_stage3(
            candidate["revision"], manifest["job_id"])
        if result.get("artifact_unit_roots") != {
                "stage1": stage1_index["root_fingerprint"],
                "stage2": stage2_index["root_fingerprint"],
                "stage3": stage3_index["root_fingerprint"],
                "assembly": assembly["assembly_fingerprint"]}:
            raise ProjectJobError(
                "STALE_EVIDENCE", "Stage 3 test unit roots are stale")
        return result

    def run(
            self, submission: dict[str, Any],
            submission_bytes: bytes | None = None) -> dict[str, Any]:
        submission = validate_stage3_submission(submission)
        if submission_bytes is None:
            submission_bytes = yaml.safe_dump(
                submission, sort_keys=False,
                allow_unicode=True).encode("utf-8")
        if (not isinstance(submission_bytes, bytes) or
                not submission_bytes or len(submission_bytes) > 1024 * 1024 or
                b"\x00" in submission_bytes):
            raise ProjectJobError(
                "INVALID_SCHEMA", "standalone Stage 3 YAML is oversized")
        try:
            parsed = yaml.safe_load(submission_bytes.decode("utf-8"))
        except (UnicodeError, yaml.YAMLError) as caught:
            raise ProjectJobError(
                "INVALID_SCHEMA", "standalone Stage 3 YAML is invalid") \
                from caught
        if parsed != submission:
            raise ProjectJobError(
                "STALE_EVIDENCE",
                "parsed Stage 3 YAML differs from exact input bytes")
        source_job_id = submission["source"]["project_job_id"]
        test_job_id = submission["job_id"]
        if source_job_id == test_job_id:
            raise ProjectJobError(
                "INVALID_INPUT", "test Job must differ from source Job")
        config = load_stage3_provider_config(
            self.root, submission["generator_config"])
        config_path = self.root / submission["generator_config"]
        generator = {
            "path": submission["generator_config"],
            "baseline_path": submission["generator_config"],
            "fingerprint": _sha256(config_path),
            "document_fingerprint": canonical_hash(config),
            "provider_id": config["provider_id"],
            "model_id": config["model_id"],
            "auth_env": config["auth_env"],
        }
        job_root = self._job_root(test_job_id)
        (
            value, source_root, spec_evidence, sources, spec_fp,
            map1, map2, testcases, shards, source_paths,
        ) = self._load_source(submission["source"])
        manifest = self._manifest(
            submission, submission_bytes, generator, source_job_id,
            source_root, value, spec_fp, map1, map2, source_paths)
        self._persist_or_validate_manifest(
            job_root, manifest, submission_bytes)
        execution_value = copy.deepcopy(value)
        execution_value["agent_profile"]["bindings"]["initial"][
            "stage3"] = copy.deepcopy(generator)
        prior = self._load_result(
            job_root, manifest, execution_value, spec_fp,
            map1, map2, testcases)
        if prior is not None:
            return prior
        self._probe(job_root, source_root, manifest["generator"])
        budget = self.staged._existing_usage(job_root)
        candidate = self.staged._generate_stage3(
            execution_value, job_root, spec_evidence, spec_fp,
            map1, map2, testcases, shards, 0, budget,
            execution_job_id=test_job_id)
        store = self.staged._incremental_store(job_root)
        stage1_index, _ = store.load(
            "STAGE1", "CURRENT", map1["revision"], test_job_id)
        stage2_index, _ = store.load(
            "STAGE2", "CURRENT", map2["revision"], test_job_id)
        stage3_index, _, assembly = store.load_stage3(
            candidate["revision"], test_job_id)
        result = {
            "schema_version": "2.0",
            "artifact_kind": "STAGE3_TEST_RESULT",
            "status": "PASS",
            "job_id": test_job_id,
            "source_project_job_id": source_job_id,
            "manifest_fingerprint": manifest["manifest_fingerprint"],
            "candidate_path":
                "staging/generated/portable_sv/testcase.r000.json",
            "candidate_fingerprint": candidate["candidate_fingerprint"],
            "content_path": candidate["output_path"],
            "content_fingerprint": candidate["content_fingerprint"],
            "artifact_unit_roots": {
                "stage1": stage1_index["root_fingerprint"],
                "stage2": stage2_index["root_fingerprint"],
                "stage3": stage3_index["root_fingerprint"],
                "assembly": assembly["assembly_fingerprint"],
            },
            "provider_calls": len(list(job_root.glob(
                "audit/pj002_provider_response.stage3.r000*.json"))),
            "provider_attempts": 1,
            "qualification_scope": "TEST_ONLY_NO_PROMOTION_OR_EDA",
            "result_fingerprint": "0" * 64,
        }
        result["result_fingerprint"] = artifact_fingerprint(
            result, "result_fingerprint")
        self.staged._immutable_json(job_root / TEST_RESULT_PATH, result)
        return result


__all__ = [
    "StandaloneStage3Workflow", "load_stage3_provider_config",
    "validate_stage3_submission",
]
