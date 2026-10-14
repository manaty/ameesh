#!/usr/bin/env python3
"""#1107 — relevé initial groupé des P1 SLO (lecture seule).

Exécute le moteur SLO de la tranche 2 (#1035, tête 5b0189769) contre la base
vivante, sans écriture : liste les alertes `slo_breach` qu'une première passe
d'activation produirait, sépare les P1 (owner notifié, urgent) et les groupe.

Usage : PYTHONPATH=<worktree t2>/src python3 releve-p1.py [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
import time

sys.path.insert(0, "/home/ameesh/work/mty-amh-impl-03-l789t2/src")

from ameesh import config, db, exploitation, storage  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    cfg = config.load()
    conn = db.open_db(cfg)
    try:
        listing = storage.of(conn).operations.listing()
        now = time.time()
        rows = exploitation._slo_breaches(cfg, conn, listing, now)
    finally:
        conn.close()

    crit = [a for a in rows if a.get("critical")]
    noncrit = [a for a in rows if not a.get("critical")]
    by_level: dict[str, int] = {}
    for a in rows:
        by_level[str(a.get("level"))] = by_level.get(str(a.get("level")), 0) + 1

    if args.json:
        out = {
            "now": now,
            "total_breaches": len(rows),
            "critical": len(crit),
            "non_critical": len(noncrit),
            "by_level": by_level,
            "lots": sorted({a.get("lot") for a in rows}),
            "critical_alerts": crit,
            "non_critical_alerts": noncrit,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 0

    print("# Relevé initial groupé — alertes `slo_breach` (lecture seule)")

    def line(a):
        since = a.get("since")
        ts = (time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(since))
              if since else "—")
        return ("| #%s | %s | %s | %s | %s | %s | %ss (cible %ss) | %s | %s |"
                % (a.get("lot"), (a.get("title") or "")[:52], a.get("lot_state"),
                   a.get("agent") or "—", a.get("step"), a.get("level"),
                   a.get("value"), a.get("threshold"),
                   ", ".join(a.get("targets") or []), ts))

    print("\nTotal alertes : %d — P1 (owner, urgent) : %d — autres : %d"
          % (len(rows), len(crit), len(noncrit)))
    print("Par niveau : %s" % by_level)
    print("Lots distincts : %d" % len({a.get("lot") for a in rows}))

    print("\n## P1 historiques (notifiés à l'owner, urgent)")
    print("| lot | titre | état | assigné | étape | niv | retard | cible | cibles | depuis |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for a in sorted(crit, key=lambda x: (x.get("lot") or 0, x.get("step") or "")):
        print(line(a))

    print("\n## Autres alertes (1×/2×/3×, non P1)")
    print("| lot | titre | état | assigné | étape | niv | retard | cible | cibles | depuis |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for a in sorted(noncrit, key=lambda x: (x.get("lot") or 0, x.get("step") or "")):
        print(line(a))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
