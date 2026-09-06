"""Deterministic contract for generated UVM testcase bundles.

The generated bundle is a Job artifact.  The UVM platform is intentionally
*not* one: this module never receives platform paths, headers, include
directories, a testbench top, or a platform fingerprint.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Mapping, Sequence



_TESTCASE_ID = re.compile(r"^TC\.[A-Z0-9_.-]+$")
_CLASS = re.compile(r"^uvm_tc_[a-z0-9_]+_[0-9a-f]{12}$")
def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def testcase_class_name(testcase_id: str) -> str:
    """Derive a readable, stable class name without accepting model naming."""
    if not isinstance(testcase_id, str) or not _TESTCASE_ID.fullmatch(testcase_id):
        raise ValueError("logical testcase ID is invalid")
    stem = re.sub(r"[^a-z0-9]+", "_", testcase_id.casefold()).strip("_")
    return "uvm_tc_{}_{}".format(
        stem[:72], hashlib.sha256(testcase_id.encode("utf-8")).hexdigest()[:12])


def testcase_marker(testcase_id: str) -> str:
    if not isinstance(testcase_id, str) or not _TESTCASE_ID.fullmatch(testcase_id):
        raise ValueError("logical testcase ID is invalid")
    return "DV_UVM_{}_PASS".format(
        hashlib.sha256(testcase_id.encode("utf-8")).hexdigest()[:16].upper())


def build_manifest(logical_testcases: Sequence[Mapping[str, Any]], *,
                   implemented_testcase_ids: Sequence[str] | None = None,
                   skipped_testcases: Sequence[Mapping[str, Any]] = (),
                   default_timeout_seconds: int = 120) -> dict[str, Any]:
    """Create all execution-selected values outside model control.

    ``seed`` and ``timeout_seconds`` may be supplied only by the logical
    testcase contract.  No UVM-platform field is accepted or emitted.
    """
    if not isinstance(default_timeout_seconds, int) or not 1 <= default_timeout_seconds <= 3600:
        raise ValueError("default timeout is invalid")
    implemented = None if implemented_testcase_ids is None else set(
        implemented_testcase_ids)
    entries = []
    seen: set[str] = set()
    for testcase in logical_testcases:
        testcase_id = testcase.get("testcase_id")
        if (testcase.get("status") != "CHECKABLE" or
                (implemented is not None and testcase_id not in implemented)):
            continue
        if testcase_id in seen or not isinstance(testcase_id, str):
            raise ValueError("logical testcase IDs are invalid")
        seen.add(testcase_id)
        seed = testcase.get("seed", int(hashlib.sha256(
            testcase_id.encode("utf-8")).hexdigest()[:8], 16) % 2147483647 + 1)
        timeout = testcase.get("timeout_seconds", default_timeout_seconds)
        if type(seed) is not int or not 1 <= seed <= 2147483647:
            raise ValueError("logical testcase seed is invalid")
        if type(timeout) is not int or not 1 <= timeout <= 3600:
            raise ValueError("logical testcase timeout is invalid")
        class_name = testcase_class_name(testcase_id)
        entries.append({
            "testcase_id": testcase_id,
            "uvm_class": class_name,
            "uvm_testname": class_name,
            "seed": seed,
            "timeout_seconds": timeout,
            "pass_marker": testcase_marker(testcase_id),
        })
    entries.sort(key=lambda item: item["testcase_id"])
    skipped = []
    skipped_ids: set[str] = set()
    for item in skipped_testcases:
        if (not isinstance(item, Mapping) or set(item) != {
                "testcase_id", "reason_kind", "reason", "routing_required"} or
                not isinstance(item["testcase_id"], str) or
                not _TESTCASE_ID.fullmatch(item["testcase_id"]) or
                item["testcase_id"] in skipped_ids or
                item["reason_kind"] not in {
                    "SPEC_AMBIGUITY", "BLOCKED_CONTRACT",
                    "RTL_CONTRACT_MISMATCH"} or
                not isinstance(item["reason"], str) or not item["reason"].strip() or
                item["routing_required"] is not True):
            raise ValueError("skipped testcase declaration is invalid")
        skipped_ids.add(item["testcase_id"])
        skipped.append(dict(item))
    skipped.sort(key=lambda item: item["testcase_id"])
    value = {
        "schema_version": "1.0",
        "artifact_kind": "GENERATED_UVM_TESTS_MANIFEST",
        "testcases": entries,
        "skipped_testcases": skipped,
        "manifest_fingerprint": "",
    }
    value["manifest_fingerprint"] = hashlib.sha256(
        _canonical({key: value[key] for key in value if key != "manifest_fingerprint"}).encode("utf-8")
    ).hexdigest()
    return value


def validate_generated_tests(
        source: str, manifest: Mapping[str, Any],
        error_or_legacy_context: Callable[[str, str], Exception] | Any,
        error: Callable[[str, str], Exception] | None = None) -> None:
    """Check only framework-owned testcase identities and unsafe constructs.

    The Project YAML supplies the complete UVM implementation text. Its
    class hierarchy and callable APIs are therefore authoring context, not
    deterministic validation input.

    ``error_or_legacy_context`` retains the former call shape used by the
    execution boundary. It is ignored when a fourth ``error`` argument is
    present; no capability data is read or validated here.
    """
    if error is None:
        error = error_or_legacy_context
    if not callable(error):
        raise TypeError("generated-test validator needs an error factory")
    if not isinstance(source, str):
        raise error("INVALID_SCHEMA", "generated UVM testcase source is invalid")
    if not isinstance(manifest, Mapping) or manifest.get("artifact_kind") != "GENERATED_UVM_TESTS_MANIFEST":
        raise error("INVALID_SCHEMA", "generated UVM manifest is invalid")
    entries = manifest.get("testcases")
    skipped = manifest.get("skipped_testcases")
    if not isinstance(entries, list) or not isinstance(skipped, list):
        raise error("INVALID_SCHEMA", "generated UVM manifest is invalid")
    expected = {item.get("uvm_class") for item in entries}
    if len(expected) != len(entries) or not all(isinstance(name, str) and _CLASS.fullmatch(name) for name in expected):
        raise error("INVALID_SCHEMA", "generated UVM class identity is invalid")
    class_declaration = re.compile(
        r"\bclass\s+(uvm_tc_[A-Za-z0-9_]+)\s+extends\s+[A-Za-z_][A-Za-z0-9_$]*\b")
    actual = set(class_declaration.findall(source))
    if actual != expected:
        raise error("TESTCASE_MAPPING_OVERREACH", "generated UVM classes do not exactly cover the manifest")
    comment_only = re.sub(
        r"//[^\n]*(?:\n|$)|/\*.*?\*/", "", source,
        flags=re.DOTALL).strip() == ""
    if not expected and not comment_only:
        raise error("INVALID_GENERATED_ARTIFACT",
                    "a fully skipped UVM manifest may contain only comments")
    for entry in entries:
        marker = entry.get("pass_marker")
        if (not isinstance(marker, str) or marker in source or
                entry.get("uvm_testname") != entry.get("uvm_class")):
            raise error("INVALID_GENERATED_ARTIFACT", "generated code may not control a platform pass marker")


__all__ = ["build_manifest", "testcase_class_name", "testcase_marker",
           "validate_generated_tests"]
