# SPDX-License-Identifier: AGPL-3.0-only
"""#1580 (tranche 3) — les jauges du widget omarchy et la portée par modèle.

Constat du propriétaire (2026-10-15 09:54) : le seau « Fable Weekly » de
claude2 est resté à 0 % quand son hebdomadaire général était à 93 %, et le
choix automatique — qui ne lit que `rate_limit_event.unifiedWindows` — ne
voyait pas ce seau : toute la semaine Fable était perdue à la remise à zéro.

Témoins : le lecteur du fichier du widget (inerte là où il n'existe pas,
périmé au-delà de sa plus courte fenêtre), la portée d'un seau de modèle (il
ne compte que pour une session de ce modèle) et le score (`rank`) qui
consomme d'abord le budget perdu — y compris un seau de modèle.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from ameesh import account_turn, accounts, cost, widget_usage
from ameesh.cost import Gauge, Gauges, Overage

from .test_l30_accounts import _Base, _dossier

JOUR = 86400.0
HEURE = 3600.0
DIR_ENV = widget_usage.DIR_ENV


def _iso(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def _profil(dossier: str, nom: str = "primaire") -> accounts.Profile:
    return accounts.Profile("claude", nom, path=dossier)


class WidgetUsageTest(unittest.TestCase):
    """Le lecteur du fichier du widget : forme, inertie, fraîcheur."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-widget-")
        self.dossier = os.path.join(self.tmp, "usage")
        os.makedirs(self.dossier, exist_ok=True)
        self.compte = os.path.join(self.tmp, "claude-secondary")
        os.makedirs(self.compte, exist_ok=True)
        self.now = time.time()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ecrit(self, nom: str, contenu, mtime: float | None = None) -> str:
        chemin = os.path.join(self.dossier, nom)
        with open(chemin, "w", encoding="utf-8") as fh:
            if isinstance(contenu, str):
                fh.write(contenu)
            else:
                json.dump(contenu, fh)
        if mtime is not None:
            os.utime(chemin, (mtime, mtime))
        return chemin

    def _instantane(self, **extra) -> dict:
        payload = {
            "schemaVersion": 1, "id": "x", "name": "Claude Code", "ready": True,
            "updatedAt": _iso(self.now - 60),
            "limits": [
                {"label": "Session (5-hour)", "percent": 0.27,
                 "resetsAt": _iso(self.now + 4 * HEURE)},
                {"label": "Weekly (7-day)", "percent": 0.13,
                 "resetsAt": _iso(self.now + 6 * JOUR)},
                {"label": "Fable Weekly", "title": "Fable Weekly", "percent": 0.0,
                 "resetsAt": _iso(self.now + 6 * JOUR)},
            ],
        }
        payload.update(extra)
        return payload

    def test_seaux_lus_et_portee_de_modele(self):
        self._ecrit("claude-secondary.json", self._instantane())
        gauges = widget_usage.read(_profil(self.compte, "secondaire"),
                                   now=self.now, directory=self.dossier)
        self.assertIsNotNone(gauges)
        self.assertEqual([g.key for g in gauges],
                         ["five_hour", "seven_day", "seven_day@fable"])
        self.assertEqual([g.window_s for g in gauges],
                         [5 * HEURE, 7 * JOUR, 7 * JOUR])
        self.assertAlmostEqual(gauges[0].used, 0.27)
        self.assertAlmostEqual(gauges[1].used, 0.13)
        self.assertEqual([g.scope for g in gauges], ["", "", "fable"])
        # resetsAt ISO → epoch, à la seconde près
        self.assertAlmostEqual(gauges[0].resets_at, self.now + 4 * HEURE, places=2)

    def test_iso_avec_et_sans_microsecondes(self):
        avant = widget_usage._iso_epoch("2026-10-21T12:00:00+00:00")
        apres = widget_usage._iso_epoch("2026-10-21T12:00:00.245717+00:00")
        self.assertAlmostEqual(apres - avant, 0.245717, places=5)
        self.assertAlmostEqual(
            widget_usage._iso_epoch("2026-10-21T12:00:00Z"), avant, places=6)
        # les deux formes se lisent sur un instantané frais
        self._ecrit("claude-secondary.json", self._instantane(limits=[
            {"label": "Weekly (7-day)", "percent": 1.0,
             "resetsAt": "2026-10-21T12:00:00+00:00"},
            {"label": "Session (5-hour)", "percent": 0.5,
             "resetsAt": "2026-10-15T15:30:00.245717+00:00"},
        ]))
        gauges = widget_usage.read(_profil(self.compte, "secondaire"),
                                   now=self.now, directory=self.dossier)
        self.assertEqual([g.key for g in gauges], ["seven_day", "five_hour"])
        self.assertAlmostEqual(gauges[0].resets_at, avant, places=5)
        court = widget_usage._iso_epoch("2026-10-15T15:30:00.245717+00:00")
        self.assertAlmostEqual(gauges[1].resets_at, court, places=5)
        self.assertAlmostEqual(gauges[1].used, 0.5)

    def test_percent_borne_et_echelle(self):
        self._ecrit("claude-secondary.json", self._instantane(limits=[
            {"label": "Session (5-hour)", "percent": 42,
             "resetsAt": _iso(self.now + HEURE)},
            {"label": "Weekly (7-day)", "percent": 150,
             "resetsAt": _iso(self.now + 6 * JOUR)},
        ]))
        gauges = widget_usage.read(_profil(self.compte, "secondaire"),
                                   now=self.now, directory=self.dossier)
        # au-delà de 1 : à sec, jamais lu comme un pourcentage (1.05 ≠ 1 %)
        self.assertAlmostEqual(gauges[0].used, 1.0)
        self.assertAlmostEqual(gauges[1].used, 1.0)

    def test_inertie_source_absente_ou_illisible(self):
        profil = _profil(self.compte, "secondaire")
        # dossier absent
        self.assertIsNone(widget_usage.read(profil, now=self.now,
                                            directory=os.path.join(self.tmp, "rien")))
        # JSON illisible
        self._ecrit("claude-secondary.json", "{pas du json")
        self.assertIsNone(widget_usage.read(profil, now=self.now, directory=self.dossier))
        # schéma inconnu
        self._ecrit("claude-secondary.json", self._instantane(schemaVersion=2))
        self.assertIsNone(widget_usage.read(profil, now=self.now, directory=self.dossier))
        # pas prêt
        self._ecrit("claude-secondary.json", self._instantane(ready=False))
        self.assertIsNone(widget_usage.read(profil, now=self.now, directory=self.dossier))
        # aucun seau lisible
        self._ecrit("claude-secondary.json", self._instantane(limits=[
            {"label": "Something else", "percent": 0.5}]))
        self.assertIsNone(widget_usage.read(profil, now=self.now, directory=self.dossier))
        # le compte par défaut lit `claude.json`
        self._ecrit("claude.json", self._instantane())
        defaut = os.path.join(self.tmp, "claude")
        os.makedirs(defaut, exist_ok=True)
        self.assertIsNotNone(widget_usage.read(_profil(defaut, "primaire"),
                                               now=self.now, directory=self.dossier))

    def test_fraicheur_plus_courte_fenetre(self):
        profil = _profil(self.compte, "secondaire")
        # 6 h de retard, avec une fenêtre de 5 h : périmé (#1561)
        self._ecrit("claude-secondary.json", self._instantane(
            updatedAt=_iso(self.now - 6 * HEURE)))
        self.assertIsNone(widget_usage.read(profil, now=self.now, directory=self.dossier))
        # 4 h de retard : encore dans la fenêtre courte
        self._ecrit("claude-secondary.json", self._instantane(
            updatedAt=_iso(self.now - 4 * HEURE)))
        self.assertIsNotNone(widget_usage.read(profil, now=self.now,
                                               directory=self.dossier))
        # un fichier sans fenêtre courte vit à l'échelle de sa fenêtre longue
        self._ecrit("claude-secondary.json", self._instantane(
            updatedAt=_iso(self.now - 6 * HEURE),
            limits=[{"label": "Weekly (7-day)", "percent": 0.5,
                     "resetsAt": _iso(self.now + 6 * JOUR)}]))
        self.assertIsNotNone(widget_usage.read(profil, now=self.now,
                                               directory=self.dossier))

    def test_updated_at_absent_repli_sur_mtime(self):
        profil = _profil(self.compte, "secondaire")
        payload = self._instantane()
        payload.pop("updatedAt")
        self._ecrit("claude-secondary.json", payload, mtime=self.now - 60)
        self.assertIsNotNone(widget_usage.read(profil, now=self.now,
                                               directory=self.dossier))
        self._ecrit("claude-secondary.json", payload, mtime=self.now - 6 * HEURE)
        self.assertIsNone(widget_usage.read(profil, now=self.now, directory=self.dossier))

    def test_dossier_par_variable_d_environnement(self):
        with mock.patch.dict(os.environ, {DIR_ENV: self.dossier}):
            self.assertEqual(widget_usage.usage_dir(), self.dossier)
            self._ecrit("claude-secondary.json", self._instantane())
            self.assertIsNotNone(widget_usage.read(_profil(self.compte, "secondaire"),
                                                   now=self.now))
        self.assertEqual(widget_usage.usage_dir({"X": "1"}),
                         os.path.abspath(os.path.expanduser(widget_usage.DEFAULT_DIR)))


class PorteeParModeleTest(unittest.TestCase):
    """Le filtrage des seaux de modèle et le score qui en découle."""

    def setUp(self) -> None:
        self.now = time.time()

    def _jauges(self) -> Gauges:
        return Gauges([
            Gauge("claude", "five_hour", 0.1, self.now + 10 * HEURE, 5 * HEURE),
            Gauge("claude", "seven_day", 0.9, self.now + 20 * HEURE, 7 * JOUR),
            Gauge("claude", "seven_day@fable", 0.0, self.now + 2 * HEURE, 7 * JOUR,
                  scope="fable"),
        ], Overage(True, False))

    def test_filtrage_par_modele(self):
        jauges = self._jauges()
        fable = accounts.model_scoped(jauges, "claude-fable-5")
        self.assertEqual([g.key for g in fable],
                         ["five_hour", "seven_day", "seven_day@fable"])
        autre = accounts.model_scoped(jauges, "claude-opus-5-5")
        self.assertEqual([g.key for g in autre], ["five_hour", "seven_day"])
        inconnu = accounts.model_scoped(jauges, "")
        self.assertEqual([g.key for g in inconnu], ["five_hour", "seven_day"])
        # pas de session (affichage) : tous les seaux, y compris de modèle
        self.assertIs(accounts.model_scoped(jauges, None), jauges)
        # la copie garde le type et les drapeaux d'overage du relevé
        self.assertIsInstance(autre, Gauges)
        self.assertTrue(autre.overage.capable)
        # aucune jauge de modèle : la liste est rendue telle quelle
        sans = Gauges([Gauge("claude", "five_hour", 0.1, self.now + HEURE, 5 * HEURE)])
        self.assertIs(accounts.model_scoped(sans, "fable"), sans)

    def test_budget_perdu_du_seau_de_modele(self):
        jauges = self._jauges()
        # pour un agent Fable : le seau Fable se remet à zéro le plus tôt, sa
        # capacité inutilisée est le budget perdu
        self.assertAlmostEqual(
            accounts.lost_at_reset(accounts.model_scoped(jauges, "fable"), self.now),
            1.0)
        # pour un autre modèle : le seau Fable ne compte pas, la fenêtre 5 h
        # est la plus proche (10 h) et sa part inutilisée vaut 0.9
        self.assertAlmostEqual(
            accounts.lost_at_reset(accounts.model_scoped(jauges, "opus"), self.now),
            0.9)

    def test_rank_prefere_le_seau_de_modele_pour_son_modele(self):
        # priorités déclarées : le secondaire passe avant le primaire à
        # égalité de budget (L226b « primaries last »)
        primaire = accounts.Profile("claude", "primaire", priority=100)
        secondaire = accounts.Profile("claude", "secondaire", priority=20)
        # A : retard identique à B ; son seau Fable à 0 % se remet à zéro dans
        # 2 h, sa fenêtre 5 h dans 5 h
        ga = Gauges([
            Gauge("claude", "five_hour", 0.05, self.now + 5 * HEURE, 5 * HEURE),
            Gauge("claude", "seven_day", 0.05, self.now + 7 * JOUR, 7 * JOUR),
            Gauge("claude", "seven_day@fable", 0.0, self.now + 2 * HEURE, 7 * JOUR,
                  scope="fable"),
        ])
        # B : même retard, même échéance, aucun seau de modèle
        gb = Gauges([
            Gauge("claude", "five_hour", 0.05, self.now + 5 * HEURE, 5 * HEURE),
            Gauge("claude", "seven_day", 0.05, self.now + 7 * JOUR, 7 * JOUR),
        ])
        ev = lambda p, g: accounts.Evaluation(p, gauges=g)  # noqa: E731
        # agent Fable : A d'abord (seau Fable perdu dans 2 h, aucune perte chez B)
        fable = {"primaire": ev(primaire, accounts.model_scoped(ga, "fable")),
                 "secondaire": ev(secondaire, gb)}
        self.assertEqual([p.name for p in accounts.rank([primaire, secondaire], fable,
                                                         self.now)],
                         ["primaire", "secondaire"])
        # autre modèle : le seau Fable est écarté, A et B à égalité de budget,
        # l'ordre déclaré départage (secondaire 20 avant primaire 100)
        autre = {"primaire": ev(primaire, accounts.model_scoped(ga, "opus")),
                 "secondaire": ev(secondaire, gb)}
        self.assertEqual([p.name for p in accounts.rank([primaire, secondaire], autre,
                                                         self.now)],
                         ["secondaire", "primaire"])


class GaugesOfTest(unittest.TestCase):
    """`gauges_of` : widget frais d'abord, journaux sinon, overage conservé."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-widget-")
        self.dossier = os.path.join(self.tmp, "usage")
        os.makedirs(self.dossier, exist_ok=True)
        self.compte = os.path.join(self.tmp, "claude")
        os.makedirs(self.compte, exist_ok=True)
        self.now = time.time()
        self.journal = Gauges(
            [Gauge("claude", "five_hour", 0.8, self.now + HEURE, 5 * HEURE)],
            Overage(True, False))
        self.book = SimpleNamespace(claude_gauges=lambda **kw: self.journal)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ecrit(self, payload) -> None:
        with open(os.path.join(self.dossier, "claude.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(payload, fh)

    def test_widget_frais_prioritaire_et_overage_du_journal(self):
        self._ecrit({"schemaVersion": 1, "ready": True,
                     "updatedAt": _iso(self.now - 60),
                     "limits": [
                         {"label": "Session (5-hour)", "percent": 0.2,
                          "resetsAt": _iso(self.now + 4 * HEURE)},
                         {"label": "Weekly (7-day)", "percent": 0.3,
                          "resetsAt": _iso(self.now + 6 * JOUR)},
                         {"label": "Fable Weekly", "title": "Fable Weekly",
                          "percent": 0.0, "resetsAt": _iso(self.now + 6 * JOUR)}]})
        profil = _profil(self.compte, "primaire")
        with mock.patch.dict(os.environ, {DIR_ENV: self.dossier}):
            vus = accounts.gauges_of(self.book, profil, [profil],
                                     model="claude-fable-5", now=self.now)
        self.assertEqual([g.key for g in vus],
                         ["five_hour", "seven_day", "seven_day@fable"])
        self.assertTrue(vus.overage.capable)     # repris du journal

    def test_repli_sur_le_journal_si_source_absente_ou_perimee(self):
        profil = _profil(self.compte, "primaire")
        with mock.patch.dict(os.environ, {DIR_ENV: self.dossier}):
            vus = accounts.gauges_of(self.book, profil, [profil], now=self.now)
            self.assertEqual([g.key for g in vus], ["five_hour"])
        self._ecrit({"schemaVersion": 1, "ready": True,
                     "updatedAt": _iso(self.now - 6 * HEURE),
                     "limits": [{"label": "Session (5-hour)", "percent": 0.2,
                                 "resetsAt": _iso(self.now + 4 * HEURE)}]})
        with mock.patch.dict(os.environ, {DIR_ENV: self.dossier}):
            vus = accounts.gauges_of(self.book, profil, [profil], now=self.now)
            self.assertEqual([g.key for g in vus], ["five_hour"])
            self.assertAlmostEqual(vus[0].used, 0.8)    # le journal, pas le widget


class ChooseAvecWidgetTest(_Base):
    """De bout en bout : le choix porte le modèle de l'agent (tranche 3).

    Le seau Fable du primaire est à sec et se remet à zéro bientôt : un agent
    Fable doit l'éviter (le seau le retient), un agent d'un autre modèle doit
    pouvoir l'ignorer. Les deux comptes sont par ailleurs identiques, le
    primaire passant devant par son ordre déclaré.
    """

    def _comptes(self):
        a = _dossier(os.path.join(self.tmp, "comptes", "claude-fable"),
                     creds=".credentials.json")
        b = _dossier(os.path.join(self.tmp, "comptes", "claude-autre"),
                     creds=".credentials.json")
        return [{"name": "primaire", "path": a, "priority": 10},
                {"name": "secondaire", "path": b}]

    def _widget(self, nom: str, fable: float | None) -> None:
        self.dossier = os.path.join(self.tmp, "usage")
        os.makedirs(self.dossier, exist_ok=True)
        limites = [
            {"label": "Session (5-hour)", "percent": 0.05,
             "resetsAt": _iso(self.now + 5 * HEURE)},
            {"label": "Weekly (7-day)", "percent": 0.05,
             "resetsAt": _iso(self.now + 7 * JOUR)},
        ]
        if fable is not None:
            limites.append({"label": "Fable Weekly", "title": "Fable Weekly",
                            "percent": fable,
                            "resetsAt": _iso(self.now + 2 * HEURE)})
        with open(os.path.join(self.dossier, nom + ".json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"schemaVersion": 1, "ready": True,
                       "updatedAt": _iso(self.now - 60), "limits": limites}, fh)

    def _choix(self, modele: str, registre: bool = False) -> str:
        self.now = time.time()
        comptes = self._comptes()
        self._widget("claude-fable", 1.0)
        self._widget("claude-autre", None)
        runner, worker = self._worker("wf", "claude", {"claude": comptes})
        if registre:
            # `ameesh set <agent> model=…` : le modèle est au registre, sans
            # fichier d'état — c'est lui que le runner passe au tour
            worker.agent = dict(worker.agent or {}, model=modele)
        else:
            os.makedirs(os.path.join(self.cfg.state_dir, worker.name), exist_ok=True)
            with open(os.path.join(self.cfg.state_dir, worker.name, "model"), "w",
                      encoding="utf-8") as fh:
                fh.write(modele + "\n")
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db,
                             tools={worker.name: "claude"})
        with mock.patch.dict(os.environ, {DIR_ENV: self.dossier}):
            raison = account_turn.choose(worker, book)
        self.assertEqual(raison, "")
        return worker._account.name

    def test_agent_fable_evite_le_seau_a_sec(self):
        self.assertEqual(self._choix("claude-fable-5"), "secondaire")

    def test_autre_modele_ignore_le_seau_de_modele(self):
        self.assertEqual(self._choix("claude-opus-5-5"), "primaire")

    def test_modele_du_registre(self):
        self.assertEqual(self._choix("claude-fable-5", registre=True), "secondaire")
        self.assertEqual(self._choix("claude-opus-5-5", registre=True), "primaire")


if __name__ == "__main__":
    unittest.main()
