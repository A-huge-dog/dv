"""Independent, checkpointed UVM generation and Xcelium gate.

The Framework validates only the provider protocol and fixed output slots in
this module.  It deliberately does not parse or judge SystemVerilog/UVM text;
the injected Xcelium build result is the sole candidate PASS/FAIL authority.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from agents.dv_worker_tools import bounded_xcelium_observation
from contracts.validator import accepted, load_document, validate
from domain.artifacts import artifact_fingerprint
from infrastructure.persistence.atomic_artifact import (
    publish_immutable_bytes, publish_immutable_text,
)
from scripts.dvlib import canonical_hash


UVM_GENERATION = "UVM_GENERATION"
UVM_WORKER_ACTION_BUDGET = 6
UVM_ELABORATION_TOP = "dv_uvm_generation_elaboration_top"


def load_effective_uvm_pass(
        job_root: Path, *, job_id: str, input_fingerprint: str,
        cycle_id: str, stage2_root: str,
        error: type[Exception]) -> tuple[tuple[dict[str, str], ...], str]:
    """Load one explicit cycle's unique PASS tree without scanning revisions."""
    audit = Path(job_root) / "audit/uvm_generation" / cycle_id
    previous = "NONE"
    terminal_checkpoint: dict[str, Any] | None = None
    for sequence in range(1, 1000):
        path = audit / "checkpoint-{:03d}.json".format(sequence)
        if not path.exists():
            break
        if not path.is_file() or path.is_symlink():
            raise error("STALE_EVIDENCE", "UVM PASS checkpoint is unsafe")
        value = load_document(path)
        if (not accepted(validate("uvm_generation_checkpoint", value)) or
                value.get("sequence") != sequence or
                value.get("job_id") != job_id or
                value.get("input_fingerprint") != input_fingerprint or
                value.get("cycle_id") != cycle_id or
                value.get("stage2_root") != stage2_root or
                value.get("previous_checkpoint_fingerprint") != previous or
                value.get("checkpoint_fingerprint") !=
                    artifact_fingerprint(value, "checkpoint_fingerprint")):
            raise error("STALE_EVIDENCE", "UVM PASS checkpoint is stale")
        terminal_checkpoint = value
        previous = value["checkpoint_fingerprint"]
    if (terminal_checkpoint is None or
            terminal_checkpoint.get("state") != "UVM_GENERATION_PASS"):
        raise error("PARTIAL_ARTIFACT", "Xcelium-PASS UVM cycle is unavailable")
    relative = terminal_checkpoint.get("candidate_path")
    candidate_path = Path(job_root) / str(relative)
    if not candidate_path.is_file() or candidate_path.is_symlink():
        raise error("PARTIAL_ARTIFACT", "passing UVM candidate is unavailable")
    candidate = load_document(candidate_path)
    if (not accepted(validate("uvm_generation_candidate", candidate)) or
            candidate.get("candidate_fingerprint") !=
            artifact_fingerprint(candidate, "candidate_fingerprint") or
            candidate.get("effective_uvm_root") !=
                terminal_checkpoint.get("effective_uvm_root") or
            candidate.get("stage2_root") != stage2_root):
        raise error("STALE_EVIDENCE", "passing UVM candidate is stale")
    files: list[dict[str, str]] = []
    for item in candidate.get("effective_files", []):
        path = Path(job_root) / item["path"]
        if not path.is_file() or path.is_symlink():
            raise error("STALE_EVIDENCE", "passing UVM bytes are missing")
        content = path.read_text(encoding="utf-8")
        fingerprint = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if fingerprint != item["fingerprint"]:
            raise error("STALE_EVIDENCE", "passing UVM bytes drifted")
        files.append({"logical_path": item["logical_path"],
                      "content": content, "fingerprint": fingerprint})
    root = canonical_hash([{
        "logical_path": item["logical_path"],
        "fingerprint": item["fingerprint"],
    } for item in files])
    if root != terminal_checkpoint["effective_uvm_root"]:
        raise error("STALE_EVIDENCE", "passing effective UVM root drifted")
    return tuple(files), root


@dataclass(frozen=True)
class UvmGenerationInput:
    project_input: dict[str, Any]
    job_root: Path
    spec_evidence: list[dict[str, Any]]
    stage2_artifact: dict[str, Any]
    logical_testcases: list[dict[str, Any]]
    stage2_shards: list[dict[str, Any]]
    cycle_id: str = "initial"
    cycle_kind: str = "initial"
    repair_revision: int | None = None
    baseline_files: tuple[dict[str, str], ...] | None = None
    baseline_root: str | None = None


@dataclass(frozen=True)
class UvmGenerationResult:
    state: str
    checkpoint: dict[str, Any]
    effective_files: tuple[dict[str, str], ...] = ()
    effective_uvm_root: str | None = None
    attempt: int = 0
    replayed: bool = False


@dataclass(frozen=True)
class UvmGenerationDependencies:
    error: type[Exception]
    run_xcelium: Callable[
        [dict[str, Any], Path, dict[str, Any]], Mapping[str, Any]
    ]


class UvmGenerationHandler:
    """Persist and validate evidence for the Continuous UVM Worker."""

    def __init__(self, dependencies: UvmGenerationDependencies):
        self.dependencies = dependencies

    @staticmethod
    def _sha(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def _immutable_bytes(self, path: Path, content: bytes) -> None:
        publish_immutable_bytes(
            path, content,
            lambda message: self.dependencies.error(
                "CONFLICTING_REPLAY", message),
            "immutable UVM generation bytes conflict")

    def _immutable_json(self, path: Path, value: Mapping[str, Any]) -> None:
        text = json.dumps(
            dict(value), sort_keys=True, indent=2, ensure_ascii=False) + "\n"
        publish_immutable_text(
            path, text,
            lambda message: self.dependencies.error(
                "CONFLICTING_REPLAY", message),
            "immutable UVM generation record conflicts")

    def _require_contract(self, kind: str, value: Mapping[str, Any]) -> None:
        if not accepted(validate(kind, dict(value))):
            raise self.dependencies.error(
                "INVALID_SCHEMA",
                "{} artifact contract is invalid".format(kind))

    @staticmethod
    def _safe_slot(path: str) -> str:
        pure = PurePosixPath(path)
        if (not isinstance(path, str) or not pure.parts or pure.is_absolute() or
                ".." in pure.parts or any(
                    part in {"", "."} or part.startswith(".")
                    for part in pure.parts)):
            raise ValueError("unsafe generated UVM slot")
        return pure.as_posix()

    def _baseline(
            self, command: UvmGenerationInput
            ) -> tuple[list[dict[str, str]], str]:
        if command.baseline_files is None:
            records = command.project_input["uvm_testcase_context"]["files"]
            files = [{
                "logical_path": str(item["logical_path"]),
                "content": str(item["content"]),
                "fingerprint": str(item["fingerprint"]),
            } for item in records]
        else:
            files = [copy.deepcopy(dict(item))
                     for item in command.baseline_files]
        paths: set[str] = set()
        for item in files:
            try:
                path = self._safe_slot(item["logical_path"])
                content = item["content"]
                fingerprint = item["fingerprint"]
            except (KeyError, TypeError, ValueError) as caught:
                raise self.dependencies.error(
                    "STALE_EVIDENCE", "UVM generation baseline is invalid") \
                    from caught
            if (path in paths or not isinstance(content, str) or
                    fingerprint != self._sha(content.encode("utf-8"))):
                raise self.dependencies.error(
                    "STALE_EVIDENCE", "UVM generation baseline is stale")
            paths.add(path)
            item["logical_path"] = path
        files.sort(key=lambda item: item["logical_path"])
        root = canonical_hash([{
            "logical_path": item["logical_path"],
            "fingerprint": item["fingerprint"],
        } for item in files])
        if command.baseline_root is not None and command.baseline_root != root:
            raise self.dependencies.error(
                "STALE_EVIDENCE", "repair UVM baseline root is stale")
        return files, root

    def _slots(self, command: UvmGenerationInput,
               baseline: Sequence[Mapping[str, str]]) -> tuple[str, ...]:
        try:
            slots = tuple(self._safe_slot(item) for item in
                          command.project_input["uvm_testcase_context"]
                          ["generated_files"])
        except (KeyError, TypeError, ValueError) as caught:
            raise self.dependencies.error(
                "INVALID_SCHEMA", "generated UVM slots are invalid") from caught
        if not slots or len(set(slots)) != len(slots):
            raise self.dependencies.error(
                "INVALID_SCHEMA", "generated UVM slots must be unique")
        baseline_paths = {item["logical_path"] for item in baseline}
        if any(item not in baseline_paths for item in slots):
            raise self.dependencies.error(
                "INVALID_SCHEMA", "generated UVM slot is absent from baseline")
        return slots

    @staticmethod
    def _stage2_root(command: UvmGenerationInput) -> str:
        value = command.stage2_artifact
        return str(value.get("artifact_fingerprint") or canonical_hash(value))

    def _initial_messages(
            self, command: UvmGenerationInput,
            baseline: Sequence[Mapping[str, str]], baseline_root: str,
            slots: Sequence[str]) -> list[dict[str, str]]:
        payload: dict[str, Any] = {
            "job_identity": {"job_id": command.project_input["job_id"]},
            "cycle": {
                "cycle_id": command.cycle_id,
                "cycle_kind": command.cycle_kind,
                "repair_revision": command.repair_revision,
            },
            "immutable_generation_context": {
                "spec_evidence": copy.deepcopy(command.spec_evidence),
                "stage2_artifact": copy.deepcopy(command.stage2_artifact),
                "logical_testcases": copy.deepcopy(command.logical_testcases),
                "stage2_shards": copy.deepcopy(command.stage2_shards),
                "baseline_uvm_root": baseline_root,
                "baseline_uvm_files": [copy.deepcopy(dict(item))
                                       for item in baseline],
                "generated_file_slots": list(slots),
            },
        }
        return [
            {"role": "SYSTEM", "content": (
                "You are one persistent UVM Generation Worker. Use only the "
                "provided scoped tools and immutable context. Generate complete "
                "authorized slots, run Xcelium, inspect structured observations, "
                "repair as needed, and call finish_task only after a current "
                "PASS. Never request shell execution, unlisted paths, baseline "
                "edits, RTL, or testcase content.")},
            {"role": "USER", "content": json.dumps(
                payload, sort_keys=True, ensure_ascii=False)},
        ]

    def _replacement_contents(
            self, replacements: Any, slots: Sequence[str]
            ) -> list[dict[str, str]]:
        if not isinstance(replacements, list):
            raise self.dependencies.error(
                "TOOL_PROTOCOL_VIOLATION",
                "UVM replacement submission is malformed")
        normalized: list[dict[str, str]] = []
        for item in replacements:
            if (not isinstance(item, Mapping) or
                    set(item) != {"logical_path", "content"} or
                    not isinstance(item.get("logical_path"), str) or
                    not isinstance(item.get("content"), str)):
                raise self.dependencies.error(
                    "TOOL_PROTOCOL_VIOLATION",
                    "UVM replacement slot or exact text is malformed")
            try:
                path = self._safe_slot(item["logical_path"])
            except ValueError as caught:
                raise self.dependencies.error(
                    "TOOL_PERMISSION_DENIED",
                    "UVM replacement path escapes fixed slots") from caught
            normalized.append({"logical_path": path,
                               "content": item["content"]})
        by_path = {item["logical_path"]: item for item in normalized}
        if (set(by_path) != set(slots) or
                len(by_path) != len(normalized) or
                len(normalized) != len(slots)):
            raise self.dependencies.error(
                "TOOL_PROTOCOL_VIOLATION",
                "UVM Generator must return every fixed slot exactly once")
        return [by_path[path] for path in slots]

    def _cycle_root(self, command: UvmGenerationInput) -> Path:
        try:
            cycle = self._safe_slot(command.cycle_id + ".json").removesuffix(
                ".json")
        except ValueError as caught:
            raise self.dependencies.error(
                "INVALID_SCHEMA", "UVM cycle identity is unsafe") from caught
        if "/" in cycle:
            raise self.dependencies.error(
                "INVALID_SCHEMA", "UVM cycle identity must be one path token")
        return command.job_root / "audit/uvm_generation" / cycle

    def _checkpoints(
            self, command: UvmGenerationInput) -> list[dict[str, Any]]:
        root = self._cycle_root(command)
        records: list[dict[str, Any]] = []
        previous = "NONE"
        for sequence in range(1, 1000):
            path = root / "checkpoint-{:03d}.json".format(sequence)
            if not path.exists():
                break
            if not path.is_file() or path.is_symlink():
                raise self.dependencies.error(
                    "STALE_EVIDENCE", "UVM checkpoint is unsafe")
            value = load_document(path)
            self._require_contract("uvm_generation_checkpoint", value)
            if (value.get("sequence") != sequence or
                    value.get("job_id") !=
                        command.project_input["job_id"] or
                    value.get("input_fingerprint") !=
                        command.project_input["input_fingerprint"] or
                    value.get("cycle_id") != command.cycle_id or
                    value.get("cycle_kind") != command.cycle_kind.upper() or
                    value.get("repair_revision") != command.repair_revision or
                    value.get("stage2_root") != self._stage2_root(command) or
                    value.get("previous_checkpoint_fingerprint") != previous or
                    value.get("checkpoint_fingerprint") !=
                        artifact_fingerprint(value, "checkpoint_fingerprint")):
                raise self.dependencies.error(
                    "STALE_EVIDENCE", "UVM checkpoint chain is stale")
            records.append(value)
            previous = value["checkpoint_fingerprint"]
        return records

    def _append_checkpoint(
            self, command: UvmGenerationInput, state: str, attempt: int,
            next_action: str, *, candidate: Mapping[str, Any] | None = None,
            xcelium: Mapping[str, Any] | None = None) -> dict[str, Any]:
        records = self._checkpoints(command)
        value = {
            "schema_version": "1.0",
            "artifact_kind": "UVM_GENERATION_CHECKPOINT",
            "job_id": command.project_input["job_id"],
            "input_fingerprint": command.project_input["input_fingerprint"],
            "cycle_id": command.cycle_id,
            "cycle_kind": command.cycle_kind.upper(),
            "repair_revision": command.repair_revision,
            "stage2_root": self._stage2_root(command),
            "sequence": len(records) + 1,
            "state": state,
            "attempt": attempt,
            "attempts_used": attempt if candidate is not None else max(
                [int(item.get("attempts_used", 0)) for item in records] or [0]),
            "next_action": next_action,
            "candidate_path": (
                candidate.get("candidate_path") if candidate else None),
            "provider_request_path": (
                candidate.get("provider_request_path") if candidate else None),
            "provider_response_path": (
                candidate.get("provider_response_path") if candidate else None),
            "transcript_path": (
                candidate.get("transcript_path") if candidate else None),
            "effective_uvm_root": (
                candidate.get("effective_uvm_root") if candidate else None),
            "xcelium_result_path": (
                xcelium.get("result_path") if xcelium else None),
            "xcelium_request_fingerprint": (
                xcelium.get("request_fingerprint") if xcelium else None),
            "xcelium_status": (
                xcelium.get("status") if xcelium else None),
            "xcelium_exit_code": (
                xcelium.get("exit_code") if xcelium else None),
            "xcelium_stdout_path": (
                xcelium.get("stdout_path") if xcelium else None),
            "xcelium_stderr_path": (
                xcelium.get("stderr_path") if xcelium else None),
            "latest_error_source_attempt": (
                attempt if xcelium and xcelium.get("status") == "FAIL" else None),
            "previous_checkpoint_fingerprint": (
                records[-1]["checkpoint_fingerprint"] if records else "NONE"),
            "checkpoint_fingerprint": "0" * 64,
        }
        value["checkpoint_fingerprint"] = artifact_fingerprint(
            value, "checkpoint_fingerprint")
        self._require_contract("uvm_generation_checkpoint", value)
        path = self._cycle_root(command) / "checkpoint-{:03d}.json".format(
            value["sequence"])
        self._immutable_json(path, value)
        return value

    def _attempt_dir(self, command: UvmGenerationInput, attempt: int) -> Path:
        return (command.job_root / "staging/generated/uvm" /
                command.cycle_id / "attempt-{:03d}".format(attempt))

    def _candidate_path(
            self, command: UvmGenerationInput, attempt: int) -> Path:
        return self._attempt_dir(command, attempt) / "candidate.json"

    def _load_candidate(
            self, command: UvmGenerationInput, attempt: int
            ) -> dict[str, Any] | None:
        path = self._candidate_path(command, attempt)
        if not path.exists():
            return None
        if not path.is_file() or path.is_symlink():
            raise self.dependencies.error(
                "STALE_EVIDENCE", "UVM candidate metadata is unsafe")
        value = load_document(path)
        self._require_contract("uvm_generation_candidate", value)
        if (value.get("job_id") != command.project_input["job_id"] or
                value.get("cycle_id") != command.cycle_id or
                value.get("attempt") != attempt or
                value.get("stage2_root") != self._stage2_root(command) or
                value.get("candidate_fingerprint") !=
                    artifact_fingerprint(value, "candidate_fingerprint")):
            raise self.dependencies.error(
                "STALE_EVIDENCE", "UVM candidate lineage is stale")
        for item in value.get("effective_files", []):
            file_path = command.job_root / item["path"]
            if (not file_path.is_file() or file_path.is_symlink() or
                    self._sha(file_path.read_bytes()) != item["fingerprint"]):
                raise self.dependencies.error(
                    "STALE_EVIDENCE", "effective UVM candidate bytes drifted")
        expected_root = canonical_hash([{
            "logical_path": item["logical_path"],
            "fingerprint": item["fingerprint"],
        } for item in value.get("effective_files", [])])
        if expected_root != value.get("effective_uvm_root"):
            raise self.dependencies.error(
                "STALE_EVIDENCE", "effective UVM candidate root drifted")
        for field in ("provider_request", "provider_response"):
            evidence_path = command.job_root / value["{}_path".format(field)]
            if not evidence_path.is_file() or evidence_path.is_symlink():
                raise self.dependencies.error(
                    "PARTIAL_ARTIFACT",
                    "UVM {} evidence is missing".format(field))
            evidence = load_document(evidence_path)
            if canonical_hash(evidence) != value[
                    "{}_fingerprint".format(field)]:
                raise self.dependencies.error(
                    "STALE_EVIDENCE",
                    "UVM {} evidence drifted".format(field))
        return value

    def _persist_candidate(
            self, command: UvmGenerationInput, attempt: int,
            baseline: Sequence[Mapping[str, str]], baseline_root: str,
            replacements: Sequence[Mapping[str, str]], request: Mapping[str, Any],
            response: Mapping[str, Any], session_id: str
            ) -> dict[str, Any]:
        attempt_dir = self._attempt_dir(command, attempt)
        replacement_by_path = {
            item["logical_path"]: item["content"] for item in replacements}
        effective_files: list[dict[str, Any]] = []
        replacement_records: list[dict[str, Any]] = []
        replacement_contents = [copy.deepcopy(dict(item))
                                for item in replacements]
        for item in baseline:
            logical = item["logical_path"]
            content = replacement_by_path.get(logical, item["content"])
            encoded = content.encode("utf-8")
            relative = (Path("staging/generated/uvm") / command.cycle_id /
                        "attempt-{:03d}".format(attempt) / "effective" /
                        PurePosixPath(logical)).as_posix()
            self._immutable_bytes(command.job_root / relative, encoded)
            record = {
                "logical_path": logical, "path": relative,
                "fingerprint": self._sha(encoded), "size_bytes": len(encoded),
                "source": "REPLACEMENT" if logical in replacement_by_path
                          else "BASELINE",
            }
            effective_files.append(record)
            if logical in replacement_by_path:
                replacement_records.append(copy.deepcopy(record))
        effective_files.sort(key=lambda item: item["logical_path"])
        effective_root = canonical_hash([{
            "logical_path": item["logical_path"],
            "fingerprint": item["fingerprint"],
        } for item in effective_files])
        candidate_relative = self._candidate_path(
            command, attempt).relative_to(command.job_root).as_posix()
        attempt_audit_relative = (
            Path("audit/uvm_generation") / command.cycle_id /
            "attempt-{:03d}".format(attempt))
        value = {
            "schema_version": "1.0",
            "artifact_kind": "UVM_GENERATION_CANDIDATE",
            "job_id": command.project_input["job_id"],
            "input_fingerprint": command.project_input["input_fingerprint"],
            "cycle_id": command.cycle_id,
            "cycle_kind": command.cycle_kind.upper(),
            "repair_revision": command.repair_revision,
            "stage2_root": self._stage2_root(command),
            "attempt": attempt,
            "baseline_uvm_root": baseline_root,
            "replacement_contents": replacement_contents,
            "replacement_files": replacement_records,
            "effective_files": effective_files,
            "effective_uvm_root": effective_root,
            "provider_request_path": (
                attempt_audit_relative / "provider_request.json").as_posix(),
            "provider_request_fingerprint": canonical_hash(request),
            "provider_response_path": (
                attempt_audit_relative / "provider_response.json").as_posix(),
            "provider_response_fingerprint": canonical_hash(response),
            "transcript_path": "transcripts/uvm_generation/{}/manifest.json".format(
                session_id),
            "candidate_path": candidate_relative,
            "candidate_fingerprint": "0" * 64,
        }
        value["candidate_fingerprint"] = artifact_fingerprint(
            value, "candidate_fingerprint")
        self._require_contract("uvm_generation_candidate", value)
        audit = (self._cycle_root(command) /
                 "attempt-{:03d}".format(attempt))
        self._immutable_json(audit / "provider_request.json", request)
        self._immutable_json(audit / "provider_response.json", response)
        self._immutable_json(self._candidate_path(command, attempt), value)
        self._immutable_json(audit / "candidate.json", value)
        return value

    def _xcelium_runs(
            self, command: UvmGenerationInput, attempt: int
            ) -> list[dict[str, Any]]:
        audit = self._cycle_root(command) / "attempt-{:03d}".format(attempt)
        result: list[dict[str, Any]] = []
        for run in range(1, 1000):
            path = audit / "xcelium-run-{:03d}.result.json".format(run)
            if not path.exists():
                break
            if not path.is_file() or path.is_symlink():
                raise self.dependencies.error(
                    "STALE_EVIDENCE", "UVM Xcelium result is unsafe")
            value = load_document(path)
            self._require_contract("uvm_generation_xcelium_result", value)
            request_path = audit / "xcelium-run-{:03d}.request.json".format(run)
            request = load_document(request_path)
            self._require_contract("uvm_generation_xcelium_request", request)
            if (value.get("request_fingerprint") !=
                    request.get("request_fingerprint") or
                    request.get("request_fingerprint") !=
                    artifact_fingerprint(request, "request_fingerprint") or
                    value.get("result_fingerprint") !=
                    artifact_fingerprint(value, "result_fingerprint")):
                raise self.dependencies.error(
                    "STALE_EVIDENCE", "UVM Xcelium evidence is stale")
            for item in request["framework_sources"]:
                source_path = command.job_root / item["path"]
                if (not source_path.is_file() or source_path.is_symlink() or
                        self._sha(source_path.read_bytes()) !=
                            item["fingerprint"]):
                    raise self.dependencies.error(
                        "STALE_EVIDENCE",
                        "UVM Xcelium framework harness drifted")
            for field in ("stdout", "stderr"):
                log_path = command.job_root / value["{}_path".format(field)]
                if (not log_path.is_file() or log_path.is_symlink() or
                        log_path.read_bytes() !=
                        value[field].encode("utf-8")):
                    raise self.dependencies.error(
                        "STALE_EVIDENCE",
                        "UVM Xcelium exact {} log drifted".format(field))
            result.append(value)
        return result

    @staticmethod
    def _compilation_units(
            candidate: Mapping[str, Any]) -> list[dict[str, Any]]:
        compilation_units = [
            {key: item[key] for key in (
                "logical_path", "path", "fingerprint")}
            for item in candidate["effective_files"]
            if PurePosixPath(item["logical_path"]).suffix in {".sv", ".v"}
        ]

        def compile_order(item: Mapping[str, Any]) -> tuple[int, str]:
            logical = PurePosixPath(str(item["logical_path"]))
            folded_parts = {part.casefold() for part in logical.parts}
            stem = logical.stem.casefold()
            if "interfaces" in folded_parts or stem.endswith("_if"):
                rank = 0
            elif "packages" in folded_parts or stem.endswith("_pkg"):
                rank = 1
            elif "tb" in folded_parts or "tb_top" in stem:
                rank = 3
            else:
                rank = 2
            return rank, logical.as_posix()

        compilation_units.sort(key=compile_order)
        return compilation_units

    def _framework_elaboration_source(
            self, command: UvmGenerationInput, attempt: int,
            run: int) -> dict[str, Any]:
        content = "module {}; endmodule\n".format(UVM_ELABORATION_TOP)
        relative = (
            self._cycle_root(command) /
            "attempt-{:03d}".format(attempt) /
            "xcelium-run-{:03d}.elaboration_top.sv".format(run)
        ).relative_to(command.job_root).as_posix()
        encoded = content.encode("utf-8")
        self._immutable_bytes(command.job_root / relative, encoded)
        return {
            "logical_path": "framework/{}.sv".format(UVM_ELABORATION_TOP),
            "path": relative,
            "fingerprint": self._sha(encoded),
        }

    def _xcelium_request(
            self, command: UvmGenerationInput, candidate: Mapping[str, Any],
            run: int) -> dict[str, Any]:
        compilation_units = self._compilation_units(candidate)
        if not compilation_units:
            raise self.dependencies.error(
                "BLOCKED_INPUT",
                "UVM context has no standalone SystemVerilog compilation unit")
        framework_source = self._framework_elaboration_source(
            command, int(candidate["attempt"]), run)
        value = {
            "schema_version": "1.0",
            "artifact_kind": "UVM_GENERATION_XCELIUM_REQUEST",
            "job_id": command.project_input["job_id"],
            "cycle_id": command.cycle_id,
            "attempt": candidate["attempt"],
            "tool_run": run,
            "stage2_root": candidate["stage2_root"],
            "effective_uvm_root": candidate["effective_uvm_root"],
            # Header files remain part of effective_uvm_root, but Xcelium sees
            # them only through the package/include graph.  Compiling .svh
            # files as standalone units would discard their UVM context.
            "sources": compilation_units,
            "framework_sources": [framework_source],
            "top": UVM_ELABORATION_TOP,
            "timeout_seconds": command.project_input["eda"]["timeout_seconds"],
            "phase": "BUILD",
            "request_fingerprint": "0" * 64,
        }
        value["request_fingerprint"] = artifact_fingerprint(
            value, "request_fingerprint")
        self._require_contract("uvm_generation_xcelium_request", value)
        return value

    def _next_xcelium_run(
            self, command: UvmGenerationInput, attempt: int) -> int:
        audit = self._cycle_root(command) / "attempt-{:03d}".format(attempt)
        for run in range(1, 1000):
            if not (audit /
                    "xcelium-run-{:03d}.request.json".format(run)).exists():
                return run
        raise self.dependencies.error(
            "ITEM_LIMIT_EXCEEDED", "too many interrupted Xcelium tool runs")

    def _normalize_xcelium_result(
            self, command: UvmGenerationInput, raw: Mapping[str, Any],
            request: Mapping[str, Any],
            result_path: str) -> dict[str, Any]:
        status = raw.get("status", raw.get("execution_status"))
        if status not in {"PASS", "FAIL", "BLOCKED_TOOL", "INCOMPLETE"}:
            status = "INCOMPLETE"
        exit_code = raw.get("exit_code")
        if ((status == "PASS" and exit_code != 0) or
                (status == "FAIL" and
                 (type(exit_code) is not int or exit_code == 0))):
            status = "INCOMPLETE"
        diagnostics = raw.get("diagnostic_codes")
        if status == "FAIL" and (
                (type(exit_code) is int and exit_code < 0) or
                (isinstance(diagnostics, list) and set(diagnostics) & {
                    "MISSING_ARTIFACT", "OUTPUT_FILE_LIMIT"})):
            # Process interruption and incomplete evidence retry Xcelium on
            # the same candidate. A positive nonzero build/elaboration exit
            # is the trusted adapter's complete code-failure result.
            status = "INCOMPLETE"
        result_parent = Path(result_path).parent
        stdout_path = (result_parent / "xcelium-run-{:03d}.stdout.log".format(
            int(request["tool_run"]))).as_posix()
        stderr_path = (result_parent / "xcelium-run-{:03d}.stderr.log".format(
            int(request["tool_run"]))).as_posix()
        stdout = str(raw.get("stdout", ""))
        stderr = str(raw.get("stderr", ""))
        self._immutable_bytes(
            command.job_root / stdout_path, stdout.encode("utf-8"))
        self._immutable_bytes(
            command.job_root / stderr_path, stderr.encode("utf-8"))
        value = {
            "schema_version": "1.0",
            "artifact_kind": "UVM_GENERATION_XCELIUM_RESULT",
            "request_fingerprint": request["request_fingerprint"],
            "status": status,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
            "request_path": raw.get("request_path"),
            "evidence_path": raw.get("evidence_path"),
            "logs": copy.deepcopy(raw.get("logs", [])),
            "raw_result": copy.deepcopy(dict(raw)),
            "result_path": result_path,
            "result_fingerprint": "0" * 64,
        }
        value["result_fingerprint"] = artifact_fingerprint(
            value, "result_fingerprint")
        self._require_contract("uvm_generation_xcelium_result", value)
        return value

    def _result(
            self, command: UvmGenerationInput, state: str,
            checkpoint: dict[str, Any], candidate: Mapping[str, Any] | None,
            replayed: bool) -> UvmGenerationResult:
        files: list[dict[str, str]] = []
        if candidate is not None:
            for item in candidate["effective_files"]:
                content = (command.job_root / item["path"]).read_text(
                    encoding="utf-8")
                files.append({
                    "logical_path": item["logical_path"],
                    "content": content,
                    "fingerprint": item["fingerprint"],
                })
        return UvmGenerationResult(
            state, checkpoint, tuple(files),
            candidate.get("effective_uvm_root") if candidate else None,
            int(candidate.get("attempt", 0)) if candidate else 0, replayed)

class UvmGenerationWorkerFacade:
    """Application operations exposed through one scoped continuous Worker.

    The facade owns no Agent loop.  It only turns authorized tool calls into
    the existing immutable candidate, checkpoint, and Xcelium evidence.
    """

    def __init__(
            self, handler: UvmGenerationHandler,
            command: UvmGenerationInput, worker_session_id: str):
        self.handler = handler
        self.command = command
        self.worker_session_id = worker_session_id
        self.baseline, self.baseline_root = handler._baseline(command)
        self.slots = handler._slots(command, self.baseline)

    @staticmethod
    def _authority_payload(
            command: UvmGenerationInput, baseline_root: str,
            slots: Sequence[str]) -> dict[str, Any]:
        return {
            "job_id": command.project_input["job_id"],
            "input_fingerprint": command.project_input["input_fingerprint"],
            "cycle_id": command.cycle_id,
            "cycle_kind": command.cycle_kind.upper(),
            "repair_revision": command.repair_revision,
            "stage2_root": UvmGenerationHandler._stage2_root(command),
            "baseline_uvm_root": baseline_root,
            "generated_file_slots": list(slots),
        }

    @property
    def authority_fingerprint(self) -> str:
        return canonical_hash(self._authority_payload(
            self.command, self.baseline_root, self.slots))

    def _authority_record(self, *, reload_source: bool) -> dict[str, Any]:
        if reload_source:
            _baseline, baseline_root = self.handler._baseline(self.command)
            slots = self.handler._slots(self.command, _baseline)
        else:
            baseline_root, slots = self.baseline_root, self.slots
        payload = self._authority_payload(self.command, baseline_root, slots)
        value = {
            "schema_version": "1.0",
            "artifact_kind": "DV_WORKER_AUTHORITY",
            **payload,
            "authority_fingerprint": canonical_hash(payload),
            "record_fingerprint": "0" * 64,
        }
        value["record_fingerprint"] = artifact_fingerprint(
            value, "record_fingerprint")
        return value

    def _authority_path(self) -> Path:
        return self.handler._cycle_root(self.command) / "worker-authority.json"

    def ensure_authority_record(self) -> dict[str, Any]:
        value = self._authority_record(reload_source=True)
        self.handler._immutable_json(self._authority_path(), value)
        return value

    def reload_authority(self) -> dict[str, Any]:
        path = self._authority_path()
        if not path.is_file() or path.is_symlink():
            raise self.handler.dependencies.error(
                "STALE_EVIDENCE", "UVM Worker authority is unavailable")
        value = load_document(path)
        expected = self._authority_record(reload_source=True)
        if (value != expected or
                value.get("record_fingerprint") != artifact_fingerprint(
                    value, "record_fingerprint") or
                value.get("authority_fingerprint") !=
                    self.authority_fingerprint):
            raise self.handler.dependencies.error(
                "STALE_EVIDENCE", "UVM Worker authority is stale")
        return value

    def initial_messages(self) -> list[dict[str, str]]:
        return self.handler._initial_messages(
            self.command, self.baseline, self.baseline_root, self.slots,
        )

    def _candidates(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for attempt in range(1, 1000):
            candidate = self.handler._load_candidate(self.command, attempt)
            if candidate is None:
                break
            result.append(candidate)
        return result

    def _current_candidate(self) -> dict[str, Any] | None:
        candidates = self._candidates()
        return candidates[-1] if candidates else None

    def task_state(self, worker_state: Mapping[str, Any]) -> dict[str, Any]:
        candidate = self._current_candidate()
        runs = (
            self.handler._xcelium_runs(
                self.command, int(candidate["attempt"]))
            if candidate is not None else [])
        return {
            "task_id": worker_state["task_id"],
            "worker_session_id": worker_state["worker_session_id"],
            "authority_fingerprint": self.authority_fingerprint,
            "status": worker_state["status"],
            "current_phase": worker_state["current_phase"],
            "generated_file_slots": list(self.slots),
            "candidate_fingerprint": (
                candidate["candidate_fingerprint"] if candidate else None),
            "effective_uvm_root": (
                candidate["effective_uvm_root"] if candidate else None),
            "xcelium_status": runs[-1]["status"] if runs else None,
            "turns_used": worker_state["turns_used"],
            "tokens_used": worker_state["tokens_used"],
            "eda_runs_used": worker_state["eda_runs_used"],
        }

    def read_candidate(self) -> dict[str, Any]:
        candidate = self._current_candidate()
        if candidate is None:
            return {"status": "NO_CANDIDATE", "files": []}
        files = []
        for item in candidate["effective_files"]:
            if item["logical_path"] not in self.slots:
                continue
            files.append({
                "logical_path": item["logical_path"],
                "content": (self.command.job_root / item["path"]).read_text(
                    encoding="utf-8"),
                "fingerprint": item["fingerprint"],
            })
        return {
            "status": "READY",
            "candidate_fingerprint": candidate["candidate_fingerprint"],
            "effective_uvm_root": candidate["effective_uvm_root"],
            "files": files,
        }

    def write_replacements(
            self, arguments: Mapping[str, Any], context: Mapping[str, Any]
            ) -> dict[str, Any]:
        response = copy.deepcopy(dict(context["response"]))
        response.setdefault("provider_metadata", {})[
            "worker_action_id"] = context["action_id"]
        replacements = self.handler._replacement_contents(
            arguments.get("replacements"), self.slots)
        attempt = len(self._candidates()) + 1
        candidate = self.handler._persist_candidate(
            self.command, attempt, self.baseline, self.baseline_root,
            replacements, context["request"], response,
            self.worker_session_id)
        checkpoint = self.handler._append_checkpoint(
            self.command, "UVM_CANDIDATE_READY", attempt, "RUN_XCELIUM",
            candidate=candidate)
        return {
            "status": "WRITTEN",
            "action_id": context["action_id"],
            "attempt": attempt,
            "candidate_path": candidate["candidate_path"],
            "candidate_fingerprint": candidate["candidate_fingerprint"],
            "effective_uvm_root": candidate["effective_uvm_root"],
            "checkpoint_fingerprint": checkpoint["checkpoint_fingerprint"],
            "changed_files": list(self.slots),
        }

    def run_xcelium_compile(
            self, _arguments: Mapping[str, Any], context: Mapping[str, Any]
            ) -> dict[str, Any]:
        candidate = self._current_candidate()
        if candidate is None:
            raise self.handler.dependencies.error(
                "BLOCKED_INPUT", "Xcelium requires a current UVM candidate")
        attempt = int(candidate["attempt"])
        run = self.handler._next_xcelium_run(self.command, attempt)
        request = self.handler._xcelium_request(
            self.command, candidate, run)
        request["worker_action_id"] = context["action_id"]
        request["request_fingerprint"] = artifact_fingerprint(
            request, "request_fingerprint")
        audit = (self.handler._cycle_root(self.command) /
                 "attempt-{:03d}".format(attempt))
        request_path = audit / "xcelium-run-{:03d}.request.json".format(run)
        self.handler._immutable_json(request_path, request)
        self.handler._append_checkpoint(
            self.command, "UVM_XCELIUM_RUNNING", attempt, "RUN_XCELIUM",
            candidate=candidate, xcelium={
                "request_fingerprint": request["request_fingerprint"]})
        raw = self.handler.dependencies.run_xcelium(
            self.command.project_input, self.command.job_root,
            copy.deepcopy(request))
        result_relative = (audit /
            "xcelium-run-{:03d}.result.json".format(run)).relative_to(
                self.command.job_root).as_posix()
        completed = self.handler._normalize_xcelium_result(
            self.command, raw, request, result_relative)
        self.handler._immutable_json(
            self.command.job_root / result_relative, completed)
        if completed["status"] == "PASS":
            state, next_action = "UVM_GENERATION_PASS", "GENERATE_STAGE3"
        elif completed["status"] == "FAIL":
            state, next_action = "UVM_GENERATION_RETRY_READY", "CALL_PROVIDER"
        else:
            state, next_action = "UVM_XCELIUM_RETRYABLE", "RUN_XCELIUM"
        checkpoint = self.handler._append_checkpoint(
            self.command, state, attempt, next_action,
            candidate=candidate, xcelium=completed)
        return {
            **bounded_xcelium_observation(completed),
            "action_id": context["action_id"],
            "attempt": attempt,
            "tool_run": run,
            "candidate_fingerprint": candidate["candidate_fingerprint"],
            "effective_uvm_root": candidate["effective_uvm_root"],
            "result_path": completed["result_path"],
            "checkpoint_fingerprint": checkpoint["checkpoint_fingerprint"],
        }

    def read_xcelium_observation(self) -> dict[str, Any]:
        candidate = self._current_candidate()
        if candidate is None:
            return {"status": "NO_CANDIDATE", "diagnostics": []}
        runs = self.handler._xcelium_runs(
            self.command, int(candidate["attempt"]))
        if not runs:
            return {"status": "NOT_RUN", "diagnostics": [],
                    "candidate_fingerprint": candidate[
                        "candidate_fingerprint"]}
        return {
            **bounded_xcelium_observation(runs[-1]),
            "candidate_fingerprint": candidate["candidate_fingerprint"],
            "effective_uvm_root": candidate["effective_uvm_root"],
        }

    def eda_runs_used(self) -> int:
        return sum(len(self.handler._xcelium_runs(
            self.command, int(candidate["attempt"])))
            for candidate in self._candidates())

    def recover_action(self, action_id: str) -> dict[str, Any]:
        for candidate in self._candidates():
            response = load_document(
                self.command.job_root / candidate["provider_response_path"])
            if response.get("provider_metadata", {}).get(
                    "worker_action_id") == action_id:
                checkpoint = next((
                    item for item in self.handler._checkpoints(self.command)
                    if item.get("state") == "UVM_CANDIDATE_READY" and
                    item.get("candidate_path") == candidate["candidate_path"]
                ), None)
                return {"status": "SUCCEEDED", "result": {
                    "status": "WRITTEN", "action_id": action_id,
                    "attempt": candidate["attempt"],
                    "candidate_path": candidate["candidate_path"],
                    "candidate_fingerprint": candidate[
                        "candidate_fingerprint"],
                    "effective_uvm_root": candidate["effective_uvm_root"],
                    "checkpoint_fingerprint": (
                        checkpoint["checkpoint_fingerprint"]
                        if checkpoint is not None else None),
                    "changed_files": list(self.slots),
                }}
            audit = (self.handler._cycle_root(self.command) /
                     "attempt-{:03d}".format(int(candidate["attempt"])))
            for run in range(1, 1000):
                request_path = audit / \
                    "xcelium-run-{:03d}.request.json".format(run)
                if not request_path.exists():
                    break
                request = load_document(request_path)
                if request.get("worker_action_id") != action_id:
                    continue
                result_path = audit / \
                    "xcelium-run-{:03d}.result.json".format(run)
                if not result_path.exists():
                    recovered = self._recover_xcelium_evidence(
                        candidate, request, audit, run)
                    if recovered is None:
                        return {"status": "UNKNOWN"}
                    completed, checkpoint = recovered
                else:
                    completed = load_document(result_path)
                    checkpoint = next((
                        item for item in reversed(
                            self.handler._checkpoints(self.command))
                        if item.get("xcelium_result_path") ==
                            completed.get("result_path")
                    ), None)
                return {"status": "SUCCEEDED", "result": {
                    **bounded_xcelium_observation(completed),
                    "action_id": action_id,
                    "attempt": candidate["attempt"], "tool_run": run,
                    "candidate_fingerprint": candidate[
                        "candidate_fingerprint"],
                    "effective_uvm_root": candidate["effective_uvm_root"],
                    "result_path": completed["result_path"],
                    "checkpoint_fingerprint": (
                        checkpoint["checkpoint_fingerprint"]
                        if checkpoint is not None else None),
                }}
        return {"status": "NOT_EXECUTED"}

    def _recover_xcelium_evidence(
            self, candidate: Mapping[str, Any], request: Mapping[str, Any],
            audit: Path, run: int
            ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Query exact adapter evidence after a crash; never rerun Xcelium."""
        execution_id = "UVM.{}.ATTEMPT{:03d}.RUN{:03d}".format(
            self.command.cycle_id.upper().replace("-", "."),
            int(candidate["attempt"]), run)
        adapter_request_id = "XCELIUM.REQUEST.{}.{}.BUILD".format(
            self.command.project_input["job_id"].removeprefix("JOB."),
            execution_id)
        token = adapter_request_id.removeprefix(
            "XCELIUM.REQUEST.").casefold()
        adapter_request_path = self.command.job_root / (
            "audit/xcelium/{}.request.json".format(token))
        adapter_evidence_path = self.command.job_root / (
            "audit/xcelium/{}.evidence.json".format(token))
        if (not adapter_request_path.exists() or
                not adapter_evidence_path.exists()):
            return None
        adapter_request = load_document(adapter_request_path)
        evidence = load_document(adapter_evidence_path)
        if (not accepted(validate("xcelium_execution_request", adapter_request)) or
                not accepted(validate("xcelium_execution_evidence", evidence)) or
                adapter_request.get("request_id") != adapter_request_id or
                evidence.get("request_id") != adapter_request_id or
                evidence.get("request_fingerprint") !=
                    adapter_request.get("request_fingerprint") or
                adapter_request.get("request_fingerprint") != canonical_hash({
                    key: value for key, value in adapter_request.items()
                    if key != "request_fingerprint"}) or
                evidence.get("evidence_fingerprint") != canonical_hash({
                    key: value for key, value in evidence.items()
                    if key != "evidence_fingerprint"})):
            return None
        raw = {
            **copy.deepcopy(evidence),
            "request_path": adapter_request_path.relative_to(
                self.command.job_root).as_posix(),
            "evidence_path": adapter_evidence_path.relative_to(
                self.command.job_root).as_posix(),
        }
        for kind, field in (("STDOUT", "stdout"), ("STDERR", "stderr")):
            matches = [item for item in evidence.get("logs", [])
                       if item.get("kind") == kind]
            if len(matches) != 1:
                return None
            log_path = self.command.job_root / matches[0]["relative_path"]
            if not log_path.is_file() or log_path.is_symlink():
                return None
            raw[field] = log_path.read_text(
                encoding="utf-8", errors="strict")
        result_relative = (audit /
            "xcelium-run-{:03d}.result.json".format(run)).relative_to(
                self.command.job_root).as_posix()
        completed = self.handler._normalize_xcelium_result(
            self.command, raw, request, result_relative)
        self.handler._immutable_json(
            self.command.job_root / result_relative, completed)
        if completed["status"] == "PASS":
            state, next_action = "UVM_GENERATION_PASS", "GENERATE_STAGE3"
        elif completed["status"] == "FAIL":
            state, next_action = "UVM_GENERATION_RETRY_READY", "CALL_PROVIDER"
        else:
            state, next_action = "UVM_XCELIUM_RETRYABLE", "RUN_XCELIUM"
        checkpoint = self.handler._append_checkpoint(
            self.command, state, int(candidate["attempt"]), next_action,
            candidate=candidate, xcelium=completed)
        return completed, checkpoint

    def _completion_decision(
            self, worker_state: Mapping[str, Any], transcript: Any
            ) -> dict[str, Any]:
        reasons: list[dict[str, str]] = []

        def reject(code: str, message: str) -> None:
            reasons.append({"code": code, "message": message})

        try:
            authority = self.reload_authority()
        except Exception as caught:
            authority = None
            reject(str(getattr(caught, "code", "STALE_AUTHORITY")),
                   "current UVM Worker authority could not be reloaded")
        if (authority is not None and
                worker_state.get("authority_fingerprint") !=
                    authority["authority_fingerprint"]):
            reject("STALE_AUTHORITY",
                   "WorkingState is not bound to the reloaded authority")
        candidate = self._current_candidate()
        if candidate is None:
            reject("MISSING_CANDIDATE", "current UVM candidate is missing")
        else:
            if authority is not None and any((
                    candidate.get("job_id") != authority["job_id"],
                    candidate.get("input_fingerprint") !=
                        authority["input_fingerprint"],
                    candidate.get("cycle_id") != authority["cycle_id"],
                    candidate.get("cycle_kind") != authority["cycle_kind"],
                    candidate.get("repair_revision") !=
                        authority["repair_revision"],
                    candidate.get("stage2_root") != authority["stage2_root"],
                    candidate.get("baseline_uvm_root") !=
                        authority["baseline_uvm_root"],
            )):
                reject("STALE_CANDIDATE",
                       "candidate is not bound to the reloaded authority")
            replacements = candidate.get("replacement_contents", [])
            if ({item.get("logical_path") for item in replacements} !=
                    set(self.slots) or len(replacements) != len(self.slots)):
                reject("MISSING_GENERATED_SLOT",
                       "not every authorized generated slot has content")
            if candidate.get("candidate_fingerprint") != \
                    artifact_fingerprint(candidate, "candidate_fingerprint"):
                reject("STALE_CANDIDATE", "candidate fingerprint is invalid")
            runs = self.handler._xcelium_runs(
                self.command, int(candidate["attempt"]))
            if not runs:
                reject("MISSING_XCELIUM_EVIDENCE",
                       "current candidate has no structured Xcelium evidence")
            else:
                result = runs[-1]
                audit = (self.handler._cycle_root(self.command) /
                         "attempt-{:03d}".format(int(candidate["attempt"])))
                request = load_document(
                    audit / "xcelium-run-{:03d}.request.json".format(
                        len(runs)))
                if (request.get("sources") !=
                        self.handler._compilation_units(candidate) or
                        request.get("effective_uvm_root") !=
                        candidate["effective_uvm_root"] or
                        request.get("phase") != "BUILD" or
                        request.get("top") != UVM_ELABORATION_TOP or
                        request.get("request_fingerprint") !=
                        artifact_fingerprint(request, "request_fingerprint")):
                    reject("STALE_XCELIUM_REQUEST",
                           "latest request is not bound to the current candidate")
                if result.get("request_fingerprint") != request.get(
                        "request_fingerprint"):
                    reject("STALE_XCELIUM_EVIDENCE",
                           "latest evidence is not bound to its request")
                if result.get("status") != "PASS" or \
                        result.get("exit_code") != 0:
                    reject("XCELIUM_NOT_PASS",
                           "latest compile/elaboration status is not PASS")
                raw = result.get("raw_result", {})
                codes = raw.get("diagnostic_codes", []) \
                    if isinstance(raw, Mapping) else []
                if codes:
                    reject("BLOCKING_DIAGNOSTIC",
                           "latest evidence contains unresolved diagnostics")
        checkpoints = self.handler._checkpoints(self.command)
        if (not checkpoints or checkpoints[-1].get("state") !=
                "UVM_GENERATION_PASS"):
            reject("INCOMPLETE_CHECKPOINT",
                   "authoritative UVM checkpoint is not PASS")
        if (candidate is not None and checkpoints and
                checkpoints[-1].get("effective_uvm_root") !=
                    candidate.get("effective_uvm_root")):
            reject("STALE_CHECKPOINT",
                   "checkpoint is not bound to the current candidate")
        action = worker_state.get("last_action", {})
        if action and action.get("status") != "SUCCEEDED":
            reject("PENDING_ACTION", "WorkingState contains a pending action")
        if (candidate is not None and
                worker_state.get("latest_candidate_fingerprint") !=
                    candidate.get("candidate_fingerprint")):
            reject("STALE_WORKING_STATE",
                   "WorkingState is not bound to the current candidate")
        if candidate is not None:
            candidate_runs = self.handler._xcelium_runs(
                self.command, int(candidate["attempt"]))
            if (candidate_runs and
                    worker_state.get("latest_validation_fingerprint") !=
                        candidate_runs[-1].get("result_fingerprint")):
                reject("STALE_WORKING_STATE",
                       "WorkingState is not bound to current validation")
        expected_transcript = (
            "transcripts/uvm_generation/{}/manifest.json".format(
                self.worker_session_id))
        if (candidate is not None and
                candidate.get("transcript_path") != expected_transcript):
            reject("STALE_TRANSCRIPT", "candidate transcript binding is stale")
        if (not getattr(transcript, "entries", None) or
                worker_state.get("transcript_cursor", 0) !=
                    len(transcript.entries)):
            reject("INCOMPLETE_TRANSCRIPT",
                   "Worker transcript events and state cursor differ")
        if reasons:
            return {"status": "NOT_COMPLETE", "diagnostics": reasons}
        return {
            "status": "PASS",
            "candidate_fingerprint": candidate["candidate_fingerprint"],
            "effective_uvm_root": candidate["effective_uvm_root"],
            "checkpoint_fingerprint": checkpoints[-1][
                "checkpoint_fingerprint"],
        }

    def completion_decision(
            self, worker_state: Mapping[str, Any], transcript: Any
            ) -> dict[str, Any]:
        """Fail a completion request as an observation, never as authority."""
        try:
            return self._completion_decision(worker_state, transcript)
        except Exception as caught:
            return {
                "status": "NOT_COMPLETE",
                "diagnostics": [{
                    "code": str(getattr(
                        caught, "code", "INVALID_COMPLETION_EVIDENCE")),
                    "message": str(caught)[:1024],
                }],
            }

    def result(self, state: str, *, replayed: bool = False
               ) -> UvmGenerationResult:
        checkpoints = self.handler._checkpoints(self.command)
        candidate = self._current_candidate()
        return self.handler._result(
            self.command, state, checkpoints[-1], candidate, replayed)


__all__ = [
    "UVM_ELABORATION_TOP", "UVM_GENERATION", "UVM_WORKER_ACTION_BUDGET",
    "UvmGenerationDependencies", "UvmGenerationHandler",
    "UvmGenerationInput", "UvmGenerationResult",
    "UvmGenerationWorkerFacade", "load_effective_uvm_pass",
]
