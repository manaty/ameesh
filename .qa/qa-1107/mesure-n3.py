#!/usr/bin/env python3
"""#1107 n3 — coût d'une passe `_slo_breaches` (lecture seule, tête 5b0189769).

Mesure le volume et le temps de chaque lecture faite à chaque passe :
lots ouverts, événements, jalons, actions, stagnation, moteur SLO complet.
Aucune écriture ; à rejouer sur la base vivante.
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "/home/ameesh/work/mty-amh-impl-03-l789t2/src")

from ameesh import config, db, exploitation, stagnation, storage, sous_utilisation  # noqa: E402


def ms(t0):
    return round((time.perf_counter() - t0) * 1000.0, 1)


def main() -> int:
    cfg = config.load()
    conn = db.open_db(cfg)
    st = storage.of(conn)

    t = time.perf_counter()
    listing = st.operations.listing()
    t_listing = ms(t)

    t = time.perf_counter()
    rows = [r for r in st.operations.open_lots_activity(None)
            if not sous_utilisation.is_backlog_item(r)]
    t_rows = ms(t)
    ids = [int(r["id"]) for r in rows]

    t = time.perf_counter()
    events = st.progress.lot_events(ids)
    t_events = ms(t)

    t = time.perf_counter()
    milestones = st.progress.lot_milestones(ids)
    t_milestones = ms(t)

    t = time.perf_counter()
    actions = st.progress.lot_actions(ids)
    t_actions = ms(t)

    t = time.perf_counter()
    described = stagnation.describe(conn, rows, now=time.time(), open_items=rows)
    t_stagnation = ms(t)

    t = time.perf_counter()
    breaches = exploitation._slo_breaches(cfg, conn, listing, time.time())
    t_full = ms(t)

    print("lots ouverts (hors backlog) : %d" % len(rows))
    print("agents (listing)            : %d  (%s ms)" % (len(listing), t_listing))
    print("open_lots_activity          : %d lots  (%s ms)" % (len(rows), t_rows))
    print("lot_events                  : %d lignes (%s ms)" % (len(events), t_events))
    print("lot_milestones              : %d lignes (%s ms)" % (len(milestones), t_milestones))
    print("lot_actions                 : %d lignes (%s ms)" % (len(actions), t_actions))
    print("stagnation.describe         : %d lots decrits (%s ms)" % (len(described), t_stagnation))
    print("_slo_breaches (passe)       : %d alertes (%s ms)" % (len(breaches), t_full))
    print("somme des lectures seules   : %s ms (%s + %s + %s + %s)"
          % (round(t_rows + t_events + t_milestones + t_actions, 1),
             t_rows, t_events, t_milestones, t_actions))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
