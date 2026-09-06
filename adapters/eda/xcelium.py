"""Isolated, no-shell Xcelium adapter.

This module deliberately has no Project workflow imports.  It can qualify an
approved ``xrun`` executable and execute exact source sets, but it cannot bind
RTL to an approved testcase, advance a Project checkpoint, or publish a
Project verdict.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import resource
import shutil
import signal
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from adapters.eda.boundary import (
    FORBIDDEN,
    SAFE_VALUE,
    TrustedEdaBoundaryError,
    TrustedExecutableRegistry,
)
from contracts.validator import accepted, diagnostic, load_document, validate
from infrastructure.persistence.atomic_artifact import publish_immutable_text
from scripts.dvlib import canonical_hash


XCELIUM_ADAPTER_VERSION = "1.0"
XCELIUM_EXECUTABLE_REF = "EDAEXEC.XCELIUM"
XCELIUM_QUALIFICATION_SCOPE = \
    "ISOLATED_XCELIUM_ADAPTER_NO_PROJECT_AUTHORITY"
DEFAULT_VERSION_PATTERN = r"(?i)\b(?:xrun|xcelium)\b.*\d{2}\.\d{2}"
DEFAULT_RESOURCE_LIMITS = {
    "cpu_seconds": 600,
    "memory_mb": 8192,
    "output_files": 20000,
}
APPROVED_EXTERNAL_ENVIRONMENT_NAMES = frozenset({
    "CDS_LIC_FILE",
    "CDS_ROOT",
    "LC_ALL",
    "LD_LIBRARY_PATH",
    "LM_LICENSE_FILE",
    "PATH",
    "UVMHOME",
    "XCELIUMHOME",
})
_ABSOLUTE_PATH = re.compile(r"/(?:[^ \t\r\n:'\";,])+")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_EXECUTION_ID = re.compile(r"^[A-Z0-9][A-Z0-9_.-]*$")
_DEFINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:=[A-Za-z0-9_./:+@=-]+)?$")
_PLUSARG = re.compile(r"^\+[A-Za-z_][A-Za-z0-9_]*(?:=[A-Za-z0-9_./:+@=-]+)?$")
_TIMESCALE = re.compile(r"^[0-9]+(?:s|ms|us|ns|ps|fs)/[0-9]+(?:s|ms|us|ns|ps|fs)$")
_ACCESS = re.compile(r"^[rwc]+$")
_PASS_MARKER = re.compile(r"^DV_[A-Z0-9_]+_PASS$")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _error(code: str, message: str, path: str,
           artifact: str = "XCELIUM_ADAPTER_INPUT") -> TrustedEdaBoundaryError:
    return TrustedEdaBoundaryError(message, [diagnostic(
        code, message, path, required_owner="DV_OWNER",
        required_artifact_kind=artifact)])


def _immutable_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = json.dumps(
        dict(value), sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    publish_immutable_text(
        path, encoded, lambda message: _error(
            "CONFLICTING_REPLAY", message, str(path),
            "XCELIUM_REPLAY_EVIDENCE"),
        "immutable Xcelium adapter evidence conflict")


@dataclass(frozen=True)
class XceliumRunConfiguration:
    """Exact inputs for one isolated Xcelium compile, build, or full run."""

    sources: tuple[str, ...]
    top: str | None
    parameters: tuple[tuple[str, int | bool], ...] = ()
    include_dirs: tuple[str, ...] = ()
    defines: tuple[str, ...] = ()
    plusargs: tuple[str, ...] = ()
    uvm: bool = False
    uvm_test: str | None = None
    seed: int = 1
    timescale: str = "1ns/1ps"
    access: str = "rwc"
    coverage: bool = False
    waves: bool = False
    pass_marker: str | None = None
    timeout_seconds: int | None = None


@dataclass(frozen=True)
class XceliumExecutionResult:
    request: dict[str, Any]
    evidence: dict[str, Any]
    request_path: str
    evidence_path: str
    replayed: bool


@dataclass(frozen=True)
class _ArtifactDigest:
    fingerprint: str
    size_bytes: int
    file_count: int


class XceliumAdapter:
    """Execute an approved ``xrun`` binary without a shell.

    The adapter writes only beneath ``result/jobs/<job_id>/runs/xcelium`` and
    ``result/jobs/<job_id>/audit/xcelium``.  Environment values remain private;
    persisted requests contain only an opaque identity and a fingerprint.
    """

    def __init__(
            self, workspace_root: Path, result_root: Path, job_id: str,
            xrun_path: Path, environment_identity: str,
            private_environment: Mapping[str, str], *,
            timeout_seconds: int = 120,
            resource_limits: Mapping[str, int] | None = None,
            expected_version_pattern: str = DEFAULT_VERSION_PATTERN):
        self.workspace_root = Path(workspace_root).resolve()
        self.result_root = Path(result_root).resolve()
        self.job_id = job_id
        self.job_root = (self.result_root / "jobs" / job_id).resolve()
        self.xrun_path = Path(xrun_path).resolve()
        self.environment_identity = environment_identity
        self.timeout_seconds = timeout_seconds
        self.resource_limits = dict(
            resource_limits or DEFAULT_RESOURCE_LIMITS)
        self.expected_version_pattern = expected_version_pattern
        self._private_environment = dict(private_environment)
        self._validate_constructor()
        executable_fingerprint = (
            _sha256_file(self.xrun_path) if self.xrun_path.is_file()
            else "0" * 64)
        self.environment_fingerprint = canonical_hash({
            "adapter_version": XCELIUM_ADAPTER_VERSION,
            "environment_identity": self.environment_identity,
            "approved_environment_names": sorted(self._private_environment),
            "private_environment_values": self._private_environment,
            "executable_fingerprint": executable_fingerprint,
        })
        self.registry = TrustedExecutableRegistry([{
            "executable_ref": XCELIUM_EXECUTABLE_REF,
            "resolved_path": str(self.xrun_path),
            "environment_fingerprint": self.environment_fingerprint,
            "approved": True,
        }])

    @classmethod
    def from_preloaded_environment(
            cls, workspace_root: Path, result_root: Path, job_id: str,
            environment_identity: str, *,
            allowed_environment_names: Sequence[str] = (
                "PATH", "LD_LIBRARY_PATH", "CDS_LIC_FILE", "LM_LICENSE_FILE",
                "CDS_ROOT", "UVMHOME", "XCELIUMHOME"),
            timeout_seconds: int = 120,
            expected_version_pattern: str = DEFAULT_VERSION_PATTERN,
            environment: Mapping[str, str] | None = None,
            xrun_path: Path | None = None) -> "XceliumAdapter":
        """Create an adapter from an already-loaded deployment environment."""
        source = dict(os.environ if environment is None else environment)
        requested = set(allowed_environment_names)
        if not requested.issubset(APPROVED_EXTERNAL_ENVIRONMENT_NAMES):
            raise _error(
                "TOOL_PERMISSION_DENIED",
                "Xcelium environment allow-list contains an unsupported name",
                "allowed_environment_names", "APPROVED_XCELIUM_ENVIRONMENT")
        private = {
            name: source[name] for name in sorted(requested)
            if isinstance(source.get(name), str) and source[name]
        }
        private.setdefault("LC_ALL", "C")
        resolved = Path(xrun_path).resolve() if xrun_path is not None else None
        if resolved is None:
            discovered = shutil.which("xrun", path=private.get("PATH"))
            if discovered is None:
                raise _error(
                    "BLOCKED_TOOL",
                    "Approved Xcelium executable is unavailable",
                    "xrun", "APPROVED_EXECUTABLE_REGISTRY")
            resolved = Path(discovered).resolve()
        return cls(
            workspace_root, result_root, job_id, resolved,
            environment_identity, private,
            timeout_seconds=timeout_seconds,
            expected_version_pattern=expected_version_pattern)

    def _validate_constructor(self) -> None:
        if (self.result_root.name != "result" or
                self.job_root.parent != self.result_root / "jobs" or
                not re.fullmatch(r"JOB\.[A-Z0-9_.-]+", self.job_id)):
            raise _error(
                "TOOL_PERMISSION_DENIED",
                "Xcelium output boundary is invalid",
                "job_root", "XCELIUM_RESULT_POLICY")
        if not re.fullmatch(
                r"XCELIUMENV\.[A-Z0-9_.-]+", self.environment_identity):
            raise _error(
                "INVALID_SCHEMA", "Xcelium environment identity is invalid",
                "environment_identity", "APPROVED_XCELIUM_ENVIRONMENT")
        if (set(self._private_environment) -
                APPROVED_EXTERNAL_ENVIRONMENT_NAMES or
                not all(isinstance(value, str) and value
                        for value in self._private_environment.values())):
            raise _error(
                "TOOL_PERMISSION_DENIED",
                "Xcelium environment contains a non-allowlisted entry",
                "private_environment", "APPROVED_XCELIUM_ENVIRONMENT")
        if not 1 <= self.timeout_seconds <= 3600:
            raise _error(
                "INVALID_SCHEMA", "Xcelium timeout is outside policy",
                "timeout_seconds")
        limits = self.resource_limits
        if (set(limits) != {"cpu_seconds", "memory_mb", "output_files"} or
                not 1 <= limits["cpu_seconds"] <= 3600 or
                not 64 <= limits["memory_mb"] <= 65536 or
                not 1 <= limits["output_files"] <= 1000000):
            raise _error(
                "INVALID_SCHEMA", "Xcelium resource limits are invalid",
                "resource_limits")
        try:
            re.compile(self.expected_version_pattern)
        except re.error as caught:
            raise _error(
                "INVALID_SCHEMA", "Xcelium version pattern is invalid",
                "expected_version_pattern") from caught

    @staticmethod
    def _literal(value: str) -> dict[str, str]:
        return {"kind": "LITERAL", "value": value}

    @staticmethod
    def _token(kind: str, value: str) -> dict[str, str]:
        return {"kind": kind, "value": value}

    @staticmethod
    def _safe_literal(value: str) -> str:
        if (not SAFE_VALUE.fullmatch(value) or
                any(item in value for item in FORBIDDEN) or
                Path(value).is_absolute() or ".." in Path(value).parts):
            raise _error(
                "TOOL_PERMISSION_DENIED",
                "Xcelium argv literal contains forbidden syntax",
                "argv_template", "XCELIUM_ARGV_CONTRACT")
        return value

    def _safe_workspace_file(self, relative: str) -> Path:
        self._safe_literal(relative)
        path = (self.workspace_root / relative).resolve()
        try:
            path.relative_to(self.workspace_root)
        except ValueError as caught:
            raise _error(
                "TOOL_PERMISSION_DENIED", "Xcelium source escapes workspace",
                relative, "APPROVED_XCELIUM_SOURCE") from caught
        original = self.workspace_root / relative
        if not path.is_file() or original.is_symlink():
            raise _error(
                "BLOCKED_INPUT", "Xcelium source is missing or unsafe",
                relative, "APPROVED_XCELIUM_SOURCE")
        return path

    def _safe_workspace_directory(self, relative: str) -> Path:
        self._safe_literal(relative)
        path = (self.workspace_root / relative).resolve()
        try:
            path.relative_to(self.workspace_root)
        except ValueError as caught:
            raise _error(
                "TOOL_PERMISSION_DENIED",
                "Xcelium include directory escapes workspace",
                relative, "APPROVED_XCELIUM_INCLUDE_DIRECTORY") from caught
        original = self.workspace_root / relative
        if not path.is_dir() or original.is_symlink():
            raise _error(
                "BLOCKED_INPUT",
                "Xcelium include directory is missing or unsafe",
                relative, "APPROVED_XCELIUM_INCLUDE_DIRECTORY")
        return path

    def _validate_configuration(
            self, configuration: XceliumRunConfiguration,
            phase: str) -> None:
        if phase not in {"COMPILE", "BUILD", "RUN"}:
            raise _error("INVALID_SCHEMA", "Unknown Xcelium phase", "phase")
        if (not configuration.sources or
                len(set(configuration.sources)) != len(configuration.sources)):
            raise _error(
                "INVALID_SCHEMA", "Xcelium source set is invalid",
                "configuration")
        if ((phase == "COMPILE" and configuration.top is not None) or
                (phase != "COMPILE" and (
                    not isinstance(configuration.top, str) or
                    not _IDENTIFIER.fullmatch(configuration.top)))):
            raise _error(
                "INVALID_SCHEMA", "Xcelium phase has an invalid top",
                "configuration.top")
        if phase == "COMPILE" and configuration.parameters:
            raise _error(
                "INVALID_SCHEMA",
                "Xcelium compile-only phase cannot elaborate parameters",
                "parameters")
        if (len({name for name, _ in configuration.parameters}) !=
                len(configuration.parameters) or
                any(not _IDENTIFIER.fullmatch(name) or
                    (type(value) is not int and type(value) is not bool)
                    for name, value in configuration.parameters)):
            raise _error(
                "INVALID_SCHEMA", "Xcelium parameters are invalid",
                "parameters")
        for source in configuration.sources:
            self._safe_workspace_file(source)
        for include in configuration.include_dirs:
            self._safe_workspace_directory(include)
        if len(set(configuration.include_dirs)) != len(configuration.include_dirs):
            raise _error(
                "INVALID_SCHEMA", "Xcelium include directories are duplicated",
                "include_dirs")
        if (any(not _DEFINE.fullmatch(value)
                for value in configuration.defines) or
                len(set(configuration.defines)) != len(configuration.defines)):
            raise _error(
                "INVALID_SCHEMA", "Xcelium defines are invalid",
                "defines")
        if (any(not _PLUSARG.fullmatch(value)
                for value in configuration.plusargs) or
                len(set(configuration.plusargs)) != len(configuration.plusargs)):
            raise _error(
                "INVALID_SCHEMA", "Xcelium plusargs are invalid",
                "plusargs")
        if (not _TIMESCALE.fullmatch(configuration.timescale) or
                not _ACCESS.fullmatch(configuration.access) or
                not 1 <= configuration.seed <= 2147483647):
            raise _error(
                "INVALID_SCHEMA", "Xcelium runtime options are invalid",
                "configuration")
        if (configuration.timeout_seconds is not None and
                (type(configuration.timeout_seconds) is not int or
                 not 1 <= configuration.timeout_seconds <= 3600)):
            raise _error(
                "INVALID_SCHEMA", "Xcelium testcase timeout is invalid",
                "timeout_seconds")
        if configuration.uvm_test is not None and (
                not configuration.uvm or
                not _IDENTIFIER.fullmatch(configuration.uvm_test)):
            raise _error(
                "INVALID_SCHEMA",
                "Xcelium UVM test requires a valid UVM configuration",
                "uvm_test")
        if configuration.pass_marker is not None and not \
                _PASS_MARKER.fullmatch(configuration.pass_marker):
            raise _error(
                "INVALID_SCHEMA", "Xcelium pass marker is invalid",
                "pass_marker")
        if phase == "RUN" and configuration.pass_marker is None:
            raise _error(
                "INVALID_SCHEMA", "Xcelium run requires an exact pass marker",
                "pass_marker")

    def _source_fingerprints(
            self, configuration: XceliumRunConfiguration
            ) -> list[dict[str, str]]:
        return sorted([{
            "path": relative,
            "fingerprint": _sha256_file(
                self._safe_workspace_file(relative)),
        } for relative in configuration.sources], key=lambda item: item["path"])

    def _common_argv(
            self, configuration: XceliumRunConfiguration,
            execution_id: str) -> list[dict[str, str]]:
        snapshot = "snapshot_{}".format(
            hashlib.sha256(execution_id.encode()).hexdigest()[:16])
        argv = [
            self._literal("-64bit"),
            self._literal("-sv"),
            self._literal("-timescale"),
            self._literal(configuration.timescale),
            self._literal("-xmlibdirname"),
            self._token("OUTPUT_PATH", "xcelium.d"),
            self._literal("-l"),
            self._token("OUTPUT_PATH", "xrun.log"),
        ]
        if configuration.top is not None:
            argv[2:2] = [
                self._literal("-top"),
                self._literal(configuration.top),
            ]
            log_index = next(
                index for index, item in enumerate(argv)
                if item["value"] == "-l")
            argv[log_index:log_index] = [
                self._literal("-access"),
                self._literal("+{}".format(configuration.access)),
                self._literal("-snapshot"),
                self._literal(snapshot),
            ]
        if configuration.uvm:
            argv.append(self._literal("-uvm"))
        for name, value in configuration.parameters:
            parameter = "{}={}".format(
                name, int(value) if isinstance(value, bool) else value)
            argv.extend([
                self._literal("-defparam"),
                self._literal("{}.{}".format(configuration.top, parameter)),
            ])
        for define in configuration.defines:
            argv.append(self._literal("+define+{}".format(define)))
        for include in configuration.include_dirs:
            argv.extend([
                self._literal("-incdir"),
                self._token("INCLUDE_DIR", include),
            ])
        argv.extend(
            self._token("SOURCE", source)
            for source in configuration.sources)
        return argv

    def _request(
            self, phase: str, execution_id: str,
            configuration: XceliumRunConfiguration | None = None
            ) -> dict[str, Any]:
        if not _EXECUTION_ID.fullmatch(execution_id):
            raise _error(
                "INVALID_SCHEMA", "Xcelium execution ID is invalid",
                "execution_id")
        if phase == "VERSION":
            argv = [self._literal("-version")]
            sources: list[dict[str, str]] = []
            expected: list[dict[str, Any]] = []
            pass_marker = None
            version_pattern: str | None = self.expected_version_pattern
        else:
            if configuration is None:
                raise _error(
                    "MISSING_REQUIRED_FIELD",
                    "Xcelium build/run configuration is required",
                    "configuration")
            self._validate_configuration(configuration, phase)
            argv = self._common_argv(configuration, execution_id)
            if phase == "COMPILE":
                argv.insert(2, self._literal("-compile"))
            elif phase == "BUILD":
                argv.insert(2, self._literal("-elaborate"))
            else:
                argv.extend([
                    self._literal("-svseed"),
                    self._literal(str(configuration.seed)),
                ])
                if configuration.uvm_test is not None:
                    argv.append(self._literal(
                        "+UVM_TESTNAME={}".format(configuration.uvm_test)))
                if configuration.coverage:
                    argv.extend([
                        self._literal("-coverage"),
                        self._literal("all"),
                        self._literal("-covoverwrite"),
                        self._literal("-covworkdir"),
                        self._token("OUTPUT_PATH", "cov_work"),
                        self._literal("-covtest"),
                        self._literal(execution_id.casefold().replace(".", "_")),
                    ])
                argv.extend(self._literal(value)
                            for value in configuration.plusargs)
            sources = self._source_fingerprints(configuration)
            expected = [{
                "kind": "XCELIUM_DATABASE",
                "path_kind": "DIRECTORY",
                "relative_path": "xcelium.d",
                "required": True,
                "minimum_bytes": 1,
            }, {
                "kind": "XRUN_LOG",
                "path_kind": "FILE",
                "relative_path": "xrun.log",
                "required": True,
                "minimum_bytes": 1,
            }]
            if phase == "RUN" and configuration.waves:
                expected.append({
                    "kind": "TRACE_VCD",
                    "path_kind": "FILE",
                    "relative_path": "dump.vcd",
                    "required": True,
                    "minimum_bytes": 1,
                })
            if phase == "RUN" and configuration.coverage:
                expected.append({
                    "kind": "COVERAGE_DATABASE",
                    "path_kind": "DIRECTORY",
                    "relative_path": "cov_work",
                    "required": True,
                    "minimum_bytes": 1,
                })
            pass_marker = configuration.pass_marker if phase == "RUN" else None
            version_pattern = None
        output_subdir = "runs/xcelium/{}/{}".format(
            execution_id.casefold(), phase.casefold())
        request = {
            "schema_version": "1.0",
            "artifact_kind": "XCELIUM_EXECUTION_REQUEST",
            "request_id": "XCELIUM.REQUEST.{}.{}.{}".format(
                self.job_id.removeprefix("JOB."), execution_id, phase),
            "job_id": self.job_id,
            "phase": phase,
            "executable_ref": XCELIUM_EXECUTABLE_REF,
            "environment_identity": self.environment_identity,
            "environment_fingerprint": self.environment_fingerprint,
            "argv_template": argv,
            "source_fingerprints": sources,
            "timeout_seconds": (
                configuration.timeout_seconds
                if configuration is not None and
                configuration.timeout_seconds is not None
                else self.timeout_seconds),
            "resource_limits": copy.deepcopy(self.resource_limits),
            "output_subdir": output_subdir,
            "expected_artifacts": expected,
            "expected_pass_marker": pass_marker,
            "expected_version_pattern": version_pattern,
            "request_fingerprint": "0" * 64,
        }
        request["request_fingerprint"] = canonical_hash({
            key: value for key, value in request.items()
            if key != "request_fingerprint"
        })
        if not accepted(validate("xcelium_execution_request", request)):
            raise _error(
                "INVALID_SCHEMA", "Generated Xcelium request is invalid",
                "request", "XCELIUM_EXECUTION_REQUEST")
        return request

    @staticmethod
    def _preexec(cpu_seconds: int, memory_mb: int):
        def apply_limits() -> None:
            resource.setrlimit(
                resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
            memory = memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        return apply_limits

    def _output_dir(self, request: Mapping[str, Any]) -> Path:
        output = (self.job_root / request["output_subdir"]).resolve()
        try:
            output.relative_to(self.job_root / "runs" / "xcelium")
        except ValueError as caught:
            raise _error(
                "TOOL_PERMISSION_DENIED",
                "Xcelium output escapes the current Job",
                "output_subdir", "XCELIUM_RESULT_POLICY") from caught
        return output

    def _materialize(
            self, request: Mapping[str, Any], output: Path) -> list[str]:
        source_by_path = {
            item["path"]: item["fingerprint"]
            for item in request["source_fingerprints"]}
        argv = [self.registry.resolve(
            request["executable_ref"], request["environment_fingerprint"])]
        for token in request["argv_template"]:
            kind, value = token["kind"], token["value"]
            if kind == "LITERAL":
                argv.append(self._safe_literal(value))
            elif kind == "SOURCE":
                path = self._safe_workspace_file(value)
                if _sha256_file(path) != source_by_path.get(value):
                    raise _error(
                        "STALE_EVIDENCE", "Xcelium source fingerprint drift",
                        value, "APPROVED_XCELIUM_SOURCE")
                argv.append(str(path))
            elif kind == "INCLUDE_DIR":
                argv.append(str(self._safe_workspace_directory(value)))
            elif kind == "OUTPUT_PATH":
                relative = self._safe_literal(value)
                path = (output / relative).resolve()
                try:
                    path.relative_to(output)
                except ValueError as caught:
                    raise _error(
                        "TOOL_PERMISSION_DENIED",
                        "Xcelium output argument escapes the run directory",
                        value, "XCELIUM_RESULT_POLICY") from caught
                argv.append(str(path))
            else:
                raise _error(
                    "INVALID_SCHEMA", "Unknown Xcelium argv token",
                    "argv_template", "XCELIUM_ARGV_CONTRACT")
        return argv

    def _sanitize(self, text: str) -> str:
        sanitized = text.replace(
            str(self.workspace_root) + os.sep, "WORKSPACE::")
        sanitized = sanitized.replace(
            str(self.result_root) + os.sep, "RESULT::")
        protected: dict[str, str] = {}
        for index, match in enumerate(re.findall(
                r"(?:WORKSPACE|RESULT)::[^ \t\r\n:'\";,]+", sanitized)):
            token = "__APPROVED_RELATIVE_PATH_{}__".format(index)
            protected[token] = match
            sanitized = sanitized.replace(match, token)
        private_values = [
            str(self.xrun_path), str(self.workspace_root),
            str(self.result_root), *self._private_environment.values()]
        for value in sorted(
                {item for item in private_values if len(item) >= 8},
                key=len, reverse=True):
            sanitized = sanitized.replace(value, "[REDACTED_PRIVATE_VALUE]")
        sanitized = _ABSOLUTE_PATH.sub("[REDACTED_PATH]", sanitized)
        for token, value in protected.items():
            sanitized = sanitized.replace(token, value)
        return sanitized

    @staticmethod
    def _tree_digest(path: Path) -> _ArtifactDigest:
        if path.is_symlink():
            raise _error(
                "TOOL_PERMISSION_DENIED",
                "Xcelium artifact root is a symlink",
                str(path), "XCELIUM_ARTIFACT_DISCOVERY")
        if path.is_file():
            return _ArtifactDigest(
                _sha256_file(path), path.stat().st_size, 1)
        if not path.is_dir():
            raise _error(
                "STALE_EVIDENCE", "Xcelium artifact is missing",
                str(path), "XCELIUM_ARTIFACT_DISCOVERY")
        inventory = []
        size = 0
        for child in sorted(path.rglob("*")):
            if child.is_symlink():
                try:
                    target = child.readlink()
                    resolved = child.resolve(strict=True)
                    resolved.relative_to(path.resolve())
                except (OSError, ValueError) as caught:
                    raise _error(
                        "TOOL_PERMISSION_DENIED",
                        "Xcelium artifact symlink escapes the artifact tree",
                        str(child), "XCELIUM_ARTIFACT_DISCOVERY") from caught
                if target.is_absolute() or not (resolved.is_file() or
                                                resolved.is_dir()):
                    raise _error(
                        "TOOL_PERMISSION_DENIED",
                        "Xcelium artifact symlink is unsafe",
                        str(child), "XCELIUM_ARTIFACT_DISCOVERY")
                inventory.append({
                    "path": child.relative_to(path).as_posix(),
                    "symlink_target": str(target),
                })
                continue
            if child.is_file():
                child_size = child.stat().st_size
                size += child_size
                inventory.append({
                    "path": child.relative_to(path).as_posix(),
                    "fingerprint": _sha256_file(child),
                    "size_bytes": child_size,
                })
        if not inventory:
            raise _error(
                "STALE_EVIDENCE", "Xcelium artifact directory is empty",
                str(path), "XCELIUM_ARTIFACT_DISCOVERY")
        return _ArtifactDigest(canonical_hash(inventory), size, len(inventory))

    def _artifact(
            self, request: Mapping[str, Any], output: Path,
            rule: Mapping[str, Any]) -> dict[str, Any] | None:
        path = (output / rule["relative_path"]).resolve()
        try:
            path.relative_to(output)
        except ValueError as caught:
            raise _error(
                "TOOL_PERMISSION_DENIED",
                "Xcelium artifact escapes the run directory",
                "expected_artifacts", "XCELIUM_ARTIFACT_DISCOVERY") from caught
        exists = path.is_file() if rule["path_kind"] == "FILE" else path.is_dir()
        if not exists:
            return None
        digest = self._tree_digest(path)
        if digest.size_bytes < rule["minimum_bytes"]:
            return None
        relative = path.relative_to(self.job_root).as_posix()
        return {
            "artifact_id": "XCELIUM.ARTIFACT.{}".format(hashlib.sha256(
                (request["request_id"] + relative).encode()
            ).hexdigest()[:16].upper()),
            "kind": rule["kind"],
            "path_kind": rule["path_kind"],
            "relative_path": relative,
            "fingerprint": digest.fingerprint,
            "size_bytes": digest.size_bytes,
            "file_count": digest.file_count,
        }

    @staticmethod
    def _classify(
            phase: str, combined: str, exit_code: int, timed_out: bool,
            expected_marker: str | None, marker_found: bool | None,
            version_pattern: str | None, simulator_version: str | None,
            missing_required: bool, output_limit: bool,
            ) -> tuple[str, list[str]]:
        diagnostics: list[str] = []
        status = "PASS"
        lower = combined.casefold()
        if timed_out:
            return "BLOCKED_TOOL", ["PROCESS_TIMEOUT"]
        if exit_code != 0:
            diagnostics.append("PROCESS_EXIT_NONZERO")
            status = "FAIL"
        if any(pattern in lower for pattern in (
                "license checkout failed", "unable to checkout license",
                "flexnet licensing error", "*f,nolicn", "no such feature exists")):
            diagnostics.append("LICENSE_UNAVAILABLE")
            status = "BLOCKED_TOOL"
        if ("error while loading shared libraries" in lower or
                "cannot open shared object file" in lower):
            diagnostics.append("RUNTIME_DEPENDENCY_MISSING")
            status = "BLOCKED_TOOL"
        if re.search(r"xmvlog:\s*\*[EF],", combined, re.IGNORECASE):
            diagnostics.append("COMPILE_FAILED")
            if status != "BLOCKED_TOOL":
                status = "FAIL"
        if (re.search(r"xmelab:\s*\*[EF],", combined, re.IGNORECASE) or
                re.search(r"xrun:\s*\*[EF],ELBERR", combined, re.IGNORECASE)):
            diagnostics.append("ELABORATION_FAILED")
            if status != "BLOCKED_TOOL":
                status = "FAIL"
        if re.search(r"xmsim:\s*\*[EF],", combined, re.IGNORECASE):
            diagnostics.append("SIMULATION_FAILED")
            if status != "BLOCKED_TOOL":
                status = "FAIL"
        if re.search(r"UVM_(?:FATAL|ERROR)\s*:\s*[1-9][0-9]*", combined):
            diagnostics.append("UVM_FAILURE")
            if status != "BLOCKED_TOOL":
                status = "FAIL"
        if expected_marker is not None and marker_found is False:
            diagnostics.append("PASS_MARKER_MISSING")
            if status != "BLOCKED_TOOL":
                status = "FAIL"
        if phase == "VERSION" and version_pattern is not None and (
                simulator_version is None or
                re.search(version_pattern, simulator_version) is None):
            diagnostics.append("VERSION_MISMATCH")
            if status != "BLOCKED_TOOL":
                status = "FAIL"
        if missing_required:
            diagnostics.append("MISSING_ARTIFACT")
            if status != "BLOCKED_TOOL":
                status = "FAIL"
        if output_limit:
            diagnostics.append("OUTPUT_FILE_LIMIT")
            if status != "BLOCKED_TOOL":
                status = "FAIL"
        return status, sorted(set(diagnostics))

    def _execute_process(self, request: Mapping[str, Any]) -> dict[str, Any]:
        executable = self.registry.resolve(
            request["executable_ref"], request["environment_fingerprint"])
        if not Path(executable).is_file() or not os.access(executable, os.X_OK):
            raise _error(
                "BLOCKED_TOOL", "Approved Xcelium executable is unavailable",
                "executable_ref", "APPROVED_EXECUTABLE_REGISTRY")
        output = self._output_dir(request)
        if output.exists():
            raise _error(
                "STALE_EVIDENCE",
                "Xcelium output exists without exact replay evidence",
                "output_subdir", "XCELIUM_REPLAY_EVIDENCE")
        output.mkdir(parents=True)
        home = output / "home"
        temporary = output / "tmp"
        home.mkdir()
        temporary.mkdir()
        environment = copy.deepcopy(self._private_environment)
        environment.update({
            "HOME": str(home),
            "TMPDIR": str(temporary),
            "LC_ALL": environment.get("LC_ALL", "C"),
        })
        argv = self._materialize(request, output)
        started = _utc()
        timed_out = False
        exit_code = -1
        stdout = b""
        stderr = b""
        process = subprocess.Popen(
            argv, cwd=str(output), env=environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, shell=False, start_new_session=True,
            preexec_fn=self._preexec(
                request["resource_limits"]["cpu_seconds"],
                request["resource_limits"]["memory_mb"]))
        try:
            stdout, stderr = process.communicate(
                timeout=request["timeout_seconds"])
            exit_code = int(process.returncode)
        except subprocess.TimeoutExpired as caught:
            timed_out = True
            stdout = caught.stdout or b""
            stderr = caught.stderr or b""
            os.killpg(process.pid, signal.SIGKILL)
            trailing_out, trailing_err = process.communicate()
            stdout += trailing_out or b""
            stderr += trailing_err or b""
            exit_code = -signal.SIGKILL
        ended = _utc()
        stdout_text = self._sanitize(stdout.decode("utf-8", "replace"))
        stderr_text = self._sanitize(stderr.decode("utf-8", "replace"))
        stdout_path = output / "stdout.log"
        stderr_path = output / "stderr.log"
        stdout_path.write_text(stdout_text, encoding="utf-8")
        stderr_path.write_text(stderr_text, encoding="utf-8")
        xrun_log_path = output / "xrun.log"
        xrun_log_text = ""
        if xrun_log_path.is_file():
            raw = xrun_log_path.read_text(encoding="utf-8", errors="replace")
            xrun_log_text = self._sanitize(raw)
            xrun_log_path.write_text(xrun_log_text, encoding="utf-8")
        combined = "\n".join((stdout_text, stderr_text, xrun_log_text))
        def uvm_count(kind: str) -> int:
            # Xcelium/UVM summaries may appear in stdout, stderr, or xrun.log.
            # Sum explicitly reported counts; a bare error/fatal is conservatively
            # treated as one.
            matches = re.findall(
                r"\bUVM_{}\s*:\s*([0-9]+)".format(kind), combined,
                re.IGNORECASE)
            return sum(int(item) for item in matches) if matches else (
                1 if re.search(r"\bUVM_{}\b".format(kind), combined,
                               re.IGNORECASE) else 0)
        uvm_error_count = uvm_count("ERROR")
        uvm_fatal_count = uvm_count("FATAL")
        marker = request["expected_pass_marker"]
        marker_found = None if marker is None else marker in combined
        simulator_version = None
        if request["phase"] == "VERSION":
            simulator_version = next(
                (line.strip()[:512] for line in combined.splitlines()
                 if line.strip()), None)
        artifacts = []
        missing_required = False
        for rule in request["expected_artifacts"]:
            artifact = self._artifact(request, output, rule)
            if artifact is None:
                missing_required = missing_required or bool(rule["required"])
            else:
                artifacts.append(artifact)
        process_files = [
            path for path in output.rglob("*")
            if path.is_file() or path.is_symlink()]
        output_limit = len(process_files) > request["resource_limits"][
            "output_files"]
        status, diagnostics = self._classify(
            request["phase"], combined, exit_code, timed_out,
            marker, marker_found, request["expected_version_pattern"],
            simulator_version, missing_required, output_limit)
        logs = []
        for kind, path in (("STDOUT", stdout_path), ("STDERR", stderr_path)):
            logs.append({
                "kind": kind,
                "relative_path": path.relative_to(self.job_root).as_posix(),
                "fingerprint": _sha256_file(path),
                "size_bytes": path.stat().st_size,
            })
        if xrun_log_path.is_file():
            logs.append({
                "kind": "XRUN_LOG",
                "relative_path": xrun_log_path.relative_to(
                    self.job_root).as_posix(),
                "fingerprint": _sha256_file(xrun_log_path),
                "size_bytes": xrun_log_path.stat().st_size,
            })
        output_digest = self._tree_digest(output)
        evidence = {
            "schema_version": "1.0",
            "artifact_kind": "XCELIUM_EXECUTION_EVIDENCE",
            "evidence_id": "XCELIUM.EVIDENCE.{}".format(hashlib.sha256(
                request["request_fingerprint"].encode()
            ).hexdigest()[:16].upper()),
            "request_id": request["request_id"],
            "request_fingerprint": request["request_fingerprint"],
            "job_id": request["job_id"],
            "phase": request["phase"],
            "executable_ref": request["executable_ref"],
            "environment_identity": request["environment_identity"],
            "environment_fingerprint": request["environment_fingerprint"],
            "started_at": started,
            "ended_at": ended,
            "exit_code": exit_code,
            "execution_status": status,
            "timed_out": timed_out,
            "pass_marker_found": marker_found,
            "uvm_error_count": uvm_error_count,
            "uvm_fatal_count": uvm_fatal_count,
            "simulator_version": simulator_version,
            "logs": sorted(logs, key=lambda item: item["relative_path"]),
            "artifacts": sorted(
                artifacts, key=lambda item: item["artifact_id"]),
            "output_tree_fingerprint": output_digest.fingerprint,
            "output_tree_size_bytes": output_digest.size_bytes,
            "output_tree_file_count": output_digest.file_count,
            "diagnostic_codes": diagnostics,
            "qualification_scope": XCELIUM_QUALIFICATION_SCOPE,
            "evidence_fingerprint": "0" * 64,
        }
        evidence["evidence_fingerprint"] = canonical_hash({
            key: value for key, value in evidence.items()
            if key != "evidence_fingerprint"
        })
        if not accepted(validate("xcelium_execution_evidence", evidence)):
            raise _error(
                "INVALID_SCHEMA",
                "Xcelium executor produced invalid evidence",
                "evidence", "XCELIUM_EXECUTION_EVIDENCE")
        return evidence

    def _verify_replay(
            self, request: Mapping[str, Any], evidence: Mapping[str, Any]
            ) -> None:
        if (not accepted(validate("xcelium_execution_request", request)) or
                not accepted(validate("xcelium_execution_evidence", evidence))):
            raise _error(
                "STALE_EVIDENCE", "Xcelium replay contract is invalid",
                "replay", "XCELIUM_REPLAY_EVIDENCE")
        request_projection = {
            key: value for key, value in request.items()
            if key != "request_fingerprint"}
        evidence_projection = {
            key: value for key, value in evidence.items()
            if key != "evidence_fingerprint"}
        if (request["request_fingerprint"] != canonical_hash(request_projection) or
                evidence["evidence_fingerprint"] != canonical_hash(
                    evidence_projection) or
                evidence["request_id"] != request["request_id"] or
                evidence["request_fingerprint"] !=
                    request["request_fingerprint"] or
                request["job_id"] != self.job_id or
                evidence["job_id"] != self.job_id or
                request["environment_fingerprint"] !=
                    self.environment_fingerprint or
                evidence["environment_fingerprint"] !=
                    self.environment_fingerprint):
            raise _error(
                "STALE_EVIDENCE", "Xcelium replay lineage is stale",
                "replay", "XCELIUM_REPLAY_EVIDENCE")
        output_digest = self._tree_digest(self._output_dir(request))
        if (output_digest.fingerprint != evidence["output_tree_fingerprint"] or
                output_digest.size_bytes != evidence["output_tree_size_bytes"] or
                output_digest.file_count != evidence["output_tree_file_count"]):
            raise _error(
                "STALE_EVIDENCE", "Xcelium replay output tree drift",
                request["output_subdir"], "XCELIUM_REPLAY_EVIDENCE")
        for item in [*evidence["logs"], *evidence["artifacts"]]:
            path = (self.job_root / item["relative_path"]).resolve()
            try:
                path.relative_to(self.job_root / "runs" / "xcelium")
            except ValueError as caught:
                raise _error(
                    "TOOL_PERMISSION_DENIED",
                    "Xcelium replay artifact escapes the Job",
                    item["relative_path"],
                    "XCELIUM_REPLAY_EVIDENCE") from caught
            digest = self._tree_digest(path)
            if (digest.fingerprint != item["fingerprint"] or
                    digest.size_bytes != item["size_bytes"] or
                    ("file_count" in item and
                     digest.file_count != item["file_count"])):
                raise _error(
                    "STALE_EVIDENCE", "Xcelium replay artifact drift",
                    item["relative_path"], "XCELIUM_REPLAY_EVIDENCE")

    def _run_request(self, request: dict[str, Any]) -> XceliumExecutionResult:
        token = request["request_id"].removeprefix(
            "XCELIUM.REQUEST.").casefold()
        request_relative = "audit/xcelium/{}.request.json".format(token)
        evidence_relative = "audit/xcelium/{}.evidence.json".format(token)
        request_path = self.job_root / request_relative
        evidence_path = self.job_root / evidence_relative
        if request_path.exists() or evidence_path.exists():
            if not request_path.is_file() or not evidence_path.is_file():
                raise _error(
                    "STALE_EVIDENCE",
                    "Xcelium replay request/evidence pair is incomplete",
                    "replay", "XCELIUM_REPLAY_EVIDENCE")
            existing_request = load_document(request_path)
            evidence = load_document(evidence_path)
            if existing_request != request:
                raise _error(
                    "CONFLICTING_REPLAY",
                    "Xcelium execution ID is bound to another request",
                    "request", "XCELIUM_REPLAY_EVIDENCE")
            self._verify_replay(existing_request, evidence)
            return XceliumExecutionResult(
                copy.deepcopy(request), copy.deepcopy(evidence),
                request_relative, evidence_relative, True)
        _immutable_json(request_path, request)
        evidence = self._execute_process(request)
        _immutable_json(evidence_path, evidence)
        return XceliumExecutionResult(
            copy.deepcopy(request), copy.deepcopy(evidence),
            request_relative, evidence_relative, False)

    def probe_version(
            self, execution_id: str = "VERSION") -> XceliumExecutionResult:
        return self._run_request(self._request("VERSION", execution_id))

    def build_only(
            self, execution_id: str,
            configuration: XceliumRunConfiguration) -> XceliumExecutionResult:
        return self._run_request(
            self._request("BUILD", execution_id, configuration))

    def compile_only(
            self, execution_id: str,
            configuration: XceliumRunConfiguration) -> XceliumExecutionResult:
        """Compile an exact source set without selecting or elaborating a top."""
        return self._run_request(
            self._request("COMPILE", execution_id, configuration))

    def run(
            self, execution_id: str,
            configuration: XceliumRunConfiguration) -> XceliumExecutionResult:
        return self._run_request(
            self._request("RUN", execution_id, configuration))

    def merge_coverage(
            self, execution_id: str, coverage_directories: Sequence[str]
            ) -> dict[str, Any]:
        """Use the colocated trusted IMC binary to merge one exact suite.

        This is intentionally part of the Xcelium adapter rather than a second
        EDA execution path.  The generated Tcl is fixed framework text and all
        paths are Job-confined coverage databases produced by this adapter.
        """
        if (not _EXECUTION_ID.fullmatch(execution_id) or
                not coverage_directories or
                len(set(coverage_directories)) != len(coverage_directories)):
            raise _error("INVALID_SCHEMA", "Xcelium coverage request is invalid",
                         "coverage_directories")
        imc = shutil.which("imc", path=self._private_environment.get("PATH"))
        if imc is None or not Path(imc).is_file() or not os.access(imc, os.X_OK):
            return {"status": "UNAVAILABLE", "report_paths": [],
                    "categories": {name: "unavailable" for name in (
                        "statement", "branch", "toggle", "expression", "fsm",
                        "assertion", "functional")}}
        roots: list[Path] = []
        for relative in coverage_directories:
            path = (self.job_root / relative).resolve()
            try:
                path.relative_to(self.job_root / "runs" / "xcelium")
            except ValueError as caught:
                raise _error("TOOL_PERMISSION_DENIED",
                             "coverage database escapes current Job", relative,
                             "XCELIUM_COVERAGE") from caught
            if not path.is_dir() or path.is_symlink():
                raise _error("STALE_EVIDENCE", "coverage database is unavailable",
                             relative, "XCELIUM_COVERAGE")
            # Tcl braces safely preserve spaces; reject its one delimiter.
            if any(character in str(path) for character in "{}\n\r"):
                raise _error("TOOL_PERMISSION_DENIED", "coverage path is unsafe",
                             relative, "XCELIUM_COVERAGE")
            roots.append(path)
        output = (self.job_root / "runs" / "xcelium" /
                  "coverage_{}".format(execution_id.casefold())).resolve()
        try:
            output.relative_to(self.job_root / "runs" / "xcelium")
        except ValueError as caught:
            raise _error("TOOL_PERMISSION_DENIED", "coverage output escapes Job",
                         "execution_id", "XCELIUM_COVERAGE") from caught
        if output.exists():
            raise _error("STALE_EVIDENCE", "coverage output already exists",
                         "execution_id", "XCELIUM_COVERAGE")
        output.mkdir(parents=True)
        merged = output / "merged_cov"
        text_report = output / "coverage.txt"
        html_report = output / "coverage_html"
        script = output / "coverage.imc.tcl"
        script.write_text("\n".join([
            "merge {} -out {{{}}}".format(
                " ".join("{{{}}}".format(path) for path in roots), merged),
            "load -run {{{}}}".format(merged),
            "report -detail -metrics all -out {{{}}}".format(text_report),
            "report -html -out {{{}}}".format(html_report), "exit", "",
        ]), encoding="utf-8")
        environment = copy.deepcopy(self._private_environment)
        home = output / "home"
        temporary = output / "tmp"
        home.mkdir()
        temporary.mkdir()
        environment.update({"HOME": str(home), "TMPDIR": str(temporary),
                            "LC_ALL": environment.get("LC_ALL", "C")})
        process = subprocess.run(
            [str(Path(imc).resolve()), "-exec", str(script)], cwd=str(output),
            env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, shell=False, timeout=self.timeout_seconds,
            preexec_fn=self._preexec(self.resource_limits["cpu_seconds"],
                                     self.resource_limits["memory_mb"]),
            check=False)
        stdout = output / "imc.stdout.log"
        stderr = output / "imc.stderr.log"
        stdout.write_text(self._sanitize(process.stdout.decode("utf-8", "replace")),
                          encoding="utf-8")
        stderr.write_text(self._sanitize(process.stderr.decode("utf-8", "replace")),
                          encoding="utf-8")
        if process.returncode != 0 or not text_report.is_file():
            return {"status": "UNAVAILABLE", "report_paths": [
                stdout.relative_to(self.job_root).as_posix(),
                stderr.relative_to(self.job_root).as_posix()],
                    "categories": {name: "unavailable" for name in (
                        "statement", "branch", "toggle", "expression", "fsm",
                        "assertion", "functional")}}
        report_text = text_report.read_text(encoding="utf-8", errors="replace")
        categories: dict[str, Any] = {name: "unavailable" for name in (
            "statement", "branch", "toggle", "expression", "fsm", "assertion",
            "functional")}
        for name in categories:
            match = re.search(r"(?i)\\b{}\\b[^%\\n]*?([0-9]+(?:\\.[0-9]+)?)%".format(name),
                              report_text)
            if match is not None:
                categories[name] = {"status": "available",
                                    "percent": float(match.group(1))}
        reports = [stdout.relative_to(self.job_root).as_posix(),
                   stderr.relative_to(self.job_root).as_posix(),
                   text_report.relative_to(self.job_root).as_posix()]
        if html_report.is_dir() and not html_report.is_symlink():
            reports.extend(path.relative_to(self.job_root).as_posix()
                           for path in html_report.rglob("*") if path.is_file())
        return {"status": "AVAILABLE", "report_paths": sorted(reports),
                "categories": categories}


__all__ = [
    "APPROVED_EXTERNAL_ENVIRONMENT_NAMES",
    "DEFAULT_RESOURCE_LIMITS",
    "DEFAULT_VERSION_PATTERN",
    "XCELIUM_ADAPTER_VERSION",
    "XCELIUM_EXECUTABLE_REF",
    "XCELIUM_QUALIFICATION_SCOPE",
    "XceliumAdapter",
    "XceliumExecutionResult",
    "XceliumRunConfiguration",
]
