# SPDX-License-Identifier: AGPL-3.0-only
"""#3876 — une pause de fenêtre courte du compte ne retire pas un relecteur.

Constat (27/10 03:34Z) : le gel #21235 de #3843 demandait deux avis ; cx1 et
cx2 étaient en pause « 2/2 concurrent turns (provider burst bucket) », que
l'exécuteur écrit sous le préfixe de pause budget (« budget : … »). La
désignation a lu ce préfixe comme une pause de jauge et n'a trouvé qu'une
famille éligible ; cx2 a rendu un avis quelques minutes plus tard.

Témoins : les trois raisons de `account_window._reason_from_state` (plafond de
tours, cooldown, écart de début) laissent l'agent disponible, avec ou sans la
marque d'agent critique ; une pause de jauge et une pause d'exploitation
restent indisponibles ; deux familles redeviennent éligibles.
"""
from __future__ import annotations

import time
import unittest

from ameesh import account_window, budget
from ameesh import review_designation as rd


def row(name, *, harness="codex", text="", status="blocked",
        capabilities=("read", "report-drift", "propose"), roles=("implementation",)):
    return {"name": name, "team": "ameesh", "harness": harness,
            "session_harness": "", "status": status, "mode": "execute",
            "status_text": text, "operator_paused": False,
            "capabilities": list(capabilities), "roles": list(roles)}


def short_reasons(now):
    """Les trois raisons réelles de la fenêtre courte, telles qu'écrites."""
    base = {"account": "cx1", "recent_429": 0, "active": [], "active_count": 0,
            "max_active": 0}
    return [
        account_window._reason_from_state(dict(
            base, active_count=2, max_active=2,
            active=[{"name": "cx2"}, {"name": "dev3"}]), now),
        account_window._reason_from_state(dict(
            base, cooldown_until=now + 240, recent_429=2), now),
        account_window._reason_from_state(dict(
            base, next_start_at=now + 20, min_start_gap_s=30,
            last_start_at=now - 10), now),
    ]


class ShortWindowPauseTest(unittest.TestCase):

    def test_les_trois_raisons_de_fenetre_courte_restent_disponibles(self):
        now = time.time()
        for reason in short_reasons(now):
            self.assertTrue(reason)
            for text in (budget.PAUSE_PREFIX + reason,
                         budget.PAUSE_PREFIX + budget.CRITICAL_MARK + reason):
                with self.subTest(text=text):
                    self.assertTrue(rd.is_short_window_pause(text))
                    self.assertTrue(rd.is_available(row("cx1", text=text)))

    def test_le_texte_du_constat_reste_disponible(self):
        text = ("budget : account cx1: 2/2 concurrent turns (provider burst "
                "bucket shared by sessions, #3779; running: cx2, dev3)")
        self.assertTrue(rd.is_available(row("cx1", text=text)))

    def test_une_pause_de_jauge_reste_indisponible(self):
        for text in ("budget : pause (account tertiaire, seven_day 83 %) until "
                     "14:20; no other account",
                     "budget : critical agent: every codex account over threshold",
                     "budget : account cx1 in overage",
                     "paused by operations"):
            with self.subTest(text=text):
                self.assertFalse(rd.is_short_window_pause(text))
                self.assertFalse(rd.is_available(row("cx1", text=text)))

    def test_la_raison_sans_prefixe_budget_n_est_pas_une_pause(self):
        # refus d'admission (`claim_start`) : statut `blocked`, raison brute,
        # déjà disponible avant #3876
        reason = short_reasons(time.time())[0]
        self.assertFalse(rd.is_short_window_pause(reason))
        self.assertTrue(rd.is_available(row("cx1", text=reason)))

    def test_deux_familles_eligibles_malgre_la_pause_codex(self):
        text = budget.PAUSE_PREFIX + short_reasons(time.time())[0]
        rows = [row("cx1", text=text), row("cx2", text=text),
                row("dev4", harness="claude", status="idle")]
        candidates = rd.candidates_from_registry(
            rows, project="ameesh", families=("claude", "codex", "deepseek"))
        families = {c.family for c in candidates if c.available}
        self.assertEqual(families, {"claude", "codex"})


if __name__ == "__main__":
    unittest.main()
