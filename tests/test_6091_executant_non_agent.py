# SPDX-License-Identifier: AGPL-3.0-only
"""#6091 (manaty/ameesh#4642, écart 4) : un exécutant externe non-agent (le
serveur IMA) se déclare avec un responsable humain, se réserve comme un agent
externe, et n'est jamais confondu avec un agent réveillable."""
from __future__ import annotations

import json
import time

from ameesh import registry, storage, work

from .test_l119_backlog import HEURE, _Base


class ExecutantNonAgentTest(_Base):
    def declare(self, name="system:ima", responsible="human:proprio", **kw):
        kw.setdefault("actor", "human:proprio")
        return work.declare_external_executor(
            self.db, name, responsible, cfg=self.cfg, **kw)

    def reserve(self, title="geste ima", executor="system:ima", **kw):
        item = work.add(self.db, title=title, app="nexlink",
                        external_executor=executor, actor="human:proprio",
                        cfg=self.cfg, **kw)
        # L165 laisse un lot tout juste créé à qui l'a créé (5 min).
        self.db.execute("UPDATE work_items SET created_at = created_at"
                        " - make_interval(secs => %s) WHERE id = %s",
                        (HEURE, item["id"]))
        return work.get(self.db, item["id"])

    # -- déclaration ---------------------------------------------------------
    def test_un_systeme_declare_se_reserve_et_n_est_pas_un_agent(self):
        row = self.declare(note="adaptateur IMA du backlog")
        self.assertEqual(row["name"], "system:ima")
        self.assertEqual(row["responsible"], "human:proprio")
        self.assertEqual(row["note"], "adaptateur IMA du backlog")
        self.assertIsNone(row.get("retired_at"))
        # Jamais un agent : rien dans le registre, rien de réveillable.
        self.assertIsNone(registry.get(self.db, "system:ima"))
        self.assertFalse(registry.wakeable(self.db, self.cfg, "system:ima")[0])
        # Le système est réservable, pas attribuable.
        lot = self.reserve()
        self.assertEqual(lot["external_executor"], "system:ima")
        self.assertIsNone(lot["assignee"])
        with self.assertRaises(work.WorkError) as raised:
            work.assign(self.db, lot["id"], "system:ima")
        self.assertIn("reserved to system:ima", str(raised.exception))
        self.assertIn("not assignable", str(raised.exception))
        with self.assertRaises(work.WorkError) as raised:
            work.delegate(self.db, lot["id"], "system:ima", within="30m",
                          actor="human:proprio", cfg=self.cfg)
        self.assertIn("reserved to system:ima", str(raised.exception))
        # L165 ne le prend pas, et la levée le rend à la file ordinaire.
        self.agent("claude1", team="nexlink")
        self.assertNotIn(lot["id"],
                         [i["id"] for i in storage.of(self.db).work.unassigned_lots(10)])
        self.assertEqual(self.take(), [])
        work.set_external_executor(self.db, lot["id"], None, actor="human:proprio",
                                   cfg=self.cfg, clear=True)
        self.assertEqual([t["item"]["id"] for t in self.take()], [lot["id"]])

    def test_un_systeme_non_declare_est_refuse_et_dit_comment_faire(self):
        with self.assertRaises(work.WorkError) as raised:
            work.add(self.db, title="geste ima", app="nexlink",
                     external_executor="system:ima", actor="human:proprio",
                     cfg=self.cfg)
        self.assertIn("not declared", str(raised.exception))
        self.assertIn("--declare system:ima", str(raised.exception))
        self.assertIn("--responsible human:<id>", str(raised.exception))
        lot = work.add(self.db, title="lot", app="nexlink")
        with self.assertRaises(work.WorkError) as raised:
            work.set_external_executor(self.db, lot["id"], "system:ima",
                                       actor="human:proprio", cfg=self.cfg)
        self.assertIn("not declared", str(raised.exception))

    def test_redeclare_meme_responsable_idempotent_autre_refuse(self):
        self.declare(note="première note")
        row = self.declare(note="note corrigée")
        self.assertEqual(row["responsible"], "human:proprio")
        self.assertEqual(row["note"], "note corrigée")
        self.assertEqual(len(work.external_executors(self.db)), 1)
        with self.assertRaises(work.WorkError) as raised:
            self.declare(responsible="human:autre")
        self.assertIn("already declared with responsible human:proprio",
                      str(raised.exception))
        self.assertEqual(work.external_executors(self.db)[0]["responsible"],
                         "human:proprio")

    def test_noms_et_responsables_invalides(self):
        for bad in ("ima", "system:", "system:deux mots", "system:-x",
                    "system:" + "a" * 65):
            with self.subTest(name=bad):
                with self.assertRaises(work.WorkError):
                    self.declare(name=bad)
        with self.assertRaises(work.WorkError) as raised:
            self.declare(responsible="bureau")
        self.assertIn("is a human (human:<id>)", str(raised.exception))
        # Un nom nu d'humain déjà résolu en base est normalisé.
        self.agent("bureau", responsible="human:proprio")
        row = self.declare(name="system:relais", responsible="proprio")
        self.assertEqual(row["responsible"], "human:proprio")

    def test_retrait_sans_toucher_aux_lots_deja_reserves(self):
        self.declare()
        lot = self.reserve()
        retired = work.retire_external_executor(
            self.db, "system:ima", actor="human:proprio", cfg=self.cfg)
        self.assertIsNotNone(retired.get("retired_at"))
        self.assertEqual(work.get(self.db, lot["id"])["external_executor"],
                         "system:ima")
        # Plus de nouvelle réservation, ni déclaration ressuscitée.
        with self.assertRaises(work.WorkError) as raised:
            self.reserve(title="second geste")
        self.assertIn("was retired", str(raised.exception))
        with self.assertRaises(work.WorkError) as raised:
            self.declare()
        self.assertIn("was retired", str(raised.exception))
        # La levée reste possible sur le lot existant.
        work.set_external_executor(self.db, lot["id"], None, actor="human:proprio",
                                   cfg=self.cfg, clear=True)
        self.assertIsNone(work.get(self.db, lot["id"])["external_executor"])
        # Le retrait est idempotent ; un nom inconnu est refusé.
        again = work.retire_external_executor(
            self.db, "system:ima", actor="human:proprio", cfg=self.cfg)
        self.assertEqual(again["name"], "system:ima")
        with self.assertRaises(work.WorkError):
            work.retire_external_executor(self.db, "system:autre",
                                          actor="human:proprio", cfg=self.cfg)
        # La déclaration retirée reste lisible pour la trace.
        self.assertEqual([r["name"] for r in work.external_executors(
            self.db, include_retired=True)], ["system:ima"])
        self.assertEqual(work.external_executors(self.db), [])

    def test_les_agents_externes_existants_ne_changent_pas(self):
        self.agent("bureau", harness="codex", responsible="human:proprio")
        self.db.execute("UPDATE agent_registry SET mode = 'externe'"
                        " WHERE name = 'bureau'")
        lot = work.add(self.db, title="geste bureau", app="nexlink",
                       external_executor="bureau", actor="human:proprio",
                       cfg=self.cfg)
        self.assertEqual(lot["external_executor"], "bureau")
        self.assertIsNone(storage.of(self.db).external_executors.get("bureau"))
        # Un `system:…` déclaré n'entre pas dans le registre, et un agent
        # `execute` reste refusé comme exécutant externe.
        self.declare()
        with self.assertRaises(work.WorkError) as raised:
            work.add(self.db, title="lot", app="nexlink",
                     external_executor="bureau2", actor="human:proprio",
                     cfg=self.cfg)
        self.assertIn("not in the registry", str(raised.exception))

    # -- CLI -----------------------------------------------------------------
    def test_cli_declare_liste_et_retire(self):
        proc = self.mesh("work", "external-executor", "--declare", "system:ima",
                         "--responsible", "human:proprio", "--note", "IMA",
                         "--actor", "human:proprio", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["name"], "system:ima")
        self.assertEqual(out["responsible"], "human:proprio")
        self.assertEqual(out["note"], "IMA")
        proc = self.mesh("work", "external-executor", "--list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("system:ima", proc.stdout)
        self.assertIn("human:proprio", proc.stdout)
        proc = self.mesh("work", "external-executor", "--list", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([r["name"] for r in json.loads(proc.stdout)],
                         ["system:ima"])
        proc = self.mesh("work", "external-executor", "--retire", "system:ima",
                         "--actor", "human:proprio")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("retired", proc.stdout)
        proc = self.mesh("work", "external-executor", "--list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("no declared non-agent external executor", proc.stdout)
        proc = self.mesh("work", "external-executor", "--list",
                         "--include-retired", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([r["name"] for r in json.loads(proc.stdout)],
                         ["system:ima"])

    def test_cli_reserve_et_clear_un_systeme(self):
        self.declare()
        proc = self.mesh("work", "add", "--title", "geste ima", "--app", "agence-x",
                         "--new-project", "--external-executor", "system:ima",
                         "--actor", "human:proprio")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("executor : system:ima (external, reserved; not assigned",
                      proc.stdout)
        lot_id = int(proc.stdout.split("#")[1].split()[0])
        item = work.get(self.db, lot_id)
        self.assertEqual(item["external_executor"], "system:ima")
        self.assertIsNone(item["assignee"])
        proc = self.mesh("work", "show", str(lot_id))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("non-agent system, responsible human:proprio", proc.stdout)
        proc = self.mesh("work", "external-executor", str(lot_id), "--clear",
                         "--actor", "human:proprio")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("external executor cleared", proc.stdout)
        self.assertIsNone(work.get(self.db, lot_id)["external_executor"])

    def test_cli_garde_les_gestes_separes(self):
        proc = self.mesh("work", "external-executor", "--declare", "system:ima")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--responsible", proc.stderr)
        proc = self.mesh("work", "external-executor", "--declare", "system:ima",
                         "--responsible", "human:proprio",
                         "--retire", "system:ima")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("two different gestures", proc.stderr)
        proc = self.mesh("work", "external-executor", "--list", "12")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--list goes alone", proc.stderr)
        proc = self.mesh("work", "external-executor",
                         "--responsible", "human:proprio")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--responsible/--note go with --declare", proc.stderr)
