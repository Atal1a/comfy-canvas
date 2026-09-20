"""Explicitly rebuild canonical mobile workflow assets for maintenance."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mobile_server.workflow_compiler import ensure_mobile_workflows


DESKTOP = ROOT / "workflow_sources"
MOBILE = ROOT / "mobile_server" / "workflows"


def main() -> None:
    ensure_mobile_workflows(DESKTOP, MOBILE)
    print("Canonical mobile workflows rebuilt and ready for review.")


if __name__ == "__main__":
    main()
