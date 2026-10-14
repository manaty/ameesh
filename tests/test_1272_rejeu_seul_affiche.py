# SPDX-License-Identifier: AGPL-3.0-only
"""#1272 — un rejeu seul est affiché NON DATÉ, jamais présenté comme frais.

Arbitrage pilote mesh-design du 14/10 08:33 : le résidu « rejeu seul sans
historique daté » (que #1132 laisse utilisable) est documenté et affiché NON
DATÉ / provenance inconnue ; il n'est pas dit « périmé » faute de preuve
explicite, aucun bornage d'âge ni de `mtime` n'est introduit, et l'admission
ne change pas.

Témoins : sans historique daté, la mention est portée et la valeur reste
lisible ; une mesure datée (fraîche) ne la porte pas — la qualification #1132
est préservée ; une jauge Codex, qui ne publie pas d'instant de production,
ne porte pas la mention.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from ameesh import accounts, cost

from .test_cost import Sandbox
from .test_l30_accounts import _dossier


def marked(account: str, ts: float) -> dict:
    return {"type": cost.ACCOUNT_MARKER, "harness": "claude", "account": account, "ts": ts}


def windows(**values) -> dict:
    """Un `rate_limit_event` Claude : `cle=(utilisation, resetsAt)`."""
    return {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
        key: {"utilization": used, "resetsAt": resets}
        for key, (used, resets) in values.items()}}}


class RejeuSeulAfficheTest(Sandbox, unittest.TestCase):
    """`ameesh accounts list` : la provenance non datée est visible."""

    def setUp(self) -> None:
        super().setUp()
        dossier = _dossier(os.path.join(self.tmp_cost, "comptes", "claude-2"),
                           creds=".credentials.json")
        self.cfg = mock.Mock(host="hote-test", accounts={"claude": [
            {"name": "secondaire", "path": dossier}]})

    def _lignes(self, *events):
        self.agent("fige", "claude")
        self.events("fige", *events)
        return accounts.report(self.cfg, None, self.book(db=None),
                               now=self.now, record=False)

    def test_un_rejeu_seul_est_affiche_non_date_et_garde_sa_valeur(self):
        # Marqueur récent + five_hour close 25 h avant : le harnais rejoue une
        # mesure d'un tour antérieur (#1132, `_replayed`).
        lignes = self._lignes(
            marked("secondaire", self.now - 5),
            windows(five_hour=(0.75, self.now - 25 * 3600),
                    seven_day=(0.67, self.now + 3 * 86400)))
        jauges = {g["key"]: g for g in lignes[0]["gauges"]}
        self.assertIsNone(jauges["seven_day"]["produced_at"])
        self.assertIsNone(jauges["five_hour"]["produced_at"])
        self.assertAlmostEqual(jauges["seven_day"]["last_used"], 0.67)
        texte = accounts.format_rows(lignes)
        # la valeur reste lisible : elle n'est pas cachée
        self.assertIn("seven_day 67% (pace ", texte)
        self.assertIn("[non daté : provenance inconnue]", texte)
        # pas de conclusion « périmé » : aucune preuve de péremption supposée
        self.assertNotIn("périmé", texte)

    def test_une_mesure_datee_ne_porte_pas_la_mention(self):
        lignes = self._lignes(
            marked("secondaire", self.now - 10),
            windows(seven_day=(0.91, self.now + 3 * 86400)))
        jauge = {g["key"]: g for g in lignes[0]["gauges"]}["seven_day"]
        self.assertAlmostEqual(jauge["produced_at"], self.now - 10, places=0)
        self.assertEqual(jauge["source"], "fige")
        texte = accounts.format_rows(lignes)
        self.assertIn("seven_day 91% (pace ", texte)
        self.assertNotIn("non daté", texte)

    def test_une_jauge_codex_ne_porte_pas_la_mention(self):
        # Codex ne publie pas d'instant de production : la mention vise les
        # relevés Claude du résidu #1272, pas l'affichage Codex.
        self.assertEqual(accounts._mention_non_datee({"harness": "codex"}, {}), "")
        self.assertEqual(
            accounts._mention_non_datee({"harness": "claude"}, {}),
            " [non daté : provenance inconnue]")
        self.assertEqual(
            accounts._mention_non_datee({"harness": "claude"}, {"produced_at": 1.0}), "")
