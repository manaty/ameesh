# SPDX-License-Identifier: AGPL-3.0-only
"""L242b (PR 2b) : un agent critique au seuil sur tous les comptes de son
harnais tourne sous un autre harnais (Codex au forfait, puis DeepSeek à clé
d'API), par rotation avec résumé, et revient au sien dès qu'un de ses comptes
a de la marge — résumé sous le harnais emprunté. Le tour emprunté est compté
sous son harnais (grand livre, plafond payé au token du mesh)."""
from __future__ import annotations

import os
from unittest import mock

from ameesh import account_turn, accounts, registry

from .support import FAKEBIN
from .test_l30_accounts import _Base

PAUSE = ("pause (account primaire, five_hour 96 %) until 14:20; all claude accounts "
         "at threshold")


def _claude_au_seuil():
    """`accounts.choose` : tous les comptes claude au seuil, les autres réels."""
    reel = accounts.choose

    def choose(db, host, harness, items, book, **kw):
        if harness == "claude":
            return accounts.Choice(None, items[0], reason=PAUSE)
        return reel(db, host, harness, items, book, **kw)
    return mock.patch.object(accounts, "choose", side_effect=choose)


class RepliTest(_Base):
    def worker(self, name="orch", critical=True, fallbacks=("codex",)):
        self.claude, self.codex = self._claude_comptes(), self._codex_comptes()
        return self._worker(name, "claude", {"claude": self.claude, "codex": self.codex},
                            critical_agents=(name,) if critical else (),
                            critical_fallback_harnesses=fallbacks)[1]

    def harnais_du_grand_livre(self):
        return [r["harness"] for r in self.db.query(
            "SELECT harness FROM turn_costs WHERE agent = 'orch' ORDER BY id")]

    def test_aller_sous_codex_puis_retour(self):
        worker = self.worker()
        try:
            self._tour(worker)
            self.assertEqual(self.turns()[-1]["harness"], "claude")
            n = len(self.turns())
            with _claude_au_seuil():
                self.assertIsNotNone(self._tour(worker, "suite"))
            tours = self.turns()[n:]
            # résumé sous claude (session reprise), puis session neuve sous codex
            self.assertEqual([t["harness"] for t in tours], ["claude", "codex"])
            self.assertIn("--resume", tours[0]["argv"])
            self.assertNotIn("resume", tours[1]["argv"])
            self.assertEqual(tours[1]["compte"]["CODEX_HOME"], self.codex[0]["path"])
            self.assertEqual(self.harnais_du_grand_livre()[-1], "codex")
            fil = self._fil()
            self.assertIn("its turns run under codex/primaire", fil)
            self.assertIn("Harness switch: session", fil)
            # le choix partagé de claude n'a pas bougé (choix propre à l'agent)
            self.assertEqual(self._bascules(), [])

            n = len(self.turns())
            self.assertIsNotNone(self._tour(worker, "retour"))   # claude libre
            tours = self.turns()[n:]
            self.assertEqual([t["harness"] for t in tours], ["codex", "claude"])
            self.assertIn("resume", tours[0]["argv"])          # résumé sous codex
            self.assertNotIn("--resume", tours[1]["argv"])
            self.assertEqual(self.harnais_du_grand_livre()[-2:], ["codex", "claude"])
        finally:
            worker.watchdog_stop.set()

    def test_le_modele_de_l_agent_ne_suit_pas_sous_un_autre_harnais(self):
        worker = self.worker()
        try:
            self.db.execute("UPDATE agent_registry SET model = 'claude-opus-5-5'"
                            " WHERE name = 'orch'")
            worker.agent = registry.get(self.db, "orch")
            with _claude_au_seuil():
                self._tour(worker)
            argv = self.turns()[-1]["argv"]
            self.assertEqual(self.turns()[-1]["harness"], "codex")
            self.assertNotIn("claude-opus-5-5", " ".join(argv))
        finally:
            worker.watchdog_stop.set()

    def test_un_non_critique_reste_en_pause(self):
        worker = self.worker(name="dev1", critical=False)
        try:
            with _claude_au_seuil():
                self.assertIsNone(self._tour(worker))
            self.assertEqual(self.turns(), [])
            self.assertTrue(registry.get(self.db, "dev1")["status_text"].startswith(
                "budget : pause (account primaire"))
        finally:
            worker.watchdog_stop.set()

    def test_sans_harnais_de_repli_avec_de_la_marge_la_pause_reste(self):
        worker = self.worker()
        try:
            reel = accounts.choose

            def choose(db, host, harness, items, book, **kw):
                return accounts.Choice(None, items[0], reason=PAUSE)
            with mock.patch.object(accounts, "choose", side_effect=choose):
                self.assertIsNone(self._tour(worker))
            self.assertIs(reel, accounts.choose)
            self.assertTrue(registry.get(self.db, "orch")["status_text"].startswith(
                "budget : critical agent: pause"))
        finally:
            worker.watchdog_stop.set()


class ChoixDuRepliTest(_Base):
    """`account_turn.fallback` : l'ordre, et le plafond payé au token."""

    def test_un_harnais_paye_au_dela_du_plafond_est_saute(self):
        _runner, worker = self._worker("orch", "claude", {"claude": self._claude_comptes()},
                                       critical_agents=("orch",),
                                       critical_fallback_harnesses=("deepseek",))
        bins = mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": FAKEBIN})
        bins.start()
        self.addCleanup(bins.stop)
        try:
            with mock.patch("ameesh.cost.CostBook.over", return_value="hourly budget: x"):
                self.assertEqual(account_turn.fallback(worker, PAUSE), PAUSE)
            self.assertIsNone(worker._borrowed)
            with mock.patch("ameesh.cost.CostBook.over", return_value=""):
                self.assertEqual(account_turn.fallback(worker, PAUSE), "")
            self.assertEqual(worker._borrowed, ("deepseek", None))
            self.assertEqual(account_turn._harness(worker), "deepseek")
        finally:
            worker.watchdog_stop.set()

    def test_un_harnais_absent_est_saute(self):
        _runner, worker = self._worker("orch", "claude", {"claude": self._claude_comptes()},
                                       critical_agents=("orch",),
                                       critical_fallback_harnesses=("inexistant",))
        try:
            self.assertEqual(account_turn.fallback(worker, PAUSE), PAUSE)
            self.assertEqual(account_turn._harness(worker), "claude")
        finally:
            worker.watchdog_stop.set()
