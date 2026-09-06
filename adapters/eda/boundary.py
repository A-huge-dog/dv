"""Trusted primitives shared by the AXI-Lite Project Verilator runner."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from contracts.validator import diagnostic
from scripts.dvlib import canonical_hash


SAFE_VALUE = re.compile(r"^[A-Za-z0-9_./:+@=-]+$")
FORBIDDEN = ("\n", "\r", ";", "|", "&&", "$(", "`", ">", "<")
ENVIRONMENT_ID = "EDAENV.VERILATOR.5_050.GCC13"
ENVIRONMENT_IDENTITY = "VERILATOR_5_050_GCC13_PRELOADED"
DEFAULT_LIMITS = {
    "cpu_seconds": 600,
    "memory_mb": 4096,
    "output_files": 5000,
}


class TrustedEdaBoundaryError(ValueError):
    def __init__(self, message: str, diagnostics: list[dict[str, Any]]):
        super().__init__(message)
        self.diagnostics = list(diagnostics)


def _error(code: str, message: str, path: str, artifact: str):
    return TrustedEdaBoundaryError(message, [diagnostic(
        code, message, path, required_owner="DV_OWNER",
        required_artifact_kind=artifact)])


class TrustedExecutableRegistry:
    """Resolve only EDA-owner-approved opaque executable references."""

    def __init__(self, entries: list[dict[str, Any]]):
        self._entries: dict[str, dict[str, str]] = {}
        for entry in entries:
            reference = entry.get("executable_ref")
            path = Path(str(entry.get("resolved_path", "")))
            fingerprint = str(entry.get("environment_fingerprint", ""))
            if (not isinstance(reference, str) or
                    not reference.startswith("EDAEXEC.") or
                    reference in self._entries or
                    not path.is_absolute() or
                    entry.get("approved") is not True or
                    not re.fullmatch(r"[0-9a-f]{64}", fingerprint)):
                raise _error(
                    "INVALID_SCHEMA",
                    "Trusted executable registry entry is invalid",
                    "executable_registry",
                    "APPROVED_EXECUTABLE_REGISTRY")
            self._entries[reference] = {
                "resolved_path": str(path),
                "environment_fingerprint": fingerprint,
            }

    def resolve(self, reference: str, environment_fingerprint: str) -> str:
        entry = self._entries.get(reference)
        if entry is None:
            raise _error(
                "TOOL_PERMISSION_DENIED",
                "Opaque executable reference is not approved",
                "executable_ref",
                "APPROVED_EXECUTABLE_REGISTRY")
        if entry["environment_fingerprint"] != environment_fingerprint:
            raise _error(
                "STALE_EVIDENCE",
                "Executable registry environment fingerprint drift",
                "environment_fingerprint",
                "APPROVED_EDA_ENVIRONMENT")
        return entry["resolved_path"]

    def approve_generated(
            self, reference: str, resolved_path: Path,
            environment_fingerprint: str, job_root: Path) -> None:
        path = Path(resolved_path).resolve()
        root = Path(job_root).resolve()
        try:
            path.relative_to(root)
        except ValueError as error:
            raise _error(
                "TOOL_PERMISSION_DENIED",
                "Generated executable escapes the approved Job",
                "generated_executable",
                "APPROVED_GENERATED_EXECUTABLE") from error
        if (not re.fullmatch(r"EDAEXEC\.[A-Z0-9_.-]+", reference) or
                reference in self._entries or
                not path.is_file() or
                not os.access(str(path), os.X_OK) or
                not re.fullmatch(r"[0-9a-f]{64}", environment_fingerprint)):
            raise _error(
                "INVALID_SCHEMA",
                "Generated executable registry entry is invalid",
                "generated_executable",
                "APPROVED_GENERATED_EXECUTABLE")
        self._entries[reference] = {
            "resolved_path": str(path),
            "environment_fingerprint": environment_fingerprint,
        }


def approved_environment(
        verilator_path: str, make_path: str, cxx_path: str,
        loader_library_path: str | None = None,
) -> tuple[str, dict[str, str]]:
    """Build the private allowlisted environment and its public fingerprint."""
    private = {
        "CXX": cxx_path,
        "LC_ALL": "C",
        "PATH": os.pathsep.join([
            str(Path(verilator_path).parent),
            str(Path(cxx_path).parent),
            str(Path(make_path).parent),
        ]),
    }
    if loader_library_path:
        private["LD_LIBRARY_PATH"] = loader_library_path
    fingerprint = canonical_hash({
        "environment_id": ENVIRONMENT_ID,
        "identity": ENVIRONMENT_IDENTITY,
        "allowlisted_names": sorted(private),
        "private_values": private,
    })
    return fingerprint, private
