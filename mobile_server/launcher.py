from __future__ import annotations

import os
import ipaddress
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse

import uvicorn

from .config import AppSettings, ConfigError, load_settings


def port_is_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def prepare_environment(settings: AppSettings) -> dict[str, str]:
    environment = os.environ.copy()
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    cache_paths = {
        "HF_HOME": settings.cache_dir / "huggingface",
        "PYTHONPYCACHEPREFIX": settings.cache_dir / "pycache",
        "NUMBA_CACHE_DIR": settings.cache_dir / "numba",
        "TORCH_HOME": settings.cache_dir / "torch",
        "XDG_CACHE_HOME": settings.cache_dir / "xdg",
    }
    environment.update({name: str(path) for name, path in cache_paths.items()})
    environment["PYTHONPATH"] = str(settings.comfyui_root)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONUTF8"] = "1"
    environment.setdefault("PYTORCH_CUDA_ALLOC_CONF", "backend:cudaMallocAsync")
    if settings.git_path.is_file():
        environment["GIT_PYTHON_GIT_EXECUTABLE"] = str(settings.git_path)
    path_entries = [
        settings.ffmpeg_path.parent if settings.ffmpeg_path.is_absolute() else None,
        settings.git_path.parent if settings.git_path.is_absolute() else None,
        Path(sys.executable).parent,
        Path(sys.executable).parent / "Scripts",
        Path(sys.prefix) / "Lib" / "site-packages" / "llama_cpp" / "lib",
        Path(sys.prefix) / "Lib" / "site-packages" / "torch" / "lib",
    ]
    environment["PATH"] = os.pathsep.join(
        [*(str(path) for path in path_entries if path and path.is_dir()), environment.get("PATH", "")]
    )
    return environment


def ensure_administrator(environment: dict[str, str]) -> None:
    from .admin import has_admin

    if has_admin():
        return
    print("首次运行需要创建管理员账号。")
    result = subprocess.run(
        [sys.executable, "-m", "mobile_server.admin", "create"],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("administrator setup was not completed")


def comfyui_command(settings: AppSettings, *, auto_launch: bool = False) -> list[str]:
    return [
        sys.executable,
        str(settings.comfyui_root / "main.py"),
        *(["--auto-launch"] if auto_launch else []),
        "--listen",
        settings.comfy_host,
        "--port",
        str(settings.comfy_port),
        *settings.comfy_launch_args,
        "--output-directory",
        str(settings.output_dir),
    ]


def start_comfyui(settings: AppSettings, environment: dict[str, str]) -> None:
    parsed = urlparse(settings.comfy_url)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ConfigError("comfyui.auto_start requires a localhost comfyui.url")
    if port_is_open(settings.comfy_host, settings.comfy_port):
        print(f"ComfyUI is already running at {settings.comfy_url}.")
        return
    main_script = settings.comfyui_root / "main.py"
    if not main_script.is_file():
        raise RuntimeError(f"ComfyUI is missing: {main_script}")
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        comfyui_command(settings),
        cwd=settings.comfyui_root,
        env=environment,
        creationflags=int(getattr(subprocess, "CREATE_NEW_CONSOLE", 0)),
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if port_is_open(settings.comfy_host, settings.comfy_port):
            return
        time.sleep(0.5)
    print("ComfyUI is still starting; queued work will resume when it becomes available.")


def local_addresses(host: str) -> list[str]:
    if host != "0.0.0.0":
        return [host]
    addresses = {"127.0.0.1"}
    try:
        for entry in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = ipaddress.ip_address(entry[4][0])
            if address.is_private and not address.is_unspecified:
                addresses.add(str(address))
    except OSError:
        pass
    return sorted(addresses)


def run() -> None:
    settings = load_settings()
    environment = prepare_environment(settings)
    os.environ.update(environment)
    if port_is_open("127.0.0.1", settings.server_port):
        raise RuntimeError(f"port {settings.server_port} is already in use")
    ensure_administrator(environment)
    if settings.comfy_auto_start:
        start_comfyui(settings, environment)
    print("\nComfy Canvas 已启动，可使用以下地址访问：")
    for address in local_addresses(settings.server_host):
        print(f"  http://{address}:{settings.server_port}")
    uvicorn.run(
        "mobile_server.app:app",
        host=settings.server_host,
        port=settings.server_port,
        proxy_headers=False,
    )


def main() -> int:
    try:
        run()
    except (ConfigError, RuntimeError, OSError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
