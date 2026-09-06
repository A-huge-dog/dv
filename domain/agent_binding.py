"""Pure lookup and lineage projection for resolved Agent profiles."""
from __future__ import annotations

import copy
from typing import Any, Mapping


def binding(profile_or_manifest: Mapping[str, Any], section: str,
            role: str) -> dict[str, Any]:
    profile = profile_or_manifest.get("agent_profile", profile_or_manifest)
    try:
        value = profile["bindings"][section][role]
    except (KeyError, TypeError) as caught:
        raise ValueError("Project Agent profile role is unavailable") from caught
    return copy.deepcopy(value)


def binding_lineage(profile_or_manifest: Mapping[str, Any], section: str,
                    role: str) -> dict[str, Any]:
    profile = profile_or_manifest.get("agent_profile", profile_or_manifest)
    selected = binding(profile, section, role)
    return {
        "profile_path": profile["path"],
        "profile_fingerprint": profile["byte_fingerprint"],
        "profile_document_fingerprint": profile["document_fingerprint"],
        "config_path": selected["path"],
        "config_fingerprint": selected["fingerprint"],
        "config_document_fingerprint": selected["document_fingerprint"],
        "provider_id": selected["provider_id"],
        "model_id": selected["model_id"],
    }
