# SPDX-License-Identifier: AGPL-3.0-only
"""#1465 — le fournisseur exact d'origine d'un commit et d'un jalon.

Contrat importable pour le qualificateur du train (#1467,
`train_review.qualify`). Pour chaque sujet, il rend un reçu aux champs de
`train_review.Receipt` — `subject`, `commit`, `actor`, `session`, `turn`,
`harness`, `record`, `milestone` — ou `None` quand la source manque.

    from ameesh import review_provenance as prov
    recu = prov.receipt_for_commit(db, sha)          # ou None (inconnu)
    recu = prov.receipt_for_milestone(db, jalon_id)  # ou None (inconnu)

Ce que le fournisseur ne fait JAMAIS :

* deviner un tour par proximité temporelle (`turn_costs.recorded_at` voisin),
  par le harnais déclaré au registre, ou par le nom d'auteur Git : ce ne sont
  pas des liens, mesh-design 15/10 03:53 ;
* compléter une preuve partielle : une ligne `provenance_receipts` sans son
  fait (`work_item_milestones`) est une origine inconnue ;
* réécrire une preuve : la première écriture fait foi, une divergence d'identité
  est une erreur (`ProvenanceError`), pas un remplacement silencieux.

La preuve est écrite à l'instant du fait (migration 0445) : le commit par
`ameesh provenance record --commit` (hook post-commit), le jalon dans la même
transaction que `work_item_milestones`. Le `turn` est l'identifiant du tour
managé (`AMEESH_TURN_ID`), la `session` celle du registre vivant de l'acteur,
et le `harness` celui du tour (`AMEESH_HARNESS`), recontrôlé contre le registre.
"""
from __future__ import annotations

import os
import re

from . import storage

#: les familles de harnais que le qualificateur de train sait opposer
FAMILIES = frozenset({"claude", "codex", "deepseek"})
#: un identifiant de commit complet : SHA-1 (40) ou SHA-256 (64)
FULL_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
#: une abréviation Git citée par un jalon (au moins 7 caractères)
ABBREV = re.compile(r"[0-9a-f]{7,64}")


class ProvenanceError(Exception):
    """Preuve incohérente ou identité d'écriture incomplète : on refuse."""


def full_sha(value) -> str:
    """Le commit complet, en minuscules ; refuse tout le reste."""
    text = str(value or "").strip().lower()
    if not FULL_SHA.fullmatch(text):
        raise ProvenanceError("full commit id expected, got %r" % (value,))
    return text


def subject_of_commit(value) -> str:
    return "commit:" + full_sha(value)


def subject_of_milestone(milestone_id) -> str:
    try:
        number = int(milestone_id)
    except (TypeError, ValueError) as exc:
        raise ProvenanceError("milestone id expected, got %r" % (milestone_id,)) from exc
    if number <= 0:
        raise ProvenanceError("milestone id expected, got %r" % (milestone_id,))
    return "milestone:%d" % number


def _receipt(row, *, commit: str, milestone) -> dict:
    """Le reçu aux champs de `train_review.Receipt`."""
    return {
        "subject": row["subject"],
        "commit": commit,
        "actor": row["actor"],
        "session": row["session"],
        "turn": row["turn"],
        "harness": row["harness"],
        # la ligne elle-même est le reçu exact, jamais une note libre
        "record": "provenance_receipts:%s" % row["subject"],
        "milestone": milestone,
    }


def receipt_for_commit(db, sha) -> dict | None:
    """Le reçu exact du commit `sha`, ou None si son origine est inconnue.

    `sha` doit être complet : le train qualifie des commits complets, et une
    abréviation ne désigne un commit qu'en présence du dépôt — le fournisseur
    ne l'a pas."""
    subject = subject_of_commit(sha)
    row = storage.of(db).provenance.get(subject)
    if row is None or row["sha"] != subject[len("commit:"):]:
        return None
    return _receipt(row, commit=row["sha"], milestone=None)


def receipt_for_milestone(db, milestone_id) -> dict | None:
    """Le reçu exact du jalon, ou None si son origine est inconnue.

    Le reçu recopie les faits du jalon (`id`, `actor`, `at_ts`, `sha`,
    `verdict`) et porte le commit COMPLET que le `sha` déclaré résout. Une
    incohérence entre la preuve et le fait — acteur différent, abréviation qui
    ne résout pas le commit enregistré — est refusée, pas rabotée."""
    subject = subject_of_milestone(milestone_id)
    row = storage.of(db).provenance.milestone(subject)
    if row is None:
        return None
    if str(row["mark_actor"]).strip().casefold() != str(row["actor"]).strip().casefold():
        raise ProvenanceError(
            "%s: proof actor %r differs from the milestone actor %r"
            % (subject, row["actor"], row["mark_actor"]))
    mark_sha = str(row["mark_sha"] or "").strip().lower()
    if mark_sha:
        if not ABBREV.fullmatch(mark_sha) or not row["sha"].startswith(mark_sha):
            raise ProvenanceError(
                "%s: declared sha %r does not resolve the recorded commit %s"
                % (subject, row["mark_sha"], row["sha"]))
    return _receipt(row, commit=row["sha"], milestone={
        "id": row["milestone_id"],
        "actor": row["mark_actor"],
        "at_ts": row["mark_at_ts"],
        "sha": row["mark_sha"],
        "verdict": row["verdict"],
    })


def capture(environ=None, *, actor: str = "") -> dict:
    """L'identité d'écriture d'un fait, lue dans le tour managé courant.

    Refuse hors tour (`AMEESH_TURN_ID` absent) ou sur un harnais inconnu : une
    preuve sans tour ou sans famille n'est pas une preuve. Le harnais du tour
    est celui du runner (`AMEESH_HARNESS`), jamais un nom choisi par le
    dépôt."""
    env = os.environ if environ is None else environ
    who = str(actor or env.get("AGENT_MAIL_NAME") or "").strip()
    turn = str(env.get("AMEESH_TURN_ID") or "").strip()
    harness = str(env.get("AMEESH_HARNESS") or "").strip().lower()
    if not who:
        raise ProvenanceError("no agent identity (AGENT_MAIL_NAME)")
    if not turn:
        raise ProvenanceError("no managed turn (AMEESH_TURN_ID): outside a turn")
    if harness not in FAMILIES:
        raise ProvenanceError("unknown harness %r (expected %s)"
                              % (harness, ", ".join(sorted(FAMILIES))))
    return {"actor": who, "turn": turn, "harness": harness}


def live_session(db, actor: str, *, harness: str = "") -> str:
    """La session VIVANTE de l'acteur, au registre ; refuse si aucune.

    La preuve nomme la session qui a produit le fait : une session absente ne
    se remplace pas par un identifiant vide. Le harnais attendu est recontrôlé
    contre celui de la session vivante (`registry.live_harness`, #1037) : un
    harnais emprunté ne peut pas signer une preuve sous son étiquette."""
    from . import registry
    row = registry.get(db, actor) or {}
    session = str(row.get("session_id") or "").strip()
    if not session:
        raise ProvenanceError("no live session for %s" % actor)
    if harness:
        live = str(registry.live_harness(row) or "").strip().lower()
        if live and live != str(harness).strip().lower():
            raise ProvenanceError(
                "harness %r does not match the live session of %s (%r)"
                % (harness, actor, live))
    return session
