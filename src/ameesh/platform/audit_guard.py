# SPDX-License-Identifier: AGPL-3.0-only
"""L234 : scope borné pour un tour exempté de la pression.

Ce fichier est aussi un trampoline autonome : aucune dépendance au paquet
installé dans le dossier de travail du harnais. Il refuse d'exécuter la
commande si systemd a accepté MemoryMax sans contrôle mémoire effectif.
"""
from pathlib import Path
import os
import shutil
import sys

MEMORY_MAX = 2 * 1024 ** 3
WALL_SECONDS = 300
READY = "AMEESH_AUDIT_GUARD_READY"


def available():
    return sys.platform == "linux" and shutil.which("systemd-run") is not None


def command(argv, seconds):
    if not available():
        raise RuntimeError("auditor memory scope unavailable")
    seconds = min(WALL_SECONDS, float(seconds))
    if seconds <= 0:
        raise RuntimeError("invalid auditor deadline")
    return [shutil.which("systemd-run"), "--scope", "--quiet", "--no-ask-password",
            *(["--user"] if os.geteuid() != 0 else []),
            "--property=MemoryMax=%d" % MEMORY_MAX,
            "--property=MemorySwapMax=0", "--property=OOMPolicy=kill",
            "--property=RuntimeMaxSec=%g" % seconds,
            "--property=TimeoutStopSec=1s", "--property=KillMode=control-group",
            "--", sys.executable, str(Path(__file__).resolve()), str(MEMORY_MAX),
            "--", *argv]


def memory_enforced(limit, *, proc=Path("/proc/self/cgroup"), root=Path("/sys/fs/cgroup")):
    """Vérifie le plafond du scope courant avant le lancement du harnais."""
    try:
        cgroup = next(line[3:] for line in proc.read_text().splitlines()
                      if line.startswith("0::"))
        target = (root / cgroup.lstrip("/")).resolve()
        target.relative_to(root.resolve())
        maximum = int((target / "memory.max").read_text().strip())
        swap = int((target / "memory.swap.max").read_text().strip())
        return 0 < maximum <= limit and swap == 0
    except (OSError, ValueError, StopIteration):
        return False


def main(args):
    limit = int(args[0])
    if args[1] != "--" or not memory_enforced(limit):
        print("L234: no memory control on the scope; harness not run"
, file=sys.stderr)
        return 125
    print(READY, flush=True)
    os.execvpe(args[2], args[2:], os.environ)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
