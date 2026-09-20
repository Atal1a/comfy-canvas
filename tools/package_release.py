"""Export a source snapshot without Git history or local runtime data."""
from __future__ import annotations

import argparse
import re
from pathlib import Path
import subprocess
import zipfile


EXCLUDED_PARTS = {
    ".git", ".venv", "node_modules", "runtime", "data", "models", "custom_nodes",
    "__pycache__", "static_dist", "test-results", "playwright-report",
}
EXCLUDED_SUFFIXES = {".key", ".pem", ".crt", ".sqlite", ".sqlite3", ".db", ".safetensors", ".gguf", ".pyc"}


def public_showcase_files(root: Path) -> set[Path]:
    """Follow public documentation links, leaving unused review media on disk."""
    pending = [root / name for name in ("README.md", "README.en.md") if (root / name).is_file()]
    visited: set[Path] = set()
    while pending:
        path = pending.pop().resolve()
        if path in visited or not path.is_relative_to(root.resolve()):
            continue
        if not path.is_file():
            raise ValueError(f"Missing public documentation target: {path.relative_to(root.resolve())}")
        visited.add(path)
        if path.suffix.lower() != ".md":
            continue
        content = path.read_text(encoding="utf-8")
        targets = re.findall(r'\]\(([^)]+)\)|(?:src|href)="([^"]+)"', content)
        for markdown, html in targets:
            target = (markdown or html).split("#", 1)[0].split("?", 1)[0].strip("<>")
            if not target or ":" in target or target.startswith("//"):
                continue
            pending.append(path.parent / target)
    return {p.relative_to(root.resolve()) for p in visited if "showcase" in p.parts}


def source_files(root: Path, include_new: bool = False) -> list[Path]:
    args = ["git", "ls-files", "-z", "--cached"]
    if include_new:
        args += ["--others", "--exclude-standard"]
    result = subprocess.run(args, cwd=root, capture_output=True, check=True)
    selected = []
    showcase = public_showcase_files(root)
    for name in sorted(set(result.stdout.decode("utf-8").split("\0")) - {""}):
        relative = Path(name)
        if relative.parts[:2] == ("docs", "showcase") and relative not in showcase:
            continue
        path = root / relative
        if not path.exists():
            continue
        if (relative.is_absolute() or ".." in relative.parts or path.is_symlink()
                or not path.resolve().is_relative_to(root.resolve())):
            raise ValueError(f"Unsafe source path: {name}")
        if (EXCLUDED_PARTS.intersection(relative.parts)
                or relative.suffix.lower() in EXCLUDED_SUFFIXES
                or relative.name == "config.toml" or relative.name.startswith(".env")
                or "portfolio" in relative.name.lower()):
            raise ValueError(f"Private/runtime file is selected for release: {name}")
        if path.is_file():
            selected.append(relative)
    return selected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--include-new", action="store_true", help="include non-ignored new files; inspect the archive before publishing")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    files = source_files(root, args.include_new)
    # Exclusive creation prevents accidentally replacing a previous release.
    with zipfile.ZipFile(args.output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in files:
            archive.write(root / relative, "comfy-canvas/" + relative.as_posix())
    print(f"Exported {len(files)} source files: {args.output.resolve()}")
    print("No Git history is included. Review licenses and archive contents before publishing.")


if __name__ == "__main__":
    main()
