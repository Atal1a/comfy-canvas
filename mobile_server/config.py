from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import shutil
import sys
import tomllib
from typing import Any
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"
DEFAULT_WORKFLOWS = (
    "krea-turbo",
    "qwen2511-modular-flux2",
    "krea-identity-edit",
    "minimax-h3",
)


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class AppSettings:
    project_root: Path
    config_path: Path
    comfyui_root: Path
    data_dir: Path
    output_dir: Path
    cache_dir: Path
    ffmpeg_path: Path
    ffprobe_path: Path
    git_path: Path
    comfy_url: str
    comfy_auto_start: bool
    comfy_launch_args: tuple[str, ...]
    enabled_workflows: tuple[str, ...]
    server_host: str
    server_port: int
    session_days: int
    user_quota_bytes: int
    max_upload_bytes: int
    max_chat_video_bytes: int
    queue_user_limit: int
    user_generation_limit: int
    admin_generation_limit: int
    krea_identity_model: str
    krea_turbo_model: str
    krea_turbo_text_encoder: str
    krea_turbo_vae: str
    krea_identity_text_encoder: str
    krea_identity_vae: str
    krea_identity_lora: str
    h3_diffusion_model: str
    h3_text_encoder: str
    h3_video_vae: str
    h3_audio_vae: str
    h3_turbo_lora: str
    qwen2511_model: str
    qwen2511_text_encoder: str
    qwen2511_vae: str
    qwen2511_lightning_lora: str
    enhanced_upscale_model: str
    enhanced_upscale_text_encoder: str
    enhanced_upscale_vae: str
    enhanced_upscale_consistency_lora: str
    enhanced_upscale_seedvr_dit: str
    enhanced_upscale_seedvr_vae: str
    prompt_assistant_model: str
    prompt_assistant_mmproj: str
    prompt_assistant_chat_handler: str
    prompt_enhance_instruction: str
    prompt_interrogate_instruction: str

    @property
    def comfy_host(self) -> str:
        return str(urlparse(self.comfy_url).hostname)

    @property
    def comfy_port(self) -> int:
        return int(urlparse(self.comfy_url).port or 80)


def _table(document: dict[str, Any], name: str) -> dict[str, Any]:
    value = document.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a TOML table")
    return value


def _known_keys(table: dict[str, Any], section: str, expected: set[str]) -> None:
    unknown = set(table) - expected
    if unknown:
        names = ", ".join(f"{section}.{name}" for name in sorted(unknown))
        raise ConfigError(f"unknown configuration keys: {names}")


def _path(value: Any, default: str) -> Path:
    raw = os.path.expandvars(str(value if value is not None else default).strip())
    if not raw:
        raise ConfigError("configured paths cannot be empty")
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _executable(value: Any, default: str) -> Path:
    raw = os.path.expandvars(str(value if value is not None else default).strip())
    if not raw:
        raise ConfigError("configured executables cannot be empty")
    if not Path(raw).is_absolute() and "/" not in raw and "\\" not in raw:
        discovered = shutil.which(raw)
        return Path(discovered).resolve() if discovered else Path(raw)
    return _path(raw, default)


def _integer(value: Any, default: int, name: str, minimum: int, maximum: int) -> int:
    candidate = default if value is None else value
    if isinstance(candidate, bool):
        raise ConfigError(f"{name} must be an integer")
    try:
        result = int(candidate)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if not minimum <= result <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return result


def _megabytes(value: Any, default: int, name: str) -> int:
    return _integer(value, default, name, 1, 1024 * 1024) * 1024 * 1024


def _boolean(value: Any, default: bool, name: str) -> bool:
    candidate = default if value is None else value
    if not isinstance(candidate, bool):
        raise ConfigError(f"{name} must be true or false")
    return candidate


def _text(value: Any, default: str, name: str) -> str:
    candidate = str(default if value is None else value).strip()
    if not candidate or "\x00" in candidate:
        raise ConfigError(f"{name} cannot be empty")
    return candidate


def _load_document(config_path: Path) -> dict[str, Any]:
    if not config_path.is_file():
        return {}
    try:
        with config_path.open("rb") as source:
            return tomllib.load(source)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc


def load_settings(config_path: Path | None = None) -> AppSettings:
    selected = config_path or Path(os.environ.get("COMFY_CANVAS_CONFIG", DEFAULT_CONFIG_PATH))
    selected = selected.expanduser().resolve()
    document = _load_document(selected)
    unknown = set(document) - {"paths", "comfyui", "server", "limits", "features", "models", "prompt_assistant"}
    if unknown:
        raise ConfigError("unknown configuration sections: " + ", ".join(sorted(unknown)))
    paths = _table(document, "paths")
    comfyui = _table(document, "comfyui")
    server = _table(document, "server")
    limits = _table(document, "limits")
    features = _table(document, "features")
    models = _table(document, "models")
    prompt_assistant = _table(document, "prompt_assistant")
    krea_identity = _table(models, "krea_identity")
    krea_turbo = _table(models, "krea_turbo")
    minimax_h3 = _table(models, "minimax_h3")
    qwen2511 = _table(models, "qwen2511")
    enhanced_upscale = _table(models, "enhanced_upscale")
    _known_keys(paths, "paths", {"comfyui", "data", "output", "cache", "ffmpeg", "ffprobe", "git"})
    _known_keys(comfyui, "comfyui", {"url", "auto_start", "launch_args"})
    _known_keys(server, "server", {"host", "port"})
    _known_keys(features, "features", {"workflows"})
    _known_keys(models, "models", {"krea_turbo", "krea_identity", "minimax_h3", "qwen2511", "enhanced_upscale"})
    _known_keys(krea_turbo, "models.krea_turbo", {"base_model", "text_encoder", "vae"})
    _known_keys(
        krea_identity,
        "models.krea_identity",
        {"base_model", "text_encoder", "vae", "identity_lora"},
    )
    _known_keys(minimax_h3, "models.minimax_h3", {"diffusion_model", "text_encoder", "video_vae", "audio_vae", "turbo_lora"})
    _known_keys(qwen2511, "models.qwen2511", {"base_model", "text_encoder", "vae", "lightning_lora"})
    _known_keys(
        enhanced_upscale,
        "models.enhanced_upscale",
        {"base_model", "text_encoder", "vae", "consistency_lora", "seedvr_dit", "seedvr_vae"},
    )
    _known_keys(prompt_assistant, "prompt_assistant", {"model", "mmproj", "chat_handler", "enhance_instruction", "interrogate_instruction"})
    _known_keys(
        limits,
        "limits",
        {
            "session_days", "user_quota_mb", "image_upload_mb", "chat_video_upload_mb",
            "queued_tasks_per_user", "images_per_user_task", "images_per_admin_task",
        },
    )
    url = str(comfyui.get("url", "http://127.0.0.1:8388")).strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme != "http" or not parsed.hostname or parsed.path not in {"", "/"}:
        raise ConfigError("comfyui.url must be an http URL without a path")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ConfigError("comfyui.url contains an invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ConfigError("comfyui.url port must be between 1 and 65535")
    launch_args = comfyui.get(
        "launch_args",
        ["--reserve-vram", "1.5", "--vram-headroom", "1.0", "--preview-method", "none"],
    )
    if not isinstance(launch_args, list) or not all(isinstance(item, str) for item in launch_args):
        raise ConfigError("comfyui.launch_args must be an array of strings")
    workflows = features.get("workflows", list(DEFAULT_WORKFLOWS))
    if not isinstance(workflows, list) or not workflows or not all(isinstance(item, str) and item for item in workflows):
        raise ConfigError("features.workflows must be a non-empty array of strings")
    if len(workflows) != len(set(workflows)):
        raise ConfigError("features.workflows cannot contain duplicates")
    return AppSettings(
        project_root=PROJECT_ROOT,
        config_path=selected,
        comfyui_root=_path(paths.get("comfyui"), "runtime/ComfyUI"),
        data_dir=_path(paths.get("data"), "data"),
        output_dir=_path(paths.get("output"), "data/output"),
        cache_dir=_path(paths.get("cache"), "data/cache"),
        ffmpeg_path=_executable(paths.get("ffmpeg"), "ffmpeg"),
        ffprobe_path=_executable(paths.get("ffprobe"), "ffprobe"),
        git_path=_executable(paths.get("git"), "git"),
        comfy_url=url,
        comfy_auto_start=_boolean(comfyui.get("auto_start"), False, "comfyui.auto_start"),
        comfy_launch_args=tuple(launch_args),
        enabled_workflows=tuple(workflows),
        server_host=str(server.get("host", "0.0.0.0")).strip() or "0.0.0.0",
        server_port=_integer(server.get("port"), 8090, "server.port", 1, 65535),
        session_days=_integer(limits.get("session_days"), 14, "limits.session_days", 1, 365),
        user_quota_bytes=_megabytes(limits.get("user_quota_mb"), 20 * 1024, "limits.user_quota_mb"),
        max_upload_bytes=_megabytes(limits.get("image_upload_mb"), 50, "limits.image_upload_mb"),
        max_chat_video_bytes=_megabytes(limits.get("chat_video_upload_mb"), 500, "limits.chat_video_upload_mb"),
        queue_user_limit=_integer(limits.get("queued_tasks_per_user"), 3, "limits.queued_tasks_per_user", 1, 100),
        user_generation_limit=_integer(limits.get("images_per_user_task"), 4, "limits.images_per_user_task", 1, 100),
        admin_generation_limit=_integer(limits.get("images_per_admin_task"), 8, "limits.images_per_admin_task", 1, 100),
        krea_identity_model=_text(krea_identity.get("base_model"), "krea2_turbo_fp8_scaled.safetensors", "models.krea_identity.base_model"),
        krea_turbo_model=_text(krea_turbo.get("base_model", krea_identity.get("base_model")), "krea2_turbo_fp8_scaled.safetensors", "models.krea_turbo.base_model"),
        krea_turbo_text_encoder=_text(krea_turbo.get("text_encoder", krea_identity.get("text_encoder")), "qwen3vl_4b_fp8_scaled.safetensors", "models.krea_turbo.text_encoder"),
        krea_turbo_vae=_text(krea_turbo.get("vae", krea_identity.get("vae")), "qwen_image_vae.safetensors", "models.krea_turbo.vae"),
        krea_identity_text_encoder=_text(krea_identity.get("text_encoder"), "qwen3vl_4b_fp8_scaled.safetensors", "models.krea_identity.text_encoder"),
        krea_identity_vae=_text(krea_identity.get("vae"), "qwen_image_vae.safetensors", "models.krea_identity.vae"),
        krea_identity_lora=_text(krea_identity.get("identity_lora"), "krea2_identity_edit_v1_2.safetensors", "models.krea_identity.identity_lora"),
        h3_diffusion_model=_text(minimax_h3.get("diffusion_model"), "minimax_h3_fl2va_pruned_int8_convrot.safetensors", "models.minimax_h3.diffusion_model"),
        h3_text_encoder=_text(minimax_h3.get("text_encoder"), "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "models.minimax_h3.text_encoder"),
        h3_video_vae=_text(minimax_h3.get("video_vae"), "minimax_h3_video_vae_fp16.safetensors", "models.minimax_h3.video_vae"),
        h3_audio_vae=_text(minimax_h3.get("audio_vae"), "minimax_h3_audio_vae_fp32.safetensors", "models.minimax_h3.audio_vae"),
        h3_turbo_lora=_text(minimax_h3.get("turbo_lora"), "minimax_h3_turbo_v4_step600_ema.safetensors", "models.minimax_h3.turbo_lora"),
        qwen2511_model=_text(qwen2511.get("base_model"), "qwen_image_edit_2511_fp8_e4m3fn_scaled.safetensors", "models.qwen2511.base_model"),
        qwen2511_text_encoder=_text(qwen2511.get("text_encoder"), "qwen_2.5_vl_7b_fp8_scaled.safetensors", "models.qwen2511.text_encoder"),
        qwen2511_vae=_text(qwen2511.get("vae"), "qwen_image_vae.safetensors", "models.qwen2511.vae"),
        qwen2511_lightning_lora=_text(qwen2511.get("lightning_lora"), "Qwen-Image-Edit-2511-Lightning-8steps-V1.0-bf16.safetensors", "models.qwen2511.lightning_lora"),
        enhanced_upscale_model=_text(enhanced_upscale.get("base_model"), "flux-2-klein-9b-fp8.safetensors", "models.enhanced_upscale.base_model"),
        enhanced_upscale_text_encoder=_text(enhanced_upscale.get("text_encoder"), "qwen_3_8b_fp8mixed.safetensors", "models.enhanced_upscale.text_encoder"),
        enhanced_upscale_vae=_text(enhanced_upscale.get("vae"), "flux2-vae.safetensors", "models.enhanced_upscale.vae"),
        enhanced_upscale_consistency_lora=_text(enhanced_upscale.get("consistency_lora"), r"flux\f2k_9B_小志一致性（0.5-0.7）lcs_consist_0412预览版preview.safetensors", "models.enhanced_upscale.consistency_lora"),
        enhanced_upscale_seedvr_dit=_text(enhanced_upscale.get("seedvr_dit"), "seedvr2_ema_3b_fp8_e4m3fn.safetensors", "models.enhanced_upscale.seedvr_dit"),
        enhanced_upscale_seedvr_vae=_text(enhanced_upscale.get("seedvr_vae"), "ema_vae_fp16.safetensors", "models.enhanced_upscale.seedvr_vae"),
        prompt_assistant_model=_text(prompt_assistant.get("model"), "Qwen3.5-9B-Q4_K_M.gguf", "prompt_assistant.model"),
        prompt_assistant_mmproj=_text(prompt_assistant.get("mmproj"), "mmproj-Qwen3.5-9B-BF16.gguf", "prompt_assistant.mmproj"),
        prompt_assistant_chat_handler=_text(prompt_assistant.get("chat_handler"), "Qwen3.5", "prompt_assistant.chat_handler"),
        prompt_enhance_instruction=_text(prompt_assistant.get("enhance_instruction"), "Improve the user's prompt while preserving its intent and explicit details. Return only the improved prompt.", "prompt_assistant.enhance_instruction"),
        prompt_interrogate_instruction=_text(prompt_assistant.get("interrogate_instruction"), "Describe the visible image accurately for reuse as an image-generation prompt. Return only the description.", "prompt_assistant.interrogate_instruction"),
    )


def public_settings(settings: AppSettings) -> dict[str, Any]:
    payload = asdict(settings)
    payload["comfy_launch_args"] = list(settings.comfy_launch_args)
    payload["enabled_workflows"] = list(settings.enabled_workflows)
    return {name: str(value) if isinstance(value, Path) else value for name, value in payload.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and inspect Comfy Canvas configuration")
    parser.add_argument("command", choices=("show", "check"), nargs="?", default="show")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if args.command == "check":
        print(f"Configuration is valid: {settings.config_path}")
    else:
        print(json.dumps(public_settings(settings), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
