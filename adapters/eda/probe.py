"""Typed, fail-closed process boundary for approved EDA capability probes."""
from __future__ import annotations

import copy
import hashlib
import os
import re
import resource
import signal
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from adapters.eda.boundary import (
    FORBIDDEN,
    SAFE_VALUE,
    TrustedEdaBoundaryError,
    TrustedExecutableRegistry,
)
from contracts.validator import (
    accepted,
    diagnostic,
    eda_probe_evidence_fingerprint,
    validate,
)


ABSOLUTE_PATH = re.compile(r"/(?:[^ \t\r\n:'\";,])+")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _probe_error(code: str, message: str, path: str,
                 artifact: str = "EDA_PROBE_REQUEST"):
    return TrustedEdaBoundaryError(message, [diagnostic(
        code, message, path, required_owner="EDA_OWNER",
        required_artifact_kind=artifact)])


class TrustedEdaProbeExecutor:
    """Execute schema-valid probes without shell or public path disclosure."""

    def __init__(
            self, registry: TrustedExecutableRegistry,
            workspace_root: Path, result_root: Path, job_id: str,
            environment_fingerprint: str,
            private_environment: dict[str, str]):
        self.registry = registry
        self.workspace_root = Path(workspace_root).resolve()
        self.result_root = Path(result_root).resolve()
        self.job_id = job_id
        self.job_root = (self.result_root / "jobs" / job_id).resolve()
        if (self.result_root.name != "result" or
                self.job_root.parent != self.result_root / "jobs" or
                not re.fullmatch(r"JOB\.[A-Z0-9_.-]+", job_id) or
                not re.fullmatch(r"[0-9a-f]{64}",
                                 environment_fingerprint)):
            raise _probe_error(
                "TOOL_PERMISSION_DENIED",
                "Trusted probe output boundary is invalid",
                "job_root", "EDA_RESULT_POLICY")
        allowed_names = {"CXX", "LC_ALL", "PATH", "LD_LIBRARY_PATH"}
        if (set(private_environment) - allowed_names or
                not all(isinstance(value, str) and value
                        for value in private_environment.values())):
            raise _probe_error(
                "TOOL_PERMISSION_DENIED",
                "Trusted probe environment contains a non-allowlisted entry",
                "environment", "APPROVED_EDA_ENVIRONMENT")
        self.environment_fingerprint = environment_fingerprint
        self._environment = copy.deepcopy(private_environment)
        self._cache: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _preexec(cpu_seconds: int, memory_mb: int):
        def apply_limits():
            resource.setrlimit(
                resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
            memory = memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        return apply_limits

    def _safe_literal(self, value: str) -> str:
        if (not SAFE_VALUE.fullmatch(value) or
                any(item in value for item in FORBIDDEN) or
                Path(value).is_absolute() or ".." in Path(value).parts):
            raise _probe_error(
                "TOOL_PERMISSION_DENIED",
                "Probe argv literal contains forbidden syntax",
                "argv_template", "EDA_ARGV_CONTRACT")
        return value

    def _source(self, relative: str, expected: str) -> str:
        relative = self._safe_literal(relative)
        path = (self.workspace_root / relative).resolve()
        try:
            path.relative_to(self.workspace_root)
        except ValueError as error:
            raise _probe_error(
                "TOOL_PERMISSION_DENIED",
                "Probe source escapes the workspace",
                "source_fingerprints",
                "APPROVED_PROBE_SOURCE") from error
        if not path.is_file() or _sha256_file(path) != expected:
            raise _probe_error(
                "STALE_EVIDENCE",
                "Probe source is missing or fingerprint-stale",
                relative, "APPROVED_PROBE_SOURCE")
        return str(path)

    def _output_dir(self, request: dict[str, Any]) -> Path:
        output = (self.job_root / request["output_subdir"]).resolve()
        try:
            output.relative_to(self.job_root / "runs")
        except ValueError as error:
            raise _probe_error(
                "TOOL_PERMISSION_DENIED",
                "Probe output escapes the current Job runs area",
                "output_subdir", "EDA_RESULT_POLICY") from error
        return output

    def _materialize(
            self, request: dict[str, Any],
            executable: str, output: Path) -> list[str]:
        source_by_path = {
            item["path"]: item["fingerprint"]
            for item in request["source_fingerprints"]}
        argv = [executable]
        for token in request["argv_template"]:
            kind, value = token["kind"], token["value"]
            if kind == "LITERAL":
                argv.append(self._safe_literal(value))
            elif kind == "SOURCE":
                argv.append(self._source(value, source_by_path[value]))
            elif kind == "OUTPUT_DIR":
                if value != "output_dir":
                    raise _probe_error(
                        "INVALID_SCHEMA",
                        "Unknown output directory token",
                        "argv_template", "EDA_ARGV_CONTRACT")
                argv.append(str(output))
            else:
                raise _probe_error(
                    "INVALID_SCHEMA", "Unsupported probe argv token",
                    "argv_template", "EDA_ARGV_CONTRACT")
        return argv

    def _sanitize(self, text: str, private_values: list[str]) -> str:
        sanitized = text.replace(
            str(self.workspace_root) + os.sep, "WORKSPACE::")
        sanitized = sanitized.replace(
            str(self.result_root) + os.sep, "RESULT::")
        protected = {}
        for index, match in enumerate(re.findall(
                r"(?:WORKSPACE|RESULT)::[^ \t\r\n:'\";,]+",
                sanitized)):
            token = "__APPROVED_RELATIVE_PATH_{}__".format(index)
            protected[token] = match
            sanitized = sanitized.replace(match, token)
        for value in sorted(
                {item for item in private_values
                 if len(item) >= 8 and
                 item not in {str(self.workspace_root),
                              str(self.result_root)}},
                key=len, reverse=True):
            sanitized = sanitized.replace(value, "[REDACTED_PRIVATE_VALUE]")
        sanitized = ABSOLUTE_PATH.sub("[REDACTED_PATH]", sanitized)
        for token, value in protected.items():
            sanitized = sanitized.replace(token, value)
        return sanitized

    def _verify_cached(self, evidence: dict[str, Any]) -> None:
        if not accepted(validate("eda_probe_evidence", evidence)):
            raise _probe_error(
                "STALE_EVIDENCE", "Cached probe evidence is invalid",
                "evidence", "EDA_PROBE_EVIDENCE")
        for item in evidence["logs"] + evidence["artifacts"]:
            path = (self.job_root / item["relative_path"]).resolve()
            try:
                path.relative_to(self.job_root / "runs")
            except ValueError as error:
                raise _probe_error(
                    "TOOL_PERMISSION_DENIED",
                    "Cached artifact escapes the Job",
                    "evidence", "EDA_PROBE_EVIDENCE") from error
            if (not path.is_file() or
                    path.stat().st_size != item["size_bytes"] or
                    _sha256_file(path) != item["fingerprint"]):
                raise _probe_error(
                    "STALE_EVIDENCE",
                    "Cached probe artifact is missing or stale",
                    item["relative_path"], "EDA_PROBE_EVIDENCE")

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        if not accepted(validate("eda_probe_request", request)):
            raise _probe_error(
                "INVALID_SCHEMA", "EDA probe request is invalid",
                "request", "EDA_PROBE_REQUEST")
        if (request["job_id"] != self.job_id or
                request["environment_fingerprint"] !=
                self.environment_fingerprint):
            raise _probe_error(
                "STALE_EVIDENCE",
                "Probe Job or environment fingerprint drift",
                "request", "APPROVED_EDA_ENVIRONMENT")
        fingerprint = request["request_fingerprint"]
        source_by_path = {
            item["path"]: item["fingerprint"]
            for item in request["source_fingerprints"]}
        for relative, expected in source_by_path.items():
            self._source(relative, expected)
        if fingerprint in self._cache:
            evidence = copy.deepcopy(self._cache[fingerprint])
            self._verify_cached(evidence)
            return evidence

        executable = self.registry.resolve(
            request["executable_ref"],
            request["environment_fingerprint"])
        executable_path = Path(executable)
        if not executable_path.is_file() or \
                not os.access(executable, os.X_OK):
            raise _probe_error(
                "BLOCKED_TOOL",
                "Approved executable is unavailable",
                "executable_ref", "APPROVED_EXECUTABLE_REGISTRY")
        output = self._output_dir(request)
        if output.exists():
            raise _probe_error(
                "STALE_EVIDENCE",
                "Probe output already exists without replay evidence",
                "output_subdir", "EDA_REPLAY_EVIDENCE")
        output.mkdir(parents=True)
        argv = self._materialize(request, executable, output)
        limits = request["resource_limits"]
        started = _utc()
        timed_out = False
        exit_code = -1
        stdout = b""
        stderr = b""
        process = subprocess.Popen(
            argv, cwd=str(output), env=self._environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, shell=False, start_new_session=True,
            preexec_fn=self._preexec(
                limits["cpu_seconds"], limits["memory_mb"]))
        try:
            stdout, stderr = process.communicate(
                timeout=request["timeout_seconds"])
            exit_code = int(process.returncode)
        except subprocess.TimeoutExpired as error:
            timed_out = True
            stdout = error.stdout or b""
            stderr = error.stderr or b""
            os.killpg(process.pid, signal.SIGKILL)
            trailing_out, trailing_err = process.communicate()
            stdout += trailing_out or b""
            stderr += trailing_err or b""
            exit_code = -signal.SIGKILL
        ended = _utc()
        private = [executable, str(self.workspace_root),
                   str(self.result_root), *self._environment.values()]
        stdout_text = self._sanitize(
            stdout.decode("utf-8", "replace"), private)
        stderr_text = self._sanitize(
            stderr.decode("utf-8", "replace"), private)
        stdout_path = output / "stdout.log"
        stderr_path = output / "stderr.log"
        stdout_path.write_text(stdout_text, encoding="utf-8")
        stderr_path.write_text(stderr_text, encoding="utf-8")

        diagnostics: list[str] = []
        status = "PASS"
        if timed_out:
            diagnostics.append("PROCESS_TIMEOUT")
            status = "BLOCKED_TOOL"
        elif exit_code != 0:
            diagnostics.append("PROCESS_EXIT_NONZERO")
            status = "FAIL"
            combined = stdout_text + stderr_text
            if ("error while loading shared libraries" in combined or
                    "cannot open shared object file" in combined):
                diagnostics.append("RUNTIME_DEPENDENCY_MISSING")
                status = "BLOCKED_TOOL"
            elif (request["probe_kind"] in {"FST_BUILD", "FST_RUN"} and
                    "lz4.h" in (stdout_text + stderr_text)):
                diagnostics.append("OPTIONAL_DEPENDENCY_MISSING")
                status = "BLOCKED_TOOL"

        process_files = [
            item for item in output.rglob("*") if item.is_file()]
        if len(process_files) > limits["output_files"]:
            diagnostics.append("OUTPUT_FILE_LIMIT")
            status = "FAIL"

        artifacts = []
        for rule in request["expected_artifacts"]:
            path = (output / rule["relative_path"]).resolve()
            try:
                path.relative_to(output)
            except ValueError as error:
                raise _probe_error(
                    "TOOL_PERMISSION_DENIED",
                    "Expected artifact escapes probe output",
                    "expected_artifacts",
                    "EDA_ARTIFACT_DISCOVERY") from error
            if (not path.is_file() or
                    path.stat().st_size < rule["minimum_bytes"]):
                if rule["required"]:
                    diagnostics.append("MISSING_ARTIFACT")
                    if status != "BLOCKED_TOOL":
                        status = "FAIL"
                continue
            relative = path.relative_to(self.job_root).as_posix()
            artifacts.append({
                "artifact_id": "ART.EDA.{}".format(
                    hashlib.sha256(
                        (request["request_id"] + relative).encode()
                    ).hexdigest()[:16].upper()),
                "kind": rule["kind"],
                "relative_path": relative,
                "fingerprint": _sha256_file(path),
                "size_bytes": path.stat().st_size,
            })

        logs = []
        for kind, path in (("STDERR", stderr_path), ("STDOUT", stdout_path)):
            logs.append({
                "kind": kind,
                "relative_path": path.relative_to(
                    self.job_root).as_posix(),
                "fingerprint": _sha256_file(path),
                "size_bytes": path.stat().st_size,
            })
        logs.sort(key=lambda item: item["relative_path"])
        artifacts.sort(key=lambda item: item["artifact_id"])
        evidence = {
            "schema_version": "1.0",
            "evidence_id": "EVIDENCE.EDA.{}".format(
                hashlib.sha256(fingerprint.encode()).hexdigest()[
                    :16].upper()),
            "request_id": request["request_id"],
            "request_fingerprint": fingerprint,
            "job_id": request["job_id"],
            "probe_kind": request["probe_kind"],
            "executable_ref": request["executable_ref"],
            "environment_fingerprint":
                request["environment_fingerprint"],
            "started_at": started,
            "ended_at": ended,
            "exit_code": exit_code,
            "execution_status": status,
            "timed_out": timed_out,
            "logs": logs,
            "artifacts": artifacts,
            "diagnostic_codes": sorted(set(diagnostics)),
            "evidence_class": "REAL_EDA_QUALIFICATION",
            "qualification_scope": (
                "SYNTHETIC_TICKET_GATE_LINKED_RERUN"
                if request["probe_kind"].startswith("G005_")
                else (
                    "SYNTHETIC_TICKET_GATE_ARTIFACT_DEBUG"
                    if request["probe_kind"].startswith("G004_")
                    else (
                        "SYNTHETIC_TICKET_GATE_SMOKE"
                        if request["probe_kind"].startswith("G003_")
                        else (
                            "F003_MODEL_BUILD_QUALIFICATION"
                            if request["probe_kind"].startswith("G002_")
                            else (
                                "PROJECT_VERILATOR_EXECUTION"
                                if request["probe_kind"].startswith("PROJECT_")
                                else "VERILATOR_G001_CAPABILITY"))))),
            "evidence_fingerprint": "0" * 64,
        }
        evidence["evidence_fingerprint"] = \
            eda_probe_evidence_fingerprint(evidence)
        if not accepted(validate("eda_probe_evidence", evidence)):
            raise _probe_error(
                "INVALID_SCHEMA",
                "Trusted executor produced invalid probe evidence",
                "evidence", "EDA_PROBE_EVIDENCE")
        self._cache[fingerprint] = copy.deepcopy(evidence)
        return evidence

    def replay(
            self, request: dict[str, Any],
            evidence: dict[str, Any]) -> dict[str, Any]:
        """Load one exact persisted probe result into the replay cache."""
        if not accepted(validate("eda_probe_request", request)):
            raise _probe_error(
                "INVALID_SCHEMA",
                "Replay probe request is invalid",
                "request", "EDA_PROBE_REQUEST")
        if (
            request["job_id"] != self.job_id or
            request["environment_fingerprint"] !=
                self.environment_fingerprint or
            evidence.get("request_id") != request["request_id"] or
            evidence.get("request_fingerprint") !=
                request["request_fingerprint"] or
            evidence.get("job_id") != self.job_id or
            evidence.get("environment_fingerprint") !=
                self.environment_fingerprint
        ):
            raise _probe_error(
                "STALE_EVIDENCE",
                "Persisted probe evidence does not match exact request",
                "evidence", "EDA_REPLAY_EVIDENCE")
        self._verify_cached(evidence)
        fingerprint = request["request_fingerprint"]
        existing = self._cache.get(fingerprint)
        if existing is not None and existing != evidence:
            raise _probe_error(
                "STALE_EVIDENCE",
                "Replay cache contains conflicting evidence",
                "evidence", "EDA_REPLAY_EVIDENCE")
        self._cache[fingerprint] = copy.deepcopy(evidence)
        return self.execute(request)
