"""Create or replay one exact immutable Project Job baseline."""
from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from contracts.validator import accepted, load_document, validate
from domain.artifacts import (
    project_authority_fingerprint,
    project_input_fingerprint,
)
from scripts.dvlib import canonical_hash


BASELINE_SUBMISSION_PATH = "input_baseline/project_job_submission.yaml"
INTERNAL_MANIFEST_PATH = "input_baseline/project_input_manifest.json"
INTERNAL_MANIFEST_VERSION = "6.0"
EDA_APPROVAL_REF = "EDAAPPROVAL.PROJECT.PROFILE.XCELIUM.V1"


@dataclass(frozen=True)
class BootstrapInput:
    """Exact public artifact and requested persistence authority."""

    submission: dict[str, Any]
    submission_bytes: bytes | None = None
    create: bool = True


class BootstrapResult(dict[str, Any]):
    """Validated manifest mapping with typed replay/output metadata."""

    def __init__(
            self, manifest: dict[str, Any],
            output_references: tuple[str, ...], replayed: bool):
        super().__init__(manifest)
        self.manifest = copy.deepcopy(manifest)
        self.output_references = output_references
        self.replayed = replayed


@dataclass(frozen=True)
class BootstrapDependencies:
    """All boundaries needed by bootstrap; none discover global Job state."""

    workspace_root: Path
    result_root: Path
    error: type[Exception]
    role_paths: tuple[tuple[str, str], ...]
    validate_submission: Callable[[dict[str, Any]], dict[str, Any]]
    validate_input: Callable[[dict[str, Any], Path], dict[str, Any]]
    exact_submission: Callable[[dict[str, Any], bytes | None], bytes]
    job_root: Callable[[dict[str, Any]], Path]
    baseline_source_records: Callable[
        [dict[str, Any]],
        tuple[dict[str, list[dict[str, str]]], dict[str, bytes]],
    ]
    load_agent_profile: Callable[
        [Path, str, type[Exception]], tuple[dict[str, Any], dict[str, bytes]]
    ]
    eda_environment_fingerprint: Callable[[], str]
    testcase_identity: Callable[[dict[str, Any]], dict[str, str]]
    immutable_bytes: Callable[[Path, bytes], None]
    immutable_json: Callable[[Path, dict[str, Any]], None]


class BootstrapHandler:
    """Validate and persist exactly one immutable Project Job bootstrap."""

    def __init__(self, dependencies: BootstrapDependencies):
        self.dependencies = dependencies

    def handle(
            self, command: BootstrapInput | dict[str, Any],
            submission_bytes: bytes | None = None,
            create: bool = True) -> BootstrapResult:
        if not isinstance(command, BootstrapInput):
            command = BootstrapInput(command, submission_bytes, create)
        deps = self.dependencies
        submission = deps.validate_submission(command.submission)
        exact_submission = deps.exact_submission(
            submission, command.submission_bytes)
        job_root = deps.job_root(submission)
        if (
            job_root.is_symlink()
            or job_root.parent.is_symlink()
            or (
                job_root.parent.exists()
                and job_root.parent.resolve() != deps.result_root / "jobs"
            )
        ):
            raise deps.error(
                "TOOL_PERMISSION_DENIED",
                "Project Job directory escapes the approved result root",
            )
        manifest_path = job_root / INTERNAL_MANIFEST_PATH
        if manifest_path.exists():
            try:
                manifest = load_document(manifest_path)
            except Exception as caught:
                raise deps.error(
                    "STALE_EVIDENCE",
                    "persisted internal Project manifest is invalid",
                ) from caught
            value = deps.validate_input(manifest, deps.workspace_root)
            if (
                value["submission"]["byte_fingerprint"]
                != hashlib.sha256(exact_submission).hexdigest()
                or value["submission"]["document_fingerprint"]
                != canonical_hash(submission)
            ):
                raise deps.error(
                    "STALE_EVIDENCE",
                    "same Job ID is already bound to a different submission",
                )
            return BootstrapResult(
                value, (INTERNAL_MANIFEST_PATH,), True)
        if not command.create:
            raise deps.error(
                "BLOCKED_INPUT",
                "Project Job immutable baseline has not been established",
            )

        records, source_bytes = deps.baseline_source_records(submission)
        agent_profile, config_bytes = deps.load_agent_profile(
            deps.workspace_root, submission["agent_profile"], deps.error)
        expected_partial = {
            BASELINE_SUBMISSION_PATH,
            agent_profile["baseline_path"],
            *{
                agent_profile["bindings"][section][role]["baseline_path"]
                for section, role in deps.role_paths
            },
            *[
                str(PurePosixPath(item).relative_to(
                    "result/jobs/{}".format(submission["job_id"])))
                for item in source_bytes
            ],
        }
        if job_root.exists():
            if any(path.is_symlink() for path in job_root.rglob("*")):
                raise deps.error(
                    "PARTIAL_BOOTSTRAP",
                    "incomplete Project Job directory contains a symlink",
                )
            unexpected = [
                path
                for path in job_root.rglob("*")
                if path.is_file()
                and not path.name.startswith(".pending-artifact-")
                and path.relative_to(job_root).as_posix() not in expected_partial
            ]
            if unexpected:
                raise deps.error(
                    "PARTIAL_BOOTSTRAP",
                    "incomplete Project Job directory contains unexpected "
                    "artifacts and cannot be promoted to a baseline",
                )
        manifest = {
            "schema_version": INTERNAL_MANIFEST_VERSION,
            "manifest_kind": "PROJECT_INPUT_MANIFEST",
            "job_id": submission["job_id"],
            "project_id": submission["project_id"],
            "submission": {
                "baseline_path": BASELINE_SUBMISSION_PATH,
                "byte_fingerprint": hashlib.sha256(exact_submission).hexdigest(),
                "document_fingerprint": canonical_hash(submission),
            },
            "spec": {"sources": records["spec"]},
            "rtl": {
                "sources": records["rtl"],
                "top": submission["rtl"]["top"],
                "parameters": copy.deepcopy(submission["rtl"]["parameters"]),
                "authority": "INTERFACE_AND_BUILD_ONLY",
            },
            "uvm_testcase_context": {
                "files": records["uvm_testcase_context"],
                "generated_files": [
                    PurePosixPath(item).as_posix() for item in
                    submission["uvm_testcase_context"]["generated_files"]],
            },
            "agent_profile": agent_profile,
            "eda": {
                "profile_id": submission["eda"]["profile_id"],
                "executable_ref": "EDAEXEC.XCELIUM",
                "environment_fingerprint": deps.eda_environment_fingerprint(),
                "approval_ref": EDA_APPROVAL_REF,
                "timeout_seconds": submission["eda"]["timeout_seconds"],
            },
            "testcase": deps.testcase_identity(submission),
            "input_authority": {
                **copy.deepcopy(submission["input_authority"]),
                "authority_fingerprint": "0" * 64,
            },
            "input_fingerprint": "0" * 64,
        }
        manifest["input_authority"]["authority_fingerprint"] = (
            project_authority_fingerprint(manifest))
        manifest["input_fingerprint"] = project_input_fingerprint(manifest)
        if not accepted(validate("project_job_input", manifest)):
            raise deps.error(
                "INVALID_SCHEMA",
                "Framework generated an invalid internal Project manifest",
            )

        references = [BASELINE_SUBMISSION_PATH, agent_profile["baseline_path"]]
        deps.immutable_bytes(job_root / BASELINE_SUBMISSION_PATH, exact_submission)
        deps.immutable_bytes(
            job_root / agent_profile["baseline_path"],
            config_bytes[agent_profile["path"]],
        )
        written_provider_snapshots: set[str] = set()
        for section, role in deps.role_paths:
            reference = agent_profile["bindings"][section][role]
            if reference["baseline_path"] in written_provider_snapshots:
                continue
            written_provider_snapshots.add(reference["baseline_path"])
            references.append(reference["baseline_path"])
            deps.immutable_bytes(
                job_root / reference["baseline_path"],
                config_bytes[reference["path"]],
            )
        for relative, content in source_bytes.items():
            deps.immutable_bytes(deps.workspace_root / relative, content)
            references.append(
                str(PurePosixPath(relative).relative_to(
                    "result/jobs/{}".format(submission["job_id"]))))
        deps.immutable_json(manifest_path, manifest)
        references.append(INTERNAL_MANIFEST_PATH)
        return BootstrapResult(
            deps.validate_input(manifest, deps.workspace_root),
            tuple(references),
            False,
        )
