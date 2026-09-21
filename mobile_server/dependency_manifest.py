"""Validation and selection for reproducible public source dependencies."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Iterable
from urllib.parse import urlparse


COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
KNOWN_PROFILES = frozenset({"main", "optional"})
KNOWN_TARGETS = frozenset(
    {
        "krea-identity-edit",
        "minimax-h3",
        "qwen2511-modular-flux2",
        "enhanced-upscale",
        "prompt-assistant",
    }
)


class DependencyManifestError(ValueError):
    pass


@dataclass(frozen=True)
class CustomNodeDependency:
    name: str
    repository: str
    commit: str
    license_spdx: str
    install_profile: str
    required_by: tuple[str, ...]


def _required_text(document: dict[str, Any], key: str, context: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DependencyManifestError(f"{context}.{key} must be a non-empty string")
    return value.strip()


def load_dependency_manifest(project_root: Path) -> tuple[CustomNodeDependency, ...]:
    path = project_root / "manifests/dependencies.lock.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DependencyManifestError(f"cannot read {path}: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 2:
        raise DependencyManifestError("dependencies.lock.json must use schema_version 2")

    comfyui = document.get("comfyui")
    if not isinstance(comfyui, dict):
        raise DependencyManifestError("comfyui must be an object")
    _validate_repository(_required_text(comfyui, "repository", "comfyui"), "comfyui")
    _validate_commit(_required_text(comfyui, "commit", "comfyui"), "comfyui")
    _required_text(comfyui, "license_spdx", "comfyui")
    patches = comfyui.get("patches")
    if not isinstance(patches, list) or not patches or not all(isinstance(item, str) and item for item in patches):
        raise DependencyManifestError("comfyui.patches must be a non-empty string array")
    for relative in patches:
        patch = (project_root / relative).resolve()
        if project_root.resolve() not in patch.parents or not patch.is_file():
            raise DependencyManifestError(f"missing or unsafe ComfyUI patch: {relative}")

    raw_nodes = document.get("verified_public_custom_nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise DependencyManifestError("verified_public_custom_nodes must be a non-empty array")
    nodes: list[CustomNodeDependency] = []
    names: set[str] = set()
    for index, item in enumerate(raw_nodes):
        context = f"verified_public_custom_nodes[{index}]"
        if not isinstance(item, dict):
            raise DependencyManifestError(f"{context} must be an object")
        name = _required_text(item, "name", context)
        if name.casefold() in names:
            raise DependencyManifestError(f"duplicate custom node name: {name}")
        names.add(name.casefold())
        repository = _required_text(item, "repository", context)
        commit = _required_text(item, "commit", context)
        license_spdx = _required_text(item, "license_spdx", context)
        profile = _required_text(item, "install_profile", context)
        required_by = item.get("required_by")
        _validate_repository(repository, context)
        _validate_commit(commit, context)
        if profile not in KNOWN_PROFILES:
            raise DependencyManifestError(f"{context}.install_profile is unknown: {profile}")
        if not isinstance(required_by, list) or not required_by or not all(isinstance(value, str) for value in required_by):
            raise DependencyManifestError(f"{context}.required_by must be a non-empty string array")
        unknown = set(required_by) - KNOWN_TARGETS
        if unknown:
            raise DependencyManifestError(f"{context}.required_by contains unknown targets: {', '.join(sorted(unknown))}")
        nodes.append(CustomNodeDependency(name, repository, commit, license_spdx, profile, tuple(required_by)))

    main_targets = {target for node in nodes if node.install_profile == "main" for target in node.required_by}
    expected_main = {"krea-identity-edit", "minimax-h3"}
    if not expected_main <= main_targets:
        missing = ", ".join(sorted(expected_main - main_targets))
        raise DependencyManifestError(f"main workflow dependency coverage is incomplete: {missing}")
    return tuple(nodes)


def select_dependencies(
    dependencies: Iterable[CustomNodeDependency],
    profile: str = "all",
) -> tuple[CustomNodeDependency, ...]:
    if profile not in {"main", "all"}:
        raise DependencyManifestError(f"unknown install profile: {profile}")
    return tuple(item for item in dependencies if profile == "all" or item.install_profile == "main")


def _validate_repository(value: str, context: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname != "github.com" or not parsed.path.endswith(".git"):
        raise DependencyManifestError(f"{context}.repository must be an HTTPS GitHub clone URL ending in .git")


def _validate_commit(value: str, context: str) -> None:
    if not COMMIT_PATTERN.fullmatch(value):
        raise DependencyManifestError(f"{context}.commit must be a lowercase 40-character Git commit")
