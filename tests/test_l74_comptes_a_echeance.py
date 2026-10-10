# SPDX-License-Identifier: AGPL-3.0-only
"""L74 — consommer d'abord la capacité qui expire le plus tôt (décision 0034).

Les comptes d'un fournisseur forment un réservoir : pour chaque tour, parmi
les comptes sous leur seuil de rythme, celui dont la capacité inutilisée
expire le plus tôt ; une session garde son compte tant qu'il est sous son
seuil ; un relevé échu compte pour 0 % ; le forçage prime ; tous au seuil :
pause. Mêmes jauges enregistrées que L30 (journaux de session Codex rejoués).
"""
from __future__ import annotations

import json
import os
import time
from unittest import mock

from ameesh import account_turn, accounts, cost, registry, reprise

from .test_l30_accounts import _Base, _codex_rollout, _dossier

HEURE = 3600.0
JOUR = 86400.0


def _codex_deux_fenetres(home: str, court: tuple, long: tuple) -> None:
    """Un journal Codex avec la fenêtre de 5 h (`primary`) et celle de 7 jours
    (`secondary`) : `(pourcentage utilisé, resets_at)` pour chacune."""
    dossier = os.path.join(home, "sessions", "2026", "10", "10")
    os.makedirs(dossier, exist_ok=True)
    with open(os.path.join(dossier, "rollout-test.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "session_meta", "payload": {"id": "x"}}) + "\n")
        fh.write(json.dumps({"type": "event_msg", "payload": {
            "type": "token_count", "rate_limits": {
                "plan_type": "pro",
                "primary": {"used_percent": court[0], "window_minutes": 300,
                            "resets_at": court[1]},
                "secondary": {"used_percent": long[0], "window_minutes": 10080,
                              "resets_at": long[1]}}}}) + "\n")


class ChoixParEcheanceTest(_Base):
    """`accounts.choose` : échéance, égalité, rythme, relevé échu, forçage,
    continuité, pause."""

    def _items(self, n: int = 2):
        comptes = self._codex_comptes()
        if n == 3:
            comptes.append({"name": "tertiaire", "path": _dossier(
                os.path.join(self.tmp, "comptes", "codex-3"), creds="auth.json")})
        return accounts.parse({"codex": comptes})["codex"]

    def _book(self):
        return cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)

    def _choisir(self, items, now, **kw):
        return accounts.choose(self.db, self.cfg.host, "codex", items, self._book(),
                               now=now, **kw)

    def test_le_compte_qui_expire_le_plus_tot_passe_devant(self):
        """Le cas du 2026-10-10 : primaire à 20 % de sa fenêtre de 7 jours,
        5 h du secondaire inutilisée qui se remet à zéro dans 52 min."""
        items = self._items()
        now = time.time()
        _codex_deux_fenetres(items[0].path, (10, now + 3 * HEURE), (20, now + 4 * JOUR))
        _codex_deux_fenetres(items[1].path, (0, now + 52 * 60), (5, now + 6 * JOUR))
        choix = self._choisir(items, now)
        self.assertEqual(choix.profile.name, "secondaire")
        self.assertIn("codex-300min expire dans 52 min, 0 % utilisé", choix.why)
        self.assertEqual(choix.switched["kind"], "bascule")
        self.assertEqual([(b["from_account"], b["to_account"]) for b in self._bascules()],
                         [("primaire", "secondaire")])
        # le journal des bascules porte la raison du choix
        self.assertIn("expire dans 52 min", self.db.query(
            "SELECT reason FROM account_switches")[0]["reason"])

    def test_egalite_ordre_declare(self):
        items = self._items(3)
        now = time.time()
        for profil in items:
            _codex_rollout(profil.path, 10, now + 2 * HEURE)
        self.assertEqual(self._choisir(items, now).profile.name, "primaire")

    def test_sans_releve_ordre_declare_apres_les_echeances_connues(self):
        """Un compte sans fenêtre en cours n'expire pas : il passe après ceux
        dont la capacité expire, mais reste choisi s'il est seul sous le seuil."""
        items = self._items(3)
        now = time.time()
        _codex_rollout(items[2].path, 10, now + 2 * HEURE)
        self.assertEqual(self._choisir(items, now).profile.name, "tertiaire")
        _codex_rollout(items[2].path, 95, now + 2 * HEURE)
        self.assertEqual(self._choisir(items, now).profile.name, "primaire")

    def test_le_rythme_reste_la_garde(self):
        """Le compte qui expire le plus tôt mais au-dessus de son plafond de
        rythme n'est pas pris ; le suivant par échéance l'est."""
        items = self._items(3)
        now = time.time()
        # 5 h : 1 h restante → 80 % écoulés, plafond 90 %
        _codex_rollout(items[0].path, 30, now + 4 * HEURE)
        _codex_rollout(items[1].path, 95, now + 1 * HEURE)     # au seuil
        _codex_rollout(items[2].path, 50, now + 2 * HEURE)
        choix = self._choisir(items, now)
        self.assertEqual(choix.profile.name, "tertiaire")
        self.assertIn("primaire", choix.why)                   # l'alternative est dite

    def test_releve_echu_compte_pour_zero(self):
        """Un compte resté à 93 % sur une fenêtre close n'est plus « saturé » :
        il est essayé (le premier tour rapporte sa jauge)."""
        items = self._items()
        now = time.time()
        _codex_rollout(items[0].path, 96, now + HEURE)         # primaire au seuil
        _codex_rollout(items[1].path, 93, now - 60)            # fenêtre échue
        choix = self._choisir(items, now)
        self.assertEqual(choix.profile.name, "secondaire")
        self.assertIn("relevé échu", choix.why)
        jauge = accounts.gauges_of(self._book(), items[1], items)[0]
        self.assertEqual(jauge.used_at(now), 0.0)
        self.assertAlmostEqual(jauge.used, 0.93)
        # le résumé de rotation sous ce compte est possible (pas « saturé »)
        _codex_rollout(items[1].path, 100, now - 60)
        ok, _raison = account_turn.summary_possible(items[1], self._book(), items, now=now)
        self.assertTrue(ok)
        # l'affichage : 0 %, dernier relevé lisible
        cfg = mock.Mock(host=self.cfg.host, accounts={"codex": self._codex_comptes_brut()})
        lignes = accounts.report(cfg, self.db, self._book(), now=now, record=False)
        second = [r for r in lignes if r["account"] == "secondaire"][0]
        self.assertEqual(second["gauges"][0]["used"], 0.0)
        self.assertAlmostEqual(second["gauges"][0]["last_used"], 1.0)
        self.assertIn("dernier relevé 100%", accounts.format_rows(lignes))

    def _codex_comptes_brut(self):
        return [{"name": "primaire", "path": os.path.join(self.tmp, "comptes", "codex-1")},
                {"name": "secondaire", "path": os.path.join(self.tmp, "comptes", "codex-2")}]

    def test_forcage_prime_puis_auto_rend_la_main(self):
        items = self._items()
        now = time.time()
        _codex_rollout(items[0].path, 10, now + 3 * HEURE)
        _codex_rollout(items[1].path, 0, now + 30 * 60)        # expire plus tôt
        self.assertTrue(accounts.force(self.db, self.cfg.host, "codex", items, "primaire",
                                       by="human:test"))
        choix = self._choisir(items, now, session_account="secondaire")
        self.assertEqual(choix.profile.name, "primaire")       # prime sur tout
        self.assertTrue(choix.forced)
        self.assertTrue(accounts.automatic(self.db, self.cfg.host, "codex", by="human:test"))
        choix = self._choisir(items, now)
        self.assertEqual(choix.profile.name, "secondaire")
        self.assertEqual([b["kind"] for b in self._bascules()], ["manuel", "auto", "bascule"])

    def test_continuite_pas_de_changement_pour_un_gain_marginal(self):
        items = self._items()
        now = time.time()
        _codex_rollout(items[0].path, 10, now + 3 * HEURE)
        _codex_rollout(items[1].path, 0, now + 20 * 60)
        choix = self._choisir(items, now, session_account="primaire")
        self.assertEqual(choix.profile.name, "primaire")
        self.assertTrue(choix.kept)
        self.assertIn("continuité", choix.why)
        self.assertEqual(self._bascules(), [])                 # rien ne change en base
        # le compte de la session atteint son seuil : choix par échéance
        _codex_rollout(items[0].path, 95, now + 3 * HEURE)
        choix = self._choisir(items, now, session_account="primaire")
        self.assertEqual(choix.profile.name, "secondaire")
        self.assertFalse(choix.kept)
        self.assertIn("primaire au seuil", choix.why)

    def test_tous_au_seuil_pause(self):
        items = self._items()
        now = time.time()
        _codex_rollout(items[0].path, 95, now + HEURE)
        _codex_rollout(items[1].path, 97, now + 30 * 60)
        choix = self._choisir(items, now, session_account="primaire")
        self.assertIsNone(choix.profile)
        self.assertIn("tous les comptes codex au seuil", choix.reason)

    def test_simulation_sans_ecriture(self):
        items = self._items()
        now = time.time()
        _codex_rollout(items[0].path, 10, now + 3 * HEURE)
        _codex_rollout(items[1].path, 0, now + 20 * 60)
        choix = self._choisir(items, now, simulate=True)
        self.assertEqual(choix.profile.name, "secondaire")
        self.assertEqual(choix.switched["to"], "secondaire")
        self.assertEqual(self._bascules(), [])
        self.assertEqual(self.db.query("SELECT count(*) AS n FROM quota_gauge_readings")[0]["n"],
                         0)


class RapportTest(_Base):
    """`ameesh accounts list` : capacité perdue à la prochaine remise à zéro."""

    def test_capacite_perdue_et_prochain_choix(self):
        comptes = self._codex_comptes()
        now = time.time()
        _codex_deux_fenetres(comptes[0]["path"], (10, now + 3 * HEURE), (20, now + 4 * JOUR))
        _codex_deux_fenetres(comptes[1]["path"], (0, now + 52 * 60), (5, now + 6 * JOUR))
        cfg = mock.Mock(host=self.cfg.host, accounts={"codex": comptes})
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)
        lignes = accounts.report(cfg, self.db, book, now=now, record=False)
        second = [r for r in lignes if r["account"] == "secondaire"][0]
        pertes = {p["key"]: p["lost"] for p in second["losses"]}
        self.assertAlmostEqual(pertes["codex-300min"], 1.0)
        self.assertAlmostEqual(pertes["codex-10080min"], 0.95)
        self.assertTrue(second["next"])
        self.assertFalse([r for r in lignes if r["account"] == "primaire"][0]["next"])
        texte = accounts.format_rows(lignes)
        self.assertIn("perdu à la remise à zéro si rien ne change : codex-300min 100 % "
                      "dans 52 min", texte)
        self.assertIn("prochain choix pour une nouvelle session : codex-300min expire "
                      "dans 52 min, 0 % utilisé", texte)
        # lecture seule : aucun relevé écrit (L71)
        self.assertEqual(self.db.query("SELECT count(*) AS n FROM quota_gauge_readings")[0]["n"],
                         0)

    def test_duree_lisible(self):
        self.assertEqual(accounts.duration(52 * 60), "52 min")
        self.assertEqual(accounts.duration(3 * HEURE + 600), "3 h 10")
        self.assertEqual(accounts.duration(2 * JOUR + 4 * HEURE), "2 j 4 h")


class ExecuteurTest(_Base):
    """Dans l'exécuteur : continuité de session, choix à l'ouverture, journal."""

    def test_session_en_cours_reste_nouvelle_session_va_a_l_echeance(self):
        comptes = self._codex_comptes()
        _runner, ancien = self._worker("cxa", "codex", {"codex": comptes})
        _runner2, neuf = self._worker("cxb", "codex", {"codex": comptes})
        journal: list = []
        try:
            with mock.patch.object(account_turn, "_log", journal.append):
                self._tour(ancien)                       # aucun relevé : ordre déclaré
                self.assertEqual(self.turns()[-1]["compte"]["CODEX_HOME"],
                                 comptes[0]["path"])
                self.assertEqual(registry.get(self.db, "cxa")["session_account"], "primaire")
                now = time.time()
                _codex_rollout(comptes[0]["path"], 10, now + 3 * HEURE)
                _codex_rollout(comptes[1]["path"], 0, now + 40 * 60)
                n = len(self.turns())
                # la session de cxa reste sur le primaire (sous son seuil) : pas
                # de rotation pour un gain marginal
                self._tour(ancien, "suite")
                tours = self.turns()[n:]
                self.assertEqual(len(tours), 1)
                self.assertEqual(tours[0]["compte"]["CODEX_HOME"], comptes[0]["path"])
                self.assertIn("resume", tours[0]["argv"])
                # cxb n'a pas de session : choix par échéance → secondaire
                self._tour(neuf)
                self.assertEqual(self.turns()[-1]["compte"]["CODEX_HOME"],
                                 comptes[1]["path"])
            texte = "\n".join(journal)
            self.assertIn("[cxa] compte codex : primaire — continuité", texte)
            self.assertIn("[cxb] compte codex : secondaire — codex-300min expire dans", texte)
            self.assertIn("0 % utilisé", texte)
            # cxb a maintenant sa session sur le secondaire : continuité,
            # journalisée une fois, pas à chaque sondage
            avant = len(journal)
            with mock.patch.object(account_turn, "_log", journal.append):
                account_turn.choose(neuf)
                account_turn.choose(neuf)
            self.assertEqual(len(journal), avant + 1)
            self.assertIn("[cxb] compte codex : secondaire — continuité", journal[-1])
        finally:
            ancien.watchdog_stop.set()
            neuf.watchdog_stop.set()

    def test_reprise_garde_la_session_sur_son_compte(self):
        comptes = self._codex_comptes()
        _runner, worker = self._worker("cxr", "codex", {"codex": comptes})
        try:
            self._tour(worker)
            now = time.time()
            _codex_rollout(comptes[0]["path"], 10, now + 3 * HEURE)
            _codex_rollout(comptes[1]["path"], 0, now + 40 * 60)
            cfg = worker.cfg
            plan = reprise.plan(cfg, self.db, registry.get(self.db, "cxr"))
            self.assertEqual(plan["next"], "primaire")
            self.assertTrue(plan["keep"])
        finally:
            worker.watchdog_stop.set()

