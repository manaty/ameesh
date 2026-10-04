# SPDX-License-Identifier: AGPL-3.0-only
"""Connecteurs d'action (spec §7.3) : le seul endroit où ameesh touche le monde.

Un connecteur déclare :

* `name` : son nom, celui de la colonne `actions.connector` ;
* `dedupe` : `guaranteed` si le système externe déduplique sur la clé
  d'idempotence (une nouvelle tentative de la MÊME action est alors sans
  risque de doublon), `none` sinon ;
* `timeout` : au-delà, la porte considère l'issue inconnue ;
* `classify(operation, args)` : la classe par défaut d'une opération (la
  classe déclarée dans le canon prime) ;
* `execute(action, idempotency_key)` : l'effet ; la clé est TOUJOURS
  l'`action_id`. Renvoie une `Outcome` : `confirmed`, `failed` (échec
  CERTAIN, aucun effet) ou `unknown`. Dans le doute : `unknown`, jamais
  `failed` ;
* `reconcile(action)` : retrouve l'issue par une lecture ; `None` =
  introuvable, une `Outcome` `unknown` = lue mais rien de certain (son
  détail dit pourquoi) : la décision revient à un humain. Même règle
  qu'`execute` : `confirmed` sur preuve, `failed` sur échec certain.

La porte (`ameesh.actions`) appelle `execute` seulement après avoir écrit
`launched` en base ; une exception, un délai dépassé ou une perte de
connexion pendant l'appel donnent `unknown`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

ACTION_CLASSES = ("read", "reversible", "irreversible", "costly")
#: gravité croissante : une classe demandée ne peut que relever celle du connecteur
SEVERITY = {"read": 0, "reversible": 1, "irreversible": 2, "costly": 3}
OUTCOMES = ("confirmed", "failed", "unknown")
DEDUPE = ("guaranteed", "none")
DEFAULT_TIMEOUT = 60.0


class ConnectorError(RuntimeError):
    """Connecteur inconnu ou mal configuré (jamais une issue d'action)."""


@dataclass(frozen=True)
class Action:
    """Ce que le connecteur voit d'une action : son identité, rien d'autre."""

    action_id: str
    project: str
    connector: str
    operation: str
    target: str
    args: dict
    action_class: str
    amount: Optional[int] = None
    currency: Optional[str] = None
    policy_version: str = "1"
    attempt: int = 0
    work_item: Optional[int] = None


@dataclass(frozen=True)
class Outcome:
    """Issue d'un appel ou d'une réconciliation."""

    state: str
    external_ref: Optional[str] = None
    detail: str = ""
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.state not in OUTCOMES:
            raise ConnectorError("issue inconnue du protocole : %r (%s)"
                                 % (self.state, ", ".join(OUTCOMES)))

    @classmethod
    def confirmed(cls, external_ref: Optional[str] = None, detail: str = "") -> "Outcome":
        return cls("confirmed", external_ref, detail)

    @classmethod
    def failed(cls, detail: str, external_ref: Optional[str] = None) -> "Outcome":
        return cls("failed", external_ref, detail)

    @classmethod
    def unknown(cls, detail: str, external_ref: Optional[str] = None) -> "Outcome":
        return cls("unknown", external_ref, detail)


@runtime_checkable
class Connector(Protocol):
    name: str
    dedupe: str

    def classify(self, operation: str, args: dict) -> str: ...

    def execute(self, action: Action, idempotency_key: str) -> Outcome: ...

    def reconcile(self, action: Action) -> Optional[Outcome]: ...


def timeout_of(connector) -> float:
    value = getattr(connector, "timeout", None)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return value if value > 0 else DEFAULT_TIMEOUT


def check(connector) -> None:
    """Refuse un objet qui ne respecte pas le protocole (avant toute écriture)."""
    if not isinstance(connector, Connector):
        raise ConnectorError("objet %r : pas un connecteur (name, dedupe, classify, execute, "
                             "reconcile)" % (connector,))
    if getattr(connector, "dedupe", None) not in DEDUPE:
        raise ConnectorError("connecteur %s : dedupe %r (guaranteed ou none)"
                             % (getattr(connector, "name", "?"), getattr(connector, "dedupe", None)))


NAMES = ("shell-noop", "git-merge")


def get(name: str, **options) -> Connector:
    """Instancie un connecteur fourni par ameesh (`shell-noop`, `git-merge`)."""
    if name == "shell-noop":
        from .shell_noop import ShellNoopConnector
        return ShellNoopConnector(**options)
    if name == "git-merge":
        from .git_merge import GitMergeConnector
        return GitMergeConnector(**options)
    raise ConnectorError("connecteur inconnu : %r (connus : %s)" % (name, ", ".join(NAMES)))
