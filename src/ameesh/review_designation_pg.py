# SPDX-License-Identifier: AGPL-3.0-only
"""#1617 — le port Postgres de la désignation des relecteurs au gel.

`review_designation` fait la **sélection** sans aucune E/S ; ce module est le
`Port` réel : il lit les jalons du lot, relit la marque idempotente du gel et
écrit la désignation en **une** transaction (verrou de la ligne du lot, marque
au journal, courriers dans la boîte). Le fil lisible n'est écrit qu'après le
commit (`mail.send(..., after_commit=…)`, motif #315 de `deep_analysis`) : un
envoi annulé ne laisse aucune trace dans le fil externe.

Deux usages, selon qui tient la transaction :

- **au gel** (hook étroit de #1467) : le port est construit sur la transaction
  ouverte du jalon — `PgPort(tx, after_commit=threads)` ; `commit` n'ouvre pas
  de transaction, écrit dans celle du jalon, et l'appelant vide `threads`
  après son commit ;
- **hors hook** (reprise, CLI) : `PgPort(db)` — `commit` ouvre sa propre
  transaction, verrouille le lot, puis vide lui-même le fil après le commit.

La marque est une ligne du journal du lot, préfixée par le lot et le jalon :
`review-designation:<lot>:<gel>: {json}`. Elle porte l'état, le nombre requis,
les relecteurs, le SHA exact et les auteurs du contenu gelé ; `recorded` la
relit telle quelle, donc un même gel rejoué rend la désignation enregistrée
sans second courrier. Un gel nouveau est une autre marque : l'ancienne ne
compte plus, et le port refuse d'écrire pour un gel qui n'est plus le dernier
(`StaleFreeze`) — aucun retour à une ancienne tête.
"""
from __future__ import annotations

import json
from typing import Mapping, Optional, Sequence

from . import mail, plan_git, registry, storage
from .review_designation import DESIGNATED, WAITING, DesignationError, StaleFreeze

#: préfixe des marques au journal : `<préfixe><lot>:<jalon>: <json>`
MARK_PREFIX = "review-designation:"
#: l'expéditeur des courriers automatiques : l'identité machine du registre
SENDER = "ameesh"
#: jalons relus pour trouver le dernier gel (large : la frise entière d'un lot)
MILESTONES = 200


def mark_prefix(lot: int, freeze_id: int) -> str:
    """Le préfixe exact de la marque d'un gel : le lot et le jalon, ensemble.

    Les deux sont dans le préfixe pour que `events_noted` ne rende que les
    marques de **ce** gel : `review-designation:3:12:` ne confond ni le lot 3
    gel 1 avec le gel 12, ni deux lots."""
    return "%s%d:%d:" % (MARK_PREFIX, int(lot), int(freeze_id))


def designation_of(note: str) -> Optional[dict]:
    """La désignation portée par une ligne du journal, ou None si ce n'est pas
    une marque. Une marque illisible est refusée, jamais interprétée : on ne
    renvoie pas un second courrier sur un doute."""
    text = str(note or "")
    if not text.startswith(MARK_PREFIX):
        return None
    _, separator, payload = text.partition(": ")
    if not separator:
        raise DesignationError("unreadable review designation mark: %r" % text[:120])
    try:
        found = json.loads(payload)
    except ValueError as exc:
        raise DesignationError(
            "unreadable review designation mark: %s" % exc) from exc
    if not isinstance(found, dict) or found.get("state") not in (DESIGNATED, WAITING) \
            or not isinstance(found.get("freeze"), dict):
        raise DesignationError("incomplete review designation mark: %r" % text[:120])
    return found


def _mark_note(lot: int, freeze_id: int, *, state: str, required: int, reviewers,
               missing: int, why: str, authors, policy_ref: str, sha: str) -> str:
    payload = {
        "state": state,
        "required": int(required),
        "reviewers": [{"name": str((r or {}).get("name") or ""),
                       "family": str((r or {}).get("family") or "")}
                      for r in reviewers or ()],
        "missing": int(missing),
        "why": str(why or ""),
        "freeze": {"id": int(freeze_id), "sha": sha},
        "authors": [{"name": str((a or {}).get("name") or ""),
                     "family": str((a or {}).get("family") or "")}
                    for a in authors or ()],
        "policy_ref": str(policy_ref or ""),
    }
    return "%s %s" % (mark_prefix(lot, freeze_id),
                      json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _designation(lot: int, freeze_id: int, *, state: str, required: int, reviewers,
                 missing: int, why: str, authors, policy_ref: str, sha: str) -> dict:
    """La forme rendue par `commit`, celle que `apply` transmet à son appelant."""
    return {
        "state": state,
        "required": int(required),
        "reviewers": [dict(r) for r in reviewers or ()],
        "missing": int(missing),
        "why": str(why or ""),
        "freeze": {"id": int(freeze_id), "sha": sha},
        "authors": [dict(a) for a in authors or ()],
        "policy_ref": str(policy_ref or ""),
    }


class PgPort:
    """Le `Port` de `review_designation` sur Postgres.

    `db` est une connexion (`PgPort(db)`) ou une transaction ouverte
    (`PgPort(tx, after_commit=threads)`). Dans le second cas, l'appelant
    **doit** fournir `after_commit` : ses entrées sont les écritures de fil à
    exécuter après le commit de sa transaction ; le port n'ouvre alors aucune
    transaction (pas d'imbrication dans celle du jalon `frozen`)."""

    def __init__(self, db, *, sender: str = SENDER, after_commit: Optional[list] = None):
        self.db = db
        self.sender = sender or SENDER
        self.after_commit = after_commit

    # -- lectures ---------------------------------------------------------
    def latest_freeze(self, lot: int) -> Optional[dict]:
        """Le dernier jalon `frozen` : `{id, sha}`, ou None. Lui seul fait
        autorité — un nom de branche n'est pas un gel."""
        row = plan_git.latest_freeze(
            storage.of(self.db).work.milestones(int(lot), MILESTONES))
        if not row:
            return None
        return {"id": int(row["id"]), "sha": str(row.get("sha") or "")}

    def recorded(self, lot: int, freeze_id: int) -> Optional[dict]:
        """La désignation déjà écrite pour **ce** gel, ou None.

        Lit la marque du journal (la dernière de ce gel si plusieurs, l'ordre
        du journal tranche) ; c'est ce qui rend le gel rejouable sans second
        courrier."""
        return self._recorded_in(self.db, int(lot), int(freeze_id))

    def _recorded_in(self, db, lot: int, freeze_id: int) -> Optional[dict]:
        found = None
        for row in storage.of(db).work.events_noted(mark_prefix(lot, freeze_id), 0.0):
            noted = designation_of(row.get("note"))
            if noted is None:
                continue
            if int(noted["freeze"].get("id") or 0) != freeze_id:
                raise DesignationError(
                    "review designation mark of lot #%d names freeze %r, not %d"
                    % (lot, noted["freeze"].get("id"), freeze_id))
            found = noted  # l'ordre du journal (id croissant) : la dernière gagne
        return found

    # -- écriture ---------------------------------------------------------
    def commit(self, lot: int, *, freeze: Mapping, required: int, state: str,
               reviewers: Sequence[Mapping], missing: int, why: str,
               authors: Sequence[Mapping] = (), policy_ref: str = "",
               notify: str = "", messages: Sequence[Mapping] = ()) -> dict:
        """Écrit la désignation du gel en une transaction et rend sa forme.

        Sous le verrou de la ligne du lot : recontrôle que `freeze` est
        toujours le dernier gel (sinon `StaleFreeze`, **rien** n'est écrit),
        recontrôle la marque idempotente (un appel concurrent rend la
        désignation du gagnant, sans second courrier), dépose les courriers
        puis la marque — le tout dans la même transaction. Une désignation est
        finale pour son gel ; une attente est réessayable et sa notification
        ne part qu'une fois par gel."""
        lot = int(lot)
        if self.after_commit is not None:
            return self._write(self.db, lot, freeze=freeze, required=required,
                               state=state, reviewers=reviewers, missing=missing,
                               why=why, authors=authors, policy_ref=policy_ref,
                               notify=notify, messages=messages)
        threads: list = []
        inner = PgPort(self.db, sender=self.sender, after_commit=threads)
        with self.db.transaction() as tx:
            out = inner._write(tx, lot, freeze=freeze, required=required, state=state,
                               reviewers=reviewers, missing=missing, why=why,
                               authors=authors, policy_ref=policy_ref, notify=notify,
                               messages=messages)
        # le fil externe après le commit seulement : un envoi annulé n'y laisse rien
        for record in threads:
            record(self.db.cfg, self.db)
        return out

    def _write(self, db, lot: int, *, freeze: Mapping, required: int, state: str,
               reviewers: Sequence[Mapping], missing: int, why: str, authors,
               policy_ref: str, notify: str, messages) -> dict:
        freeze = dict(freeze or {})
        sha = str(freeze.get("sha") or "").strip().lower()
        try:
            freeze_id = int(freeze.get("id"))
        except (TypeError, ValueError):
            raise DesignationError("freeze without milestone id: %r" % (freeze,)) from None
        ops = storage.of(db).work
        rows = db.query("SELECT id, state FROM work_items WHERE id = %s FOR NO KEY UPDATE",
                        (lot,))
        if not rows:
            raise DesignationError("lot #%d not found: no designation" % lot)
        last = plan_git.latest_freeze(ops.milestones(lot, MILESTONES))
        if not last or int(last["id"]) != freeze_id \
                or str(last.get("sha") or "").strip().lower() != sha:
            raise StaleFreeze(
                "freeze #%d (%s) of lot #%d is no longer the last: nothing written"
                % (freeze_id, sha[:12] or "?", lot))
        existing = self._recorded_in(db, lot, freeze_id)
        # Une désignation est finale ; une attente ne se réécrit pas (une seule
        # notification par gel) mais **cède** la place à la désignation quand la
        # capacité revient : c'est le même gel, la marque suivante fait foi.
        if existing is not None and (existing.get("state") == DESIGNATED
                                     or state == WAITING):
            return existing
        if state not in (DESIGNATED, WAITING):
            raise DesignationError("a designation is %s or %s, not %r"
                                   % (DESIGNATED, WAITING, state))
        reviewers = [dict(r) for r in reviewers or ()]
        if state == DESIGNATED and not reviewers:
            raise DesignationError("a designated review names its reviewers")
        if state == WAITING and reviewers:
            raise DesignationError("a waiting designation designates no reviewer")
        threads = self.after_commit if self.after_commit is not None else []
        for message in messages or ():
            row = dict(message or {})
            recipient = str(row.get("to") or "").strip()
            if not recipient:
                raise DesignationError(
                    "no recipient for the %s of lot #%d: name the pilot (notify)"
                    % (state, lot))
            if registry.get(db, recipient) is None:
                # un courrier à un nom inconnu ne réveille personne : le refus
                # vaut mieux qu'une désignation qui ne parvient jamais
                raise DesignationError(
                    "unknown recipient %r for the %s of lot #%d: nothing written"
                    % (recipient, state, lot))
            mail.send(db, self.sender, recipient, str(row.get("body") or ""),
                      work_item_id=str(lot),
                      payload={"review_designation": {
                          "lot": lot, "freeze": freeze_id, "sha": sha,
                          "required": int(required), "state": state,
                          "reviewer": str(row.get("family") or "")}},
                      after_commit=threads)
        ops.note(lot, str(rows[0].get("state") or ""),
                 _mark_note(lot, freeze_id, state=state, required=required,
                            reviewers=reviewers, missing=missing, why=why,
                            authors=authors, policy_ref=policy_ref, sha=sha),
                 self.sender)
        return _designation(lot, freeze_id, state=state, required=required,
                            reviewers=reviewers, missing=missing, why=why,
                            authors=authors, policy_ref=policy_ref, sha=sha)
