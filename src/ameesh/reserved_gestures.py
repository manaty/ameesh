# SPDX-License-Identifier: AGPL-3.0-only
"""Journal des gestes réservés (#7631, point 5 ; spec owner du 17/11, §3.4).

En mode journal (Manaty), quatre gestes passent par la porte : mise en
production, dépense, publication store ou publique, signature de décision.
La porte les inscrit, elle ne les bloque pas. Cette vue donne, pour une
période :

* les actions de ces opérations : qui les a proposées, l'accord (le go cité
  par le reçu, ou le grant qui les couvre), le reçu, l'issue ;
* les décisions signées avec une clé owner : signées par l'owner lui-même,
  ou par son avatar sur un accord (`meta.signed_by`, point 4).

Est signalé ce qui est détectable sans inscription : une décision signée
dont la preuve ne dit pas qui a tenu la clé (`signed_by` absent : avant
#7631, ou signature hors de `ameesh decide`). Un déploiement de production
fait hors de la porte n'est pas détectable ici : ameesh ne tient pas de
journal des déploiements de production ; le module qui déclarera
l'opération `prod-deploy` (point 2, après #7628) apportera sa détection.

Lecture seule ; aucun texte d'accord ni de réponse n'est recopié.
"""
from __future__ import annotations

import json

from . import receipts, storage
from .db import Db

#: opération → geste réservé (décision owner du 17/11)
RESERVED_OPERATIONS = {
    "prod-deploy": "production",
    "spend": "spending",
    "publish": "publication",
    "decision-sign": "decision",
    "decision-sign-key": "decision",
}

UNRECORDED_SIGNER = "decision_signer_unrecorded"


def _accord_of(receipt_text) -> dict | None:
    if not receipt_text:
        return None
    try:
        request = receipts.parse_receipt(receipt_text).request
    except (receipts.ReceiptError, TypeError, ValueError):
        return None
    accord = request.get("accord")
    return dict(accord) if isinstance(accord, dict) else None


def _meta(value) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def journal(db: Db, *, since_ts: float, until_ts: float) -> dict:
    """{"gestures": [...], "signals": [...]} pour [since, until[."""
    st = storage.of(db)
    gestures: list[dict] = []
    for row in st.actions.reserved(sorted(RESERVED_OPERATIONS), since_ts=since_ts,
                                   until_ts=until_ts):
        if row.get("auth_kind") == "standing":
            accord = {"grant": row.get("auth_grant_id")}
        else:
            accord = _accord_of(row.get("auth_receipt"))
        gestures.append({
            "kind": "action", "gesture": RESERVED_OPERATIONS[row["operation"]],
            "operation": row["operation"], "action_id": row["action_id"],
            "project": row.get("project"), "target": row.get("target"),
            "proposed_by": row.get("proposed_by"), "approver": row.get("auth_approver"),
            "receipt": (row.get("auth_challenge") or "")[:12] or None,
            "accord": accord, "state": row.get("state"),
            "external_ref": row.get("external_ref"), "ts": row.get("created_ts"),
        })
    signals: list[dict] = []
    for row in st.approvals.decision_signatures(since_ts=since_ts, until_ts=until_ts):
        meta = _meta(row.get("meta"))
        signed_by = meta.get("signed_by")
        decision = str(row.get("consumed_by") or "").split(":", 1)[-1]
        entry = {
            "kind": "decision", "gesture": "decision", "operation": "decision-sign",
            "decision": int(decision) if decision.isdigit() else None,
            "approval_id": int(row["id"]), "approver": row.get("approver"),
            "signed_by": signed_by, "agent": meta.get("agent"),
            "accord": meta.get("accord"), "owner_key": bool(row.get("key_is_owner")),
            "ts": row.get("consumed_ts"),
        }
        gestures.append(entry)
        if signed_by not in ("owner", "avatar"):
            signals.append({
                "code": UNRECORDED_SIGNER, "decision": entry["decision"],
                "approval_id": entry["approval_id"], "approver": entry["approver"],
                "ts": entry["ts"],
                "reason": "decision signed with the key of %s without saying who held "
                          "it (owner or avatar)" % entry["approver"]})
    gestures.sort(key=lambda g: (float(g.get("ts") or 0), str(g.get("action_id")
                                                                or g.get("approval_id"))))
    return {"since_ts": since_ts, "until_ts": until_ts, "gestures": gestures,
            "signals": signals}


def accord_text(accord) -> str:
    if not isinstance(accord, dict) or not accord:
        return "no accord"
    if "grant" in accord:
        return "grant #%s" % accord["grant"]
    return "go %s#%s" % (accord.get("agent"), accord.get("seq"))
