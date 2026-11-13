# SPDX-License-Identifier: AGPL-3.0-only
"""Une échéance monotone partagée par les étapes d'un même passage (#2665).

Les plafonds par opération ne composent pas une borne : chaque étape lit ici
le temps qui lui reste, jamais son seul plafond. Une échéance épuisée est une
indisponibilité (``run-incomplete``), jamais un rouge. L'horloge est
injectable pour que la borne se teste sans dormir.

#7161 : le budget de travail et la borne murale sont deux choses. Une attente
hors travail (créneau de la file heavy) peut rendre son budget au travail
(``pause``), jamais du temps au-delà de la borne murale de l'appelant.
"""
from __future__ import annotations

import time


class Deadline:
    """Échéance monotone d'un passage, partagée par ses étapes."""

    def __init__(self, budget: float, clock=time.monotonic, wall: float | None = None):
        self.clock = clock
        #: #7161 : le budget tel que l'appelant l'a posé. `at - started` ne le
        #: dit plus après une pause : les messages gardent le budget d'origine.
        self.budget = max(0.0, float(budget))
        self.started = clock()
        self.at = self.started + self.budget
        #: #7161 : borne murale absolue que ``pause`` ne repousse jamais.
        #: Par défaut l'échéance de travail est elle-même la borne (aucun
        #: temps n'est rendu) ; un appelant qui a un plafond extérieur plus
        #: lointain le pose ici, et l'attente peut alors être suspendue sans
        #: dépasser ce plafond.
        self.wall = self.at if wall is None else float(wall)

    def _edge(self) -> float:
        return min(self.at, self.wall)

    def remaining(self) -> float:
        return max(0.0, self._edge() - self.clock())

    def wall_remaining(self) -> float:
        """Le temps de mur avant la borne absolue de l'appelant."""
        return max(0.0, self.wall - self.clock())

    def timeout(self, cap: float) -> float:
        """Le délai d'une commande : ``cap``, jamais au-delà de l'échéance."""
        return min(float(cap), self.remaining())

    def pause(self, seconds: float) -> None:
        """Suspend l'échéance : le temps passé hors du travail ne court pas.

        #7161 : l'attente d'un créneau de la file heavy n'est pas du travail
        de prévalidation ; le run qui suit garde le budget qu'il avait, tant
        que la borne murale de l'appelant le permet (``wall``)."""
        self.at += max(0.0, float(seconds))

    def expired(self) -> bool:
        return self.remaining() <= 0.0
