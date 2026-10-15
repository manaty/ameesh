# SPDX-License-Identifier: AGPL-3.0-only
"""#1580 point 2 — « marge de forfait disponible : N agents déplaçables ».

La proposition lit les jauges des comptes au forfait **sous le seuil des
non-critiques** (`RESERVE` gardée pour les agents critiques, L242b), estime la
part de fenêtre longue qu'un agent consomme (observée, plancher
`DEFAULT_AGENT_SHARE`), ne vide jamais une famille de relecture et n'annonce
jamais plus d'agents que la marge mesurée n'en porte. Aucune bascule n'est
faite : c'est un affichage (`ameesh accounts list`).
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import io
import json
import os
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import accounts, cost, mesh_cli, rebalance

from .test_l30_accounts import _Base, _codex_rollout

JOUR = 86400.0


def _deux_fenetres(home: str, court: tuple, long: tuple) -> None:
    """Un journal Codex avec la fenêtre de 5 h (`primary`) et celle de 7 jours
    (`secondary`) : `(pourcentage utilisé, resets_at)` pour chacune."""
    dossier = os.path.join(home, "sessions", "2026", "10", "15")
    os.makedirs(dossier, exist_ok=True)
    with open(os.path.join(dossier, "rollout-1580.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "session_meta", "payload": {"id": "x"}}) + "\n")
        fh.write(json.dumps({"type": "event_msg", "payload": {
            "type": "token_count", "rate_limits": {
                "plan_type": "pro",
                "primary": {"used_percent": court[0], "window_minutes": 300,
                            "resets_at": court[1]},
                "secondary": {"used_percent": long[0], "window_minutes": 10080,
                              "resets_at": long[1]}}}}) + "\n")


#: 3 % de la semaine, 6 jours avant la remise à zéro : le seuil non-critique
#: vaut max(écoulé + 10 − 10, 10) = 1/7 ≈ 14,3 %, la marge ≈ 11,3 points par
#: compte, l'agent Codex observé en consomme 6 (0,06), donc 3 agents portés.
MARGE_3_POURCENT = (3, 6 * JOUR)


class MargeForfaitTest(_Base):
    """La marge des forfaits, les candidats et les gardes de la proposition."""

    def setUp(self) -> None:
        super().setUp()
        self.now = time.time()

    def _cfg(self, comptes: dict):
        return dataclasses.replace(self.cfg, accounts=comptes)

    def _book(self, cfg):
        return cost.CostBook(state_dir=cfg.state_dir, db=self.db)

    def _marge(self, cfg, fleet: dict, critical=()):
        return rebalance.proposal(cfg, self.db, self._book(cfg), now=self.now,
                                  fleet=fleet, critical=critical)

    def _codex_a_3_pourcent(self) -> tuple:
        comptes = {"codex": self._codex_comptes()}
        for compte in comptes["codex"]:
            _deux_fenetres(compte["path"], (0, self.now + 300 * 60),
                           (MARGE_3_POURCENT[0], self.now + MARGE_3_POURCENT[1]))
        return self._cfg(comptes), comptes

    def _agent_sous_bail(self, name: str, harness: str) -> None:
        self.db.execute(
            "INSERT INTO agent_registry(name, harness, host, lease_owner, "
            "lease_expires_at) VALUES (%s, %s, %s, 'runner-test', "
            "now() + interval '1 hour')",
            (name, harness, self.cfg.host))

    # -- la marge et les candidats ----------------------------------------

    def test_la_marge_du_forfait_donne_des_agents_deplacables(self):
        cfg, _ = self._codex_a_3_pourcent()
        prop = self._marge(cfg, {"dev1": "deepseek", "dev2": "deepseek",
                                 "dev3": "deepseek", "cx1": "codex"})
        self.assertEqual(prop.candidates, 3)
        self.assertEqual(prop.kept_for_review, 1)      # la famille DeepSeek survit
        self.assertEqual(prop.movable, 2)              # 3 portés, 1 gardé
        self.assertEqual(prop.moves, (("codex", 2),))
        self.assertEqual(prop.offers[0].harness, "codex")
        self.assertEqual(prop.offers[0].accounts, ("primaire", "secondaire"))
        self.assertIn("forfait margin available: 2 movable agent(s) (codex 2)",
                      prop.text())
        self.assertEqual(prop.as_dict()["moves"], [{"harness": "codex", "agents": 2}])

    def test_un_agent_critique_ne_se_deplace_pas(self):
        cfg, _ = self._codex_a_3_pourcent()
        prop = self._marge(cfg, {"dev1": "deepseek", "dev2": "deepseek",
                                 "dev3": "deepseek", "cx1": "codex"},
                           critical=("dev1", "dev2"))
        self.assertEqual(prop.candidates, 1)
        self.assertEqual(prop.kept_for_review, 0)      # dev1/dev2 restent chez DeepSeek
        self.assertEqual(prop.movable, 1)

    def test_la_derniere_famille_de_relecture_est_gardee(self):
        """Déplacer le dernier agent DeepSeek ne laisserait qu'une famille : la
        relecture croisée (canon §2) exige d'en garder au moins deux."""
        cfg, _ = self._codex_a_3_pourcent()
        prop = self._marge(cfg, {"dev1": "deepseek"})
        self.assertEqual(prop.candidates, 1)
        self.assertEqual(prop.kept_for_review, 1)
        self.assertEqual(prop.movable, 0)
        self.assertIn("forfait margin: no movable agent", prop.text())

    def test_la_reserve_des_critiques_reste_hors_de_la_marge(self):
        cfg, _ = self._codex_a_3_pourcent()
        prop = self._marge(cfg, {"dev1": "deepseek", "cx1": "codex"})
        # 1/7 d'écoulé + 10 points − 10 points de réserve − 3 % utilisés
        self.assertAlmostEqual(prop.offers[0].free, 2 * (1 / 7 - 0.03), places=6)
        with mock.patch.object(accounts, "RESERVE", 0.0):
            sans = self._marge(cfg, {"dev1": "deepseek", "cx1": "codex"})
        self.assertAlmostEqual(sans.offers[0].free, 2 * (1 / 7 + 0.10 - 0.03), places=6)
        self.assertGreater(sans.offers[0].free, prop.offers[0].free)

    def test_aucun_depassement_la_marge_porte_les_agents_annonces(self):
        cfg, _ = self._codex_a_3_pourcent()
        prop = self._marge(cfg, {"dev1": "deepseek", "dev2": "deepseek",
                                 "dev3": "deepseek", "cx1": "codex"})
        for offer in prop.offers:
            self.assertLessEqual(offer.capacity * offer.unit, offer.free + 1e-9)
        self.assertLessEqual(prop.movable, sum(o.capacity for o in prop.offers))
        self.assertLessEqual(prop.movable, prop.candidates - prop.kept_for_review)

    def test_sans_fenetre_longue_mesuree_aucune_promesse(self):
        """Fenêtre courte seule et relevé absent : rien ne se promet."""
        comptes = {"codex": self._codex_comptes()}
        _codex_rollout(comptes["codex"][0]["path"], 0, self.now + 300 * 60)
        prop = self._marge(self._cfg(comptes), {"dev1": "deepseek", "cx1": "codex"})
        self.assertEqual(prop.offers[0].free, 0.0)
        self.assertEqual(prop.movable, 0)
        self.assertIn("no movable agent", prop.text())

    def test_aucun_agent_facture_au_token_rien_a_deplacer(self):
        cfg, _ = self._codex_a_3_pourcent()
        prop = self._marge(cfg, {"cx1": "codex", "cx2": "codex"})
        self.assertEqual(prop.movable, 0)
        self.assertIn("no usage-billed agent to move", prop.text())
        self.assertIn("forfait margin: no usage-billed agent to move",
                      self._marge(cfg, {}).text())

    # -- le vivier et l'affichage -----------------------------------------

    def test_seuls_les_agents_sous_bail_sont_deplacables(self):
        self._agent_sous_bail("m1580-live", "deepseek")
        self.db.execute(
            "INSERT INTO agent_registry(name, harness, host) VALUES "
            "('m1580-mort', 'deepseek', %s)", (self.cfg.host,))
        fleet = rebalance.live_fleet(self.db, self.now, self.cfg.host)
        self.assertEqual(fleet.get("m1580-live"), "deepseek")
        self.assertNotIn("m1580-mort", fleet)

    def test_accounts_list_affiche_la_proposition(self):
        cfg, _ = self._codex_a_3_pourcent()
        for name in ("m1580-a", "m1580-b"):
            self._agent_sous_bail(name, "deepseek")
        self._agent_sous_bail("m1580-cx", "codex")
        args = argparse.Namespace(accounts_command="list", json=False, last=5)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = mesh_cli.cmd_accounts(cfg, args)
        self.assertEqual(rc, 0)
        self.assertIn("forfait margin available: 1 movable agent(s) (codex 1)",
                      buf.getvalue())

    def test_le_json_des_comptes_porte_la_proposition(self):
        cfg, _ = self._codex_a_3_pourcent()
        self._agent_sous_bail("m1580-a", "deepseek")
        self._agent_sous_bail("m1580-cx", "codex")
        args = argparse.Namespace(accounts_command="list", json=True, last=5)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = mesh_cli.cmd_accounts(cfg, args)
        self.assertEqual(rc, 0)
        charge = json.loads(buf.getvalue())
        self.assertEqual(charge["proposal"]["movable"], 0)   # dernière famille gardée


class GardesMargeTest(unittest.TestCase):
    """Contre-exemples de relecture : aucun accès aux comptes ou à la base."""

    def test_un_compte_retenu_ou_un_releve_perime_ne_promet_rien(self):
        now = 1_000_000.0
        profile = accounts.Profile("codex", "test", hourly_usd=1)
        fresh = cost.Gauge("codex", "long", 0.03, now + 6 * JOUR, 7 * JOUR)
        old = dataclasses.replace(fresh)
        object.__setattr__(old, "read_at", now - 8 * JOUR)
        cases = {
            "courant": [fresh],
            "rythme court": [cost.Gauge("codex", "court", .2, now + 18000, 18000), fresh],
            "courte echue longue ouverte": [cost.Gauge("codex", "court", .2, now - 1, 18000), fresh],
            "toutes echues": [dataclasses.replace(fresh, resets_at=now - 1)],
            "age connu depasse": [old],
            "plafond horaire": [fresh],
        }
        for label, gauges in cases.items():
            with self.subTest(label=label):
                book = mock.Mock(db=object())
                book.codex_gauges.return_value = gauges
                book.spent.return_value = 2 if label == "plafond horaire" else 0
                with mock.patch.object(accounts, "check", return_value=[]):
                    offer = rebalance._offer([profile], ["cx"], now, book)
                if label == "courant":
                    self.assertEqual(offer.capacity, 2)
                    self.assertAlmostEqual(offer.free, 1 / 7 - .03)
                else:
                    self.assertEqual((offer.free, offer.capacity, offer.accounts), (0, 0, ()))

    def test_une_cle_api_ne_fournit_pas_de_marge_de_forfait(self):
        profile = accounts.Profile("codex", "api", kind="api_key_env")
        with mock.patch.object(accounts, "evaluate") as evaluate:
            offer = rebalance._offer([profile], [], 1_000_000, mock.Mock())
        evaluate.assert_not_called()
        self.assertEqual(offer.capacity, 0)

    def test_l_usage_d_un_compte_ecarte_compte_dans_l_unite(self):
        """#1789/N5 : un compte que la garde écarte (fenêtre courte au seuil)
        garde ses agents ; son usage de la fenêtre longue entre donc dans
        l'unité observée — sinon la capacité annoncée est trop grande."""
        now = 1_000_000.0
        sature = accounts.Profile("codex", "sature", path="/tmp/sature", hourly_usd=1)
        libre = accounts.Profile("codex", "libre", path="/tmp/libre", hourly_usd=1)
        gauges = {
            "sature": [cost.Gauge("codex", "court", 1.0, now + 18000, 18000),
                       cost.Gauge("codex", "long", 0.95, now + 6 * JOUR, 7 * JOUR)],
            "libre": [cost.Gauge("codex", "long", 0.03, now + 6 * JOUR, 7 * JOUR)],
        }
        book = mock.Mock(db=object())
        book.spent.return_value = 0
        book.codex_gauges.side_effect = lambda **kw: gauges[
            next(name for name in gauges if name in kw["sessions"])]
        with mock.patch.object(accounts, "check", return_value=[]):
            offer = rebalance._offer([sature, libre], ["a", "b"], now, book)
        # 0,95 + 0,03 sur deux agents : l'unité vaut 0,49 (et non le plancher
        # de 5 %, qui annoncerait 2 agents portés au lieu d'aucun)
        self.assertAlmostEqual(offer.unit, 0.49)
        self.assertEqual(offer.accounts, ("libre",))
        self.assertAlmostEqual(offer.free, 1 / 7 - 0.03)
        self.assertEqual(offer.capacity, int((1 / 7 - 0.03) / 0.49))

    def test_les_critiques_de_roles_et_declaration_restant_dans_leur_famille(self):
        from ameesh import auditor, registry, sous_utilisation
        cfg = SimpleNamespace(accounts={}, critical_agents=("config-critical",), host="test")
        fleet = {name: "deepseek" for name in
                 ("auditeur", "designer", "pilot", "config-critical", "explicit", "dev")}
        with (mock.patch.object(cost, "paid_harnesses_of", return_value=("deepseek",)),
              mock.patch.object(auditor, "load_all", return_value=[]),
              mock.patch.object(sous_utilisation, "declared_orchestrators", return_value={"pilot"}),
              mock.patch.object(sous_utilisation, "role_orchestrators", return_value={"designer"}),
              mock.patch.object(auditor, "owned_card", return_value=object()),
              mock.patch.object(registry, "get", return_value={"name": "designer"})):
            prop = rebalance.proposal(cfg, object(), object(), now=1_000_000,
                                      fleet=fleet, critical=("explicit",))
        self.assertEqual((prop.candidates, prop.kept_for_review), (1, 0))

    def test_un_bail_ne_rend_pas_un_agent_arrete_ou_externe_deplacable(self):
        from ameesh import registry
        rows = [dict(name=name, harness="deepseek", lease_owner="r", lease_expires_ts=200,
                     status=status, mode=mode) for name, status, mode in
                (("live", "idle", "execute"), ("stopped", "stopped", "execute"),
                 ("external", "idle", "externe"))]
        with mock.patch.object(registry, "overview", return_value=rows):
            self.assertEqual(rebalance.live_fleet(object(), 100), {"live": "deepseek"})
