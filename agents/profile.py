"""Load and bind the single code-owned Project Agent model profile."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

import yaml

from contracts.validator import accepted, validate
from scripts.dvlib import canonical_hash


ROLE_PATHS = (
    ("initial", "stage1"),
    ("initial", "stage2"),
    ("initial", "uvm"),
    ("initial", "stage3"),
    ("repair", "orchestrator"),
    ("repair", "stage1"),
    ("repair", "stage2"),
    ("repair", "uvm"),
    ("repair", "stage3"),
    ("review", "initial"),
    ("review", "final"),
)


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode,
                       deep: bool = False) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError("duplicate YAML mapping key")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _safe_relative_path(relative: str, kind: str,
                        error: Callable[[str, str], Exception]) -> None:
    if not isinstance(relative, str) or not relative:
        raise error("INVALID_SCHEMA", "{} path is invalid".format(kind))
    path = PurePosixPath(relative)
    denied = {".env", "credentials", "credential", "secrets", "secret"}
    if (path.is_absolute() or ".." in path.parts or
            any(part.startswith(".") or part.casefold() in denied
                for part in path.parts)):
        raise error(
            "TOOL_PERMISSION_DENIED",
            "{} path is outside the code-owned workspace".format(kind))


def _read_yaml(path: Path, kind: str,
               error: Callable[[str, str], Exception]
               ) -> tuple[bytes, dict[str, Any]]:
    try:
        content = path.read_bytes()
        text = content.decode("utf-8")
        if any(isinstance(event, yaml.AliasEvent)
               for event in yaml.parse(text)):
            raise ValueError("YAML aliases are not accepted")
        value = yaml.load(text, Loader=_UniqueKeyLoader)
    except (OSError, UnicodeError, yaml.YAMLError, ValueError) as caught:
        raise error(
            "INVALID_AGENT_PROFILE" if kind == "Agent profile"
            else "INVALID_PROVIDER_CONFIG",
            "{} is not valid canonical YAML".format(kind)) from caught
    if not isinstance(value, dict):
        raise error(
            "INVALID_AGENT_PROFILE" if kind == "Agent profile"
            else "INVALID_PROVIDER_CONFIG",
            "{} must be a YAML object".format(kind))
    return content, value


def _resolve(workspace_root: Path, relative: str, kind: str,
             error: Callable[[str, str], Exception]) -> Path:
    _safe_relative_path(relative, kind, error)
    root = Path(workspace_root).resolve()
    lexical = root / relative
    cursor = lexical
    while cursor != root:
        if cursor.is_symlink():
            raise error(
                "TOOL_PERMISSION_DENIED",
                "{} path may not traverse a symlink".format(kind))
        cursor = cursor.parent
    try:
        path = lexical.resolve(strict=True)
        path.relative_to(root)
    except (FileNotFoundError, ValueError) as caught:
        raise error(
            "BLOCKED_INPUT" if isinstance(caught, FileNotFoundError)
            else "TOOL_PERMISSION_DENIED",
            "{} is missing or escapes the workspace".format(kind)) from caught
    if path.is_symlink() or not path.is_file():
        raise error("BLOCKED_INPUT", "{} is not a regular file".format(kind))
    return path


def load_project_agent_profile(
        workspace_root: Path, relative: str,
        error: Callable[[str, str], Exception]) -> tuple[dict[str, Any],
                                                         dict[str, bytes]]:
    """Resolve the exact profile and all Provider YAMLs once."""
    profile_path = _resolve(workspace_root, relative, "Agent profile", error)
    profile_bytes, profile = _read_yaml(profile_path, "Agent profile", error)
    if not accepted(validate("project_agent_profile", profile)):
        raise error("INVALID_AGENT_PROFILE", "Agent profile schema is invalid")

    snapshots: dict[str, bytes] = {relative: profile_bytes}
    bindings: dict[str, dict[str, dict[str, Any]]] = {
        "initial": {}, "repair": {}, "review": {}}
    resolved_paths: dict[Path, str] = {}
    for section, role in ROLE_PATHS:
        config_relative = profile[section][role]
        config_path = _resolve(
            workspace_root, config_relative, "Provider config", error)
        config_bytes, config = _read_yaml(
            config_path, "Provider config", error)
        if not accepted(validate("provider_config", config)):
            raise error(
                "INVALID_PROVIDER_CONFIG",
                "Agent profile references a non-Provider config")
        if role == "uvm" and config.get("model_id") != "openai/gpt-5.6-sol":
            raise error(
                "INVALID_AGENT_PROFILE",
                "initial.uvm and repair.uvm must use openai/gpt-5.6-sol")
        previous = resolved_paths.get(config_path)
        if previous is not None and snapshots[previous] != config_bytes:
            raise error("STALE_EVIDENCE", "Provider config resolution changed")
        resolved_paths[config_path] = config_relative
        snapshots[config_relative] = config_bytes
        basename = PurePosixPath(config_relative).name
        snapshot_name = "{}_{}".format(
            _sha256(config_relative.encode("utf-8"))[:16], basename)
        bindings[section][role] = {
            "path": config_relative,
            "baseline_path": "input_baseline/agent_profile/providers/{}".format(
                snapshot_name),
            "fingerprint": _sha256(config_bytes),
            "document_fingerprint": canonical_hash(config),
            "provider_id": config["provider_id"],
            "model_id": config["model_id"],
            "auth_env": config["auth_env"],
        }
    result = {
        "path": relative,
        "baseline_path": "input_baseline/agent_profile/profile.yaml",
        "byte_fingerprint": _sha256(profile_bytes),
        "document_fingerprint": canonical_hash(profile),
        "profile_id": profile["profile_id"],
        "bindings": bindings,
    }
    return result, snapshots


__all__ = ["ROLE_PATHS", "load_project_agent_profile"]
