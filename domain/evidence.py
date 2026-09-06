"""Shared pure evidence normalization and bounded diagnostics helpers."""
from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timezone
from typing import Any, Callable


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def _bounded_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    sanitized = value.replace("\x00", "\ufffd")
    encoded = sanitized.encode("utf-8")
    if len(encoded) <= limit:
        return sanitized
    return encoded[:limit].decode("utf-8", errors="ignore")

def _failure_with_context(
        error: Callable[..., Exception], code: str, message: str,
        context: dict[str, Any]) -> Exception:
    caught = error(code, message)
    setattr(caught, "failure_context", copy.deepcopy(context))
    return caught

def _provider_identity(response: dict[str, Any]) -> dict[str, Any]:
    metadata = response.get("provider_metadata", {})
    usage = response.get("usage", {})
    return {
        "provider_id": metadata.get("provider_id", ""),
        "model_id": response.get("model_id", ""),
        "request_id": response.get("request_id", ""),
        "response_id": metadata.get("response_id", ""),
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
    }

def _enrich_evidence(
        values: Any, sources: dict[str, str], error: Callable[..., Exception],
        *, max_snippet_bytes: int = 4096) -> list[dict[str, Any]]:
    if not isinstance(values, list) or not values:
        raise error("MISSING_SPEC_EVIDENCE", "exact Spec evidence is required")
    if len(values) > 32:
        raise error("ITEM_LIMIT_EXCEEDED",
                    "Spec evidence count budget exceeded")
    result: list[dict[str, Any]] = []
    for source in values:
        if not isinstance(source, dict) or set(source) != {
                "path", "line_start", "line_end"}:
            raise error("INVALID_MAPPING",
                        "Spec evidence has missing or forbidden fields")
        path = source["path"]
        if not isinstance(path, str) or path not in sources:
            raise error("UNKNOWN_SPEC_REFERENCE",
                        "mapping cites a source outside baseline Spec")
        start, end = source["line_start"], source["line_end"]
        lines = sources[path].splitlines()
        if (type(start) is not int or type(end) is not int or
                not 1 <= start <= end <= len(lines)):
            raise error("SPEC_EVIDENCE_MISMATCH",
                        "Spec evidence line range is invalid")
        snippet = "\n".join(lines[start - 1:end])
        if not snippet:
            raise error("SPEC_EVIDENCE_MISMATCH",
                        "Spec evidence range resolves to empty text")
        if len(snippet) > max_snippet_bytes:
            raise error("FILE_LIMIT_EXCEEDED",
                        "Spec evidence snippet exceeds size budget")
        item = {
            "path": path,
            "line_start": start,
            "line_end": end,
            "snippet": snippet,
            "snippet_fingerprint": _sha(snippet),
        }
        result.append(item)
    canonical = sorted(
        result, key=lambda item: (
            item["path"], item["line_start"], item["line_end"]))
    keys = [
        (item["path"], item["line_start"], item["line_end"])
        for item in canonical]
    if len(keys) != len(set(keys)):
        raise error("DUPLICATE_SPEC_REFERENCE",
                    "duplicate exact Spec range is not allowed")
    return canonical

def _validate_enriched_evidence(
        values: Any, sources: dict[str, str],
        error: Callable[..., Exception]) -> list[dict[str, Any]]:
    """Validate immutable formal evidence against deterministic derivation."""
    if not isinstance(values, list) or not values:
        raise error("MISSING_SPEC_EVIDENCE", "exact Spec evidence is required")
    ranges: list[dict[str, Any]] = []
    for source in values:
        if not isinstance(source, dict) or set(source) != {
                "path", "line_start", "line_end", "snippet",
                "snippet_fingerprint"}:
            raise error("SPEC_EVIDENCE_MISMATCH",
                        "formal Spec evidence shape is invalid")
        ranges.append({
            key: source[key] for key in ("path", "line_start", "line_end")})
    enriched = _enrich_evidence(ranges, sources, error)
    if values != enriched:
        raise error("SPEC_EVIDENCE_MISMATCH",
                    "formal Spec evidence is stale or does not match exact lines")
    return enriched
