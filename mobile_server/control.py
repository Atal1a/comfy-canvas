from __future__ import annotations

import argparse
import asyncio
import sqlite3

from . import app as mobile_app


def administrator_id() -> int:
    with sqlite3.connect(mobile_app.DATABASE) as db:
        row = db.execute(
            "SELECT user_id FROM users WHERE role='admin' AND disabled=0 ORDER BY user_id LIMIT 1"
        ).fetchone()
    if not row:
        raise RuntimeError("没有可用的管理员账号")
    return int(row[0])


async def apply_mode(enabled: bool) -> None:
    admin_id = administrator_id()
    await mobile_app.set_performance_mode(enabled, admin_id)
    if enabled:
        # The web dispatcher may already be between claiming a task and saving
        # its Comfy prompt id. Recheck briefly so a cross-process toggle cannot
        # miss that narrow dispatch window.
        for _ in range(3):
            await asyncio.sleep(0.75)
            await mobile_app.set_performance_mode(True, admin_id)


def main() -> int:
    parser = argparse.ArgumentParser(description="控制 Comfy Canvas 电脑独占模式")
    parser.add_argument("action", choices=("on", "off", "toggle", "status"), nargs="?", default="toggle")
    args = parser.parse_args()
    mobile_app.init_database()
    current = mobile_app.performance_mode_enabled()
    if args.action == "status":
        print("电脑独占模式：" + ("已开启" if current else "已关闭"))
        return 0
    enabled = not current if args.action == "toggle" else args.action == "on"
    asyncio.run(apply_mode(enabled))
    print("电脑独占模式：" + ("已开启，Comfy Canvas 队列已暂停" if enabled else "已关闭，队列将自动恢复"))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"操作失败：{exc}")
        raise SystemExit(1)
