#!/usr/bin/env python3
"""Create the prioritizer's custom fields and attach them to every managed
project. Idempotent; run once per workspace (Starter plan or above).

  (set -a; source .env; set +a; .venv/bin/python scripts/setup_custom_fields.py [--dry-run])
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

import clients.asana as asana  # noqa: E402
from services import custom_fields as cf  # noqa: E402
from services import managed_projects  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    projects = sorted(managed_projects.gids())
    if not projects:
        raise SystemExit("ASANA_MANAGED_PROJECTS is empty — nothing to attach to")
    existing = {f["name"] for f in asana.list_custom_fields()}
    print(f"existing fields: {sorted(existing) or '—'}")
    print(
        f"would ensure {cf.STORY_POINTS!r} (number) and {cf.STARTED_AT!r} (date) on {len(projects)} project(s)"
    )
    if args.dry_run:
        return
    out = cf.ensure(projects)
    for name, gid in out.items():
        print(f"{name}: {gid}")
    print("done")


if __name__ == "__main__":
    main()
