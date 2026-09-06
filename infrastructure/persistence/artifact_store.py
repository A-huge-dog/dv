"""Explicit-path persistence for incremental Project artifacts.

Domain modules own fingerprints, units, indexes, assembly, and impact rules.
This module only validates caller-supplied Job-relative paths and performs
immutable writes or lineage-checked reads; it does not select a current Job,
revision, workflow transition, or authority.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from contracts.validator import accepted, load_document, validate
from infrastructure.persistence.atomic_artifact import publish_immutable_text
from scripts.dvlib import canonical_hash
from domain.repair import evaluate_impact
from domain.artifacts import (
    ASSEMBLY_CONTRACT_VERSION, IMPACT_CONTRACT_VERSION,
    INCREMENTAL_POLICY_VERSION, REVIEW, STAGE1, STAGE2, STAGE3,
    _ZERO, _index_relative, _lineage_fingerprint, _producer_dependency,
    _unit_relative, artifact_fingerprint as _artifact_fingerprint,
    build_review_bundle, build_stage1_bundle, build_stage2_bundle,
    build_stage3_bundle, unrouted_owner_scope, validate_assembly,
    validate_unit,
)










def _safe_target(
        job_root: Path, relative: str,
        error: Callable[[str, str], Exception]) -> Path:
    pure = PurePosixPath(relative)
    if (pure.is_absolute() or not pure.parts or
            any(part in {"", ".", ".."} or part.startswith(".")
                for part in pure.parts)):
        raise error("PATH_ESCAPE", "incremental artifact path is unsafe")
    root = job_root.resolve()
    target = job_root.joinpath(*pure.parts)
    current = job_root
    for part in pure.parts[:-1]:
        current = current / part
        if current.exists() and current.is_symlink():
            raise error("PATH_ESCAPE", "incremental artifact parent is a symlink")
    try:
        target.resolve(strict=False).relative_to(root)
    except ValueError as caught:
        raise error("PATH_ESCAPE", "incremental artifact escapes its Job") from caught
    return target


def _immutable_json(
        path: Path, value: dict[str, Any], max_file_bytes: int,
        error: Callable[[str, str], Exception]) -> None:
    content = json.dumps(
        value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    if len(content.encode("utf-8")) > max_file_bytes:
        raise error("FILE_LIMIT_EXCEEDED", "incremental artifact exceeds file budget")
    publish_immutable_text(
        path, content,
        lambda message: error("STALE_EVIDENCE", message),
        "append-only incremental artifact conflicts")




def _persist_bundle(
        *, job_root: Path, stage: str, variant: str, revision: int,
        units: list[dict[str, Any]], index: dict[str, Any],
        max_file_bytes: int, error: Callable[[str, str], Exception]
        ) -> dict[str, Any]:
    if len({item["unit_id"] for item in units}) != len(units):
        raise error("DUPLICATE_MAPPING_ID", "incremental unit IDs collide")
    # Child artifacts are written first. A crash before the index is a typed
    # partial state; an index can therefore never authorize missing children.
    for unit in units:
        relative = _unit_relative(stage, variant, revision, unit["unit_id"])
        _immutable_json(
            _safe_target(job_root, relative, error), unit,
            max_file_bytes, error)
    relative = _index_relative(stage, variant, revision)
    _immutable_json(
        _safe_target(job_root, relative, error), index,
        max_file_bytes, error)
    validate_index(index, job_root, error)
    return {
        "path": relative,
        "index": index,
        "units": {item["unit_id"]: item for item in units},
    }


def validate_index(
        index: dict[str, Any], job_root: Path,
        error: Callable[[str, str], Exception],
        expected_stage: str | None = None,
        expected_job_id: str | None = None) -> dict[str, dict[str, Any]]:
    if not accepted(validate("project_artifact_index", index)):
        raise error("INVALID_SCHEMA", "incremental artifact index schema is invalid")
    if index["root_fingerprint"] != _artifact_fingerprint(
            index, "root_fingerprint"):
        raise error("STALE_EVIDENCE", "incremental aggregate root is stale")
    if ((expected_stage is not None and index["stage"] != expected_stage) or
            (expected_job_id is not None and index["job_id"] != expected_job_id)):
        raise error("CROSS_JOB_ARTIFACT", "incremental index identity is stale")
    children = index["children"]
    canonical = sorted(children, key=lambda item: (item["unit_kind"], item["unit_id"]))
    ids = [item["unit_id"] for item in children]
    paths = [item["path"] for item in children]
    if (children != canonical or len(ids) != len(set(ids)) or
            len(paths) != len(set(paths)) or
            index["completeness"] != {
                "unit_ids": ids, "unit_count": len(ids)}):
        raise error("NON_CANONICAL_INDEX", "incremental child index is incomplete")
    units: dict[str, dict[str, Any]] = {}
    for child in children:
        path = _safe_target(job_root, child["path"], error)
        if not path.is_file() or path.is_symlink():
            raise error("PARTIAL_ARTIFACT", "indexed incremental child is missing")
        try:
            unit = load_document(path)
        except Exception as caught:
            raise error("INVALID_SCHEMA", "indexed incremental child is malformed") from caught
        validate_unit(unit, error)
        if (unit["unit_id"] != child["unit_id"] or
                unit["unit_kind"] != child["unit_kind"] or
                unit["stage"] != index["stage"] or
                unit["job_id"] != index["job_id"] or
                unit["revision"] != index["revision"] or
                unit["input_fingerprint"] != index["input_fingerprint"] or
                unit["spec_fingerprint"] != index["spec_fingerprint"] or
                unit["policy_fingerprint"] != index["policy_fingerprint"] or
                unit["owner_scope_fingerprint"] != index["owner_scope_fingerprint"] or
                any(unit[field] != child[field] for field in (
                    "content_fingerprint", "dependency_fingerprint",
                    "artifact_fingerprint"))):
            raise error("INDEX_SUBSTITUTION", "incremental child/index lineage differs")
        units[unit["unit_id"]] = unit
    return units


def load_index(
        job_root: Path, relative: str,
        error: Callable[[str, str], Exception],
        expected_stage: str | None = None,
        expected_job_id: str | None = None
        ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    path = _safe_target(job_root, relative, error)
    if not path.is_file() or path.is_symlink():
        raise error("PARTIAL_ARTIFACT", "incremental index is missing")
    try:
        index = load_document(path)
    except Exception as caught:
        raise error("INVALID_SCHEMA", "incremental index is malformed") from caught
    units = validate_index(index, job_root, error, expected_stage, expected_job_id)
    return index, units





















class IncrementalArtifactStore:
    """Append-only storage and replay validation for PJ-002.9-HF1 units."""

    def __init__(
            self, job_root: Path, max_file_bytes: int,
            error: Callable[[str, str], Exception]):
        self.job_root = Path(job_root)
        self.max_file_bytes = max_file_bytes
        self.error = error

    def persist_stage1(
            self, map1: dict[str, Any], owner_scope_fingerprint: str,
            variant: str = "PROVIDER", job_id: str | None = None
            ) -> dict[str, Any]:
        units, index = build_stage1_bundle(
            map1, owner_scope_fingerprint, self.error, variant, job_id)
        return _persist_bundle(
            job_root=self.job_root, stage=STAGE1, variant=variant,
            revision=map1["revision"], units=units, index=index,
            max_file_bytes=self.max_file_bytes, error=self.error)

    def persist_stage2(
            self, map2: dict[str, Any], logical_testcases: list[dict[str, Any]],
            stage1_units: dict[str, dict[str, Any]],
            owner_scope_fingerprint: str, job_id: str | None = None
            ) -> dict[str, Any]:
        units, index = build_stage2_bundle(
            map2, logical_testcases, stage1_units,
            owner_scope_fingerprint, self.error, job_id)
        return _persist_bundle(
            job_root=self.job_root, stage=STAGE2, variant="CURRENT",
            revision=map2["revision"], units=units, index=index,
            max_file_bytes=self.max_file_bytes, error=self.error)

    def persist_stage3(
            self, candidate: dict[str, Any],
            stage1_units: dict[str, dict[str, Any]],
            stage2_units: dict[str, dict[str, Any]],
            owner_scope_fingerprint: str, job_id: str | None = None
            ) -> dict[str, Any]:
        units, index, assembly = build_stage3_bundle(
            candidate, stage1_units, stage2_units,
            owner_scope_fingerprint, self.error, job_id)
        result = _persist_bundle(
            job_root=self.job_root, stage=STAGE3, variant="CURRENT",
            revision=candidate["revision"], units=units, index=index,
            max_file_bytes=self.max_file_bytes, error=self.error)
        relative = index["metadata"]["assembly_path"]
        _immutable_json(
            _safe_target(self.job_root, relative, self.error), assembly,
            self.max_file_bytes, self.error)
        validate_assembly(assembly, index, result["units"], self.error)
        result.update({
            "assembly_path": relative,
            "assembly": assembly,
        })
        return result

    def persist_review(
            self, report: dict[str, Any],
            stage1_units: dict[str, dict[str, Any]],
            stage2_units: dict[str, dict[str, Any]],
            stage3_units: dict[str, dict[str, Any]],
            owner_scope_fingerprint: str, policy_fingerprint: str,
            spec_fingerprint: str, job_id: str | None = None, *,
            storage_revision: int | None = None
            ) -> dict[str, Any]:
        units, index = build_review_bundle(
            report, stage1_units, stage2_units, stage3_units,
            owner_scope_fingerprint, policy_fingerprint, spec_fingerprint,
            self.error, job_id, storage_revision=storage_revision)
        return _persist_bundle(
            job_root=self.job_root, stage=REVIEW, variant="CURRENT",
            revision=(report["review_round"] - 1 if storage_revision is None
                      else storage_revision),
            units=units, index=index,
            max_file_bytes=self.max_file_bytes, error=self.error)

    def load(
            self, stage: str, variant: str, revision: int,
            job_id: str | None = None
            ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        return load_index(
            self.job_root, _index_relative(stage, variant, revision),
            self.error, stage, job_id)

    def load_stage3(
            self, revision: int, job_id: str | None = None
            ) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, Any]]:
        index, units = self.load(STAGE3, "CURRENT", revision, job_id)
        relative = index["metadata"].get("assembly_path", "")
        path = _safe_target(self.job_root, relative, self.error)
        if not path.is_file() or path.is_symlink():
            raise self.error("PARTIAL_ARTIFACT", "Stage 3 assembly is missing")
        assembly = load_document(path)
        validate_assembly(assembly, index, units, self.error)
        return index, units, assembly

    def compare_and_persist(
            self, old_index_paths: dict[str, str],
            new_index_paths: dict[str, str], revision: int
            ) -> dict[str, Any]:
        """Validate two exact revision sets and append one impact manifest."""
        if set(old_index_paths) != set(new_index_paths):
            raise self.error(
                "PARTIAL_ARTIFACT",
                "old/new impact index sets must name the same stages")
        old_indexes = {
            name: load_index(self.job_root, path, self.error)
            for name, path in sorted(old_index_paths.items())}
        new_indexes = {
            name: load_index(self.job_root, path, self.error)
            for name, path in sorted(new_index_paths.items())}
        manifest = evaluate_impact(old_indexes, new_indexes, self.error)
        path = persist_impact(
            self.job_root, manifest, revision,
            self.max_file_bytes, self.error)
        return {"path": path, "manifest": manifest}




def persist_impact(
        job_root: Path, manifest: dict[str, Any], revision: int,
        max_file_bytes: int, error: Callable[[str, str], Exception]) -> str:
    if manifest["impact_fingerprint"] != _artifact_fingerprint(
            manifest, "impact_fingerprint"):
        raise error("STALE_EVIDENCE", "impact manifest fingerprint is stale")
    relative = "staging/units/impact/impact.r{:03d}.json".format(revision)
    _immutable_json(
        _safe_target(job_root, relative, error), manifest,
        max_file_bytes, error)
    return relative
