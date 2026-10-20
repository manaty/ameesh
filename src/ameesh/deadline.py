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
        self.started = clock()
        self.at = self.started + max(0.0, float(budget))

    def remaining(self) -> float:
        return max(0.0, self.at - self.clock())

    def timeout(self, cap: float) -> float:
        """Le délai d'une commande : ``cap``, jamais au-delà de l'échéance."""
        return min(float(cap), self.remaining())

    def expired(self) -> bool:
        return self.remaining() <= 0.0
