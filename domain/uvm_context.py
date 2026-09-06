"""Validated public UVM testcase-generation context.

The deployment owns this input.  A Project Job never supplies, snapshots, or
discovers it: the framework only passes its public text to agents and records
the resulting capability identity/fingerprint in derived evidence.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import yaml


_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_CAPABILITY_ID = re.compile(r"^[A-Z][A-Z0-9_.-]{0,127}$")
_LOGICAL_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_PRIVATE_DECLARATION = re.compile(
    r"\b(?:uvm_driver|uvm_monitor|uvm_scoreboard|uvm_env|"
    r"virtual\s+interface|interface\s+[A-Za-z_][A-Za-z0-9_$]*|"
    r"tb_top|uvm_config_db|uvm_hdl_)\b", re.IGNORECASE)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _fail(error, code: str, message: str):
    if error is None:
        raise ValueError(message)
    raise error(code, message)


@dataclass(frozen=True)
class RuntimeCapability:
    """The limited public surface a generated testcase may use."""

    capability_id: str
    base_class: str
    extension_point: str
    api_type: str
    api_object: str
    methods: Mapping[str, str]
    operations: frozenset[str]
    aggregate_fingerprint: str

    def supports(self, required_operations: Sequence[str]) -> bool:
        return all(isinstance(item, str) and item in self.operations
                   for item in required_operations)


@dataclass(frozen=True)
class UvmContext:
    """Public, model-visible context plus parsed validation authority."""

    files: tuple[dict[str, str], ...]
    capability: RuntimeCapability

    def request_value(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability.capability_id,
            "aggregate_fingerprint": self.capability.aggregate_fingerprint,
            "uvm_context_files": [dict(item) for item in self.files],
        }


@dataclass(frozen=True)
class ProjectUvmContext:
    """The complete UVM source snapshot supplied by one Project YAML.

    This type is intentionally separate from the retired deployment context:
    it has no parsed API contract and no executable/platform authority.
    """

    files: tuple[dict[str, str], ...]

    def request_value(self) -> dict[str, Any]:
        return {"uvm_testcase_context": {"files": [
            {"logical_path": item["logical_path"],
             "fingerprint": item["fingerprint"],
             "content": item["content"]}
            for item in self.files
        ]}}


def project_uvm_context(
        project_input: Mapping[str, Any], error=None) -> ProjectUvmContext:
    """Load only the immutable UVM text frozen in a Project Job baseline."""
    try:
        entries = project_input["uvm_testcase_context"]["files"]
    except (KeyError, TypeError):
        _fail(error, "BLOCKED_INPUT", "Project YAML has no UVM testcase context files")
        raise AssertionError
    files: list[dict[str, str]] = []
    paths: set[str] = set()
    for item in entries:
        if not isinstance(item, Mapping) or set(item) != {
                "logical_path", "baseline_path", "fingerprint", "content"}:
            _fail(error, "STALE_EVIDENCE", "frozen UVM testcase context record is invalid")
        logical_path, fingerprint, content = (
            item["logical_path"], item["fingerprint"], item["content"])
        if (not isinstance(logical_path, str) or
                not _LOGICAL_PATH.fullmatch(logical_path) or
                not isinstance(content, str) or not content or
                not isinstance(fingerprint, str) or
                hashlib.sha256(content.encode("utf-8")).hexdigest() != fingerprint or
                logical_path in paths):
            _fail(error, "STALE_EVIDENCE", "frozen UVM testcase context is stale")
        paths.add(logical_path)
        files.append({"logical_path": logical_path, "fingerprint": fingerprint,
                      "content": content})
    if not files:
        _fail(error, "BLOCKED_INPUT", "Project YAML UVM testcase context is empty")
    files.sort(key=lambda item: item["logical_path"])
    return ProjectUvmContext(tuple(files))


def _parse_text(text: str, logical_path: str, error) -> Mapping[str, Any]:
    try:
        parsed = yaml.safe_load(text) if logical_path.endswith((".yaml", ".yml")) \
            else json.loads(text)
    except (yaml.YAMLError, json.JSONDecodeError) as caught:
        _fail(error, "BLOCKED_INPUT", "runtime capability contract is malformed")
        raise AssertionError from caught
    if not isinstance(parsed, Mapping):
        _fail(error, "BLOCKED_INPUT", "runtime capability contract must be an object")
    return parsed


def _contract_from_file(files: Sequence[Mapping[str, str]], error) -> RuntimeCapability:
    contracts = [item for item in files if item.get("kind") == "CAPABILITY_CONTRACT"]
    if len(contracts) != 1:
        _fail(error, "BLOCKED_INPUT", "deployment must provide exactly one capability contract")
    contract = _parse_text(contracts[0]["content"], contracts[0]["logical_path"], error)
    testcase = contract.get("testcase")
    api = testcase.get("api") if isinstance(testcase, Mapping) else None
    methods = api.get("methods") if isinstance(api, Mapping) else None
    operations = contract.get("operations")
    required = {
        "schema_version": "1.0",
        "artifact_kind": "RUNTIME_CAPABILITY_CONTRACT",
    }
    if (any(contract.get(key) != value for key, value in required.items()) or
            not isinstance(contract.get("capability_id"), str) or
            not _CAPABILITY_ID.fullmatch(contract["capability_id"]) or
            not isinstance(testcase, Mapping) or set(testcase) != {"base_class", "extension_point", "api"} or
            not isinstance(api, Mapping) or set(api) != {"type", "object", "methods"} or
            not isinstance(methods, Mapping) or not methods or
            not isinstance(operations, list) or not operations):
        _fail(error, "BLOCKED_INPUT", "runtime capability contract structure is invalid")
    if (any(not isinstance(value, str) or not value.strip()
            for value in methods.values()) or
            any(not isinstance(name, str) or not _ID.fullmatch(name)
                for name in methods) or
            any(not isinstance(name, str) or not _ID.fullmatch(name)
                for name in (testcase["base_class"], testcase["extension_point"],
                             api["type"], api["object"])) or
            any(not isinstance(item, str) or item not in methods
                for item in operations) or len(set(operations)) != len(operations)):
        _fail(error, "BLOCKED_INPUT", "runtime capability contract declarations are invalid")
    aggregate = hashlib.sha256(_canonical([
        {"logical_path": item["logical_path"], "fingerprint": item["fingerprint"]}
        for item in files]).encode("utf-8")).hexdigest()
    return RuntimeCapability(
        capability_id=contract["capability_id"],
        base_class=testcase["base_class"], extension_point=testcase["extension_point"],
        api_type=api["type"], api_object=api["object"],
        methods=dict(methods), operations=frozenset(operations),
        aggregate_fingerprint=aggregate)


def validated_context_files(files: Sequence[Mapping[str, Any]], error=None) -> UvmContext:
    """Validate already-read public context text, never local source paths."""
    normalized: list[dict[str, str]] = []
    paths: set[str] = set()
    for item in files:
        if not isinstance(item, Mapping) or set(item) != {
                "logical_path", "fingerprint", "content", "kind"}:
            _fail(error, "BLOCKED_INPUT", "UVM context file declaration is invalid")
        path, content, fingerprint, kind = (
            item["logical_path"], item["content"], item["fingerprint"], item["kind"])
        if (not isinstance(path, str) or not _LOGICAL_PATH.fullmatch(path) or
                path in paths or not isinstance(content, str) or not content or
                not isinstance(fingerprint, str) or
                hashlib.sha256(content.encode("utf-8")).hexdigest() != fingerprint or
                kind not in {"CAPABILITY_CONTRACT", "PUBLIC_DECLARATION"}):
            _fail(error, "BLOCKED_INPUT", "UVM context file fingerprint or structure is invalid")
        if kind == "PUBLIC_DECLARATION" and _PRIVATE_DECLARATION.search(content):
            _fail(error, "BLOCKED_INPUT", "UVM context exposes platform implementation")
        paths.add(path)
        normalized.append({"logical_path": path, "fingerprint": fingerprint,
                           "content": content, "kind": kind})
    if not normalized:
        _fail(error, "BLOCKED_INPUT", "UVM context file set is missing")
    normalized.sort(key=lambda item: item["logical_path"])
    return UvmContext(tuple(normalized), _contract_from_file(normalized, error))


def capability_assessment(required_operations: Sequence[str], capability: RuntimeCapability) -> tuple[str, list[str]]:
    """Classify one Spec item from explicit required public operations."""
    unsupported = sorted({item for item in required_operations
                          if not isinstance(item, str) or item not in capability.operations})
    return ("CHECKABLE", []) if not unsupported else ("BLOCKED_CONTRACT", unsupported)


__all__ = ["ProjectUvmContext", "RuntimeCapability", "UvmContext", "capability_assessment",
           "project_uvm_context", "validated_context_files"]
