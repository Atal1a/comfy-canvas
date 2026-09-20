"""Resolve the standard model directory aliases supported by ComfyUI."""
from pathlib import Path


def model_path(comfy_root: Path, directory: str, filename: str) -> Path:
    directories = {
        "text_encoders": ("text_encoders", "clip"),
        "diffusion_models": ("unet", "diffusion_models"),
    }.get(directory, (directory,))
    candidates = [comfy_root / "models" / folder / filename for folder in directories]
    return next((path for path in candidates if path.is_file()),
                comfy_root / "models" / directory / filename)
