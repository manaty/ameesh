# SPDX-License-Identifier: AGPL-3.0-only
"""#7760 : un lot démarre sur l'empreinte d'un lot validé dont il déclare la
dépendance, au lieu d'attendre sa fusion.

Décision du 18/11 (auditeur #225658 §2, mesh-design) : le démarrage n'est
permis que si (1) chaque lot occupant qui chevauche est `validated` (gelé,
relu, sans bloquant), (2) le lot déclare la dépendance sur chacun, (3) sa
branche part de leur tête gelée. La fusion, elle, attend toujours : le
composeur prend le dépendant avec ses prérequis, et le retire avec eux.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest

from ameesh import freeze_stack, packet_candidates, slo, storage, work
from ameesh.storage.postgres.work import WorkItems
from ameesh.storage.postgres.work_footprints import start_on_sql

from .test_4173_empreinte import _Empreintes


class _Demarrage(_Empreintes):
    def dependency(self, item, blocker):
        storage.of(self.db).work_dependencies.add(
            int(item["id"]), int(blocker["id"]), actor="test")

    def validate(self, item, sha="a" * 40):
        """L'état `validated` n'est écrit qu'après le gel relu (#4423) ; les
        témoins écrivent les deux dans cet ordre, sans la porte du gel (qui
        interroge la forge) : le stockage écrit le jalon."""
        storage.of(self.db).work.add_milestone(
            int(item["id"]), "frozen", sha=sha, actor="test", verdict=None, note="")
        self.db.execute("UPDATE work_items SET state = 'validated' WHERE id = %s",
                        (item["id"],))
        return sha

    def move(self, item, state):
        self.db.execute("UPDATE work_items SET state = %s WHERE id = %s",
                        (state, item["id"]))

    def taken(self, limit=10):
        return [row["id"] for row in storage.of(self.db).work.unassigned_lots(limit)]


class DemarrageTest(_Demarrage):
    def test_refuse_sur_un_occupant_en_build_ou_qa(self):
        premier = self.lot("premier, en cours")
        second = self.lot("second, prioritaire", priority=1)
        self.footprint(premier, "src/ameesh/backlog.py")
        self.footprint(second, "src/ameesh")
        self.dependency(second, premier)

        for state in ("build", "qa"):
            with self.subTest(state=state):
                self.move(premier, state)
                self.assertNotIn(second["id"], self.taken())
                self.assertIsNone(WorkItems(self.db).assign(
                    second["id"], "claude1", current=None, note="t", actor="t",
                    ready_only=True))
                with self.assertRaises(work.WorkError) as ctx:
                    work.assign(self.db, second["id"], "human:autre", actor="test")
                self.assertIn("footprint overlaps", str(ctx.exception))

    def test_demarre_sur_un_occupant_valide_dont_il_declare_la_dependance(self):
        premier = self.lot("premier, validé")
        second = self.lot("second, sur sa tête", priority=1)
        self.footprint(premier, "src/ameesh/backlog.py")
        self.footprint(second, "src/ameesh")
        sha = self.validate(premier)
        self.dependency(second, premier)

        # Le démarrage est ouvert : la paire est un « démarrage sur », pas une
        # attente de fusion.
        self.assertIn(second["id"], self.taken())
        overlap = storage.of(self.db).work_footprints.overlapping(second["id"])
        self.assertEqual(len(overlap), 1)
        self.assertTrue(overlap[0]["start_on"])
        self.assertEqual(overlap[0]["other_frozen_sha"], sha)

        row = WorkItems(self.db).assign(
            second["id"], "claude1", current=None, note="t", actor="t",
            ready_only=True)
        self.assertIsNotNone(row, "le démarrage sur un lot validé est permis")
        self.assertEqual(storage.of(self.db).work.unassigned_lots(10), [])

        # La fusion, elle, attend toujours : le prérequis `validated` bloque.
        edge = storage.of(self.db).work_dependencies.for_items([second["id"]])[0]
        self.assertTrue(edge["blocking"], "la fusion attend la fusion du prérequis")
        self.assertTrue(edge["start_on"])
        payload = self.mesh("work", "dependencies", str(second["id"]))
        self.assertEqual(payload.returncode, 0, payload.stderr)
        self.assertIn("starts on, frozen %s (merge still waits)" % sha[:12],
                      payload.stdout)

    def test_la_garde_manuelle_et_le_plan_lisent_la_meme_regle(self):
        premier = self.lot("premier, validé")
        second = self.lot("second")
        self.footprint(premier, "a.py")
        self.footprint(second, "a.py")
        self.validate(premier)
        self.dependency(second, premier)

        # `work assign` (lot libre) et le plan de dispatch (#4174) laissent
        # passer ; la trace lisible nomme le démarrage sur.
        work.assign(self.db, second["id"], "human:autre", actor="test")
        proc = self.mesh("work", "footprint", str(second["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("starts on #%d (validated" % premier["id"], proc.stdout)
        self.assertIn("frozen %s" % ("a" * 12), proc.stdout)

    def test_retour_en_build_rebloque_la_paire(self):
        premier = self.lot("premier, validé")
        second = self.lot("second")
        self.footprint(premier, "a.py")
        self.footprint(second, "a.py")
        self.validate(premier)
        self.dependency(second, premier)
        self.assertIn(second["id"], self.taken())

        # Une dépendance rendue en build redevient bloquante (le gel du
        # prérequis peut encore changer) : le démarrage attend de nouveau.
        self.move(premier, "build")
        self.assertNotIn(second["id"], self.taken())
        self.assertIsNone(WorkItems(self.db).assign(
            second["id"], "claude1", current=None, note="t", actor="t",
            ready_only=True))
        self.assertFalse(storage.of(self.db).work_footprints.overlapping(
            second["id"])[0]["start_on"])

    def test_chaque_occupant_doit_etre_valide_et_declare(self):
        valide = self.lot("occupant validé")
        en_cours = self.lot("occupant en cours")
        second = self.lot("second")
        for item in (valide, en_cours):
            self.footprint(item, "src/a.py")
        self.footprint(second, "src")
        self.validate(valide)
        self.dependency(second, valide)
        self.move(en_cours, "build")

        # Un seul occupant non validé suffit à retenir le démarrage.
        self.assertNotIn(second["id"], self.taken())
        # Et un occupant validé sans dépendance déclarée aussi.
        self.move(en_cours, "validated")
        self.assertNotIn(second["id"], self.taken())
        self.dependency(second, en_cours)
        self.assertIn(second["id"], self.taken())

    def test_retirer_la_dependance_rebloque(self):
        premier = self.lot("premier, validé")
        second = self.lot("second")
        self.footprint(premier, "a.py")
        self.footprint(second, "a.py")
        self.validate(premier)
        self.dependency(second, premier)
        self.assertIn(second["id"], self.taken())
        storage.of(self.db).work_dependencies.remove(
            int(second["id"]), int(premier["id"]), actor="test")
        self.assertNotIn(second["id"], self.taken())


def _git(repo, *args):
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                          text=True, check=True)
    return proc.stdout.strip()


def _commit(repo, message, path, text):
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full) or full, exist_ok=True)
    with open(full, "w", encoding="utf-8") as handle:
        handle.write(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


class FreezeBaseTest(_Demarrage):
    """Condition (3) : la tête gelée du dépendant contient celle du prérequis."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory(prefix="lot-7760-")
        self.repo = self.tmp.name
        _git(self.repo, "init", "-q", "-b", "main")
        self.base = _commit(self.repo, "base", "base.txt", "base\n")

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()

    def pair(self, sha="a" * 40):
        parent = self.lot("parent validé")
        child = self.lot("dépendant")
        self.footprint(parent, "src/a.py")
        self.footprint(child, "src/a.py")
        self.validate(parent, sha)
        self.dependency(child, parent)
        return parent, child

    def test_refuse_une_base_qui_ne_contient_pas_la_tete_du_parent(self):
        _git(self.repo, "checkout", "-q", "-b", "lot/parent", self.base)
        self.parent_head = _commit(self.repo, "parent", "src/a.py", "parent\n")
        parent, child = self.pair(self.parent_head)
        _git(self.repo, "checkout", "-q", "-b", "lot/child", self.base)
        child_head = _commit(self.repo, "child", "src/b.py", "child\n")

        with self.assertRaises(work.WorkError) as ctx:
            freeze_stack.guard_start_base(
                self.db, child, child_head, repo=self.repo)
        self.assertIn("start-base", str(ctx.exception))
        self.assertIn(self.parent_head[:12], str(ctx.exception))
        self.assertIn("git rebase --onto", str(ctx.exception))

        # La même tête, reposée sur le gel du parent, passe.
        _git(self.repo, "checkout", "-q", "-b", "lot/child-ok", self.parent_head)
        ok_head = _commit(self.repo, "child on parent", "src/b.py", "child\n")
        self.assertEqual(freeze_stack.guard_start_base(
            self.db, child, ok_head, repo=self.repo), "")

    def test_sans_dependance_declaree_aucune_preuve_de_base(self):
        parent, child = self.pair()
        storage.of(self.db).work_dependencies.remove(
            int(child["id"]), int(parent["id"]), actor="test")
        _git(self.repo, "checkout", "-q", "-b", "lot/child", self.base)
        child_head = _commit(self.repo, "child", "src/b.py", "child\n")
        self.assertEqual(freeze_stack.guard_start_base(
            self.db, child, child_head, repo=self.repo), "")


class SloTest(unittest.TestCase):
    """Le SLO ne date plus une attente d'empreinte sur un prérequis validé
    déclaré : le lot démarre sur sa tête gelée (#7760)."""

    def setUp(self):
        self.item = {"id": 1, "state": "intake", "assignee": None, "app": "nexlink"}
        self.other = {"id": 2, "state": "validated", "assignee": None,
                      "app": "nexlink"}
        self.paths = {1: ["src/a.py"], 2: ["src/a.py"]}

    def test_valide_declare_ne_retient_plus_le_demarrage(self):
        self.assertEqual([b["id"] for b in slo.footprint_blockers(
            self.item, [self.other], self.paths)], [2])
        self.assertEqual(slo.footprint_blockers(
            self.item, [self.other], self.paths, declared=[2]), [])

    def test_un_occupant_non_valide_reste_bloquant(self):
        self.other["state"] = "build"
        self.assertEqual([b["id"] for b in slo.footprint_blockers(
            self.item, [self.other], self.paths, declared=[2])], [2])


class CompositeurTest(unittest.TestCase):
    """Le composeur prend le dépendant avec son prérequis, dans cet ordre, et
    le retire quand le prérequis n'est plus prêt (retour en build, conflit)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="paquet-7760-")
        self.repo = self.tmp.name
        _git(self.repo, "init", "-q", "-b", "main")
        self.base = _commit(self.repo, "base", "base.txt", "base\n")
        _git(self.repo, "checkout", "-q", "-b", "lot/parent", self.base)
        self.parent = _commit(self.repo, "parent", "src/a.py", "parent\n")
        _git(self.repo, "checkout", "-q", "-b", "lot/child", self.parent)
        self.child = _commit(self.repo, "child", "src/b.py", "child\n")

    def tearDown(self):
        self.tmp.cleanup()

    def entry(self, lot, source, *, ready=True, starts=(), priority=2, created=0.0):
        return {"work_item": lot, "source": source, "published_head": source,
                "ready": ready, "causes": [], "starts_on": list(starts),
                "priority": priority, "created_ts": created, "error": None}

    def test_ordre_preerequis_avant_dependant(self):
        parent = self.entry(2, self.parent, priority=2)
        child = self.entry(1, self.child, starts=[2], priority=1)
        packet_candidates._settle_start_on([child, parent], self.repo)
        self.assertTrue(child["ready"], child["causes"])
        self.assertEqual(packet_candidates._packet_order({1: child, 2: parent}),
                         [2, 1])

    def test_preerequis_non_pret_retire_le_dependant(self):
        parent = self.entry(2, self.parent, ready=False)
        child = self.entry(1, self.child, starts=[2])
        packet_candidates._settle_start_on([child, parent], self.repo)
        self.assertFalse(child["ready"])
        self.assertEqual([cause["code"] for cause in child["causes"]],
                         ["dependency-unready"])

    def test_base_hors_du_gel_du_preerequis_refusee(self):
        parent = self.entry(2, self.parent)
        # le dépendant ne contient pas la tête du parent : base refusée
        off = self.entry(1, self.base, starts=[2])
        packet_candidates._settle_start_on([off, parent], self.repo)
        self.assertFalse(off["ready"])
        self.assertEqual([cause["code"] for cause in off["causes"]],
                         ["dependency-base"])

    def test_sql_start_on(self):
        # le fragment SQL porte la règle : validé + déclaration + empreinte
        sql = start_on_sql("i", "w")
        self.assertIn("w.state = 'validated'", sql)
        self.assertIn("work_item_dependencies", sql)
        self.assertIn("work_item_footprints", sql)


if __name__ == "__main__":
    unittest.main()
