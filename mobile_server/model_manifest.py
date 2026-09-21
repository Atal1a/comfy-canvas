"""Validation for the model provenance lock file; no download behavior lives here."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import tomllib
from typing import Any


HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
EXPECTED_CONFIG_KEYS = frozenset(
    {
        "models.krea_turbo.base_model",
        "models.krea_turbo.text_encoder",
        "models.krea_turbo.vae",
        "models.krea_identity.base_model",
        "models.krea_identity.text_encoder",
        "models.krea_identity.vae",
        "models.krea_identity.identity_lora",
        "models.minimax_h3.diffusion_model",
        "models.minimax_h3.text_encoder",
        "models.minimax_h3.video_vae",
        "models.minimax_h3.audio_vae",
        "models.minimax_h3.turbo_lora",
        "models.qwen2511.base_model",
        "models.qwen2511.text_encoder",
        "models.qwen2511.vae",
        "models.qwen2511.lightning_lora",
    }
)


class ModelManifestError(ValueError):
    pass


@dataclass(frozen=True)
class ModelArtifact:
    id: str
    required_by: tuple[str, ...]
    config_keys: tuple[str, ...]
    directory: str
    filename: str
    repository: str
    revision: str
    source_path: str
    license: str
    size_bytes: int
    sha256: str

    @property
    def source_url(self) -> str:
        return f"https://huggingface.co/{self.repository}/resolve/{self.revision}/{self.source_path}"


def _text(item: dict[str, Any], key: str, context: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ModelManifestError(f"{context}.{key} must be a non-empty string")
    return value.strip()


def _config_value(document: dict[str, Any], dotted: str) -> str:
    value: Any = document
    for key in dotted.split("."):
        if not isinstance(value, dict) or key not in value:
            raise ModelManifestError(f"config.example.toml is missing {dotted}")
        value = value[key]
    if not isinstance(value, str):
        raise ModelManifestError(f"config.example.toml value is not text: {dotted}")
    return value


def load_model_manifest(project_root: Path) -> tuple[ModelArtifact, ...]:
    path = project_root / "manifests/models.lock.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        with (project_root / "config.example.toml").open("rb") as source:
            defaults = tomllib.load(source)
    except (OSError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ModelManifestError(f"cannot read model supply-chain files: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ModelManifestError("models.lock.json must use schema_version 1")
    raw_models = document.get("models")
    if not isinstance(raw_models, list) or not raw_models:
        raise ModelManifestError("models must be a non-empty array")

    models: list[ModelArtifact] = []
    ids: set[str] = set()
    targets: set[tuple[str, str]] = set()
    covered_keys: set[str] = set()
    for index, raw in enumerate(raw_models):
        context = f"models[{index}]"
        if not isinstance(raw, dict):
            raise ModelManifestError(f"{context} must be an object")
        model_id = _text(raw, "id", context)
        if model_id in ids:
            raise ModelManifestError(f"duplicate model id: {model_id}")
        ids.add(model_id)
        required_by = raw.get("required_by")
        config_keys = raw.get("config_keys")
        if not isinstance(required_by, list) or not required_by or not all(isinstance(value, str) and value for value in required_by):
            raise ModelManifestError(f"{context}.required_by must be a non-empty string array")
        if not isinstance(config_keys, list) or not config_keys or not all(isinstance(value, str) and value for value in config_keys):
            raise ModelManifestError(f"{context}.config_keys must be a non-empty string array")
        directory = _text(raw, "directory", context)
        filename = _text(raw, "filename", context)
        if Path(directory).name != directory or Path(filename).name != filename:
            raise ModelManifestError(f"{context} directory and filename must not contain path traversal")
        target = (directory.casefold(), filename.casefold())
        if target in targets:
            raise ModelManifestError(f"duplicate model target: {directory}/{filename}")
        targets.add(target)
        repository = _text(raw, "repository", context)
        revision = _text(raw, "revision", context)
        source_path = _text(raw, "source_path", context)
        license_name = _text(raw, "license", context)
        sha256 = _text(raw, "sha256", context)
        size_bytes = raw.get("size_bytes")
        if not REPOSITORY.fullmatch(repository):
            raise ModelManifestError(f"{context}.repository must be a Hugging Face owner/name")
        if not HEX40.fullmatch(revision):
            raise ModelManifestError(f"{context}.revision must be a lowercase 40-character commit")
        if not HEX64.fullmatch(sha256):
            raise ModelManifestError(f"{context}.sha256 must be a lowercase SHA-256")
        if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes <= 0:
            raise ModelManifestError(f"{context}.size_bytes must be a positive integer")
        for key in config_keys:
            if key not in EXPECTED_CONFIG_KEYS:
                raise ModelManifestError(f"{context}.config_keys contains unknown key: {key}")
            if _config_value(defaults, key) != filename:
                raise ModelManifestError(f"{context}.filename does not match {key}")
            covered_keys.add(key)
        models.append(
            ModelArtifact(
                model_id, tuple(required_by), tuple(config_keys), directory,
                filename, repository, revision, source_path, license_name,
                size_bytes, sha256,
            )
        )
    if covered_keys != EXPECTED_CONFIG_KEYS:
        missing = ", ".join(sorted(EXPECTED_CONFIG_KEYS - covered_keys))
        raise ModelManifestError(f"model config coverage is incomplete: {missing}")
    return tuple(models)
