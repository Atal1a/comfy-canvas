from __future__ import annotations

from ..model_paths import model_path

from dataclasses import dataclass
import json
from pathlib import Path
import secrets
from typing import Any

from fastapi import HTTPException

from ..workflow_compiler import compile_workflow


@dataclass(frozen=True)
class EnhancedUpscaleConfig:
    workflow_path: Path
    comfy_root: Path
    base_model: str
    text_encoder: str
    vae: str
    consistency_lora: str
    seedvr_dit: str
    seedvr_vae: str


def as_number(
    value: Any,
    name: str,
    minimum: float,
    maximum: float,
    integer: bool = False,
) -> int | float:
    try:
        parsed = int(value) if integer else float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"Invalid {name}") from exc
    if not minimum <= parsed <= maximum:
        raise HTTPException(400, f"{name} is outside the allowed range")
    return parsed


def as_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "1", "on"}:
        return True
    if isinstance(value, str) and value.lower() in {"false", "0", "off", ""}:
        return False
    if value in {0, 1}:
        return bool(value)
    raise HTTPException(400, f"Invalid {name}")


def normalize_enhanced_upscale_settings(
    payload: dict[str, Any],
    profiles: dict[str, dict[str, Any]],
    seedvr_max_seed: int,
) -> dict[str, Any]:
    resolution = as_number(
        payload.get("resolution", 4096),
        "enhanced upscale resolution",
        2048,
        4096,
        True,
    )
    if resolution not in {2048, 3072, 4096}:
        raise HTTPException(400, "Enhanced upscale resolution must be 2048, 3072, or 4096")
    style_profile = str(payload.get("style_profile") or "realistic_detail").strip().lower()
    profile = profiles.get(style_profile)
    if profile is None:
        raise HTTPException(400, "Enhanced upscale style profile is invalid")
    random_seed = as_bool(payload.get("random_seed", True), "enhanced upscale random seed")
    seed = as_number(
        payload.get("seed", 0),
        "enhanced upscale seed",
        0,
        18_446_744_073_709_551_615,
        True,
    )
    seed = secrets.randbits(32) if random_seed else seed % (seedvr_max_seed + 1)
    return {
        "engine": "flux2",
        "resolution": resolution,
        "style_profile": style_profile,
        "strength": as_number(
            payload.get("strength", profile["strength"]),
            "enhanced detail strength",
            0.4,
            0.9,
        ),
        "grain": as_number(
            payload.get("grain", profile["grain"]),
            "enhanced grain",
            0,
            0.5,
        ),
        "seed": seed,
        "random_seed": random_seed,
        "use_detail": as_bool(
            payload.get("use_detail", False),
            "enhanced Flux detail source",
        ),
        "prompt": profile["prompt"],
        "latent_noise_scale": profile["latent_noise_scale"],
    }


def build_enhanced_upscale_graph(
    config: EnhancedUpscaleConfig,
    settings: dict[str, Any],
    image_name: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not config.workflow_path.is_file():
        raise HTTPException(503, "Flux2 enhanced upscale workflow is not installed")
    required_models = (
        model_path(config.comfy_root, "diffusion_models", config.base_model),
        model_path(config.comfy_root, "text_encoders", config.text_encoder),
        config.comfy_root / "models" / "vae" / Path(config.vae),
        config.comfy_root / "models" / "SEEDVR2" / Path(config.seedvr_dit),
        config.comfy_root / "models" / "SEEDVR2" / Path(config.seedvr_vae),
    )
    missing = next((path.name for path in required_models if not path.is_file()), None)
    if missing:
        raise HTTPException(503, f"Flux2 enhanced upscale model is not installed: {missing}")
    overrides = {
        "120": {"image": image_name},
        "139": {
            "seed": settings["seed"],
            "latent_noise_scale": settings.get("latent_noise_scale", 0.0),
        },
        "186": {"scale_to_length": settings["resolution"]},
        "199": {
            "grain_power": settings["grain"],
            "grain_scale": max(0.1, settings["grain"]),
        },
        "201": {"prompt": settings["prompt"]},
        "211": {"seed": settings["seed"], "sampler_name": "res_2s"},
        "143": {"filename_prefix": "mobile/enhanced-upscale/Flux2_Klein_Enhanced"},
    }
    graph = compile_workflow(config.workflow_path, overrides)
    graph.update({
        "4": {"class_type": "UNETLoader", "inputs": {
            "unet_name": config.base_model, "weight_dtype": "default",
        }},
        "5": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": config.text_encoder, "type": "flux2", "device": "default",
        }},
        "6": {"class_type": "VAELoader", "inputs": {"vae_name": config.vae}},
        "131": {"class_type": "SeedVR2LoadVAEModel", "inputs": {
            "model": config.seedvr_vae, "device": "cuda:0",
            "encode_tiled": True, "encode_tile_size": 1024, "encode_tile_overlap": 128,
            "decode_tiled": True, "decode_tile_size": 1024, "decode_tile_overlap": 128,
            "tile_debug": "false", "offload_device": "cpu", "cache_model": False,
        }},
        "132": {"class_type": "SeedVR2LoadDiTModel", "inputs": {
            "model": config.seedvr_dit, "device": "cuda:0",
            "blocks_to_swap": 32, "swap_io_components": False,
            "offload_device": "cpu", "cache_model": False, "attention_mode": "sdpa",
        }},
        "9001": {"class_type": "VRAMCleanup", "inputs": {
            "offload_model": True, "offload_cache": True, "anything": ["202", 0],
        }},
    })
    graph["139"]["inputs"].update({"dit": ["132", 0], "vae": ["131", 0]})
    graph["201"]["inputs"].update({
        "clip": ["5", 0], "vae": ["6", 0], "image1": ["9001", 0],
    })
    lora_path = config.comfy_root / "models" / "loras" / Path(config.consistency_lora)
    if lora_path.is_file():
        graph["223"] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "model": ["4", 0],
                "lora_name": config.consistency_lora,
                "strength_model": settings["strength"],
            },
        }
        graph["211"]["inputs"]["model"] = ["223", 0]
    else:
        graph.pop("223", None)
        graph["211"]["inputs"]["model"] = ["4", 0]
    graph["83"]["inputs"]["vae"] = ["6", 0]
    editor = json.loads(config.workflow_path.read_text(encoding="utf-8"))
    return graph, editor
