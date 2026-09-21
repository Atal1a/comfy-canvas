"""Offline installation diagnostics for source releases."""
from __future__ import annotations

from .model_paths import model_path

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
import sys
from typing import Iterable

from .config import AppSettings, ConfigError, load_settings
from .dependency_manifest import DependencyManifestError, load_dependency_manifest
from .model_manifest import ModelManifestError, load_model_manifest
from .workflow_compiler import WorkflowCompileError, get_spec, validate_mobile_workflows


@dataclass(frozen=True)
class Check:
    level: str
    name: str
    detail: str


def _model_requirements(settings: AppSettings) -> dict[str, tuple[tuple[str, str], ...]]:
    return {
        "krea-turbo": (
            ("diffusion_models", settings.krea_turbo_model),
            ("text_encoders", settings.krea_turbo_text_encoder),
            ("vae", settings.krea_turbo_vae),
        ),
        "krea-identity-edit": (
            ("diffusion_models", settings.krea_identity_model),
            ("text_encoders", settings.krea_identity_text_encoder),
            ("vae", settings.krea_identity_vae),
            ("loras", settings.krea_identity_lora),
        ),
        "minimax-h3": (
            ("diffusion_models", settings.h3_diffusion_model),
            ("text_encoders", settings.h3_text_encoder),
            ("vae", settings.h3_video_vae),
            ("vae", settings.h3_audio_vae),
            ("loras", settings.h3_turbo_lora),
        ),
        "qwen2511-modular-flux2": (
            ("diffusion_models", settings.qwen2511_model),
            ("text_encoders", settings.qwen2511_text_encoder),
            ("vae", settings.qwen2511_vae),
            ("loras", settings.qwen2511_lightning_lora),
        ),
    }


def _command_available(path: Path) -> bool:
    value = str(path)
    return path.is_file() if path.is_absolute() or path.parent != Path(".") else bool(shutil.which(value))


def collect_checks(
    settings: AppSettings,
    source_only: bool = False,
    verify_model_hashes: bool = False,
) -> list[Check]:
    checks: list[Check] = []
    version_ok = sys.version_info >= (3, 11)
    checks.append(Check("ok" if version_ok else "error", "python", sys.version.split()[0]))

    workflows_dir = settings.project_root / "mobile_server" / "workflows"
    try:
        validate_mobile_workflows(workflows_dir, settings.enabled_workflows)
    except WorkflowCompileError as exc:
        checks.append(Check("error", "workflows", str(exc)))
    else:
        labels = ", ".join(get_spec(key).label for key in settings.enabled_workflows)
        checks.append(Check("ok", "workflows", labels or "none enabled"))

    config_detail = str(settings.config_path) if settings.config_path else "built-in defaults"
    checks.append(Check("ok", "config", config_detail))
    try:
        dependencies = load_dependency_manifest(settings.project_root)
    except DependencyManifestError as exc:
        dependencies = ()
        checks.append(Check("error", "dependencies", str(exc)))
    else:
        checks.append(Check("ok", "dependencies", f"{len(dependencies)} pinned public custom nodes"))
    try:
        model_artifacts = load_model_manifest(settings.project_root)
    except ModelManifestError as exc:
        model_artifacts = ()
        checks.append(Check("error", "model-manifest", str(exc)))
    else:
        checks.append(Check("ok", "model-manifest", f"{len(model_artifacts)} pinned model artifacts"))
    if source_only:
        return checks

    for name, executable in (
        ("git", settings.git_path),
        ("ffmpeg", settings.ffmpeg_path),
        ("ffprobe", settings.ffprobe_path),
    ):
        checks.append(
            Check(
                "ok" if _command_available(executable) else "warning",
                name,
                str(executable) if _command_available(executable) else f"not found: {executable}",
            )
        )

    comfy_main = settings.comfyui_root / "main.py"
    checks.append(
        Check(
            "ok" if comfy_main.is_file() else "error",
            "comfyui",
            str(comfy_main) if comfy_main.is_file() else f"missing: {comfy_main}",
        )
    )
    enabled = set(settings.enabled_workflows)
    main_dependencies = [
        item for item in dependencies
        if item.install_profile == "main" and enabled.intersection(item.required_by)
    ]
    optional_dependencies = [item for item in dependencies if item.install_profile == "optional"]
    custom_nodes_root = settings.comfyui_root / "custom_nodes"
    missing_main = [item.name for item in main_dependencies if not (custom_nodes_root / item.name).is_dir()]
    missing_optional = [item.name for item in optional_dependencies if not (custom_nodes_root / item.name).is_dir()]
    checks.append(
        Check(
            "ok" if not missing_main else "error",
            "nodes:main",
            "all enabled workflow nodes found" if not missing_main else "missing: " + ", ".join(missing_main),
        )
    )
    checks.append(
        Check(
            "ok" if not missing_optional else "warning",
            "nodes:optional",
            "all optional feature nodes found" if not missing_optional else "missing: " + ", ".join(missing_optional),
        )
    )
    requirements = _model_requirements(settings)
    for workflow in settings.enabled_workflows:
        missing = [
            f"models/{folder}/{filename}"
            for folder, filename in requirements.get(workflow, ())
            if not model_path(settings.comfyui_root, folder, filename).is_file()
        ]
        checks.append(
            Check(
                "ok" if not missing else "warning",
                f"models:{workflow}",
                "all configured files found" if not missing else "missing: " + ", ".join(missing),
            )
        )

    for artifact in model_artifacts:
        path = settings.comfyui_root / "models" / artifact.directory / artifact.filename
        if not path.is_file():
            continue
        actual_size = path.stat().st_size
        if actual_size != artifact.size_bytes:
            checks.append(
                Check(
                    "error",
                    f"model-file:{artifact.id}",
                    f"size mismatch: expected {artifact.size_bytes}, found {actual_size}",
                )
            )
        elif verify_model_hashes:
            digest = hashlib.sha256()
            with path.open("rb") as source:
                while chunk := source.read(8 * 1024 * 1024):
                    digest.update(chunk)
            actual_hash = digest.hexdigest()
            checks.append(
                Check(
                    "ok" if actual_hash == artifact.sha256 else "error",
                    f"model-file:{artifact.id}",
                    "SHA-256 verified" if actual_hash == artifact.sha256 else f"SHA-256 mismatch: {actual_hash}",
                )
            )

    return checks


def _print_checks(checks: Iterable[Check]) -> None:
    marks = {"ok": "OK", "warning": "WARN", "error": "ERROR"}
    for check in checks:
        print(f"[{marks[check.level]:5}] {check.name}: {check.detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a Comfy Canvas source installation")
    parser.add_argument("--config", type=Path, help="path to config.toml")
    parser.add_argument("--source-only", action="store_true", help="validate only files kept in Git")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    parser.add_argument(
        "--verify-model-hashes",
        action="store_true",
        help="read installed pinned models and verify SHA-256 (slow)",
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit machine-readable output")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
        checks = collect_checks(settings, args.source_only, args.verify_model_hashes)
    except ConfigError as exc:
        checks = [Check("error", "config", str(exc))]
    if args.as_json:
        print(json.dumps([check.__dict__ for check in checks], ensure_ascii=False, indent=2))
    else:
        _print_checks(checks)
    failed = any(check.level == "error" for check in checks)
    if args.strict:
        failed = failed or any(check.level == "warning" for check in checks)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
