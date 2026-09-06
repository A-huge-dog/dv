"""Verilator-first real Project Job vertical workflow."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from application.bootstrap import (
    BootstrapDependencies,
    BootstrapHandler,
    INTERNAL_MANIFEST_PATH,
    INTERNAL_MANIFEST_VERSION,
)
from contracts.validator import accepted, load_document, validate
from domain.artifacts import (
    project_authority_fingerprint, project_input_fingerprint,
)
from infrastructure.persistence.atomic_artifact import (
    publish_immutable_bytes, publish_immutable_text,
)
from agents.profile import (
    ROLE_PATHS,
    load_project_agent_profile,
)
from domain.agent_binding import binding as agent_binding
from runtime.errors import ProjectJobError
from scripts.dvlib import canonical_hash


DENIED_NAMES = {
    ".env", "credentials", "credential", "secrets", "secret",
    "id_rsa", "id_ed25519",
}
PUBLIC_SUBMISSION_VERSION = "2.0"
SAFE_PROVIDER_SIGNATURE = re.compile(
    r"^type=[A-Za-z0-9_.-]{1,64} "
    r"status=(?:unknown|[1-5][0-9]{2}) "
    r"code=[A-Za-z0-9_.-]{1,64} "
    r"param=[A-Za-z0-9_.-]{1,64}$")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _bytes_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()












def _immutable_text(path: Path, content: str) -> None:
    publish_immutable_text(
        path, content,
        lambda message: ProjectJobError("STALE_EVIDENCE", message),
        "immutable Project Job artifact conflicts with existing bytes")


def _immutable_bytes(path: Path, content: bytes) -> None:
    publish_immutable_bytes(
        path, content,
        lambda message: ProjectJobError("STALE_EVIDENCE", message),
        "immutable Project Job artifact conflicts with existing bytes")


def _immutable_json(path: Path, value: dict[str, Any]) -> None:
    _immutable_text(path, json.dumps(
        value, sort_keys=True, indent=2, ensure_ascii=False) + "\n")


def _persist_provider_probe(
        job_root: Path, probe: dict[str, Any],
        runtime_role: str = "initial.stage1") -> Path:
    """Append changed probe outcomes without overwriting prior evidence."""
    stem = "provider_probe.{}".format(runtime_role.replace(".", "_"))
    path = job_root / "audit/{}.{}.json".format(
        stem, canonical_hash(probe)[:24])
    _immutable_json(path, probe)
    return path


def _validate_relative_path(relative: str, kind: str) -> PurePosixPath:
    pure = PurePosixPath(relative)
    denied = {
        part.casefold() for part in pure.parts
    } & DENIED_NAMES
    if (
        pure.is_absolute() or not pure.parts or ".." in pure.parts or
        any(part in {"", "."} or part.startswith(".") for part in pure.parts) or
        denied
    ):
        raise ProjectJobError(
            "TOOL_PERMISSION_DENIED",
            "{} path is outside the approved workspace policy".format(kind))
    return pure


def _resolved_workspace_file(
        workspace_root: Path, relative: str, kind: str) -> Path:
    _validate_relative_path(relative, kind)
    workspace = Path(workspace_root).resolve()
    lexical = workspace / relative
    try:
        resolved = lexical.resolve(strict=True)
    except FileNotFoundError as error:
        raise ProjectJobError(
            "BLOCKED_INPUT",
            "{} file is missing: {}".format(kind, relative)) from error
    try:
        resolved.relative_to(workspace)
    except ValueError as error:
        raise ProjectJobError(
            "TOOL_PERMISSION_DENIED",
            "{} path escapes the approved workspace".format(kind)) from error
    if not resolved.is_file():
        raise ProjectJobError(
            "BLOCKED_INPUT",
            "{} path is not a regular file: {}".format(kind, relative))
    return resolved


def _logical_uvm_path(entry: str) -> str:
    """Return a model-safe identity for a YAML-listed UVM source."""
    if not isinstance(entry, str) or not entry:
        raise ProjectJobError("INVALID_SCHEMA", "UVM testcase context path is invalid")
    candidate = Path(entry)
    logical = candidate.name if candidate.is_absolute() else entry
    pure = PurePosixPath(logical)
    if (not pure.parts or pure.is_absolute() or ".." in pure.parts or
            any(part in {"", "."} or part.startswith(".") for part in pure.parts) or
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*\.(?:sv|svh|v)", logical)):
        raise ProjectJobError(
            "INVALID_SCHEMA", "UVM testcase context path is invalid: {}".format(entry))
    return pure.as_posix()


def validate_project_submission(value: dict[str, Any]) -> dict[str, Any]:
    """Validate only user-authored fields; no fingerprints are accepted."""
    if not isinstance(value, dict):
        raise ProjectJobError(
            "INVALID_SCHEMA", "Project Job submission must be a YAML object")
    if value.get("schema_version") == "1.0":
        raise ProjectJobError(
            "LEGACY_PROJECT_INPUT_REJECTED",
            "legacy fingerprint-bearing Project input cannot be reused")
    forbidden = {
        "fingerprint", "content", "testcase", "pass_marker", "decisions",
        "scenarios", "requirements", "acceptance_criteria", "credential",
        "credentials", "api_key", "private_key", "owner_approval",
        "providers", "generator_config", "reviewer_config",
    }

    def walk(item: Any) -> set[str]:
        if isinstance(item, dict):
            found = {
                str(key).casefold() for key in item
                if str(key).casefold() in forbidden
            }
            for child in item.values():
                found.update(walk(child))
            return found
        if isinstance(item, list):
            found: set[str] = set()
            for child in item:
                found.update(walk(child))
            return found
        return set()

    violations = walk(value)
    if violations:
        code = (
            "BEHAVIOR_SIDECAR_FORBIDDEN"
            if violations & {
                "decisions", "scenarios", "requirements",
                "acceptance_criteria"
            }
            else "INVALID_SCHEMA")
        raise ProjectJobError(
            code,
            "Project submission contains forbidden derived or private fields")
    prospective_paths: list[tuple[str, str]] = []
    for section in ("spec", "rtl"):
        section_value = value.get(section)
        if isinstance(section_value, dict) and isinstance(
                section_value.get("sources"), list):
            prospective_paths.extend([
                (item, "Project source")
                for item in section_value["sources"]
                if isinstance(item, str)
            ])
    context = value.get("uvm_testcase_context")
    if isinstance(context, dict) and isinstance(context.get("files"), list):
        prospective_paths.extend([
            (item, "UVM testcase context")
            for item in context["files"] if isinstance(item, str) and
            not Path(item).is_absolute()
        ])
    if isinstance(value.get("agent_profile"), str):
        prospective_paths.append((value["agent_profile"], "Agent profile"))
    for relative, kind in prospective_paths:
        _validate_relative_path(relative, kind)
    if value.get("schema_version") != PUBLIC_SUBMISSION_VERSION or \
            not accepted(validate("project_job_submission", value)):
        raise ProjectJobError(
            "INVALID_SCHEMA", "Project Job submission contract is invalid")
    if set(value["input_authority"]["roles"]) != {
            "SPEC_OWNER", "DESIGN_OWNER"}:
        raise ProjectJobError(
            "MISSING_AUTHORITY",
            "Project submission requires Spec Owner and Design Owner roles")
    source_paths = (
        value["spec"]["sources"] + value["rtl"]["sources"])
    normalized = [item.casefold() for item in source_paths]
    if len(normalized) != len(set(normalized)):
        raise ProjectJobError(
            "CONFLICTING_SOURCE",
            "Project Spec/RTL source paths must be unique and non-overlapping")
    for relative in source_paths:
        _validate_relative_path(relative, "Project source")
    context_paths = value["uvm_testcase_context"]["files"]
    logical_paths = [_logical_uvm_path(item) for item in context_paths]
    if len(logical_paths) != len({path.casefold() for path in logical_paths}):
        raise ProjectJobError(
            "CONFLICTING_SOURCE",
            "UVM testcase context logical paths must be unique")
    generated_paths = [
        _logical_uvm_path(item)
        for item in value["uvm_testcase_context"]["generated_files"]]
    if len(generated_paths) != len({path.casefold() for path in generated_paths}):
        raise ProjectJobError(
            "CONFLICTING_SOURCE", "generated UVM slots must be unique")
    available = {path.casefold() for path in logical_paths}
    if any(path.casefold() not in available for path in generated_paths):
        raise ProjectJobError(
            "INVALID_SCHEMA",
            "every generated UVM slot must also be listed in context files")
    _validate_relative_path(value["agent_profile"], "Agent profile")
    parameters = value["rtl"]["parameters"]
    for name, parameter in parameters.items():
        if not isinstance(name, str) or not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ProjectJobError(
                "INVALID_SCHEMA", "RTL parameter name is invalid")
        if not isinstance(parameter, (int, bool)) or isinstance(
                parameter, float):
            raise ProjectJobError(
                "INVALID_SCHEMA",
                "RTL parameters accept only integer or boolean values")
    return copy.deepcopy(value)


def validate_project_input(
        value: dict[str, Any], workspace_root: Path) -> dict[str, Any]:
    """Validate an internal manifest and every immutable baseline byte."""
    if value.get("schema_version") in {
            "1.0", "2.0", "3.0", "4.0", "5.0"}:
        raise ProjectJobError(
            "LEGACY_PROJECT_INPUT_REJECTED",
            "legacy user-authored Project input cannot be reused")
    if value.get("schema_version") != INTERNAL_MANIFEST_VERSION or \
            not accepted(validate("project_job_input", value)):
        raise ProjectJobError(
            "INVALID_SCHEMA", "internal Project input manifest is invalid")
    if value["input_fingerprint"] != project_input_fingerprint(value):
        raise ProjectJobError(
            "STALE_EVIDENCE", "Project input manifest fingerprint is stale")
    authority = value["input_authority"]
    if authority["authority_fingerprint"] != \
            project_authority_fingerprint(value):
        raise ProjectJobError(
            "INVALID_APPROVAL_PROVENANCE",
            "input authority does not bind exact normalized Project input")
    if set(authority["roles"]) != {"SPEC_OWNER", "DESIGN_OWNER"}:
        raise ProjectJobError(
            "MISSING_AUTHORITY",
            "Project input requires Spec Owner and Design Owner authority")
    workspace = Path(workspace_root).resolve()
    job_root = workspace / "result/jobs" / value["job_id"]
    submission_path = job_root / value["submission"]["baseline_path"]
    try:
        if (
            job_root.is_symlink() or
            job_root.parent.resolve() != workspace / "result/jobs"
        ):
            raise ValueError("unsafe Job baseline path")
        submission_path.resolve(strict=True).relative_to(
            job_root.resolve())
    except (FileNotFoundError, ValueError) as error:
        raise ProjectJobError(
            "STALE_EVIDENCE",
            "Project Job baseline path is invalid or escaped") from error
    if (
        not submission_path.is_file() or submission_path.is_symlink() or
        _sha256(submission_path) != value["submission"]["byte_fingerprint"]
    ):
        raise ProjectJobError(
            "STALE_EVIDENCE", "baseline Project submission is stale")
    try:
        baseline_submission = yaml.safe_load(
            submission_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ProjectJobError(
            "STALE_EVIDENCE", "baseline Project submission is invalid") \
            from error
    validate_project_submission(baseline_submission)
    if canonical_hash(baseline_submission) != \
            value["submission"]["document_fingerprint"]:
        raise ProjectJobError(
            "STALE_EVIDENCE",
            "baseline submission document fingerprint is stale")
    derived_identity = canonical_hash({
        "job_id": baseline_submission["job_id"],
        "project_id": baseline_submission["project_id"],
        "artifact_kind": "PORTABLE_SV_TESTBENCH",
    })
    expected_testcase = {
        "top": "tb_project_{}".format(derived_identity[:16]),
        "pass_marker":
            "DV_PROJECT_{}_PASS".format(
                derived_identity[:16].upper()),
    }
    if (
        value["job_id"] != baseline_submission["job_id"] or
        value["project_id"] != baseline_submission["project_id"] or
        [item["path"] for item in value["spec"]["sources"]] !=
            baseline_submission["spec"]["sources"] or
        [item["path"] for item in value["rtl"]["sources"]] !=
            baseline_submission["rtl"]["sources"] or
        [item["logical_path"] for item in value["uvm_testcase_context"]["files"]] !=
            [_logical_uvm_path(item) for item in
             baseline_submission["uvm_testcase_context"]["files"]] or
        value["uvm_testcase_context"]["generated_files"] !=
            baseline_submission["uvm_testcase_context"]["generated_files"] or
        value["rtl"]["top"] != baseline_submission["rtl"]["top"] or
        value["rtl"]["parameters"] !=
            baseline_submission["rtl"]["parameters"] or
        value["agent_profile"]["path"] !=
            baseline_submission["agent_profile"] or
        value["eda"]["profile_id"] !=
            baseline_submission["eda"]["profile_id"] or
        value["eda"]["timeout_seconds"] !=
            baseline_submission["eda"]["timeout_seconds"] or
        {
            key: value["input_authority"][key]
            for key in ("actor_type", "identity", "roles", "decision")
        } != baseline_submission["input_authority"] or
        value["testcase"] != expected_testcase
    ):
        raise ProjectJobError(
            "STALE_EVIDENCE",
            "internal Project manifest does not bind the exact submission")
    source_paths: set[str] = set()
    for section in ("spec", "rtl"):
        for source in value[section]["sources"]:
            relative = source["baseline_path"]
            _validate_relative_path(relative, "Baseline source")
            path = workspace / relative
            try:
                path.resolve(strict=True).relative_to(job_root.resolve())
            except (FileNotFoundError, ValueError) as error:
                raise ProjectJobError(
                    "STALE_EVIDENCE",
                    "baseline source escapes its immutable Job") from error
            if path.is_symlink() or not path.is_file() or \
                    _sha256(path) != source["fingerprint"]:
                raise ProjectJobError(
                    "STALE_EVIDENCE", "baseline source bytes are stale")
            try:
                normalized = path.read_text(encoding="utf-8").replace(
                    "\r\n", "\n").replace("\r", "\n")
            except UnicodeError as error:
                raise ProjectJobError(
                    "STALE_EVIDENCE",
                    "baseline source is no longer valid UTF-8 text") from error
            normalized_fp = _bytes_sha256(normalized.encode("utf-8"))
            identity = canonical_hash({
                "section": section,
                "path": source["path"],
                "normalized_fingerprint": normalized_fp,
            })
            if (
                normalized_fp != source["normalized_fingerprint"] or
                identity != source["source_identity"] or
                source["path"].casefold() in source_paths
            ):
                raise ProjectJobError(
                    "STALE_EVIDENCE",
                    "baseline normalized source identity is stale")
            source_paths.add(source["path"].casefold())
    for source in value["uvm_testcase_context"]["files"]:
        relative = source["baseline_path"]
        path = workspace / relative
        try:
            path.resolve(strict=True).relative_to(job_root.resolve())
        except (FileNotFoundError, ValueError) as error:
            raise ProjectJobError(
                "STALE_EVIDENCE",
                "baseline UVM testcase context escapes its immutable Job") from error
        if (path.is_symlink() or not path.is_file() or
                _sha256(path) != source["fingerprint"]):
            raise ProjectJobError(
                "STALE_EVIDENCE",
                "baseline UVM testcase context file is stale: {}".format(
                    source["logical_path"]))
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeError as error:
            raise ProjectJobError(
                "STALE_EVIDENCE",
                "baseline UVM testcase context is no longer UTF-8 text: {}".format(
                    source["logical_path"])) from error
        if content != source["content"]:
            raise ProjectJobError(
                "STALE_EVIDENCE",
                "baseline UVM testcase context text drifted: {}".format(
                    source["logical_path"]))
    try:
        current_profile, snapshot_bytes = load_project_agent_profile(
            workspace, value["agent_profile"]["path"], ProjectJobError)
    except ProjectJobError as error:
        raise ProjectJobError(
            "STALE_EVIDENCE",
            "Project Agent profile/config chain is no longer valid") from error
    if current_profile != value["agent_profile"]:
        raise ProjectJobError(
            "STALE_EVIDENCE", "Project Agent profile/config chain drifted")
    profile_snapshot = job_root / value["agent_profile"]["baseline_path"]
    if (not profile_snapshot.is_file() or profile_snapshot.is_symlink() or
            profile_snapshot.read_bytes() !=
            snapshot_bytes[value["agent_profile"]["path"]]):
        raise ProjectJobError(
            "STALE_EVIDENCE", "Project Agent profile snapshot is stale")
    checked_snapshots: set[str] = set()
    for section, role in ROLE_PATHS:
        config = value["agent_profile"]["bindings"][section][role]
        if config["baseline_path"] in checked_snapshots:
            continue
        checked_snapshots.add(config["baseline_path"])
        path = job_root / config["baseline_path"]
        try:
            provider_value = load_document(path)
        except Exception as error:
            raise ProjectJobError(
                "STALE_EVIDENCE",
                "Project provider snapshot is no longer valid") \
                from error
        if (
            _sha256(path) != config["fingerprint"] or
            not accepted(validate("provider_config", provider_value)) or
            canonical_hash(provider_value) != config["document_fingerprint"] or
            provider_value.get("provider_id") != config["provider_id"] or
            provider_value.get("model_id") != config["model_id"] or
            provider_value.get("auth_env") != config["auth_env"]
        ):
            raise ProjectJobError(
                "STALE_EVIDENCE",
                "Project provider snapshot fingerprint is stale")
    return copy.deepcopy(value)


class ProjectJobWorkflow:
    """Start at an approved input and stop only at the exact Human gate."""

    def __init__(
            self, workspace_root: Path, result_root: Path,
            provider: Any | None = None,
            reviewer_provider: Any | None = None,
            role_providers: dict[str, Any] | None = None,
            max_total_provider_calls: int = 12,
            max_total_tokens: int = 1000000,
            max_elapsed_seconds: int = 900,
            max_mapping_items_per_shard: int = 32,
            max_stage_revisions: int = 4,
            max_staged_file_bytes: int = 1024 * 1024,
            project_input_root: Path | None = None,
            uvm_build_runner: Any | None = None):
        self.workspace_root = Path(workspace_root).resolve()
        self.result_root = Path(result_root).resolve()
        if self.result_root != self.workspace_root / "result":
            raise ProjectJobError(
                "TOOL_PERMISSION_DENIED",
                "Project Job result root must be workspace/result")
        self.provider = provider
        self.reviewer_provider = reviewer_provider
        self.role_providers = dict(role_providers or {})
        self.uvm_build_runner = uvm_build_runner
        if max_total_provider_calls < 1 or max_total_tokens < 1 or \
                max_elapsed_seconds < 1:
            raise ProjectJobError(
                "INVALID_INPUT", "Project provider budget is invalid")
        if not 1 <= max_mapping_items_per_shard <= 512 or \
                not 0 <= max_stage_revisions <= 100 or \
                not 1024 <= max_staged_file_bytes <= 8 * 1024 * 1024:
            raise ProjectJobError(
                "INVALID_INPUT", "Project staged artifact budget is invalid")
        self.max_total_provider_calls = max_total_provider_calls
        self.max_total_tokens = max_total_tokens
        self.max_elapsed_seconds = max_elapsed_seconds
        self.max_mapping_items_per_shard = max_mapping_items_per_shard
        self.max_stage_revisions = max_stage_revisions
        self.max_staged_file_bytes = max_staged_file_bytes
        self.project_input_root = Path(
            project_input_root or self.workspace_root).resolve()
        self.bootstrap_handler = BootstrapHandler(BootstrapDependencies(
            workspace_root=self.workspace_root,
            result_root=self.result_root,
            error=ProjectJobError,
            role_paths=tuple(ROLE_PATHS),
            validate_submission=validate_project_submission,
            validate_input=validate_project_input,
            exact_submission=self._submission_bytes,
            job_root=self._job_root,
            baseline_source_records=self._baseline_source_records,
            load_agent_profile=load_project_agent_profile,
            eda_environment_fingerprint=self._eda_environment_fingerprint,
            testcase_identity=self._derived_testcase_identity,
            immutable_bytes=_immutable_bytes,
            immutable_json=_immutable_json,
        ))

    def _job_root(self, value: dict[str, Any]) -> Path:
        return self.result_root / "jobs" / value["job_id"]

    @staticmethod
    def _submission_bytes(
            submission: dict[str, Any],
            exact_bytes: bytes | None = None) -> bytes:
        if exact_bytes is None:
            exact_bytes = yaml.safe_dump(
                submission, sort_keys=False, allow_unicode=True).encode(
                    "utf-8")
        if not exact_bytes or len(exact_bytes) > 1024 * 1024 or \
                b"\x00" in exact_bytes:
            raise ProjectJobError(
                "INVALID_SCHEMA",
                "Project submission exceeds the portable YAML policy")
        try:
            parsed = yaml.safe_load(exact_bytes.decode("utf-8"))
        except (UnicodeError, yaml.YAMLError) as error:
            raise ProjectJobError(
                "INVALID_SCHEMA", "Project submission YAML is invalid") \
                from error
        if parsed != submission:
            raise ProjectJobError(
                "STALE_EVIDENCE",
                "parsed Project submission differs from exact submitted bytes")
        return exact_bytes

    @staticmethod
    def _eda_environment_fingerprint() -> str:
        # Bootstrap freezes the approved Xcelium profile identity, not live
        # tool availability.  A missing executable/license is a retryable UVM
        # tool pause and must not prevent immutable Job creation.
        return canonical_hash({
            "profile_id": "EDAPROFILE.XCELIUM.PROJECT.V1",
            "executable_ref": "EDAEXEC.XCELIUM",
            "phase": "UVM_COMPILE_ONLY",
            "adapter_contract": "XCELIUM_ADAPTER.1.0",
        })

    @staticmethod
    def _derived_testcase_identity(
            submission: dict[str, Any]) -> dict[str, str]:
        identity = canonical_hash({
            "job_id": submission["job_id"],
            "project_id": submission["project_id"],
            "artifact_kind": "PORTABLE_SV_TESTBENCH",
        })
        return {
            "top": "tb_project_{}".format(identity[:16]),
            "pass_marker":
                "DV_PROJECT_{}_PASS".format(identity[:16].upper()),
        }

    def _baseline_source_records(
            self, submission: dict[str, Any]
            ) -> tuple[dict[str, list[dict[str, str]]], dict[str, bytes]]:
        records: dict[str, list[dict[str, str]]] = {
            "spec": [], "rtl": [], "uvm_testcase_context": []}
        exact_bytes: dict[str, bytes] = {}
        resolved_seen: dict[Path, str] = {}
        basenames_seen: dict[str, set[str]] = {
            "spec": set(), "rtl": set()}
        for section in ("spec", "rtl"):
            for index, relative in enumerate(
                    submission[section]["sources"], start=1):
                source_path = _resolved_workspace_file(
                    self.workspace_root, relative, "Project source")
                if source_path in resolved_seen:
                    raise ProjectJobError(
                        "CONFLICTING_SOURCE",
                        "Project sources overlap through an identical or "
                        "symlink-resolved path")
                resolved_seen[source_path] = section
                content = source_path.read_bytes()
                if len(content) > 16 * 1024 * 1024 or b"\x00" in content:
                    raise ProjectJobError(
                        "BLOCKED_INPUT",
                        "Project source exceeds the portable text policy")
                try:
                    text = content.decode("utf-8")
                except UnicodeError as error:
                    raise ProjectJobError(
                        "BLOCKED_INPUT",
                        "Project source must be valid UTF-8 text") from error
                normalized = text.replace(
                    "\r\n", "\n").replace("\r", "\n")
                normalized_fp = _bytes_sha256(
                    normalized.encode("utf-8"))
                basename = PurePosixPath(relative).name
                if basename.casefold() in basenames_seen[section]:
                    raise ProjectJobError(
                        "CONFLICTING_SOURCE",
                        "Project source basenames collide in the immutable "
                        "baseline namespace")
                basenames_seen[section].add(basename.casefold())
                baseline_relative = (
                    "result/jobs/{}/input_baseline/{}/{}".format(
                        submission["job_id"], section, basename))
                source_identity = canonical_hash({
                    "section": section,
                    "path": relative,
                    "normalized_fingerprint": normalized_fp,
                })
                records[section].append({
                    "path": relative,
                    "baseline_path": baseline_relative,
                    "fingerprint": _bytes_sha256(content),
                    "normalized_fingerprint": normalized_fp,
                    "source_identity": source_identity,
                })
                exact_bytes[baseline_relative] = content
        for index, relative in enumerate(
                submission["uvm_testcase_context"]["files"], start=1):
            source_path = self._resolved_uvm_context_file(relative)
            try:
                content = source_path.read_bytes()
            except OSError as error:
                raise ProjectJobError(
                    "BLOCKED_INPUT",
                    "UVM testcase context file cannot be read: {}".format(
                        relative)) from error
            if len(content) > 16 * 1024 * 1024 or b"\x00" in content:
                raise ProjectJobError(
                    "BLOCKED_INPUT",
                    "UVM testcase context file exceeds the portable text policy: {}".format(
                        relative))
            try:
                text = content.decode("utf-8")
            except UnicodeError as error:
                raise ProjectJobError(
                    "BLOCKED_INPUT",
                    "UVM testcase context file must be valid UTF-8 text: {}".format(
                        relative)) from error
            logical_path = _logical_uvm_path(relative)
            basename = PurePosixPath(logical_path).name
            baseline_relative = (
                "result/jobs/{}/input_baseline/uvm_testcase_context/{:03d}-{}".format(
                    submission["job_id"], index, basename))
            records["uvm_testcase_context"].append({
                "logical_path": logical_path,
                "baseline_path": baseline_relative,
                "fingerprint": _bytes_sha256(content),
                "content": text,
            })
            exact_bytes[baseline_relative] = content
        return records, exact_bytes

    def _resolved_uvm_context_file(self, entry: str) -> Path:
        """Resolve one YAML-listed UVM source without exposing it to the model."""
        candidate = Path(entry)
        source = candidate if candidate.is_absolute() else \
            self.project_input_root / candidate
        try:
            resolved = source.resolve(strict=True)
        except OSError as error:
            raise ProjectJobError(
                "BLOCKED_INPUT",
                "UVM testcase context file is unavailable: {}".format(entry)) from error
        if source.is_symlink() or not resolved.is_file():
            raise ProjectJobError(
                "BLOCKED_INPUT",
                "UVM testcase context path is not a regular file: {}".format(entry))
        return resolved

    def _provider_for_role(self, profile_role: str) -> Any | None:
        if profile_role in self.role_providers:
            return self.role_providers[profile_role]
        if profile_role.startswith("review."):
            return self.reviewer_provider
        return self.provider

    def _probe_provider(
            self, job_root: Path, profile_role: str = "initial.stage1"
            ) -> dict[str, Any]:
        provider = self._provider_for_role(profile_role)
        if provider is None:
            raise ProjectJobError(
                "BLOCKED_TOOL",
                "{} production LLM provider is unavailable".format(
                    profile_role))
        stem = "provider_probe.{}".format(profile_role.replace(".", "_"))
        expected = self._provider_config_for_role(
            job_root, profile_role)
        prior_paths = sorted((job_root / "audit").glob(
            "{}*.json".format(stem)))
        for path in prior_paths:
            try:
                prior = load_document(path)
            except Exception as error:
                raise ProjectJobError(
                    "STALE_EVIDENCE",
                    "persisted provider probe evidence is invalid") from error
            if (
                not accepted(validate("provider_probe", prior)) or
                prior.get("provider_id") != expected["provider_id"] or
                prior.get("model_id") != expected["model_id"]
            ):
                raise ProjectJobError(
                    "STALE_EVIDENCE",
                    "persisted provider probe identity is stale")
            if prior["status"] == "PASS":
                if not prior["tool_call_capable"]:
                    raise ProjectJobError(
                        "STALE_EVIDENCE",
                        "persisted PASS provider probe lacks required capability")
                restore = getattr(provider, "restore_probe", None)
                if callable(restore):
                    try:
                        restore(prior)
                    except Exception as error:
                        raise ProjectJobError(
                            "STALE_EVIDENCE",
                            "persisted provider probe cannot activate the "
                            "configured provider instance") from error
                return prior
        try:
            probe = provider.probe()
        except Exception as error:
            raise ProjectJobError(
                "BLOCKED_TOOL",
                "{} production LLM provider probe failed safely".format(
                    profile_role)) from error
        if (
            not accepted(validate("provider_probe", probe)) or
            probe.get("provider_id") != expected["provider_id"] or
            probe.get("model_id") != expected["model_id"]
        ):
            raise ProjectJobError(
                "INVALID_AGENT_BINDING",
                "Provider probe identity does not match the profile role")
        _persist_provider_probe(job_root, probe, profile_role)
        if probe["status"] != "PASS":
            routes = sorted({
                "{}@{}".format(
                    item.get("code", "PROVIDER_PROBE_FAILED"),
                    item.get("path", "provider_config"))
                for item in probe.get("diagnostics", [])
                if isinstance(item, dict)
            })
            raise ProjectJobError(
                "BLOCKED_TOOL",
                "{} production LLM provider probe failed; route={}".format(
                    profile_role,
                    ",".join(routes) or
                    "PROVIDER_PROBE_FAILED@provider_config"))
        return probe

    @staticmethod
    def _provider_config_for_role(
            job_root: Path, profile_role: str) -> dict[str, Any]:
        try:
            manifest = load_document(job_root / INTERNAL_MANIFEST_PATH)
            section, role = profile_role.split(".", 1)
            config = agent_binding(manifest, section, role)
        except Exception as error:
            raise ProjectJobError(
                "STALE_EVIDENCE",
                "Project provider manifest evidence is invalid") from error
        if not isinstance(config, dict):
            raise ProjectJobError(
                "STALE_EVIDENCE", "Project provider config is invalid")
        return config

    @staticmethod
    def _budget() -> dict[str, Any]:
        return {
            "calls": 0,
            "tokens": 0,
            "started": time.monotonic(),
        }

    def _complete(
            self, profile_role: str, request: dict[str, Any],
            budget: dict[str, Any]) -> dict[str, Any]:
        provider = self._provider_for_role(profile_role)
        if provider is None:
            raise ProjectJobError(
                "BLOCKED_TOOL",
                "{} provider is unavailable".format(profile_role))
        elapsed = time.monotonic() - budget["started"]
        if elapsed > self.max_elapsed_seconds:
            raise ProjectJobError(
                "TOOL_LIMIT_EXCEEDED",
                "Project provider elapsed-time budget was exceeded")
        if budget["calls"] >= self.max_total_provider_calls:
            raise ProjectJobError(
                "TOOL_LIMIT_EXCEEDED",
                "Project total provider-call budget was exceeded")
        try:
            response = (
                provider.select_tools(request)
                if request.get("operation") == "SELECT_TOOLS"
                else provider.complete(request))
        except Exception as error:
            if getattr(error, "safe_failure_code", "") == \
                    "INVALID_PROVIDER_REQUEST":
                raise ProjectJobError(
                    "INVALID_PROVIDER_REQUEST",
                    "{} provider request violates the local adapter contract".
                    format(profile_role)) from error
            signature = getattr(
                error, "safe_remote_signature", "")
            routed = (
                "; remote={}".format(signature)
                if isinstance(signature, str) and
                SAFE_PROVIDER_SIGNATURE.fullmatch(signature)
                else "")
            raise ProjectJobError(
                "BLOCKED_TOOL",
                "{} provider invocation failed safely{}".format(
                    profile_role, routed)) from error
        budget["calls"] += 1
        usage = response.get("usage", {})
        budget["tokens"] += (
            usage.get("input_tokens", 0) +
            usage.get("output_tokens", 0))
        if budget["tokens"] > self.max_total_tokens:
            raise ProjectJobError(
                "TOOL_LIMIT_EXCEEDED",
                "Project total provider-token budget was exceeded")
        if time.monotonic() - budget["started"] > \
                self.max_elapsed_seconds:
            raise ProjectJobError(
                "TOOL_LIMIT_EXCEEDED",
                "Project provider elapsed-time budget was exceeded")
        return response

    def start(
            self, project_submission: dict[str, Any],
            submission_bytes: bytes | None = None) -> dict[str, Any]:
        from runtime.staged_workflow import StagedProjectWorkflow
        return StagedProjectWorkflow(self).start(
            project_submission, submission_bytes)

    def retry_blocked_review(
            self, project_submission: dict[str, Any],
            submission_bytes: bytes | None = None) -> dict[str, Any]:
        """Explicitly retry only a provider-blocked review, never replay it."""
        from runtime.staged_workflow import StagedProjectWorkflow
        return StagedProjectWorkflow(self).retry_blocked_review(
            project_submission, submission_bytes)

    def submit_repair_plan(
            self, project_submission: dict[str, Any],
            repair_plan: dict[str, Any],
            submission_bytes: bytes | None = None) -> dict[str, Any]:
        """Submit one Orchestrator final plan to the deterministic Router."""
        from runtime.staged_workflow import StagedProjectWorkflow
        return StagedProjectWorkflow(self).submit_repair_plan(
            project_submission, repair_plan, submission_bytes)

    def route_scenarios(
            self, project_submission: dict[str, Any],
            owner_review: dict[str, Any],
            submission_bytes: bytes | None = None) -> dict[str, Any]:
        """Submit one completed DV Owner direct-review form."""
        from runtime.staged_workflow import StagedProjectWorkflow
        return StagedProjectWorkflow(self).route_scenarios(
            project_submission, owner_review, submission_bytes)

__all__ = [
    "ProjectJobWorkflow",
    "validate_project_input",
    "validate_project_submission",
]
