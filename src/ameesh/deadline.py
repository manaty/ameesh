# SPDX-License-Identifier: AGPL-3.0-only
"""Une échéance monotone partagée par les étapes d'un même passage (#2665).

Les plafonds par opération ne composent pas une borne : chaque étape lit ici
le temps qui lui reste, jamais son seul plafond. Une échéance épuisée est une
indisponibilité (``run-incomplete``), jamais un rouge. L'horloge est
injectable pour que la borne se teste sans dormir.
"""
from __future__ import annotations

import time


class Deadline:
    """Échéance monotone d'un passage, partagée par ses étapes."""

    def __init__(self, budget: float, clock=time.monotonic):
        self.clock = clock
        #: #7161 : le budget tel que l'appelant l'a posé. `at - started` ne le
        #: dit plus après une pause : les messages gardent le budget d'origine.
        self.budget = max(0.0, float(budget))
        self.started = clock()
        self.at = self.started + self.budget

    def remaining(self) -> float:
        return max(0.0, self.at - self.clock())

    def timeout(self, cap: float) -> float:
        """Le délai d'une commande : ``cap``, jamais au-delà de l'échéance."""
        return min(float(cap), self.remaining())

    def pause(self, seconds: float) -> None:
        """Suspend l'échéance : le temps passé hors du travail ne court pas.

        #7161 : l'attente d'un créneau de la file heavy n'est pas du travail
        de prévalidation ; le run qui suit garde le budget qu'il avait."""
        self.at += max(0.0, float(seconds))

    def expired(self) -> bool:
        return self.remaining() <= 0.0
