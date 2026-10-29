# SPDX-License-Identifier: AGPL-3.0-only
"""L99 T1 (#4349) — les alias d'états du cycle de vie générique.

`work_lifecycle.ALIASES` et `canonical()` acceptent les deux orthographes
(v1 et générique) ; `work move`, `work list --state` et le filtre d'API les
lisent ; `work show` et `--json` portent le champ dérivé `lifecycle` à côté
de `state`. Rien ne change en base : l'état persisté reste l'orthographe v1.
"""
from __future__ import annotations

import json
import types
import unittest

from ameesh import mesh_cli, serve, work, work_lifecycle

from .support import PgTestCase

#: les deux orthographes, telles que la tranche 1 les nomme.
GENERIC = {
    "intake": "requested",
    "build": "in_progress",
    "qa": "to_validate",
    "validated": "validated",
    "merged": "delivered",
    "promoted": "delivered",
    "blocked": "blocked",
    "waiting_human": "waiting_human",
    "closed": "closed",
}


class AliasVocabularyTest(unittest.TestCase):
    def test_aller_retour_des_alias(self):
        for v1, generic in GENERIC.items():
            with self.subTest(state=v1):
                self.assertEqual(work_lifecycle.canonical(generic),
                                 "delivered" if v1 in ("merged", "promoted") else v1)
                self.assertEqual(work_lifecycle.canonical(v1),
                                 "delivered" if v1 in ("merged", "promoted") else v1)
                self.assertEqual(work_lifecycle.lifecycle_name(v1), generic)
                self.assertEqual(work_lifecycle.lifecycle_name(generic), generic)

    def test_les_deux_orthographes_sont_acceptees(self):
        self.assertEqual(set(work_lifecycle.ACCEPTED_STATES),
                         set(GENERIC) | set(GENERIC.values()))
        # les groupes existants gardent l'orthographe persistée
        self.assertEqual(work_lifecycle.STATES, ("intake", "build", "qa", "validated",
                                                "delivered", "blocked", "waiting_human", "closed"))

    def test_un_nom_inconnu_est_refuse(self):
        for name in ("unknown", "inprogress", "", "Build"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    work_lifecycle.canonical(name)
                with self.assertRaises(ValueError):
                    work_lifecycle.lifecycle_name(name)


class AliasCommandsTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.item = work.add(self.db, title="lot alias", app="demo")
        self.lot = int(self.item["id"])

    def test_move_accepte_le_nom_generique(self):
        work.move(self.db, self.lot, "in_progress")
        self.assertEqual(work.get(self.db, self.lot)["state"], "build")
        work.move(self.db, self.lot, "to_validate")
        self.assertEqual(work.get(self.db, self.lot)["state"], "qa")
        # la base garde l'orthographe v1, jamais l'alias
        journal = [event["state"] for event in work.events(self.db, self.lot)]
        self.assertEqual(journal[0], "qa")
        self.assertNotIn("to_validate", journal)

    def test_move_refuse_un_nom_inconnu(self):
        with self.assertRaises(work.WorkError) as caught:
            work.move(self.db, self.lot, "started")
        self.assertIn("unknown state", str(caught.exception))
        self.assertEqual(work.get(self.db, self.lot)["state"], "intake")

    def test_list_state_accepte_le_nom_generique(self):
        work.move(self.db, self.lot, "build")
        for state in ("build", "in_progress"):
            with self.subTest(state=state):
                rows = work.list_items(self.db, state=state, limit=10)
                self.assertEqual([int(row["id"]) for row in rows], [self.lot])
        with self.assertRaises(work.WorkError):
            work.list_items(self.db, state="started", limit=10)

    def test_le_filtre_d_api_accepte_le_nom_generique(self):
        work.move(self.db, self.lot, "build")
        ctx = types.SimpleNamespace(cfg=self.cfg)
        out = serve._lots(ctx, self.db, {}, {"state": ["in_progress"]})
        self.assertEqual([int(row["id"]) for row in out["lots"]], [self.lot])
        self.assertEqual(out["lots"][0]["state"], "build")
        self.assertEqual(out["lots"][0]["lifecycle"], "in_progress")

    def test_les_vues_portent_le_lifecycle(self):
        work.move(self.db, self.lot, "build")
        item = mesh_cli.work_detail(self.cfg, self.db, self.lot)
        self.assertEqual((item["state"], item["lifecycle"]), ("build", "in_progress"))
        rows = mesh_cli.work_rows(self.db, state="in_progress", limit=10)
        self.assertEqual((rows[0]["state"], rows[0]["lifecycle"]),
                         ("build", "in_progress"))

    def test_show_et_json_portent_le_lifecycle(self):
        work.move(self.db, self.lot, "build")
        proc = self.mesh("work", "show", str(self.lot), "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual((payload["state"], payload["lifecycle"]),
                         ("build", "in_progress"))
        proc = self.mesh("work", "show", str(self.lot))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("state: build", proc.stdout)
        self.assertIn("lifecycle: in_progress", proc.stdout)

    def test_list_json_porte_le_lifecycle(self):
        work.move(self.db, self.lot, "build")
        proc = self.mesh("work", "list", "--state", "in_progress", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = json.loads(proc.stdout)
        self.assertEqual([int(row["id"]) for row in rows], [self.lot])
        self.assertEqual((rows[0]["state"], rows[0]["lifecycle"]),
                         ("build", "in_progress"))

    def test_move_cli_accepte_le_nom_generique(self):
        proc = self.mesh("work", "move", str(self.lot), "in_progress")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.get(self.db, self.lot)["state"], "build")
        proc = self.mesh("work", "move", str(self.lot), "started")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("invalid choice", proc.stderr)


if __name__ == "__main__":
    unittest.main()
