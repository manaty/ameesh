# SPDX-License-Identifier: AGPL-3.0-only
"""#4501 — la tête poussée d'un porteur de paquet, et sa protection.

Un enregistrement de composition ne remplace jamais le manifeste d'une tête
poussée différente de la sienne. La tête protégée est le dernier gel déclaré
(la tête que `paquet-auto.py` gèle juste après l'avoir poussée), ou la tête de
branche **vue en avance après ce gel** (`branch_head`, relevé de la forge) :
entre la poussée et son gel, cette observation est la seule trace partagée
entre les clones.

Ce module ne dépend d'aucune autre couche : `packet_link.record` (lecture
initiale) et `storage.postgres.work.record_packet` (relecture sous le verrou
de la ligne du porteur, dans la transaction de l'écriture) appliquent la même
règle avec le même message.
"""
from __future__ import annotations


def same_commit(a, b) -> bool:
    """Deux identifiants qui nomment le même commit (préfixe admis)."""
    a, b = str(a or "").strip().lower(), str(b or "").strip().lower()
    return bool(a and b) and (a == b or a.startswith(b) or b.startswith(a))


def protected_head(frozen: str, frozen_ts: float, branch_head: str,
                   branch_head_ts: float) -> str:
    """La tête poussée à protéger : le gel, ou une tête vue en avance après.

    Une `branch_head` identique au gel (ou plus ancienne que lui) n'ajoute
    rien ; une tête vue en avance après le gel est une poussée plus récente,
    pas encore gelée.
    """
    frozen = str(frozen or "").strip().lower()
    branch_head = str(branch_head or "").strip().lower()
    if branch_head and not same_commit(branch_head, frozen) \
            and float(branch_head_ts or 0) > float(frozen_ts or 0):
        return branch_head
    return frozen


def refusal(package_id: int, protected: str, head_sha: str,
            expect_pushed: str) -> str:
    """Le message unique du refus (#4501), pour les deux lecteurs."""
    return ("packet carrier #%d already has the pushed head %s, not %s: "
            "a composition record never replaces the manifest of another "
            "pushed head; nothing written (the head's own composer passes "
            "`--expect-pushed %s`)"
            % (int(package_id), str(protected or "")[:12],
               str(head_sha or "")[:12], str(protected or "")[:12]))
