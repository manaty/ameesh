# SPDX-License-Identifier: AGPL-3.0-only
"""D'où ameesh-approve lit une action : jamais du texte de l'agent.

Le service reçoit seulement un `action_id` ; il relit l'action lui-même,
recalcule son empreinte (spec §7.1) et en rend son propre résumé. Une
`ActionSource` rend un dict normalisé :

    action_id, project, connector, operation, target, args (objet),
    amount (entier en unités mineures ou None), currency (ou None),
    policy_version, class, state, et facultativement proposed_by,
    work_item, digest (empreinte stockée, recoupée avec le recalcul), dedupe
    (guaranteed | none), replaces, replaced_by, canon (L44 : canon de l'action,
    '' = canon par défaut ; absent = canon par défaut).

`DbActionSource` lit la table `actions` du lot L5 (spec §7.1). Si elle n'existe
pas (L5 non migré), ou s'il lui manque une colonne de l'empreinte, l'erreur le
dit clairement. `MemoryActionSource` sert aux tests et aux démonstrations.
"""
from __future__ import annotations

import json
import re
import threading
from typing import Protocol

from .. import receipts, storage
from ..db import DbError, quote_ident

ACTION_ID_RE = re.compile(r"^act_[0-9A-Za-z]{26}$")
#: états où une approbation ORDINAIRE a un sens (spec §7.2, `actions.py` du
#: lot L5) : proposed → approved ; approved : reçu précédent échu avant le
#: lancement ; failed : nouvelle tentative après un échec certain (`retry`,
#: nouveau reçu, MÊME action_id) ; unknown : nouvelle tentative seulement si
#: le connecteur garantit la déduplication (`dedupe` = guaranteed). La
#: décision « assumer le doublon » a ses propres états (duplicate.py).
REQUESTABLE_STATES = ("proposed", "approved", "failed", "unknown")


class ActionSourceError(RuntimeError):
    """La source d'actions est indisponible ou incompatible."""


class ActionSource(Protocol):
    def get_action(self, action_id: str) -> dict | None:
        """L'action normalisée, ou None si elle n'existe pas."""


def _text(value, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ActionSourceError("action : %s doit être un texte non vide" % name)
    return value


def normalize_action(raw: dict) -> dict:
    """Valide et normalise une action lue (types stricts, empreinte calculable)."""
    if not isinstance(raw, dict):
        raise ActionSourceError("action : objet attendu")
    action = {}
    action["action_id"] = _text(raw.get("action_id"), "action_id")
    if not ACTION_ID_RE.fullmatch(action["action_id"]):
        raise ActionSourceError("action : action_id invalide")
    for name in ("project", "connector", "operation", "target"):
        action[name] = _text(raw.get(name), name)
    args = raw.get("args")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError as exc:
            raise ActionSourceError("action : args illisibles") from exc
    if not isinstance(args, dict):
        raise ActionSourceError("action : args doit être un objet JSON")
    action["args"] = args
    amount = raw.get("amount")
    if isinstance(amount, float) and amount.is_integer():
        amount = int(amount)
    if amount is not None and (type(amount) is not int or amount < 0):
        raise ActionSourceError("action : montant entier positif (unités mineures) attendu")
    action["amount"] = amount
    currency = raw.get("currency")
    if currency is not None and (not isinstance(currency, str)
                                 or not re.fullmatch(r"[A-Z]{3}", currency)):
        raise ActionSourceError("action : devise ISO 4217 attendue")
    action["currency"] = currency
    if "policy_version" not in raw:
        raise ActionSourceError("action : policy_version absente (elle fait partie de "
                                "l'empreinte, spec §7.1)")
    policy_version = raw["policy_version"]
    if isinstance(policy_version, float) and policy_version.is_integer():
        policy_version = int(policy_version)
    action["policy_version"] = policy_version
    action_class = raw.get("class", raw.get("action_class"))
    if action_class not in receipts.ACTION_CLASSES:
        raise ActionSourceError("action : classe inconnue %r" % (action_class,))
    action["class"] = action_class
    action["state"] = _text(raw.get("state"), "state")
    for name in ("proposed_by", "work_item", "digest", "dedupe", "replaces", "replaced_by"):
        value = raw.get(name)
        if value is not None:
            action[name] = str(value)
    # L44 (0031) : le canon de l'action borne les authentificateurs admis
    canon = raw.get("canon")
    if canon is not None and not isinstance(canon, str):
        raise ActionSourceError("action : canon doit être un texte")
    action["canon"] = canon or ""
    try:
        action["computed_digest"] = receipts.action_digest(
            {name: action[name] for name in receipts.ACTION_DIGEST_FIELDS})
    except receipts.ReceiptError as exc:
        raise ActionSourceError("action : empreinte impossible : %s" % exc) from exc
    return action


class MemoryActionSource:
    """Source en mémoire (tests, démonstration) : {action_id: dict}."""

    def __init__(self, actions: dict | None = None):
        self._actions = dict(actions or {})
        self._lock = threading.Lock()

    def put(self, action: dict) -> None:
        with self._lock:
            self._actions[action["action_id"]] = dict(action)

    def get_action(self, action_id: str) -> dict | None:
        with self._lock:
            raw = self._actions.get(action_id)
        return None if raw is None else normalize_action(dict(raw))


class DbActionSource:
    """La table `actions` du lot L5 (spec §7.1), en lecture seule."""

    REQUIRED = ("action_id", "project", "connector", "operation", "target", "args", "amount",
                "currency", "policy_version", "state")
    #: tout ce que le service peut lire d'une action, et rien d'autre (lot
    #: L27) : ni reçu, ni nonce, ni challenge. Le rôle Postgres d'approve
    #: (deploy/sql/role-approve.sql) n'accorde que ces colonnes ; la lecture
    #: les nomme donc une à une (un `SELECT *` échouerait sous ce rôle).
    READ = REQUIRED + ("class", "action_class", "proposed_by", "work_item", "digest",
                       "dedupe", "replaces", "replaced_by", "canon")

    def __init__(self, db, table: str = "actions", lock: threading.Lock | None = None):
        self.db = db
        self.table = table
        quote_ident(table)  # un nom de table invalide est refusé dès ici (DbError)
        self._lock = lock or threading.Lock()

    def columns(self) -> set:
        return storage.of(self.db).action_source.columns(self.table)

    def check(self) -> None:
        """Lève ActionSourceError si la table manque ou est incompatible."""
        with self._lock:
            try:
                columns = self.columns()
            except DbError as exc:
                # le texte de l'erreur SQL n'est pas rendu (il peut finir sur une page)
                raise ActionSourceError("source d'actions injoignable (%s)"
                                        % type(exc).__name__) from exc
        if not columns:
            raise ActionSourceError(
                "table %r absente : la porte des actions (lot L5, migration 0010) n'est pas "
                "migrée dans ce schéma ; ameesh-approve ne peut pas lire l'action lui-même"
                % self.table)
        missing = [name for name in self.REQUIRED if name not in columns]
        if "class" not in columns and "action_class" not in columns:
            missing.append("class")
        if missing:
            raise ActionSourceError(
                "table %r incompatible : colonnes manquantes %s (l'empreinte de la spec §7.1 "
                "les exige)" % (self.table, missing))

    def get_action(self, action_id: str) -> dict | None:
        if not isinstance(action_id, str) or not ACTION_ID_RE.fullmatch(action_id):
            return None
        self.check()
        with self._lock:
            try:
                present = self.columns()
                wanted = [name for name in self.READ if name in present]
                rows = storage.of(self.db).action_source.rows(self.table, action_id, wanted)
            except DbError as exc:
                raise ActionSourceError("lecture de l'action impossible (%s)"
                                        % type(exc).__name__) from exc
        if not rows:
            return None
        if len(rows) > 1:
            raise ActionSourceError("action %s en double dans %r" % (action_id, self.table))
        return normalize_action(dict(rows[0]))
