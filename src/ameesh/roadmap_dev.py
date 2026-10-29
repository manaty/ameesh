# SPDX-License-Identifier: AGPL-3.0-only
"""Correspondances du cycle de vie actuel vers les jalons génériques (lot L96).

La feuille de route (`roadmap`) appartient au cœur générique : elle ne
raisonne que sur des jalons génériques (demandée, en cours, soumise, verdict,
livrée, abandonnée) et sur des états génériques. Le cycle actuel des tâches
porte encore les noms du module « développement » (étude « cœur générique et
modules métier », §2.2) ; leur traduction tient dans **cette seule table**,
que le futur module `dev` reprendra telle quelle (L99, L100), et que le
garde-fou du cœur (L97) n'aura qu'une ligne d'exception à porter.

Rien d'autre dans `roadmap` ne nomme un état ou un jalon de module.
"""
from __future__ import annotations

from ameesh import work_lifecycle as lifecycle

from . import work as work_mod

#: LA table. Clés génériques → valeurs du cycle actuel.
CORRESPONDANCES = {
    # jalons réels d'une tâche : clé générique → clé de `progress._work_item_milestones`
    "jalons": {
        "demandee": "requested",
        "soumise": "frozen",
        "verdict": "verdict",
        "livree": "merged",
    },
    # états du journal qui marquent le premier passage « en cours »
    "etats_en_cours": (lifecycle.IN_PROGRESS,),
    # états où la date prévue reste pilotable par le porteur : la tâche est
    # demandée ou en cours ; plus avancée (relecture, attente), elle ne l'est
    # plus, et sa date ne déclenche donc pas d'alerte (#2342)
    "etats_actifs": lifecycle.ACTIVE,
    # états d'une tâche livrée (l'effet a eu lieu) et d'une tâche abandonnée
    "etats_livres": tuple(work_mod.MERGED_STATES),
    "etats_abandonnes": (lifecycle.CLOSED,),
    # états qui attendent une décision humaine (jalon de décision)
    "etats_decision": (lifecycle.WAITING_HUMAN,),
    # état affiché par `progress.lot_state` → état générique
    "etats_affiches": {
        "active": "en_cours",
        "review": "a_valider",
        "approved": "validee",
        "blocked": "bloquee",
        "merged": "livree",
        "closed": "abandonnee",
    },
}


def milestone(generic: str) -> str:
    """La clé de `progress._work_item_milestones` d'un jalon générique."""
    return CORRESPONDANCES["jalons"][generic]


def delivered(state: str | None, close_reason: str | None = None) -> bool:
    """Livrée : fusionnée, promue, ou livrée hors cible (#988 : `closed` +
    `delivered`)."""
    return ((state or "") in CORRESPONDANCES["etats_livres"]
            or (state == lifecycle.CLOSED and close_reason == "delivered"))


def abandoned(state: str | None, close_reason: str | None = None) -> bool:
    """Abandonnée : fermée sans livraison."""
    return ((state or "") in CORRESPONDANCES["etats_abandonnes"]
            and not delivered(state, close_reason))


def in_progress(state: str | None) -> bool:
    return (state or "") in CORRESPONDANCES["etats_en_cours"]


def active(state: str | None) -> bool:
    """La date prévue reste pilotable par le porteur (tâche demandée ou en
    cours) : plus avancée, elle attend un tiers et n'alerte pas (#2342)."""
    return (state or "") in CORRESPONDANCES["etats_actifs"]


def attend_decision(state: str | None) -> bool:
    return (state or "") in CORRESPONDANCES["etats_decision"]


def generic_state(shown_state: str | None) -> str:
    """L'état générique d'une tâche depuis l'état affiché par `progress`."""
    return CORRESPONDANCES["etats_affiches"].get(shown_state or "", "en_cours")


#: L148 (stage 2c): old spellings kept for the callers not yet renamed.
jalon = milestone
livree = delivered
abandonnee = abandoned
en_cours = in_progress
actif = active
etat_generique = generic_state
