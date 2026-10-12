# SPDX-License-Identifier: AGPL-3.0-only
"""Keep dsh's tool processes in the caller's cgroup (lot #490)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .platform import NotAvailable
from .platform.tool_cgroups import check, unified_cgroup


PRELOAD = Path(__file__).with_name("deepseek_cgroup.cjs").resolve()


def node_options(existing: str = "") -> str:
    """Node parses double-quoted option values, rather than shell quoting."""
    option = "--require=" + json.dumps(str(PRELOAD), ensure_ascii=False)
    if option in existing:
        return existing
    return (existing + " " + option).strip()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Read-only tool cgroup placement check")
    parser.add_argument("--runner-pid", type=int, required=True)
    args = parser.parse_args()
    try:
        report = check(args.runner_pid)
    except (OSError, ValueError, IndexError, NotAvailable) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    print(json.dumps(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
