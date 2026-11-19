# SPDX-License-Identifier: AGPL-3.0-only
"""#7624 — complete in-flight review seats after an audited canon move.

Runs after canon sync, before the ordinary unavailable/capacity recovery.
The journal keys each successful check to its exact freeze and policy sources;
another host or a restarted runner does not resend acquired review requests.
"""
from __future__ import annotations

import hashlib

from . import mail, registry, review_designation_activation as act
from . import storage, work_lifecycle as lifecycle

CURSOR = "review-canon-refresh.cursor"
PREFIX = "review-canon-refresh:"


def _refs(cfg, db, project):
    errors = []
    refs = [str(loaded.source or "%s@%s" % (source.repo, commit))
            for source, commit, loaded in act.policy_sources(cfg, db, project, errors)]
    if not refs:
        raise act.NoCanon("; ".join(errors) or "no readable review policy")
    return refs


def _record(cfg, db, item, freeze, prefix, result):
    """A successful check or one pilot notice; never alter the freeze.

    #7662 : a `stale` result means the designation moved under the read, not
    that the policy failed: nothing is written, so the next pass re-reads it.
    """
    lot = int(item["id"])
    state = str(result.get("state") or "")
    if state == "stale":
        # #7662 : un gel ou un siège a bougé pendant la complétion du
        # plancher : la désignation lue n'est plus la courante, ce n'est pas
        # un échec de la politique. Aucune note, aucun courrier au pilote et
        # pas de clé `checked:` — la passe suivante relit ce lot.
        return
    threads = []
    unavailable = state not in {"designated", "manual", "none"}
    key = prefix + ("notice:" if unavailable else "checked:")
    with db.transaction() as tx:
        if not act._lock_current_freeze(tx, lot, freeze):
            return
        if storage.of(tx).work.events_noted(key, 0.0, ids=[lot]):
            return
        text = "%s canon policy rechecked: %s; %s" % (
            key, result.get("state"), result.get("why") or "review seats complete")
        storage.of(tx).work.note(lot, item["state"], text, act.SENDER)
        if unavailable:
            notify = act._notify(cfg, tx, str(item.get("app") or ""),
                                 registry.overview(tx))
            if notify:
                mail.send(tx, act.SENDER, notify,
                          "Lot #%d: canon policy changed; reviewer completion needs "
                          "attention on freeze #%s (%s). %s" % (
                              lot, freeze["id"], freeze["sha"], text),
                          work_item_id=str(lot), after_commit=threads)
    act._drain(db, threads)


def after_sync(cfg, db, *, limit=act.RETRY_LIMIT):
    """Fair bounded scan of frozen lots, including members already in a packet.

    A policy publication can raise the gate even when the former designation
    was final. Keep that designation and complete only its missing seats.
    Failed reads remain retryable; an explicit notice goes to the pilot.
    """
    out = {"examined": 0, "refreshed": 0, "errors": []}
    cursor = act._read_cursor(cfg, CURSOR)
    try:
        def rows(after):
            return db.query(
                "SELECT w.id, w.state, w.app FROM work_items w"
                " WHERE w.state IN (%s, %s) AND w.id > %s"
                " AND EXISTS (SELECT 1 FROM work_item_milestones m"
                " WHERE m.work_item_id = w.id AND m.kind = 'frozen')"
                " ORDER BY w.id LIMIT %s",
                (lifecycle.QA, lifecycle.VALIDATED, after, max(0, int(limit))))
        pending = rows(cursor)
        if not pending and cursor:
            pending = rows(0)
        sources = {}
        for item in pending:
            lot = int(item["id"])
            out["examined"] += 1
            cursor = lot
            freeze = None
            try:
                freeze = act._latest_freeze(db, lot)
                if not freeze:
                    continue
                project = str(item.get("app") or "")
                if project not in sources:
                    sources[project] = _refs(cfg, db, project)
                refs = sources[project]
                signature = hashlib.sha256("\n".join([project, *refs]).encode()).hexdigest()
                prefix = "%s%d:%d:%s:" % (PREFIX, lot, freeze["id"], signature)
                if storage.of(db).work.events_noted(prefix + "checked:", 0.0, ids=[lot]):
                    continue
                result = act.designate(cfg, db, lot, freeze=freeze, refresh_policy=True)
                _record(cfg, db, item, freeze, prefix, result)
                if str((result or {}).get("state") or "") != "stale":
                    out["refreshed"] += 1
            except Exception as exc:  # one lot never prevents the next one
                out["errors"].append("#%d: %s: %s" % (lot, type(exc).__name__, str(exc)[:160]))
                if freeze:
                    try:
                        _record(cfg, db, item, freeze,
                                "%s%d:%d:unavailable:" % (PREFIX, lot, freeze["id"]),
                                {"state": "unavailable", "why": str(exc)[:200]})
                    except Exception:
                        pass  # a pilot notice must not stop the fair scan
        act._write_cursor(cfg, CURSOR, cursor)
    except Exception as exc:  # canon sync must survive an unreadable queue
        out["errors"].append("%s: %s" % (type(exc).__name__, str(exc)[:160]))
    return out
