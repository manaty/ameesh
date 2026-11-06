# SPDX-License-Identifier: AGPL-3.0-only
"""#6046 (manaty/ameesh#4646) : réglages de contexte par rôle, en plus de l'agent.

`turn_reread_budget` et `compact_tokens` se réglaient par agent (0531, 0553)
ou par hôte (`AMEESH_TURN_REREAD_BUDGET`), jamais par rôle. Le canon les pose
désormais sous `session.by_role.<classe>` ; ordre de lecture agent → rôle →
hôte → défaut du harnais, la valeur la plus stricte pour un agent à plusieurs
rôles, et la source dite par `ameesh show`, `list --json` et
`turn.context.policy.sources`. Un canon sans bloc garde la lecture d'avant.
"""
from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import context_profile, cost, exploitation, mechanics, mesh_cli, registry
from ameesh.runner import _SESSION_UNREAD, AgentWorker

from .test_4448_rotation_par_harnais import document
from .test_l105_plafonds_du_tour import _Base

SESSION = "session:\n  max_tokens: 150000\n"
BY_ROLE = SESSION + (
    "  by_role:\n"
    "    review: {turn_reread_budget: 1500000, compact_tokens: 100000}\n"
    "    build: {turn_reread_budget: 1000000, compact_tokens: 120000}\n"
    "    other: {compact_tokens: 110000}\n")
HOTE = SimpleNamespace(turn_reread_budget=777.0)


def _bloc(text: str = BY_ROLE) -> mechanics.Session:
    return document(text).session


class LecteurTest(unittest.TestCase):
    """Le lecteur strict de `session.by_role`."""

    def test_bloc_lu(self):
        bloc = _bloc()
        self.assertEqual(dict(bloc.by_role)["review"],
                         (("turn_reread_budget", 1500000), ("compact_tokens", 100000)))
        self.assertEqual(bloc.to_dict()["by_role"]["other"], {"compact_tokens": 110000})

    def test_sans_by_role_lecture_d_avant(self):
        bloc = _bloc(SESSION)
        self.assertEqual(bloc.by_role, ())
        self.assertNotIn("by_role", bloc.to_dict())
        self.assertIsNone(mechanics.role_setting(bloc, ["relecture"], "compact_tokens"))
        self.assertIsNone(mechanics.role_setting(None, ["relecture"], "compact_tokens"))

    def test_refus(self):
        for fautif in (
                "    relecture: {compact_tokens: 100000}\n",   # nom de fiche, pas une classe
                "    revue: {compact_tokens: 100000}\n",       # faute de frappe
                "    review: {max_tokens: 100000}\n",          # clé inconnue
                "    review: {}\n",                            # classe vide
                "    review: {turn_reread_budget: 0}\n",       # 0 n'éteint rien ici
                "    review: {turn_reread_budget: 1.5}\n",
                "    review: {turn_reread_budget: true}\n",
                "    review: {compact_tokens: 9999}\n",        # sous le plancher
        ):
            with self.subTest(fautif=fautif.strip()):
                with self.assertRaises(mechanics.MechanicsError):
                    document(SESSION + "  by_role:\n" + fautif)
        with self.assertRaises(mechanics.MechanicsError):
            document(SESSION + "  by_role: [review]\n")

    def test_plancher_commun_avec_ameesh_set(self):
        self.assertEqual(mechanics.COMPACT_TOKENS_MIN, mesh_cli.COMPACT_TOKENS_MIN)


class ResolutionTest(unittest.TestCase):
    """Ordre agent → rôle → hôte → défaut, valeur la plus stricte."""

    def test_classes_de_tous_les_roles(self):
        classes = context_profile.role_classes
        self.assertEqual(classes(["implementation", "relecture"]), ("build", "review"))
        self.assertEqual(classes(["relecture", "qa"]), ("review",))
        self.assertEqual(classes(["orchestrateur", "conception"]), ("pilot", "other"))
        self.assertEqual(classes("review"), ("review",))
        self.assertEqual(classes([]), ("other",))
        self.assertEqual(classes(None), ("other",))

    def test_budget_de_relecture(self):
        bloc, lire = _bloc(), exploitation.turn_reread_budget_source
        # l'agent passe devant, 0 compris (éteint pour lui)
        self.assertEqual(lire(HOTE, {"turn_reread_budget": 0, "roles": ["relecture"]}, bloc),
                         (0, "agent"))
        self.assertEqual(lire(HOTE, {"turn_reread_budget": 2000000, "roles": ["qa"]}, bloc),
                         (2000000, "agent"))
        self.assertEqual(lire(HOTE, {"roles": ["relecture"]}, bloc),
                         (1500000, "role:review"))
        # plusieurs rôles : la plus petite valeur de leurs classes
        self.assertEqual(lire(HOTE, {"roles": ["relecture", "implementation"]}, bloc),
                         (1000000, "role:build"))
        # classe sans valeur (pilot), rôle hors bloc, ou aucun bloc : l'hôte
        self.assertEqual(lire(HOTE, {"roles": ["orchestrateur"]}, bloc), (777, "host"))
        self.assertEqual(lire(HOTE, {"roles": ["relecture"]}, None), (777, "host"))
        self.assertEqual(exploitation.effective_turn_reread_budget(
            HOTE, {"roles": ["relecture"]}), 777)

    def test_seuil_de_compaction(self):
        bloc, lire = _bloc(), exploitation.compact_tokens_source
        self.assertEqual(lire({"compact_tokens": 80000, "roles": ["relecture"]}, bloc),
                         (80000, "agent"))
        self.assertEqual(lire({"roles": ["implementation"]}, bloc), (120000, "role:build"))
        self.assertEqual(lire({"roles": ["implementation", "relecture"]}, bloc),
                         (100000, "role:review"))
        # aucune classe nommée : `other` (110000) ; sans bloc : défaut du harnais
        self.assertEqual(lire({"roles": None}, bloc), (110000, "role:other"))
        self.assertEqual(lire({"roles": ["relecture"]}, None), (None, "default"))
        self.assertEqual(lire({"roles": ["relecture"]}, _bloc(SESSION)), (None, "default"))
        # l'appel d'avant #6046 (sans bloc) garde son sens
        self.assertIsNone(exploitation.effective_compact_tokens({"roles": ["relecture"]}))

    def test_egalite_premiere_classe(self):
        bloc = _bloc(SESSION + "  by_role:\n"
                     "    review: {compact_tokens: 100000}\n"
                     "    build: {compact_tokens: 100000}\n")
        self.assertEqual(bloc.role_value(("build", "review"), "compact_tokens"),
                         (100000, "build"))


class LectureParTourTest(unittest.TestCase):
    """Le budget se relit à chaque usage du flux : le canon, une fois par tour."""

    def test_un_bloc_par_tour(self):
        worker = SimpleNamespace(cfg=HOTE, agent={"roles": ["relecture"]})
        with mock.patch.object(mechanics, "session_block", return_value=_bloc()) as lu:
            for _ in range(5):
                self.assertEqual(AgentWorker.turn_reread_budget(worker), 1500000)
            self.assertEqual(AgentWorker.compact_tokens(worker), 100000)
            self.assertEqual(AgentWorker.context_setting_sources(worker),
                             {"turn_reread_budget": "role:review",
                              "compact_tokens": "role:review"})
            self.assertEqual(lu.call_count, 1)
            worker._canon_session = _SESSION_UNREAD  # ce que fait `_run_turn`
            AgentWorker.turn_reread_budget(worker)
            self.assertEqual(lu.call_count, 2)

    def test_canon_illisible_lecture_d_avant(self):
        worker = SimpleNamespace(cfg=HOTE, agent={"roles": ["relecture"]})
        with mock.patch.object(mechanics, "configured",
                               side_effect=mechanics.MechanicsError("no readable canon")):
            self.assertEqual(AgentWorker.turn_reread_budget(worker), 777)
            self.assertIsNone(AgentWorker.compact_tokens(worker))


class TourTest(_Base):
    """Un vrai tour du banc : seuil du rôle passé au harnais, source publiée."""

    def test_vue_d_exploitation(self):
        _runner, worker = self._worker("cdx-role-vue")
        ligne = registry.get(self.db, "cdx-role-vue")
        self.assertEqual(ligne["roles"], ["implementation"])  # rôle posé par le banc
        vue = exploitation.agent_view(worker.cfg, ligne, session=_bloc())
        self.assertEqual((vue["compact_tokens"], vue["compact_tokens_source"]),
                         (120000, "role:build"))
        self.assertEqual((vue["turn_reread_budget"], vue["turn_reread_budget_source"]),
                         (1000000, "role:build"))
        vue = exploitation.agent_view(worker.cfg, registry.get(self.db, "cdx-role-vue"))
        self.assertEqual((vue["compact_tokens"], vue["compact_tokens_source"]),
                         (None, "default"))

    def test_tour_codex_recoit_le_seuil_du_role(self):
        from ameesh.runner import Runner
        runner = Runner(self.cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", "cdx-role")
        os.makedirs(cwd, exist_ok=True)
        self.register("cdx-role", "codex", cwd=cwd)
        lease = registry.claim(self.db, "cdx-role", runner.runner_id, 3600)
        worker = AgentWorker(runner, registry.get(self.db, "cdx-role"), lease)
        self.addCleanup(worker.watchdog_stop.set)
        with self._env(), mock.patch.object(mechanics, "session_block",
                                            return_value=_bloc()):
            worker.run_turn({"kind": "prompt", "prompt": "x", "ids": []})
        argv = self.turns()[-1]["argv"]
        # rôle `implementation` du banc : classe `build`
        self.assertIn("model_auto_compact_token_limit=120000", argv)
        rows = self.db.query(
            "select data from ameesh_events where type = %s"
            " order by id desc limit 1", (cost.CONTEXT_EVENT,))
        self.assertTrue(rows, "aucun événement turn.context publié")
        data = rows[0]["data"]
        data = json.loads(data) if isinstance(data, str) else data
        self.assertEqual(data["policy"]["compact_tokens"], 120000)
        self.assertEqual(data["policy"]["turn_reread_budget"], 1000000)
        self.assertEqual(data["policy"]["sources"], {"turn_reread_budget": "role:build",
                                                     "compact_tokens": "role:build"})

    def test_tour_sans_bloc_ne_publie_pas_de_source(self):
        from ameesh.runner import Runner
        runner = Runner(self.cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", "cdx-sans")
        os.makedirs(cwd, exist_ok=True)
        self.register("cdx-sans", "codex", cwd=cwd)
        lease = registry.claim(self.db, "cdx-sans", runner.runner_id, 3600)
        worker = AgentWorker(runner, registry.get(self.db, "cdx-sans"), lease)
        self.addCleanup(worker.watchdog_stop.set)
        with self._env(), mock.patch.object(mechanics, "session_block",
                                            return_value=_bloc(SESSION)):
            worker.run_turn({"kind": "prompt", "prompt": "x", "ids": []})
        self.assertFalse(any(a.startswith("model_auto_compact_token_limit")
                             for a in self.turns()[-1]["argv"]))
        rows = self.db.query(
            "select data from ameesh_events where type = %s"
            " order by id desc limit 1", (cost.CONTEXT_EVENT,))
        data = rows[0]["data"]
        data = json.loads(data) if isinstance(data, str) else data
        self.assertNotIn("sources", data["policy"])
        self.assertNotIn("compact_tokens", data["policy"])


if __name__ == "__main__":
    unittest.main()
