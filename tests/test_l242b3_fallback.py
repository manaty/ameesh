# SPDX-License-Identifier: AGPL-3.0-only
"""L242b (PR 2b) : un agent critique au seuil sur tous les comptes de son
harnais tourne sous un autre harnais (Codex au forfait, puis DeepSeek à clé
d'API), par rotation avec résumé, et revient au sien dès qu'un de ses comptes
a de la marge — résumé sous le harnais emprunté. Le tour emprunté est compté
sous son harnais (grand livre, plafond payé au token du mesh)."""
from __future__ import annotations

import errno
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


class _Repli(_Base):
    def worker(self, name="orch", critical=True, fallbacks=("codex",)):
        self.claude, self.codex = self._claude_comptes(), self._codex_comptes()
        return self._worker(name, "claude", {"claude": self.claude, "codex": self.codex},
                            critical_agents=(name,) if critical else (),
                            critical_fallback_harnesses=fallbacks)[1]

    def harnais_du_grand_livre(self):
        return [r["harness"] for r in self.db.query(
            "SELECT harness FROM turn_costs WHERE agent = 'orch' ORDER BY id")]


class RepliTest(_Repli):
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


class Relecture389Test(_Repli):
    """#389 r1 : le P1 et les quatre P2."""

    def test_harnais_du_tour_non_ecrit_le_tour_ne_part_pas(self):
        worker = self.worker()
        try:
            reel = worker._state_note

            def note(key, value):
                return False if key == "turn_harness" else reel(key, value)
            with mock.patch.object(worker, "_state_note", side_effect=note), \
                    _claude_au_seuil():
                self._tour(worker)
            self.assertEqual(self.turns(), [])
            self.assertEqual(self.harnais_du_grand_livre(), [])
            self.assertEqual(self.db.query(
                "SELECT count(*) AS n FROM spend_pending WHERE agent = 'orch'")[0]["n"], 0)
        finally:
            worker.watchdog_stop.set()

    def _claude(self, marge_pause=True, codex_pause=False):
        """claude au seuil avec la marge du retour seulement (sans elle, de la
        marge) ; codex au seuil si `codex_pause`."""
        reel = accounts.choose

        def choose(db, host, harness, items, book, **kw):
            if harness == "claude" and kw.get("margin") and marge_pause:
                return accounts.Choice(None, items[0], reason=PAUSE)
            if harness == "codex" and codex_pause:
                return accounts.Choice(None, items[0], reason=PAUSE)
            return reel(db, host, harness, items, book, **kw)
        return mock.patch.object(accounts, "choose", side_effect=choose)

    def test_le_retour_demande_une_marge(self):
        worker = self.worker()
        # le repli exige un harnais installé : les faux, pas ceux de l'hôte
        env = mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": FAKEBIN})
        env.start()
        self.addCleanup(env.stop)
        try:
            # dernier tour sous claude : pas de marge, il reste sous claude
            worker._state_note("turn_harness", "claude")
            with self._claude():
                worker._borrowed = None
                self.assertEqual(worker.budget_reason(), "")
                self.assertIsNone(worker._borrowed)
            # sous codex emprunté : claude juste sous le plafond ne suffit pas
            worker._state_note("turn_harness", "codex")
            self.assertTrue(account_turn.borrowing(worker))
            with self._claude():
                worker._borrowed = None
                self.assertEqual(worker.budget_reason(), "")
                self.assertEqual(worker._borrowed[0], "codex")
            # codex au seuil à son tour : claude, sans la marge, plutôt que la pause
            with self._claude(codex_pause=True):
                worker._borrowed = None
                self.assertEqual(worker.budget_reason(), "")
                self.assertIsNone(worker._borrowed)
            # avec la marge, le retour
            with self._claude(marge_pause=False):
                worker._borrowed = None
                self.assertEqual(worker.budget_reason(), "")
                self.assertIsNone(worker._borrowed)
        finally:
            worker.watchdog_stop.set()

    def test_pas_de_rotation_ordinaire_sous_un_autre_harnais(self):
        worker = self.worker()
        try:
            self._tour(worker)
            self.assertIsNotNone(worker.current_session())
            self.assertEqual(account_turn.session_harness(worker), "claude")
            self.assertTrue(worker._rotation_allowed())
            # claude au seuil : le tour emprunterait codex, dont les comptes
            # portent les mêmes noms — la rotation attend `continuity`
            with _claude_au_seuil():
                self.assertFalse(worker._rotation_allowed())
            compte = registry.get(self.db, "orch")["session_account"] or "primaire"
            self.assertFalse(account_turn.session_on(
                worker, accounts.Profile("codex", compte, path=self.codex[0]["path"])))
        finally:
            worker.watchdog_stop.set()

    def test_pas_de_resume_paye_au_dela_du_plafond(self):
        worker = self.worker()
        try:
            self._tour(worker)
            session = worker.current_session()
            n = len(self.turns())
            with mock.patch("ameesh.cost.CostBook.over", return_value="hourly budget: x"):
                self.assertEqual(account_turn.paid_cap(worker, "deepseek"),
                                 "hourly budget: x")
                self.assertEqual(account_turn.paid_cap(worker, "claude"), "")  # forfait
                self.assertTrue(account_turn.rotate_harness(worker, "deepseek", "codex",
                                                            session))
            self.assertEqual(len(self.turns()), n)            # aucun tour de résumé
            self.assertEqual(worker.resume_origin, "ameesh")  # brief déterministe
        finally:
            worker.watchdog_stop.set()

    def test_le_resume_de_passation_survit_a_une_panne(self):
        worker = self.worker()
        try:
            self._tour(worker)
            session = worker.current_session()
            with mock.patch("ameesh.cost.CostBook.over", return_value="hourly budget: x"):
                self.assertTrue(account_turn.rotate_harness(worker, "deepseek", "codex",
                                                            session))
            attendu = worker.resume_summary
            self.assertTrue(attendu)
        finally:
            worker.watchdog_stop.set()
        # panne : un nouveau worker, même état local
        self.db.execute("UPDATE agent_registry SET lease_owner = NULL, lease_expires_at = NULL"
                        " WHERE name = 'orch'")
        _runner, repris = self._worker("orch", "claude",
                                       {"claude": self.claude, "codex": self.codex},
                                       critical_agents=("orch",),
                                       critical_fallback_harnesses=("codex",))
        try:
            self.assertEqual(repris.resume_summary, attendu)
            self.assertEqual(repris.resume_origin, "ameesh")
            n = len(self.turns())
            self._tour(repris, "suite")
            tour = self.turns()[n]
            self.assertNotIn("--resume", tour["argv"])
            self.assertIn(attendu[:60], " ".join(tour["argv"]) + tour.get("stdin", ""))
            self.assertEqual(repris.resume_summary, "")
            self.assertFalse(os.path.exists(repris._path(repris.RESUME_PENDING)))
        finally:
            repris.watchdog_stop.set()


class Relecture389r2Test(_Repli):
    """#389 r2 : le résumé de passation tient jusqu'à une session connue, et
    une rotation dont le résumé n'a pu être gardé sur disque n'a pas lieu."""

    def _passation(self, worker):
        self._tour(worker)
        session = worker.current_session()
        with mock.patch("ameesh.cost.CostBook.over", return_value="hourly budget: x"):
            self.assertTrue(account_turn.rotate_harness(worker, "deepseek", "codex",
                                                        session))
        return session

    def test_un_depart_avorte_garde_le_resume(self):
        worker = self.worker()
        try:
            self._passation(worker)
            attendu = worker.resume_summary
            n = len(self.turns())
            self._tour(worker, "suite", AMEESH_TEST_KILL_SELF="1")  # mort avant l'init
            self.assertGreater(len(self.turns()), n)
            self.assertEqual(worker.resume_summary, attendu)
            self.assertTrue(os.path.exists(worker._path(worker.RESUME_PENDING)))
            n = len(self.turns())
            self._tour(worker, "suite")
            tour = self.turns()[n]
            self.assertIn(attendu[:60], " ".join(tour["argv"]) + tour.get("stdin", ""))
            self.assertEqual(worker.resume_summary, "")
            self.assertFalse(os.path.exists(worker._path(worker.RESUME_PENDING)))
        finally:
            worker.watchdog_stop.set()

    def _disque_plein(self, worker):
        reel = os.replace

        def replace(src, dst, *a, **kw):
            if str(dst) == worker._path(worker.RESUME_PENDING):
                raise OSError(errno.ENOSPC, "No space left on device")
            return reel(src, dst, *a, **kw)
        return mock.patch("os.replace", side_effect=replace)

    def test_resume_non_garde_la_rotation_n_a_pas_lieu(self):
        worker = self.worker()
        try:
            self._tour(worker)
            session = worker.current_session()
            with mock.patch("ameesh.cost.CostBook.over", return_value="hourly budget: x"), \
                    self._disque_plein(worker):
                self.assertFalse(account_turn.rotate_harness(worker, "deepseek", "codex",
                                                             session))
            self.assertEqual(worker.current_session(), session)
            self.assertEqual(worker.resume_summary, "")
            self.assertFalse(os.path.exists(worker._path(worker.RESUME_PENDING)))
            self.assertFalse(os.path.exists(worker._path(worker.RESUME_PENDING) + ".tmp"))
            # au sondage : rotation requise (autre harnais) mais annulée — le
            # tour ne part pas, la consigne reste
            worker._state_note("session_harness", "deepseek")
            n = len(self.turns())
            with mock.patch("ameesh.cost.CostBook.over", return_value="hourly budget: x"), \
                    self._disque_plein(worker):
                self.assertIsNone(self._tour(worker, "suite"))
            self.assertEqual(len(self.turns()), n)
            self.assertEqual(worker.current_session(), session)
        finally:
            worker.watchdog_stop.set()
