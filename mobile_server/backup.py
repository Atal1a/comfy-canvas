from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

from .app import AVATAR_DIR, CHAT_MEDIA_DIR, DATABASE, ROOT


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_tree(source: Path, target: Path) -> int:
    copied = 0
    if not source.is_dir():
        return copied
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        copied += 1
    return copied


def create_backup(destination: Path, include_chat_media: bool = False) -> Path:
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    backup_dir = destination.resolve() / timestamp
    backup_dir.mkdir(parents=True, exist_ok=False)
    database_backup = backup_dir / DATABASE.name
    with closing(sqlite3.connect(DATABASE, timeout=30)) as source, closing(sqlite3.connect(database_backup)) as target:
        source.backup(target)
    with closing(sqlite3.connect(database_backup)) as check:
        quick_check = str(check.execute("PRAGMA quick_check").fetchone()[0])
    if quick_check != "ok":
        raise RuntimeError(f"Backup integrity check failed: {quick_check}")
    avatar_count = copy_tree(AVATAR_DIR, backup_dir / "avatars")
    chat_media_count = copy_tree(CHAT_MEDIA_DIR, backup_dir / "chat_media") if include_chat_media else 0
    manifest = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "database": database_backup.name,
        "database_bytes": database_backup.stat().st_size,
        "database_sha256": sha256(database_backup),
        "quick_check": quick_check,
        "avatar_files": avatar_count,
        "chat_media_included": include_chat_media,
        "chat_media_files": chat_media_count,
    }
    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return backup_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a consistent Comfy Canvas runtime backup.")
    parser.add_argument(
        "--destination",
        type=Path,
        default=ROOT / "maintenance" / "backups",
        help="Directory that will contain timestamped backups.",
    )
    parser.add_argument(
        "--include-chat-media",
        action="store_true",
        help="Also copy chat uploads; generation outputs are intentionally excluded.",
    )
    args = parser.parse_args()
    print(create_backup(args.destination, args.include_chat_media))


if __name__ == "__main__":
    main()
