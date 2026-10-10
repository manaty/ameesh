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

from . import work as work_mod

#: LA table. Clés génériques → valeurs du cycle actuel.
CORRESPONDANCES = {
    # jalons réels d'une tâche : clé générique → clé de `progress._jalons_de_lot`
    "jalons": {
        "demandee": "requested",
        "soumise": "frozen",
        "verdict": "verdict",
        "livree": "merged",
    },
    # états du journal qui marquent le premier passage « en cours »
    "etats_en_cours": ("build",),
    # états d'une tâche livrée (l'effet a eu lieu) et d'une tâche abandonnée
    "etats_livres": tuple(work_mod.MERGED_STATES),
    "etats_abandonnes": ("closed",),
    # états qui attendent une décision humaine (jalon de décision)
    "etats_decision": ("waiting_human",),
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


def jalon(generic: str) -> str:
    """La clé de `progress._jalons_de_lot` d'un jalon générique."""
    return CORRESPONDANCES["jalons"][generic]


def livree(state: str | None) -> bool:
    return (state or "") in CORRESPONDANCES["etats_livres"]


def abandonnee(state: str | None) -> bool:
    return (state or "") in CORRESPONDANCES["etats_abandonnes"]


def en_cours(state: str | None) -> bool:
    return (state or "") in CORRESPONDANCES["etats_en_cours"]


def attend_decision(state: str | None) -> bool:
    return (state or "") in CORRESPONDANCES["etats_decision"]


def etat_generique(shown_state: str | None) -> str:
    """L'état générique d'une tâche depuis l'état affiché par `progress`."""
    return CORRESPONDANCES["etats_affiches"].get(shown_state or "", "en_cours")
