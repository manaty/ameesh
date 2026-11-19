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
#7631, ou signature hors de `ameesh decide`), et un déploiement de
production que le journal externe du module `dev` montre sans action de
porte qui le porte (#7684, `detect_reserved_gesture`). Le cœur ne tient pas
ce journal : le module le relit, la vue ne fait que comparer.

Chaque geste porte ce qui l'a financé : le grant et sa réservation (liés à
l'empreinte de la réponse pour une décision), et la preuve d'exécution
inscrite par le connecteur `journal` (`run`, `sha`, `resource`, `ref`).

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
UNRECORDED_PROD_DEPLOY = "prod_deploy_unrecorded"


def _proof_of(external_ref):
    """La preuve d'exécution relue d'`actions.external_ref`.

    Le connecteur `journal` y écrit un objet JSON canonique ; une valeur
    d'une autre forme (action d'un autre connecteur, ou ligne d'avant #7684)
    est rendue telle quelle, pour ne rien perdre à l'affichage.
    """
    if not external_ref:
        return None
    if isinstance(external_ref, str):
        try:
            parsed = json.loads(external_ref)
        except ValueError:
            return external_ref
        return parsed if isinstance(parsed, dict) else external_ref
    return external_ref if isinstance(external_ref, dict) else None


def _detected_prod_deploys(since_ts: float, until_ts: float) -> list[dict]:
    """Les déploiements de production que le module `dev` relit hors porte.

    Le registre refuse un détecteur déclaré sans appelable : une panne de
    détection ne peut donc pas être silencieuse. Seule la lecture du journal
    externe est tolérante (fichier absent ou illisible : rien de détecté).
    """
    from .modules import registry
    detector = registry.reserved_gesture_detectors().get("prod-deploy")
    if detector is None:
        return []
    _provider, detect = detector
    try:
        found = detect("prod-deploy", since_ts=since_ts, until_ts=until_ts)
    except Exception:  # noqa: BLE001 - la vue reste lisible sans détection
        return []
    return [row for row in found or () if isinstance(row, dict)]


def _recorded_tokens(gestures) -> set:
    """Les valeurs de preuve qui peuvent attester d'un déploiement.

    Un déploiement détecté est « inscrit » quand au moins une de ses valeurs
    (`id`, `ref`, preuve) se retrouve dans la preuve d'une action
    `prod-deploy` de la période. La cible seule ne suffit pas : deux
    déploiements successifs sur la même cible sont deux gestes.
    """
    tokens = set()
    for gesture in gestures:
        if gesture.get("operation") != "prod-deploy":
            continue
        proof = gesture.get("proof")
        if isinstance(proof, dict):
            tokens.update(str(value) for value in proof.values() if value)
        elif isinstance(proof, str) and proof:
            tokens.add(proof)
    return tokens


def _deploy_tokens(deploy: dict) -> set:
    tokens = set()
    for key in ("id", "ref"):
        value = deploy.get(key)
        if isinstance(value, str) and value:
            tokens.add(value)
    proof = deploy.get("proof")
    if isinstance(proof, dict):
        tokens.update(str(value) for value in proof.values() if value)
    return tokens


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
        # #7684 : la signature d'une décision a sa propre entrée (approbation
        # consommée, grant, réservation) ; l'action de porte qui porte la
        # réservation ne serait qu'un doublon.
        if row["operation"] in ("decision-sign", "decision-sign-key"):
            continue
        if row.get("auth_kind") == "standing":
            accord = {"grant": row.get("auth_grant_id")}
        else:
            accord = _accord_of(row.get("auth_receipt"))
        attempts = st.actions.attempts(row["action_id"])
        reservation = next((int(a["reservation_id"]) for a in reversed(attempts)
                            if a.get("reservation_id") is not None), None)
        gestures.append({
            "kind": "action", "gesture": RESERVED_OPERATIONS[row["operation"]],
            "operation": row["operation"], "action_id": row["action_id"],
            "project": row.get("project"), "target": row.get("target"),
            "proposed_by": row.get("proposed_by"), "approver": row.get("auth_approver"),
            "receipt": (row.get("auth_challenge") or "")[:12] or None,
            "accord": accord, "grant": row.get("auth_grant_id"),
            "reservation": reservation, "state": row.get("state"),
            "proof": _proof_of(row.get("external_ref")),
            "external_ref": row.get("external_ref"), "ts": row.get("created_ts"),
        })
    signals: list[dict] = []
    for row in st.approvals.decision_signatures(since_ts=since_ts, until_ts=until_ts):
        meta = _meta(row.get("meta"))
        signed_by = meta.get("signed_by")
        decision = str(row.get("consumed_by") or "").split(":", 1)[-1]
        standing = _meta(meta.get("standing"))
        entry = {
            "kind": "decision", "gesture": "decision",
            "operation": meta.get("operation") or "decision-sign",
            "decision": int(decision) if decision.isdigit() else None,
            "approval_id": int(row["id"]), "approver": row.get("approver"),
            "signed_by": signed_by, "agent": meta.get("agent"),
            "accord": meta.get("accord") or ({"grant": standing["grant"]}
                                             if standing.get("grant") else None),
            "grant": standing.get("grant"), "reservation": standing.get("reservation"),
            "key": standing.get("key"),
            "owner_key": bool(row.get("key_is_owner")),
            "proof": None, "ts": row.get("consumed_ts"),
        }
        gestures.append(entry)
        if signed_by not in ("owner", "avatar"):
            signals.append({
                "code": UNRECORDED_SIGNER, "decision": entry["decision"],
                "approval_id": entry["approval_id"], "approver": entry["approver"],
                "ts": entry["ts"],
                "reason": "decision signed with the key of %s without saying who held "
                          "it (owner or avatar)" % entry["approver"]})
    recorded = _recorded_tokens(gestures)
    for deploy in _detected_prod_deploys(since_ts, until_ts):
        if _deploy_tokens(deploy) & recorded:
            continue
        signals.append({
            "code": UNRECORDED_PROD_DEPLOY, "deploy": deploy.get("id"),
            "target": deploy.get("target"), "ref": deploy.get("ref"),
            "ts": deploy.get("ts"),
            "reason": "production deployment %s on %s (%s) is not recorded by the gate"
                      % (deploy.get("id"), deploy.get("target"), deploy.get("ref"))})
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


def proof_text(proof) -> str:
    """La preuve d'exécution en clair, pour la ligne du journal."""
    if isinstance(proof, dict):
        return ", ".join("%s=%s" % (key, proof[key])
                         for key in ("run", "sha", "resource", "ref") if key in proof)
    return str(proof)
