from __future__ import annotations

import subprocess
import sys

from .config import ConfigError, load_settings
from .launcher import comfyui_command, prepare_environment


def main() -> int:
    try:
        settings = load_settings()
        if not (settings.comfyui_root / "main.py").is_file():
            raise RuntimeError(f"ComfyUI is missing: {settings.comfyui_root}")
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        return subprocess.run(
            comfyui_command(settings, auto_launch=True),
            cwd=settings.comfyui_root,
            env=prepare_environment(settings),
            check=False,
        ).returncode
    except (ConfigError, RuntimeError, OSError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
