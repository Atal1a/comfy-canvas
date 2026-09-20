from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any


H3_RUNTIME_PROFILE_VERSION = "comfy032-h3-v1"


def h3_settings_fingerprint(parameters: dict[str, Any]) -> str:
    """Return a stable identity for H3 crash-cooldown decisions."""
    loras = sorted(
        (str(item.get("name") or ""), round(float(item.get("weight") or 0), 4))
        for item in parameters.get("loras", []) if isinstance(item, dict)
    )
    settings = {
        "model": parameters.get("h3_model", "original_int8"),
        "mode": parameters.get("mode", "t2v"),
        "width": int(parameters.get("width") or 0),
        "height": int(parameters.get("height") or 0),
        "frames": int(parameters.get("frames") or 0),
        "steps": int(parameters.get("steps") or 0),
        "turbo": parameters.get("turbo_lora", ""),
        "turbo_strength": float(parameters.get("lora_strength") or 0),
        "low_vram": bool(parameters.get("low_vram")),
        "loras": loras,
    }
    encoded = json.dumps(settings, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def queue_profile(task_kind: str, parameters: dict[str, Any], workflow: str = "") -> str:
    """Describe comparable tasks without exposing prompts or user data."""
    def megapixel_bucket(value: Any) -> str:
        try:
            megapixels = max(0.25, float(value))
        except (TypeError, ValueError):
            megapixels = 1.0
        return f"{round(megapixels * 4) / 4:g}"

    if task_kind == "generation":
        if workflow == "minimax-h3":
            width = int(parameters.get("width") or 0)
            height = int(parameters.get("height") or 0)
            actual_mp = width * height / 1_000_000 if width and height else float(parameters.get("megapixels", 0.6))
            return (
                f"generation:minimax-h3:tier={parameters.get('performance_tier','standard')}:"
                f"model={parameters.get('h3_model','original_int8')}:"
                f"mode={parameters.get('mode','t2v')}:"
                f"mp={float(parameters.get('megapixels',0.6)):.1f}:"
                f"px={actual_mp:.2f}:"
                f"frames={int(parameters.get('frames',124))}:"
                f"steps={int(parameters.get('steps',6))}:"
                f"turbo={'v4' if 'v4_' in str(parameters.get('turbo_lora') or '') else 'v1'}:"
                f"low={int(bool(parameters.get('low_vram')))}:"
                f"loras={len(parameters.get('loras') or [])}:"
                f"runtime={H3_RUNTIME_PROFILE_VERSION}"
            )
        if workflow == "krea-identity-edit":
            model = parameters.get("krea_model", "official_turbo")
            megapixels = float(parameters.get("width", 1920)) * float(parameters.get("height", 1080)) / 1_000_000
            return f"generation:{workflow}:model={model}:mp={megapixel_bucket(megapixels)}:count={int(parameters.get('count', 1))}"
        megapixels = megapixel_bucket(parameters.get("stage1_scale_megapixels", 1.25))
        return f"generation:{workflow}:mp={megapixels}:count={int(parameters.get('count', 1))}"
    if task_kind == "preprocess":
        return f"preprocess:{parameters.get('model','3b')}:{parameters.get('resolution',1080)}"
    if task_kind == "prompt_tool":
        return f"prompt-tool:{parameters.get('operation','enhance')}:{parameters.get('target_workflow','qwen2511-modular-flux2')}"
    return f"upscale:{parameters.get('engine','seedvr2')}:{parameters.get('model','')}:{parameters.get('resolution',1080)}"


def queue_profile_candidates(profile_key: str) -> list[str]:
    candidates = [profile_key]
    if profile_key.startswith("generation:minimax-h3:"):
        legacy = re.sub(r":px=[^:]+", "", profile_key)
        legacy = re.sub(r":turbo=[^:]+:low=[^:]+:loras=[^:]+:runtime=[^:]+$", "", legacy)
        if legacy not in candidates:
            candidates.append(legacy)
    legacy = re.sub(r":mp=[^:]+", "", profile_key)
    if legacy != profile_key:
        candidates.append(legacy)
    return candidates


def queue_percentile(samples: list[float], fraction: float) -> float:
    if not samples:
        return 0
    index = (len(samples) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(samples) - 1)
    weight = index - lower
    return samples[lower] * (1 - weight) + samples[upper] * weight


def h3_profile_fields(profile_key: str) -> dict[str, Any]:
    """Read both the current and legacy H3 queue-profile spellings."""
    raw = dict(re.findall(r":([^:=]+)=([^:]+)", profile_key))

    def number(name: str, fallback: float) -> float:
        try:
            return float(raw.get(name, fallback))
        except (TypeError, ValueError):
            return fallback

    return {
        "mode": raw.get("mode", "t2v"),
        "model": raw.get("model", "original_int8"),
        "pixels": number("px", number("mp", 0.6)),
        "frames": max(1, int(number("frames", 124))),
        "steps": max(1, int(number("steps", 6))),
        "turbo": raw.get("turbo"),
        "low_vram": raw.get("low"),
        "loras": max(0, int(number("loras", 0))),
    }


def weighted_percentile(samples: list[tuple[float, float]], fraction: float) -> float:
    ordered = sorted((float(value), max(0.0, float(weight))) for value, weight in samples if weight > 0)
    if not ordered:
        return 0.0
    target = sum(weight for _, weight in ordered) * min(1.0, max(0.0, fraction))
    cumulative = 0.0
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= target:
            return value
    return ordered[-1][0]


def h3_similarity(target: dict[str, Any], sample: dict[str, Any]) -> float:
    """Rank comparable completed H3 runs without changing measured time."""
    pixel_gap = abs(math.log(max(sample["pixels"], 0.05) / max(target["pixels"], 0.05), 2))
    frame_gap = abs(sample["frames"] - target["frames"]) / max(sample["frames"], target["frames"], 1)
    step_gap = abs(sample["steps"] - target["steps"]) / max(sample["steps"], target["steps"], 1)
    score = 8.0 if sample["mode"] == target["mode"] else 0.5
    score += 3.0 if sample["model"] == target["model"] else 0.0
    score += max(0.0, 6.0 * (1.0 - min(1.0, pixel_gap)))
    score += max(0.0, 5.0 * (1.0 - min(1.0, frame_gap)))
    score += max(0.0, 4.0 * (1.0 - min(1.0, step_gap)))
    if target["turbo"] and sample["turbo"]:
        score += 2.0 if target["turbo"] == sample["turbo"] else 0.0
    if target["low_vram"] is not None and sample["low_vram"] is not None:
        score += 1.5 if target["low_vram"] == sample["low_vram"] else 0.0
    score += max(0.0, 1.0 - abs(sample["loras"] - target["loras"]) / 4.0)
    return score


def queue_default_estimate(profile_key: str) -> tuple[int, int]:
    if profile_key.startswith("generation:minimax-h3:"):
        return 0, 0
    if profile_key.startswith("preprocess:") or profile_key.startswith("upscale:seedvr2"):
        return 15, 45
    if profile_key.startswith("prompt-tool:"):
        return 30, 120
    if profile_key.startswith("generation:"):
        try:
            count = max(1, int(profile_key.rsplit("=", 1)[-1]))
        except ValueError:
            count = 1
        return 90 * count, 210 * count
    if profile_key.startswith("upscale:flux2"):
        resolution = int(profile_key.rsplit(":", 1)[-1] or 2048)
        return (150, 360) if resolution >= 4096 else (90, 240)
    return 60, 240


def queue_remaining_estimate(estimate: dict[str, Any], elapsed: int) -> tuple[int, int]:
    residuals = sorted(max(0.0, duration - elapsed) for duration in estimate["durations"] if duration > elapsed)
    if len(residuals) >= 3:
        low = max(5, round(queue_percentile(residuals, 0.25)))
        high = max(low + 5, round(queue_percentile(residuals, 0.75)))
        return low, high
    low = max(10, round(float(estimate["low"]) * 0.15))
    high = max(30, round(float(estimate["high"]) * 0.4))
    return low, high
