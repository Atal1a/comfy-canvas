from __future__ import annotations

import argparse
from getpass import getpass
import sqlite3
import sys

from fastapi import HTTPException

from . import app as app_module


def has_admin() -> bool:
    app_module.init_database()
    with sqlite3.connect(app_module.DATABASE) as db:
        return bool(db.execute("SELECT 1 FROM users WHERE role='admin' AND disabled=0").fetchone())


def main() -> int:
    parser = argparse.ArgumentParser(description="ComfyUI Mobile account administration")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="create the first administrator")
    create.add_argument("username", nargs="?")
    sub.add_parser("check", help="check whether an active administrator exists")
    args = parser.parse_args()
    if args.command == "check":
        if has_admin():
            print("Administrator account is ready.")
            return 0
        print("No administrator account exists.")
        print(r"Run: .venv\Scripts\python.exe -m mobile_server.admin create")
        return 1
    username = args.username or input("Administrator username: ").strip()
    password = getpass("Administrator password (10-128 characters): ")
    confirm = getpass("Confirm password: ")
    if password != confirm:
        print("Passwords do not match.", file=sys.stderr)
        return 2
    try:
        user = app_module.create_admin_account(username, password)
    except HTTPException as exc:
        print(str(exc.detail), file=sys.stderr)
        return 2
    print(f"Administrator created: {user['username']}")
    print("Existing private history now belongs to this account.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
