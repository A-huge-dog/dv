"""Generic trusted Verilator execution for an approved Project Job."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from adapters.eda.boundary import (
    DEFAULT_LIMITS,
    TrustedEdaBoundaryError,
    TrustedExecutableRegistry,
    approved_environment,
)
from adapters.eda.probe import TrustedEdaProbeExecutor
from contracts.validator import (
    accepted,
    eda_probe_request_fingerprint,
    validate,
)
from infrastructure.persistence.atomic_artifact import publish_immutable_text


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _immutable_json(path: Path, value: dict[str, Any]) -> None:
    encoded = json.dumps(
        value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    publish_immutable_text(
        path, encoded, ValueError,
        "immutable Project EDA evidence conflict")


class ProjectVerilatorRunner:
    """Build and run exact approved sources without invoking a shell."""

    def __init__(
            self, workspace_root: Path, result_root: Path, job_id: str,
            environment_fingerprint: str, timeout_seconds: int):
        self.workspace_root = Path(workspace_root).resolve()
        self.result_root = Path(result_root).resolve()
        self.job_id = job_id
        self.job_root = self.result_root / "jobs" / job_id
        verilator = shutil.which("verilator")
        make = shutil.which("make")
        cxx = shutil.which(os.environ.get("CXX", "g++"))
        if not all((verilator, make, cxx)):
            raise ValueError(
                "Verilator deployment environment is not preloaded")
        actual_fingerprint, private_environment = approved_environment(
            str(verilator), str(make), str(cxx),
            os.environ.get("LD_LIBRARY_PATH"))
        if actual_fingerprint != environment_fingerprint:
            raise ValueError(
                "approved Verilator environment fingerprint drift")
        registry = TrustedExecutableRegistry([{
            "executable_ref": "EDAEXEC.VERILATOR",
            "resolved_path": str(Path(verilator).resolve()),
            "environment_fingerprint": actual_fingerprint,
            "approved": True,
        }])
        self.registry = registry
        self.executor = TrustedEdaProbeExecutor(
            registry, self.workspace_root, self.result_root, job_id,
            actual_fingerprint, private_environment)
        self.environment_fingerprint = actual_fingerprint
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _token(kind: str, value: str) -> dict[str, str]:
        return {"kind": kind, "value": value}

    def _request(
            self, kind: str, executable_ref: str,
            argv: list[dict[str, str]], sources: list[dict[str, str]],
            approval_ref: str,
            expected_artifacts: list[dict[str, Any]],
            timeout_seconds: int | None = None, *,
            request_suffix: str | None = None,
            output_subdir: str | None = None) -> dict[str, Any]:
        suffix = request_suffix or (
            "BUILD" if kind == "PROJECT_BUILD" else "RUN")
        request = {
            "schema_version": "1.0",
            "request_id": "EDAPROBE.{}.{}".format(
                self.job_id.removeprefix("JOB."), suffix),
            "job_id": self.job_id,
            "probe_kind": kind,
            "executable_ref": executable_ref,
            "argv_template": copy.deepcopy(argv),
            "environment_fingerprint": self.environment_fingerprint,
            "timeout_seconds":
                timeout_seconds or self.timeout_seconds,
            "resource_limits": copy.deepcopy(DEFAULT_LIMITS),
            "output_subdir": output_subdir or
                "runs/main/{}".format(suffix.casefold()),
            "expected_artifacts": copy.deepcopy(expected_artifacts),
            "source_fingerprints": copy.deepcopy(sources),
            "approval_ref": approval_ref,
            "request_fingerprint": "0" * 64,
        }
        request["request_fingerprint"] = \
            eda_probe_request_fingerprint(request)
        if not accepted(validate("eda_probe_request", request)):
            raise ValueError("generated Project EDA request is invalid")
        return request

    def build_only(
            self, source_paths: list[str], top: str, approval_ref: str,
            authority_fingerprint: str, *,
            purpose: str = "oches003") -> dict[str, Any]:
        """Compile one exact uncommitted source set and persist replay evidence."""
        if purpose not in {"oches003", "stage3"}:
            raise ValueError("unsupported Project build purpose")
        token = authority_fingerprint[:24]
        sources = sorted([{
            "path": relative,
            "fingerprint": _sha256(self.workspace_root / relative),
        } for relative in source_paths], key=lambda item: item["path"])
        argv = [
            self._token("LITERAL", "--binary"),
            self._token("LITERAL", "--timing"),
            self._token("LITERAL", "-Wall"),
            self._token("LITERAL", "-Wno-fatal"),
            self._token("LITERAL", "--Mdir"),
            self._token("OUTPUT_DIR", "output_dir"),
            *[self._token("SOURCE", item) for item in source_paths],
            self._token("LITERAL", "--top-module"),
            self._token("LITERAL", top),
            self._token("LITERAL", "-o"),
            self._token("LITERAL", "precommit_test"),
        ]
        stage3 = purpose == "stage3"
        request = self._request(
            "PROJECT_BUILD", "EDAEXEC.VERILATOR", argv, sources,
            approval_ref, [{
                "kind": "EXECUTABLE",
                "relative_path": "precommit_test",
                "required": True,
                "minimum_bytes": 1,
            }], timeout_seconds=max(self.timeout_seconds, 60),
            request_suffix="{}.{}".format(
                "STAGE3" if stage3 else "PRECOMMIT", token.upper()),
            output_subdir="runs/{}/{}/build".format(
                "stage3" if stage3 else "oches003", token))
        audit_prefix = "stage3_eda" if stage3 else "oches003_eda"
        request_path = self.job_root / (
            "audit/{}_request.{}.json".format(audit_prefix, token))
        evidence_path = self.job_root / (
            "audit/{}_evidence.{}.json".format(audit_prefix, token))
        if evidence_path.exists():
            existing_request = json.loads(request_path.read_text(encoding="utf-8"))
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            if existing_request != request or not accepted(validate(
                    "eda_probe_evidence", evidence)) or \
                    evidence.get("request_fingerprint") != \
                        request["request_fingerprint"]:
                raise ValueError("stale Project compile replay evidence")
            self.executor._verify_cached(evidence)
            return {"request": request, "evidence": evidence}
        if request_path.exists():
            existing_request = json.loads(request_path.read_text(encoding="utf-8"))
            if existing_request != request:
                raise ValueError("conflicting Project compile request")
        else:
            _immutable_json(request_path, request)
        evidence = self.executor.execute(request)
        _immutable_json(evidence_path, evidence)
        return {"request": request, "evidence": evidence}

    def run(
            self, source_paths: list[str], top: str,
            approval_ref: str) -> dict[str, Any]:
        sources = sorted([{
            "path": relative,
            "fingerprint": _sha256(self.workspace_root / relative),
        } for relative in source_paths], key=lambda item: item["path"])
        argv = [
            self._token("LITERAL", "--binary"),
            self._token("LITERAL", "--timing"),
            self._token("LITERAL", "--trace"),
            self._token("LITERAL", "--coverage"),
            self._token("LITERAL", "-Wall"),
            self._token("LITERAL", "--Mdir"),
            self._token("OUTPUT_DIR", "output_dir"),
            *[self._token("SOURCE", item) for item in source_paths],
            self._token("LITERAL", "--top-module"),
            self._token("LITERAL", top),
            self._token("LITERAL", "-o"),
            self._token("LITERAL", "project_test"),
        ]
        build_request = self._request(
            "PROJECT_BUILD", "EDAEXEC.VERILATOR", argv, sources,
            approval_ref, [{
                "kind": "EXECUTABLE",
                "relative_path": "project_test",
                "required": True,
                "minimum_bytes": 1,
            }], timeout_seconds=max(self.timeout_seconds, 60))
        build_evidence = self.executor.execute(build_request)
        run_request: dict[str, Any] | None = None
        run_evidence: dict[str, Any] | None = None
        if build_evidence["execution_status"] == "PASS":
            binary = self.job_root / next(
                item["relative_path"]
                for item in build_evidence["artifacts"]
                if item["kind"] == "EXECUTABLE")
            model_ref = "EDAEXEC.{}.PROJECT_TEST".format(
                self.job_id.removeprefix("JOB."))
            self.registry.approve_generated(
                model_ref, binary, self.environment_fingerprint,
                self.job_root)
            run_request = self._request(
                "PROJECT_RUN", model_ref, [], sources, approval_ref, [{
                    "kind": "TRACE_VCD",
                    "relative_path": "project.vcd",
                    "required": True,
                    "minimum_bytes": 1,
                }, {
                    "kind": "COVERAGE",
                    "relative_path": "coverage.dat",
                    "required": False,
                    "minimum_bytes": 1,
                }])
            run_evidence = self.executor.execute(run_request)
        bundle = {
            "schema_version": "1.0",
            "job_id": self.job_id,
            "build_request": build_request,
            "build_evidence": build_evidence,
            "run_request": run_request,
            "run_evidence": run_evidence,
        }
        _immutable_json(
            self.job_root / "reports/project/eda_evidence.json",
            bundle)
        return bundle


__all__ = ["ProjectVerilatorRunner", "TrustedEdaBoundaryError"]
