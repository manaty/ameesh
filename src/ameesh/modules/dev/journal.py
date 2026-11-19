# SPDX-License-Identifier: AGPL-3.0-only
"""Connecteur `journal` : les gestes réservés de Manaty, inscrits par la porte.

#7631 a livré le journal des quatre gestes réservés (production, dépense,
publication, signature de décision) et le reçu par clé fichier sous accord.
#7684 declare le connecteur qui les sert. Il n'agit pas : le bureau fait le
geste avec ses outils, puis `ameesh action execute <id> --proof run=…` inscrit
la preuve d'exécution (URL de run, SHA, identifiant de ressource). La porte a
déjà écrit l'action, son go et son reçu ; le connecteur ajoute la preuve que
le geste a bien eu lieu.

`proof` est un objet (ou, pour un essai, une chaîne qui vaut `resource`) dont
les clés sont `run`, `sha`, `resource` et `ref`. Une clé vide est ignorée, une
clé inconnue est refusée : la preuve est recopiée telle quelle dans
`actions.external_ref` en JSON canonique, et la vue `ameesh action journal`
la relit. Sans preuve, l'exécution rend `failed` : rien n'est enregistré, et
un nouvel essai porteur de la preuve peut être autorisé (le geste lui-même
est hors de la porte en mode journal).

Le connecteur garantit la déduplication : la preuve est écrite dans l'action
elle-même, une nouvelle tentative de la MÊME action ne peut pas la dupliquer.
`reconcile` ne lit rien : cette porte n'a pas d'effet externe à retrouver.

Le module fournit aussi la détection des déploiements de production faits
hors porte (#7684, point 4) : le journal externe est fourni par le système de
déploiement, jamais tenu par le cœur d'ameesh (`canon §14` : staging
seulement). `detect_prod_deploys` lit le fichier JSONL désigné par
`AMEESH_PROD_DEPLOY_JOURNAL` ; sans fichier, rien n'est détecté.
"""
from __future__ import annotations

import json
import os

from ... import jcs
from ...connectors import Action, ConnectorError, Outcome

#: opération → classe par défaut (le canon prime ; elle est toujours déclarée)
OPERATIONS = {
    "prod-deploy": "irreversible",
    "spend": "costly",
    "publish": "irreversible",
    "decision-sign": "irreversible",
    "decision-sign-key": "irreversible",
}
#: champs de la preuve d'exécution (#7684) : au moins un, valeurs non vides
PROOF_FIELDS = ("run", "sha", "resource", "ref")
MAX_PROOF_VALUE = 1024
#: journal externe des déploiements de production (JSONL), fourni au module
PROD_DEPLOY_JOURNAL_ENV = "AMEESH_PROD_DEPLOY_JOURNAL"


def clean_proof(value) -> dict:
    """La preuve normalisée (dict clé → texte), ou ConnectorError.

    Une chaîne vaut `resource` (essai rapide) ; un objet ne porte que
    `PROOF_FIELDS`, avec des valeurs texte non vides, 1024 caractères au
    plus. Aucun champ n'est obligatoire en particulier, mais la preuve ne
    peut pas être vide.
    """
    if isinstance(value, str):
        value = {"resource": value}
    if not isinstance(value, dict) or not value:
        raise ConnectorError("journal: proof is an object with run, sha, resource or ref")
    unknown = sorted(set(value) - set(PROOF_FIELDS))
    if unknown:
        raise ConnectorError("journal: unknown proof field(s) %s (run, sha, resource, ref)"
                             % ", ".join(unknown))
    cleaned = {}
    for key in PROOF_FIELDS:
        if key not in value:
            continue
        text = value[key]
        if not isinstance(text, str):
            raise ConnectorError("journal: proof.%s is text" % key)
        text = text.strip()
        if not text:
            continue
        if len(text) > MAX_PROOF_VALUE:
            raise ConnectorError("journal: proof.%s is at most %d characters"
                                 % (key, MAX_PROOF_VALUE))
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
            raise ConnectorError("journal: proof.%s has control characters" % key)
        cleaned[key] = text
    if not cleaned:
        raise ConnectorError("journal: proof is empty (run, sha, resource or ref)")
    return cleaned


def proof_text(proof: dict) -> str:
    """La preuve en clair, pour le détail d'une issue (courte)."""
    return ", ".join("%s=%s" % (key, proof[key]) for key in PROOF_FIELDS if key in proof)


def parse_proof(external_ref) -> dict | None:
    """Relit une preuve écrite par `execute` ; None si le champ n'en est pas une."""
    if not isinstance(external_ref, str) or not external_ref:
        return None
    try:
        value = json.loads(external_ref)
    except ValueError:
        return None
    try:
        return clean_proof(value)
    except ConnectorError:
        return None


class JournalConnector:
    name = "journal"
    dedupe = "guaranteed"

    def __init__(self, timeout: float = 30.0):
        self.timeout = float(timeout)

    def classify(self, operation: str, args: dict) -> str:
        return OPERATIONS.get(operation, "irreversible")

    def execute(self, action: Action, idempotency_key: str) -> Outcome:
        raw = action.args.get("proof") if isinstance(action.args, dict) else None
        try:
            proof = clean_proof(raw)
        except ConnectorError as exc:
            return Outcome.failed("no execution proof recorded: %s" % exc)
        return Outcome.confirmed(jcs.dumps(proof),
                                 "execution proof recorded: %s" % proof_text(proof))

    def reconcile(self, action: Action) -> Outcome | None:
        # The proof is written in the action row: there is no external effect
        # to read again. `unknown` stays for a human, never a guessed outcome.
        return None


def detect_prod_deploys(*, since_ts: float, until_ts: float) -> list[dict]:
    """Déploiements de production lus dans le journal externe du module.

    Le cœur d'ameesh ne tient pas ce journal (canon §14 : staging seulement) :
    le système de déploiement écrit une ligne JSON par déploiement, et le
    module la relit. Chaque ligne : `{"id": "…", "target": "…", "ref": "…",
    "ts": <epoch>, "proof": {...}}`. Les lignes illisibles sont ignorées :
    un journal externe n'est jamais une autorité, seulement un signalement.
    """
    path = os.environ.get(PROD_DEPLOY_JOURNAL_ENV)
    if not path:
        return []
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    detected = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        ident = row.get("id")
        target = row.get("target")
        ref = row.get("ref")
        ts = row.get("ts")
        if not (isinstance(ident, str) and ident and isinstance(target, str) and target
                and isinstance(ref, str) and ref
                and isinstance(ts, (int, float)) and not isinstance(ts, bool)):
            continue
        if not (float(since_ts) <= float(ts) < float(until_ts)):
            continue
        proof = row.get("proof")
        try:
            proof = clean_proof(proof) if proof is not None else None
        except ConnectorError:
            proof = None
        detected.append({"id": ident, "target": target, "ref": ref,
                         "ts": float(ts), "proof": proof})
    return detected
