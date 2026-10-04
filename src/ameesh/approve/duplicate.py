# SPDX-License-Identifier: AGPL-3.0-only
"""Décision « assumer le doublon » (spec §7.2 point 5, lot L5).

Une action `unknown` dont le connecteur ne garantit pas la déduplication ne
se relance pas : son effet a peut-être déjà eu lieu. Seule une décision
humaine signée qui ASSUME LE RISQUE D'UN DOUBLON la remplace par une nouvelle
action (`replaces` = l'ancienne, même contenu, nouvel `action_id`), qui naît
`proposed` et exige sa propre approbation.

Le reçu est un reçu d'action ordinaire (`ameesh-receipt/1`, décision
`approve`) dont `action_id` est l'action INCONNUE remplacée et dont `digest`
est :

    SHA-256("ameesh-assume-duplicate/1\\0" ‖ JCS({action_digest,
            decision: "assume-duplicate", replaces: action_id}))

Domaine distinct : un reçu d'approbation ordinaire de l'action (nouvelle
tentative) ne vaut jamais décision d'assomption, et inversement.

`assume_duplicate_digest` est une réplique EXACTE de
`ameesh.actions.assume_duplicate_digest` (lot L5), pour qu'ameesh-approve ne
dépende pas de ce module : tests/test_approve.py compare les deux quand
`ameesh.actions` est importable, et fige un vecteur de référence sinon.
"""
from __future__ import annotations

import hashlib

from .. import jcs, receipts

ASSUME_DUPLICATE_DOMAIN = b"ameesh-assume-duplicate/1\x00"
ASSUME_DUPLICATE_DECISION = "assume-duplicate"
#: seul état où une décision « assumer le doublon » a un sens (`actions.replace`)
ASSUMABLE_STATES = ("unknown",)


def digest_fields(action: dict) -> dict:
    """Les neuf champs signés (spec §7.1), lus comme `actions.digest_fields`."""
    fields = {name: action.get(name) for name in receipts.ACTION_DIGEST_FIELDS}
    fields["target"] = fields["target"] or ""
    return fields


def assume_duplicate_digest(action: dict) -> str:
    """Empreinte de la décision « assumer le doublon » qui remplace `action`."""
    payload = {"action_digest": receipts.action_digest(digest_fields(action)),
               "decision": ASSUME_DUPLICATE_DECISION,
               "replaces": action.get("action_id")}
    return "sha256:" + hashlib.sha256(ASSUME_DUPLICATE_DOMAIN
                                      + jcs.canonicalize(payload)).hexdigest()
