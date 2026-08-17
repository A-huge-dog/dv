#!/usr/bin/env python3
"""Capture deterministic source and test-isolation facts for refactoring.

This utility is deliberately read-only unless ``--write`` is supplied.  It
does not import project modules, execute workflows, or access runtime results.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
PRODUCTION_ROOTS = ("core", "adapters", "contracts", "scripts")
TARGET_PACKAGES = ("domain", "agents", "runtime", "application", "infrastructure")
HOTSPOT_PATHS = (
    "core/project_staged.py",
    "core/project_job.py",
    "core/project_commit_runtime.py",
)
FILESYSTEM_CALLS = {
    "exists", "glob", "is_dir", "is_file", "iterdir", "lstat", "mkdir",
    "open", "read_bytes", "read_text", "rename", "replace", "resolve",
    "rglob", "rmdir", "stat", "touch", "unlink", "write_bytes",
    "write_text",
}
PROVIDER_CALLS = {"probe", "restore_probe", "select_tools"}


def _python_files(roots: tuple[str, ...]) -> list[Path]:
    return sorted(
        path
        for root in roots
        for path in (ROOT / root).rglob("*.py")
        if "__pycache__" not in path.parts and path.resolve() != SELF
    )


def _module_name(path: Path) -> str:
    relative = path.relative_to(ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _owner(parents: dict[ast.AST, ast.AST], node: ast.AST) -> str:
    names = []
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(current.name)
        current = parents.get(current)
    return ".".join(reversed(names)) or "<module>"


def _call_name(node: ast.Call) -> str:
    function = node.func
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        parts = [function.attr]
        value = function.value
        while isinstance(value, ast.Attribute):
            parts.append(value.attr)
            value = value.value
        if isinstance(value, ast.Name):
            parts.append(value.id)
        return ".".join(reversed(parts))
    return "<dynamic>"


def _internal_target(name: str, known: set[str]) -> str | None:
    if not name or name.split(".", 1)[0] not in PRODUCTION_ROOTS:
        return None
    candidate = name
    while candidate:
        if candidate in known:
            return candidate
        candidate = candidate.rpartition(".")[0]
    return None


def _strong_components(graph: dict[str, set[str]]) -> list[list[str]]:
    index = 0
    indexes: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    result: list[list[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indexes[node] = lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in sorted(graph[node]):
            if target not in indexes:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indexes[target])
        if lowlinks[node] == indexes[node]:
            component = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            component.sort()
            if len(component) > 1 or node in graph[node]:
                result.append(component)

    for node in sorted(graph):
        if node not in indexes:
            visit(node)
    return sorted(result)


def capture() -> dict:
    paths = _python_files(PRODUCTION_ROOTS)
    module_by_path = {path: _module_name(path) for path in paths}
    known_modules = set(module_by_path.values())
    modules = []
    imports = []
    while_true = []
    access = defaultdict(list)
    hotspot_definitions: dict[str, list[dict]] = {}

    for path in paths:
        relative = path.relative_to(ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative)
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        public_classes = sorted(
            node.name for node in tree.body
            if isinstance(node, ast.ClassDef) and not node.name.startswith("_"))
        public_functions = sorted(
            node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not node.name.startswith("_"))
        modules.append({
            "path": relative,
            "module": module_by_path[path],
            "line_count": len(source.splitlines()),
            "public_classes": public_classes,
            "public_functions": public_functions,
        })

        if relative in HOTSPOT_PATHS:
            definitions = []
            for node in tree.body:
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    definitions.append({
                        "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                        "name": node.name,
                        "line": node.lineno,
                        "end_line": node.end_lineno,
                        "line_count": node.end_lineno - node.lineno + 1,
                    })
            hotspot_definitions[relative] = definitions

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                names = []
            for name in names:
                target = _internal_target(name, known_modules)
                if target:
                    imports.append({
                        "source": module_by_path[path],
                        "target": target,
                        "line": node.lineno,
                    })

            if (isinstance(node, ast.While)
                    and isinstance(node.test, ast.Constant)
                    and node.test.value is True):
                while_true.append({
                    "path": relative,
                    "line": node.lineno,
                    "owner": _owner(parents, node),
                })

            if not isinstance(node, ast.Call):
                continue
            call_name = _call_name(node)
            leaf = call_name.rsplit(".", 1)[-1]
            record = {
                "path": relative,
                "line": node.lineno,
                "owner": _owner(parents, node),
                "call": call_name,
            }
            lowered = call_name.casefold()
            if leaf in FILESYSTEM_CALLS:
                access["filesystem"].append(record)
            if leaf in PROVIDER_CALLS or "provider_factory" in lowered:
                access["provider"].append(record)
            if (relative.startswith("adapters/eda/") or "verilator" in lowered
                    or "compile_runner" in lowered or "compile_factory" in lowered):
                access["eda"].append(record)
            if "checkpoint" in lowered:
                access["checkpoint"].append(record)

    imports.sort(key=lambda item: (item["source"], item["line"], item["target"]))
    graph = {module: set() for module in known_modules}
    for edge in imports:
        graph[edge["source"]].add(edge["target"])

    tests = sorted((ROOT / "tests").rglob("*.py"))
    forbidden_absolute_result_references = []
    production_job_literals = []
    temporary_directories = []
    for path in tests:
        relative = path.relative_to(ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "/home/xinyu/result" in node.value:
                    forbidden_absolute_result_references.append({
                        "path": relative, "line": node.lineno})
                if "JOB.PROJECT.AXI_LITE_SRAM" in node.value:
                    production_job_literals.append({
                        "path": relative, "line": node.lineno})
            if isinstance(node, ast.Call):
                call_name = _call_name(node)
                if call_name.endswith("TemporaryDirectory"):
                    temporary_directories.append({
                        "path": relative, "line": node.lineno})

    tests_owned_non_python = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "tests").rglob("*")
        if path.is_file() and path.suffix not in {".py", ".pyc"}
        and path.relative_to(ROOT).as_posix()
        != "tests/baselines/refactor_architecture.json"
    )
    target_packages_present = sorted(
        name for name in TARGET_PACKAGES if (ROOT / name).exists())

    return {
        "schema_version": "1.0",
        "source_root": ".",
        "production_roots": list(PRODUCTION_ROOTS),
        "module_count": len(modules),
        "modules": modules,
        "import_edges": imports,
        "circular_import_components": _strong_components(graph),
        "while_true": sorted(
            while_true, key=lambda item: (item["path"], item["line"])),
        "access_locations": {
            key: sorted(value, key=lambda item: (item["path"], item["line"], item["call"]))
            for key, value in sorted(access.items())
        },
        "hotspot_definitions": hotspot_definitions,
        "fixture_isolation": {
            "forbidden_absolute_result_references": forbidden_absolute_result_references,
            "production_job_literals": production_job_literals,
            "temporary_directory_constructors": temporary_directories,
            "tests_owned_non_python_files": tests_owned_non_python,
        },
        "target_packages_present": target_packages_present,
    }


def _serialized(value: dict) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--write", type=Path, metavar="PATH")
    action.add_argument("--check", type=Path, metavar="PATH")
    arguments = parser.parse_args()
    value = capture()
    rendered = _serialized(value)
    if arguments.write:
        arguments.write.parent.mkdir(parents=True, exist_ok=True)
        arguments.write.write_text(rendered, encoding="utf-8")
        return 0
    if arguments.check:
        expected = arguments.check.read_text(encoding="utf-8")
        if expected != rendered:
            print("architecture baseline differs: {}".format(arguments.check),
                  file=sys.stderr)
            return 1
        return 0
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
