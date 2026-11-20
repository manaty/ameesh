# SPDX-License-Identifier: AGPL-3.0-only
"""#7833 — bascule d'un avatar `releve: preference` quand son runner s'arrête.

Un avatar vit sur son poste (rang 0) et se replie sur son hôte cloud (rang 1+)
quand le poste n'est plus **présent** (design 5322 §3.2). Cette présence était
celle d'un hôte : un relevé frais et un *standing* frais, tous deux publiés par
l'exécuteur. Un runner qui s'arrête pour échecs rapides (L48,
`runner.stop_after_failures`) publie bien un arrêt, mais laissait la ligne
`stopped` : plus aucun hôte ne la publiait, plus aucun candidat ne la classait,
et l'avatar restait muet alors que le poste était allumé et joignable.

Le présent module porte la règle unique de cet arrêt :

* seuls les avatars `preference` sont concernés — un arrêt `erreur` d'un agent
  épinglé reste un arrêt humain à examiner (`ameesh resume`) ;
* seuls les arrêts **automatiques** (`stop_reason = 'erreur'`, posé par
  `stop_after_failures`) sont concernés ; un `ameesh stop` (`manuel`) et un
  retrait de canon ne sont jamais repris par la bascule ;
* après `GRACE_S` (10 min, décision du ticket #6055), un autre hôte candidat
  peut reprendre la conversation ; avant, l'humain a le temps de réparer et
  de `ameesh resume` sur le poste ;
* la cible est le premier hôte déclaré du Placement autre que l'hôte courant
  (l'ordre de `candidats`/`admitted_hosts` est celui de la fiche) ;
* la reprise passe par `conversation_moves` (`recovered`), pas par un brief :
  la session native ne traverse pas, le segment de conversation si.

`role_standings_pg.elected_roles` et les décisions (`candidacy`,
`preference_return`) partagent le même prédicat et la même constante : aucun
seuil n'est recopié en dur ailleurs. La réclamation elle-même reste celle de
#7680 : un avatar ne se réclame jamais de l'autre hôte, le déplacement de
conversation met la ligne chez la cible ; le `switch` réveille alors l'arrêt
automatique pour que la cible puisse prendre le bail.
"""
from __future__ import annotations

from typing import Mapping

#: Décision owner du ticket #6055 (commentaire du 19/11, non signé) : 10 min
#: après l'arrêt du runner du poste avant de placer l'avatar sur son cloud.
GRACE_S = 600.0
#: raison d'arrêt posée par `Runner.stop_after_failures` (L48, 0030)
STOP_REASON = "erreur"


def is_preference(row: Mapping) -> bool:
    """La ligne est-elle un avatar à placement ordonné (rang 0 = poste) ?"""
    return str(row.get("releve") or "") == "preference"


def stopped_by_runner(row: Mapping) -> bool:
    """Arrêt automatique après échecs rapides — jamais un arrêt délibéré."""
    return (str(row.get("status") or "") == "stopped"
            and str(row.get("stop_reason") or "") == STOP_REASON)


def _since(row: Mapping) -> float | None:
    value = row.get("status_since_ts")
    if value is None:
        value = row.get("status_since")
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def age_s(row: Mapping, now: float) -> float | None:
    """Âge de l'arrêt (secondes, horloge de la base) ; None si illisible."""
    since = _since(row)
    if since is None:
        return None
    try:
        return max(0.0, float(now) - since)
    except (TypeError, ValueError):
        return None


def left_s(row: Mapping, now: float) -> float:
    """Délai de grâce restant, 0 quand la bascule est due."""
    age = age_s(row, now)
    if age is None:
        return 0.0
    return max(0.0, GRACE_S - age)


def due(row: Mapping, now: float) -> bool:
    """La bascule est-elle due (arrêt automatique d'un avatar, grâce écoulée) ?

    Sans autre hôte déclaré, il n'y a pas de bascule : l'arrêt reste un arrêt
    à réparer sur place (`target_host` vide), et aucun candidat n'est dégelé.
    """
    if not is_preference(row) or not stopped_by_runner(row):
        return False
    if not target_host(row):
        return False
    age = age_s(row, now)
    return age is not None and age >= GRACE_S


def target_hosts(row: Mapping) -> list[str]:
    """Les hôtes de repli déclarés, dans l'ordre du Placement, sans le courant."""
    current = str(row.get("host") or "")
    declared = list(row.get("candidats") or row.get("admitted_hosts") or ())
    return [str(host) for host in declared
            if host and str(host) != current]


def target_host(row: Mapping) -> str:
    """Le premier hôte de repli ("" si l'avatar n'en a aucun)."""
    hosts = target_hosts(row)
    return hosts[0] if hosts else ""


def stopped_sql(alias: str = "r", *, host_expr: str | None = None) -> str:
    """Prédicat SQL « arrêt automatique d'un avatar, repris par un AUTRE hôte ».

    Sans borne de grâce : sert à publier le standing de la cible avant
    l'échéance, pour qu'il soit frais quand la bascule devient due. La grâce
    est appliquée par `sql()`, `candidacy()` et `preference_return()`.
    """
    a = str(alias)
    other = host_expr if host_expr is not None else ("%s.host" % a)
    return ("(%(a)s.releve = 'preference' AND %(a)s.status = 'stopped'"
            " AND %(a)s.stop_reason = '%(reason)s'"
            " AND %(a)s.host IS DISTINCT FROM %(other)s)"
            % {"a": a, "reason": STOP_REASON, "other": other})
