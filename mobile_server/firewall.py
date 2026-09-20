from __future__ import annotations

import subprocess
import sys

from .config import ConfigError, load_settings


RULE_NAME = "Comfy-Canvas-LAN"


def main() -> int:
    try:
        port = load_settings().server_port
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    subprocess.run(
        ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={RULE_NAME}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    result = subprocess.run(
        [
            "netsh", "advfirewall", "firewall", "add", "rule", f"name={RULE_NAME}",
            "dir=in", "action=allow", "protocol=TCP", f"localport={port}", "profile=private", "remoteip=localsubnet",
        ],
        check=False,
    )
    if result.returncode:
        return result.returncode
    print(f"已允许专用网络访问 TCP {port}；ComfyUI 后端仍只监听本机。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
