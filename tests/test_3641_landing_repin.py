# SPDX-License-Identifier: AGPL-3.0-only
"""#3641 : un regel de véhicule à unités constantes garde les atterrissages.

Constat (mesh-design #141541, 26/10 03:22Z) : les déclarations d'atterrissage
visaient la tête gelée 011c4073 du véhicule #3627 ; le regel sur 67aaf293, avec
les mêmes 17 unités, les a fait tomber sans prévenir le pilote, et l'étape SLO
`verdict_merge` de #3619 est passée au niveau 3 alors que le lot attendait
seulement son paquet.

Une tête qui avance (nouvelle tête descendante de la tête épinglée) reporte la
déclaration, à condition que la composition enregistrée prouve encore l'unité
et qu'aucun revert ne l'annule. Une tête réécrite, un lot retiré de la
composition ou une unité annulée ne la reportent pas et préviennent le
déclarant. Le report revalide sous verrou le gel courant et l'ancienne épingle
(CAS) : un gel retardé n'écrase pas une déclaration renouvelée depuis.
"""
from __future__ import annotations

import datetime
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ameesh import registry, storage, work, work_dependencies
from .support import PgTestCase


def _git(repo, *args):
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True)
    if proc.returncode:
        raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


class LandingRepinTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.repo = Path(tempfile.mkdtemp(prefix="ameesh-3641-repo-", dir=self.tmp))
        _git(self.repo, "init", "-q", "-b", "work")
        _git(self.repo, "config", "user.name", "t")
        _git(self.repo, "config", "user.email", "t@example.invalid")
        (self.repo / "a.txt").write_text("base\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "base")
        self.base = _git(self.repo, "rev-parse", "HEAD")
        (self.repo / "a.txt").write_text("old\n")
        _git(self.repo, "commit", "-qam", "vehicle old")
        self.old = _git(self.repo, "rev-parse", "HEAD")
        (self.repo / "a.txt").write_text("mid\n")
        _git(self.repo, "commit", "-qam", "vehicle mid")
        self.mid = _git(self.repo, "rev-parse", "HEAD")
        (self.repo / "a.txt").write_text("new\n")
        _git(self.repo, "commit", "-qam", "vehicle new")
        self.new = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "-q", "--orphan", "autre")
        (self.repo / "b.txt").write_text("other\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "other root")
        self.other = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "-q", "work")

        registry.upsert(self.db, "decl", harness="deepseek", host="source",
                        cwd="/work/source")
        self.st = storage.of(self.db)
        self.vehicle = work.add(self.db, title="vehicule", app="ameesh",
                                actor="human:op")
        self.unit = work.add(self.db, title="unite", app="ameesh", actor="human:op")
        work.milestone(self.db, int(self.vehicle["id"]), "frozen", sha=self.old,
                       repo=str(self.repo), actor="mty-amh-impl-07")
        work.milestone(self.db, int(self.unit["id"]), "frozen", sha=self.old,
                       repo=str(self.repo), actor="mty-amh-impl-07")
        work.milestone(self.db, int(self.unit["id"]), "verdict", verdict="ok",
                       sha=self.old, repo=str(self.repo), actor="mty-amh-impl-07")
        self.until = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        work_dependencies.land(self.db, int(self.unit["id"]),
                               int(self.vehicle["id"]), until_on=self.until,
                               actor="decl")

    def landing(self):
        rows = self.st.work_dependencies.landings_for([int(self.unit["id"])])
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def pin(self):
        return self.landing()["pinned_sha"]

    def vehicle_notes(self):
        return [row["note"] for row in work.events(self.db, int(self.vehicle["id"]))]

    def record_member(self, head, unit_sha, source_sha=""):
        self.st.work.record_packet(
            int(self.vehicle["id"]), branch="integration/paquet-3641",
            base_sha=self.base, head_sha=head,
            units=[{"work_item_id": int(self.unit["id"]), "unit_sha": unit_sha,
                    "source_sha": source_sha, "frozen_sha": head}],
            note="composition #3641", actor="mty-amh-impl-07")

    def test_a_forward_refreeze_keeps_the_declaration_on_the_new_head(self):
        self.record_member(self.old, self.old)

        work.milestone(self.db, int(self.vehicle["id"]), "frozen", sha=self.new,
                       repo=str(self.repo), actor="mty-amh-impl-07")

        self.assertEqual(self.pin(), self.new)
        notes = [row["note"] for row in work.events(self.db, int(self.unit["id"]))]
        self.assertTrue(any("Landing re-pinned" in note for note in notes), notes)

    def test_a_unit_removed_by_a_revert_is_not_repinned_and_warns(self):
        # Composition : une unité U entre par une fusion sur la branche du
        # véhicule ; le gel A la porte. Une nouvelle tête B annule sa fusion
        # par `git revert -m 1` et corrige le manifeste.
        _git(self.repo, "checkout", "-q", "-b", "unite", self.base)
        (self.repo / "u.txt").write_text("unit\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "unite U")
        unit_sha = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "-q", "work")
        _git(self.repo, "merge", "--no-ff", "-q", "unite", "-m", "compose U")
        composed = _git(self.repo, "rev-parse", "HEAD")
        work.milestone(self.db, int(self.vehicle["id"]), "frozen", sha=composed,
                       repo=str(self.repo), actor="mty-amh-impl-07")
        work_dependencies.land(self.db, int(self.unit["id"]),
                               int(self.vehicle["id"]), until_on=self.until,
                               actor="decl")
        self.record_member(composed, composed, source_sha=unit_sha)
        _git(self.repo, "revert", "-m", "1", "--no-edit", composed)
        reverted = _git(self.repo, "rev-parse", "HEAD")

        work.milestone(self.db, int(self.vehicle["id"]), "frozen", sha=reverted,
                       repo=str(self.repo), actor="mty-amh-impl-07")

        self.assertEqual(self.pin(), composed)
        self.assertTrue(any("no longer carried" in note
                            for note in self.vehicle_notes()), self.vehicle_notes())
        messages = self.st.mailbox.unread("decl", 20)
        self.assertTrue(any("no longer holds" in (row.get("body") or "")
                            for row in messages), messages)

    def test_a_unit_dropped_from_the_recorded_composition_is_not_repinned(self):
        """Un `packet record` refait sans U ne prouve plus sa présence."""
        self.record_member(self.old, self.old)
        self.st.work.record_packet(
            int(self.vehicle["id"]), branch="integration/paquet-3641",
            base_sha=self.base, head_sha=self.mid, units=[],
            note="composition corrigée sans U", actor="mty-amh-impl-07")

        work.milestone(self.db, int(self.vehicle["id"]), "frozen", sha=self.new,
                       repo=str(self.repo), actor="mty-amh-impl-07")

        self.assertEqual(self.pin(), self.old)
        self.assertTrue(any("no longer carried" in note
                            for note in self.vehicle_notes()), self.vehicle_notes())

    def test_a_delayed_older_freeze_does_not_overwrite_a_renewed_pin(self):
        self.record_member(self.old, self.old)
        work.milestone(self.db, int(self.vehicle["id"]), "frozen", sha=self.mid,
                       repo=str(self.repo), actor="mty-amh-impl-07")
        work.milestone(self.db, int(self.vehicle["id"]), "frozen", sha=self.new,
                       repo=str(self.repo), actor="mty-amh-impl-07")
        self.assertEqual(self.pin(), self.new)

        # Le processus du gel `mid` avait lu l'épingle avant le renouvellement
        # sur `new` : son écriture doit être refusée sous verrou.
        stale = [dict(self.landing(), pinned_sha=self.old)]
        with mock.patch.object(self.st.work_dependencies, "landings_on_vehicle",
                               return_value=stale):
            result = work_dependencies.repin_after_freeze(
                self.db, int(self.vehicle["id"]), self.mid, repo=str(self.repo))
        self.assertEqual(result["pinned"], [])
        self.assertEqual(self.pin(), self.new)

        # Même gel courant, mais l'ancienne épingle a changé : CAS refuse.
        self.assertEqual(
            self.st.work_dependencies.repin_landing(
                int(self.unit["id"]), int(self.vehicle["id"]), self.old,
                self.other, frozen_sha=self.new, actor="mty-amh-impl-07"),
            "moved")
        self.assertEqual(self.pin(), self.new)
