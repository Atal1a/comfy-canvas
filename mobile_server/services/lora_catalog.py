from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import re
import struct
from typing import Any


@lru_cache(maxsize=1024)
def _cached_safetensors_lora_signature(
    path_value: str,
    size: int,
    modified_ns: int,
) -> tuple[dict[str, Any], str, list[str]] | None:
    """Read a safetensors header once per file revision."""
    path = Path(path_value)
    try:
        with path.open("rb") as file:
            header_size = struct.unpack("<Q", file.read(8))[0]
            if header_size < 2 or header_size > 64 * 1024 * 1024:
                return None
            header = json.loads(file.read(header_size))
    except (OSError, ValueError, struct.error, json.JSONDecodeError):
        return None
    if not isinstance(header, dict):
        return None
    metadata_values = header.get("__metadata__", {})
    if not isinstance(metadata_values, dict):
        metadata_values = {}
    metadata = json.dumps(metadata_values, ensure_ascii=False).lower()
    normalized_metadata = re.sub(r"[^a-z0-9]+", "", metadata)
    names = [name.lower() for name in header if name != "__metadata__"]
    return metadata_values, normalized_metadata, names


def safetensors_lora_signature(path: Path) -> tuple[dict[str, Any], str, list[str]] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return _cached_safetensors_lora_signature(
        str(path.resolve()), stat.st_size, stat.st_mtime_ns,
    )


def normalized_lora_model_metadata(metadata_values: dict[str, Any]) -> str:
    values = [
        f"{key}:{value}"
        for key, value in metadata_values.items()
        if any(
            marker in re.sub(r"[^a-z0-9]+", "", str(key).lower())
            for marker in ("model", "architecture", "arch")
        )
    ]
    return re.sub(r"[^a-z0-9]+", "", json.dumps(values, ensure_ascii=False).lower())


def normalized_lora_architecture_metadata(metadata_values: dict[str, Any]) -> str:
    values = [
        f"{key}:{value}"
        for key, value in metadata_values.items()
        if "arch" in re.sub(r"[^a-z0-9]+", "", str(key).lower())
    ]
    return re.sub(r"[^a-z0-9]+", "", json.dumps(values, ensure_ascii=False).lower())


@dataclass(frozen=True)
class LoraClassification:
    family: str | None
    confidence: str
    evidence: str
    reserved: bool = False


@dataclass(frozen=True)
class LoraClassifier:
    """Classify LoRAs by architecture without inspecting content names."""

    krea_identity_lora: str
    h3_turbo_loras: tuple[str, ...]

    def classify(self, path: Path) -> LoraClassification:
        filename = path.name.lower()
        if path.name == self.krea_identity_lora:
            return LoraClassification("krea2", "exact", "built-in Krea2 LoRA", True)
        if path.name in self.h3_turbo_loras:
            return LoraClassification("minimax_h3", "exact", "built-in MiniMax H3 Turbo LoRA", True)
        if filename.endswith("qwen-image-edit-2511-lightning-8steps-v1.0-bf16.safetensors"):
            return LoraClassification("qwen2511", "exact", "built-in Qwen 2511 Lightning LoRA", True)
        if filename == "add_real_details.safetensors":
            return LoraClassification(None, "exact", "built-in detail-upscale LoRA", True)

        signature = safetensors_lora_signature(path)
        if not signature:
            parent_hint = re.sub(r"[^a-z0-9]+", "", path.parent.name.lower())
            if parent_hint == "minimaxh3":
                return LoraClassification("minimax_h3", "directory", "minimax_h3 directory")
            return LoraClassification(None, "none", "unreadable or empty safetensors header")
        metadata_values, _normalized_metadata, names = signature
        model_metadata = normalized_lora_model_metadata(metadata_values)
        architecture_metadata = normalized_lora_architecture_metadata(metadata_values)
        path_parts = {re.sub(r"[^a-z0-9]+", "", part.lower()) for part in path.parts}

        if "minimaxh3" in architecture_metadata:
            return LoraClassification("minimax_h3", "high", "MiniMax H3 architecture metadata")
        if "qwenimage" in architecture_metadata:
            return LoraClassification("qwen2511", "high", "Qwen Image architecture metadata")
        if "krea2" in architecture_metadata:
            return LoraClassification("krea2", "high", "Krea2 architecture metadata")
        unsupported = ("wan21", "wan22", "hunyuanvideo", "flux1", "flux2")
        if any(token in architecture_metadata for token in unsupported):
            return LoraClassification(None, "high", "unsupported model architecture")
        if "minimaxh3" in model_metadata or "minimaxh3" in path_parts:
            return LoraClassification("minimax_h3", "high", "MiniMax H3 model metadata or directory")
        if "krea2" in model_metadata:
            return LoraClassification("krea2", "high", "Krea2 model metadata")
        if "qwenimage" in model_metadata:
            return LoraClassification("qwen2511", "high", "Qwen Image model metadata")
        if any(token in model_metadata for token in unsupported):
            return LoraClassification(None, "high", "unsupported model architecture")

        if any(re.search(r"(?:^|\.)transformer_blocks\.\d+\.attn\.add_[kqv]_proj", name) for name in names):
            return LoraClassification("qwen2511", "high", "Qwen Image joint-attention tensors")
        if any(name.startswith("lora_unet_transformer_blocks_") for name in names):
            return LoraClassification("qwen2511", "medium", "Qwen Image kohya tensor namespace")
        if any(name.startswith("lora_unet_blocks_") and "_attn_qkv_proj" in name for name in names):
            return LoraClassification("minimax_h3", "high", "MiniMax H3 qkv projection tensors")
        if any(
            name.startswith("transformer.text_fusion.")
            or name.startswith("diffusion_model.text_fusion.")
            for name in names
        ):
            return LoraClassification("krea2", "high", "Krea2 text-fusion tensors")
        network = str(metadata_values.get("network", "")).lower()
        if network in {"lokr", "loha", "lycoris"} and any(
            name.startswith("lycoris_text_fusion_") for name in names
        ):
            return LoraClassification("krea2", "high", "Krea2 LyCORIS text-fusion tensors")
        if any(
            name.startswith("diffusion_model.blocks.")
            or name.startswith("lora_unet_blocks_")
            for name in names
        ):
            return LoraClassification("krea2", "medium", "Krea2 block tensor namespace")
        if any(name.startswith("single_transformer_blocks.") for name in names):
            return LoraClassification(None, "high", "unsupported FLUX tensor namespace")
        return LoraClassification(None, "none", "no supported model signature")
