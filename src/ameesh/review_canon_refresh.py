# SPDX-License-Identifier: AGPL-3.0-only
"""#7624 — complete in-flight review seats after an audited canon move.

Runs after canon sync, before the ordinary unavailable/capacity recovery.
The journal keys each successful check to its exact freeze and policy sources;
another host or a restarted runner does not resend acquired review requests.
#7941 : un constat (attente, échec terminal, échec transitoire après la borne)
est clé par gel, jamais par signature du canon — une nouvelle époque ne
renvoie pas le même avis au pilote.
"""
from __future__ import annotations

import hashlib
import re

from . import mail, registry, review_designation_activation as act
from . import storage, work_lifecycle as lifecycle

CURSOR = "review-canon-refresh.cursor"
PREFIX = "review-canon-refresh:"
#: #7941 : un échec transitoire de complétion ne fait alerter qu'après ce
#: nombre de passes ; les passes antérieures ne laissent qu'une ligne d'essai.
TRANSIENT_NOTICE_AFTER = 3
#: une issue finale du gel : le contrôle de politique est clos pour cette
#: signature du canon (`checked:`), aucun courrier au pilote.
RESOLVED_STATES = frozenset(("designated", "manual", "none", "carried"))


def _refs(cfg, db, project):
    errors = []
    refs = [str(loaded.source or "%s@%s" % (source.repo, commit))
            for source, commit, loaded in act.policy_sources(cfg, db, project, errors)]
    if not refs:
        raise act.NoCanon("; ".join(errors) or "no readable review policy")
    return refs


def _recorded_policy(db, lot, freeze) -> str:
    """The policy ref of this freeze's current designation mark, or "".

    The mark is written with the canon source read at designation time
    (`policy_ref`), so it is the exact reference to compare with the policy
    sources of the refresh pass. A carried mark or an old writer may not name
    one: the caller treats that as a move and keeps the pre-#8265 behaviour.
    """
    try:
        effective = act._effective_mark(
            db, int(lot), int(freeze["id"]),
            since_ts=float((freeze or {}).get("at_ts") or 0.0))
    except Exception:  # noqa: BLE001 - an unreadable journal is not a proof
        return ""
    if effective is None:
        return ""
    return str((effective[2] or {}).get("policy_ref") or "")


#: the exact canon commit inside a policy ref (`<checkout>@<commit>:<file>`)
_REF_COMMIT = re.compile(r"@([0-9a-f]{40}|[0-9a-f]{64}):")


def _ref_commit(ref) -> str:
    found = _REF_COMMIT.search(str(ref or ""))
    return found.group(1) if found else ""


def _policy_moved(result, refs, recorded: str) -> bool:
    """Did the audited canon change since the freeze's designation (#8265)?

    The comparison is on the canon commit, never on the checkout path: two
    hosts of the mesh may hold the same canon at different paths, and a path
    difference alone is not a policy publication.
    """
    current = {str(ref) for ref in refs or ()}
    commits = {_ref_commit(ref) for ref in current}
    commits.discard("")

    def moved(ref) -> bool:
        commit = _ref_commit(ref)
        if commit and commits:
            return commit not in commits
        return str(ref or "") not in current

    if recorded:
        return moved(recorded)
    # No mark before the pass: the refresh may itself have written the first
    # designation. Its policy ref says whether that read is the current one.
    return moved((result or {}).get("policy_ref"))


def _record(cfg, db, item, freeze, prefix, result, *, refs=(), recorded_policy=""):
    """A successful check per canon signature, an alert at most once per freeze.

    #7662 : a `stale` result means the designation moved under the read, not
    that the policy failed: nothing is written, so the next pass re-reads it.

    #7941 : le constat ne se répète plus à chaque époque du canon. Un succès
    reste clé par signature (`checked:`) : la politique doit être relue quand
    elle bouge. Une attente, un échec terminal (cible disparue à la forge) et
    un échec transitoire après la borne sont clés **par gel**, sans signature :
    le pilote reçoit un seul courrier par gel, quel que soit le nombre de
    signatures publiées ensuite.

    #8265 : une attente n'alerte que si la politique du canon a réellement
    changé depuis la désignation du gel. À politique inchangée, le gel a déjà
    envoyé son avis « Designation at freeze incomplete » ; le contrôle ne le
    double pas d'un « canon policy changed » qui serait faux. La désignation
    reste rejouée à chaque passe : la capacité peut revenir.
    """
    lot = int(item["id"])
    state = str(result.get("state") or "")
    if state == "stale":
        # #7662 : un gel ou un siège a bougé pendant la complétion du
        # plancher : la désignation lue n'est plus la courante, ce n'est pas
        # un échec de la politique. Aucune note, aucun courrier au pilote et
        # pas de clé `checked:` — la passe suivante relit ce lot.
        return
    if state == act.rd.WAITING and not _policy_moved(result, refs, recorded_policy):
        # #8265 : le gel a déjà son avis d'attente (capacité, provenance) ;
        # à politique inchangée, un second courrier au pilote serait un faux
        # « canon policy changed ». Rien n'est écrit : la désignation reste
        # rejouée à la passe suivante, où la capacité peut être revenue.
        return
    freeze_prefix = "%s%d:%d:" % (PREFIX, lot, freeze["id"])
    checked = prefix + "checked:"
    why = result.get("why") or "review seats complete"
    terminal = bool(result.get("terminal"))
    threads = []
    with db.transaction() as tx:
        if not act._lock_current_freeze(tx, lot, freeze):
            return
        journal = storage.of(tx).work
        if state in RESOLVED_STATES:
            if journal.events_noted(checked, 0.0, ids=[lot]):
                return
            journal.note(lot, item["state"],
                         "%s canon policy rechecked: %s; %s"
                         % (checked, state, why), act.SENDER)
            return
        if state == act.rd.WAITING:
            # Une attente de capacité est une désignation valide : elle est
            # réessayée à chaque passe (la capacité peut revenir) mais
            # n'alerte qu'une fois pour ce gel, pas à chaque époque du canon.
            key = freeze_prefix + "waiting:"
        elif terminal:
            key = freeze_prefix + "terminal:"
        else:
            attempts_key = freeze_prefix + "attempt:"
            attempts = len(journal.events_noted(attempts_key, 0.0, ids=[lot]) or ())
            key = freeze_prefix + "transient:"
            if attempts + 1 < TRANSIENT_NOTICE_AFTER:
                # Silencieux jusqu'à la borne : une forge ou un fetch qui
                # échoue une fois ne doit pas alarmer le pilote.
                journal.note(lot, item["state"],
                             "%s attempt %d/%d: %s"
                             % (attempts_key, attempts + 1,
                                TRANSIENT_NOTICE_AFTER, why), act.SENDER)
                return
        if not journal.events_noted(key, 0.0, ids=[lot]):
            text = "%s canon policy rechecked: %s; %s" % (key, state, why)
            journal.note(lot, item["state"], text, act.SENDER)
            notify = act._notify(cfg, tx, str(item.get("app") or ""),
                                 registry.overview(tx))
            if notify:
                mail.send(tx, act.SENDER, notify,
                          "Lot #%d: canon policy changed; reviewer completion needs "
                          "attention on freeze #%s (%s). %s" % (
                              lot, freeze["id"], freeze["sha"], text),
                          work_item_id=str(lot), after_commit=threads)
        if terminal and not journal.events_noted(checked, 0.0, ids=[lot]):
            # Un échec terminal ne redevient pas lisible en relisant la même
            # époque : la signature est marquée close, une nouvelle époque du
            # canon rejouera la lecture (sans nouveau courrier).
            journal.note(lot, item["state"],
                         "%s canon policy rechecked: unavailable; %s"
                         % (checked, why), act.SENDER)
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
                recorded_policy = _recorded_policy(db, lot, freeze)
                result = act.designate(cfg, db, lot, freeze=freeze, refresh_policy=True)
                _record(cfg, db, item, freeze, prefix, result,
                        refs=refs, recorded_policy=recorded_policy)
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
