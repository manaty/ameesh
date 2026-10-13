# SPDX-License-Identifier: AGPL-3.0-only
"""#804 : la prise au repos est déclenchée par l'exécuteur de l'agent, pour ses
propres agents (canon « Processus de lot » §4) — mêmes garde-fous que L119 et
L165, la distribution ne dépend plus d'un `ameesh notify` central."""
from __future__ import annotations

import threading
import unittest

from ameesh import backlog, runner, work

from .test_l119_backlog import HEURE, _Base


class _Lots(_Base):
    def lot(self, title, *, app="nexlink", age_s=HEURE, **kw):
        item = work.add(self.db, title=title, app=app, **kw)
        # created_at croissant avec l'ordre de création (l'ancienneté départage)
        self.db.execute("UPDATE work_items SET created_at = created_at - make_interval("
                        "secs => %s) WHERE id = %s", (age_s, item["id"]))
        return item

    def settings(self, **kw):
        out = {"idle_s": backlog.DEFAULT_TAKE_IDLE_S,
               "max_per_hour": backlog.DEFAULT_TAKE_MAX_PER_HOUR,
               "lot_idle_s": backlog.DEFAULT_LOT_IDLE_S,
               "lot_max_per_hour": backlog.DEFAULT_LOT_MAX_PER_HOUR}
        out.update(kw)
        return out

    def take(self, agents, **kw):
        kw.setdefault("orchestras", ["orchestrateur"])
        kw.setdefault("now", self.now)
        kw.setdefault("paid", ("deepseek",))
        kw.setdefault("budget_check", lambda agent: "")
        return runner.local_take(self.db, self.cfg, agents, self.settings(), **kw)


class PriseDeLExecuteurTest(_Lots):
    def test_seuls_les_agents_de_l_executeur_prennent(self):
        self.agent("a1", team="nexlink", since_s=2 * HEURE)
        self.agent("a2", team="nexlink", since_s=2 * HEURE)
        un = self.lot("un")
        deux = self.lot("deux")
        out = self.take({"a1"})
        self.assertEqual([(t["agent"], t["item"]["id"], t["kind"]) for t in out],
                         [("a1", un["id"], "lot")])
        self.assertEqual(work.get(self.db, un["id"])["assignee"], "a1")
        self.assertIsNone(work.get(self.db, deux["id"])["assignee"])
        # l'exécuteur de l'autre agent prend le lot restant ; le premier, qui
        # a maintenant un lot et du courrier, ne reprend rien.
        out = self.take({"a2"})
        self.assertEqual([(t["agent"], t["item"]["id"]) for t in out], [("a2", deux["id"])])
        self.assertEqual(self.take({"a1", "a2"}), [])

    def test_ensemble_vide_ou_inconnu(self):
        self.agent("a1", team="nexlink", since_s=2 * HEURE)
        lot = self.lot("un")
        self.assertEqual(self.take(set()), [])
        self.assertEqual(self.take({"absente"}), [])
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])

    def test_orchestrateur_local_ne_se_sert_pas(self):
        # L165 : un orchestrateur répartit, il ne se sert jamais lui-même —
        # même quand il est le seul agent de l'exécuteur.
        self.agent("orchestrateur", team="nexlink", since_s=3 * HEURE)
        lot = self.lot("un")
        self.assertEqual(self.take({"orchestrateur"}), [])
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])

    def test_garde_fous_conserves(self):
        # repos trop court (seuil de la file d'amélioration)
        self.agent("a1", team="nexlink", since_s=60)
        self.lot("trop tôt")
        self.assertEqual(self.take({"a1"}, idle_s=2 * HEURE), [])
        # plafond de budget atteint pour cet agent
        self.agent("a2", team="nexlink", since_s=2 * HEURE)
        self.lot("sous plafond")
        self.assertEqual(self.take({"a2"}, budget_check=lambda agent: "plafond"), [])
        # hôte en pause : l'alerte locale de l'exécuteur écarte ses agents
        self.assertEqual(self.take({"a2"}, alerts=[{"type": "host_pressure",
                                                    "host": self.cfg.host}]), [])
        # les deux prises coupées (seuils nuls) : rien
        self.agent("a3", team="nexlink", since_s=2 * HEURE)
        self.lot("seuils nuls")
        self.assertEqual(self.take({"a3"}, idle_s=0, max_per_hour=0,
                                   lot_idle_s=0, lot_max_per_hour=0), [])

    def test_debit_horaire_compte_en_base(self):
        # L119/0037 : le débit est compté en base pour tout le mesh — deux
        # exécuteurs ne le dépassent pas ensemble.
        self.agent("a1", team="nexlink", since_s=2 * HEURE)
        self.agent("a2", team="nexlink", since_s=3 * HEURE)
        self.lot("un")
        self.lot("deux")
        self.assertEqual(len(self.take({"a1"}, lot_max_per_hour=1)), 1)
        self.assertEqual(self.take({"a2"}, lot_max_per_hour=1), [])


class _Worker:
    def __init__(self, alive=True):
        self._alive = alive

    def is_alive(self):
        return self._alive


class _FauxRunner:
    """Ce que `Runner.take_pass` lit de l'exécuteur, sans baux ni harnais."""

    once = False
    mediated = False
    dry_run = False

    def __init__(self, db, cfg, take, workers):
        self.db, self.cfg, self.take = db, cfg, take
        self.workers = workers
        self.lock = threading.Lock()
        self.host = cfg.host

    def host_pressure(self, now=None):
        return {"blocked": False}


class TakePassTest(_Lots):
    def test_prend_pour_les_workers_vivants_seulement(self):
        self.agent("a1", team="nexlink", since_s=2 * HEURE)
        self.agent("a2", team="nexlink", since_s=2 * HEURE)
        un = self.lot("un")
        deux = self.lot("deux")
        faux = _FauxRunner(self.db, self.cfg, self.settings(),
                           {"a1": _Worker(), "a2": _Worker(False)})
        out = runner.Runner.take_pass(faux)
        self.assertEqual([(t["agent"], t["item"]["id"]) for t in out], [("a1", un["id"])])
        self.assertIsNone(work.get(self.db, deux["id"])["assignee"])
        # l'agent a un lot et du courrier : la passe suivante ne reprend rien
        self.assertEqual(runner.Runner.take_pass(faux), [])

    def test_passe_coupee(self):
        self.agent("a1", team="nexlink", since_s=2 * HEURE)
        self.lot("un")
        faux = _FauxRunner(self.db, self.cfg, None, {"a1": _Worker()})
        self.assertEqual(runner.Runner.take_pass(faux), [])
        faux.take = self.settings()
        faux.once = True
        self.assertEqual(runner.Runner.take_pass(faux), [])
        faux.once = False
        faux.mediated = True
        self.assertEqual(runner.Runner.take_pass(faux), [])

    def test_hote_sous_pression_ecarte_la_prise(self):
        self.agent("a1", team="nexlink", since_s=2 * HEURE)
        lot = self.lot("un")
        faux = _FauxRunner(self.db, self.cfg, self.settings(), {"a1": _Worker()})
        faux.host_pressure = lambda now=None: {"blocked": True}
        self.assertEqual(runner.Runner.take_pass(faux), [])
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])


class ReglagesTest(unittest.TestCase):
    def test_options_et_variables_des_reglages(self):
        parser = runner.build_parser()
        args = parser.parse_args([])
        self.assertEqual(args.take_interval, runner.TAKE_INTERVAL_S)
        self.assertEqual(runner._take_settings(args),
                         {"idle_s": backlog.DEFAULT_TAKE_IDLE_S,
                          "max_per_hour": backlog.DEFAULT_TAKE_MAX_PER_HOUR,
                          "lot_idle_s": backlog.DEFAULT_LOT_IDLE_S,
                          "lot_max_per_hour": backlog.DEFAULT_LOT_MAX_PER_HOUR})
        # #754 : la prise au token est acquise, `allow_paid` n'a plus d'objet
        self.assertNotIn("allow_paid", runner._take_settings(args))
        # les deux prises coupées : aucune prise à déclencher
        off = parser.parse_args(["--take-idle", "0", "--take-lot-idle", "0"])
        self.assertIsNone(runner._take_settings(off))
        # l'exécuteur peut être coupé seul, sans toucher aux réglages du mesh
        self.assertEqual(parser.parse_args(["--take-interval", "0"]).take_interval, 0.0)
        # une seule prise coupée : l'autre reste
        self.assertEqual(runner._take_settings(parser.parse_args(["--take-idle", "0"]))
                         ["idle_s"], 0.0)


if __name__ == "__main__":
    unittest.main()
