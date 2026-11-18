# SPDX-License-Identifier: AGPL-3.0-only
"""Vocabulaire des rôles de gouvernance, lus sur une fiche Agent du canon.

#7792 (#7637 tranche 3a) : le cœur ne reconnaît jamais un rôle sur le titre
d'une persona. La fiche Agent déclare ses rôles (`roles: [...]`, ou `role:`)
et `sous_utilisation` compare cette déclaration aux jetons ci-dessous. Ce
module est le seul endroit où ces jetons sont écrits : les lecteurs les
importent, ils ne les recopient pas. Un mesh dont le canon nomme ses rôles
autrement n'a qu'à déclarer les siens — le titre de la persona ne gouverne
rien.
"""
from __future__ import annotations

#: rôles d'une fiche Agent qui désignent l'auditeur
AUDITOR_ROLES = ("auditeur", "auditor")
#: rôles d'une fiche Agent qui désignent le pilote/orchestrateur
ORCHESTRATOR_ROLES = ("orchestrateur", "orchestrator")
#: rôles de conception, qui portent les passes d'observation
DESIGN_ROLES = ("conception", "design")
#: rôles critiques par défaut (une organisation les règle par `critical_roles`)
CRITICAL_ROLES = (ORCHESTRATOR_ROLES[0], DESIGN_ROLES[0], AUDITOR_ROLES[0],
                  "relecture")
#: synonymes reconnus, ramenés au nom canonique du rôle
ROLE_SYNONYMS = {
    ORCHESTRATOR_ROLES[0]: ORCHESTRATOR_ROLES[1:],
    DESIGN_ROLES[0]: DESIGN_ROLES[1:],
    AUDITOR_ROLES[0]: AUDITOR_ROLES[1:],
    "relecture": ("review", "reviewer"),
}
