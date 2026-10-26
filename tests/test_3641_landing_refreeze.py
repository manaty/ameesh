# SPDX-License-Identifier: AGPL-3.0-only
"""#3641 : un regel de véhicule prévient le déclarant, sans report automatique.

Constat (mesh-design #141541, 26/10 03:22Z) : les déclarations d'atterrissage
visaient la tête gelée 011c4073 du véhicule #3627 ; le regel sur 67aaf293, avec
les mêmes 17 unités, les a fait tomber sans prévenir le pilote, et l'étape SLO
`verdict_merge` de #3619 est passée au niveau 3 alors que le lot attendait
seulement son paquet.

Le report automatique sur une tête descendante a été retiré après la revue
codex #142034 : l'ascendance et la détection de revert ne prouvent pas le
contenu courant (un commit ordinaire peut retirer l'unité, et `plan_git` ne
reconnaît que les `Revert` qui citent le commit). Le geste sûr est de prévenir
le déclarant et de laisser la déclaration en place : le signal stale revient,
le pilote renouvelle explicitement. L'avis est revalidé sous verrou (gel
courant + ancienne épingle) pour qu'un gel retardé ne prévienne pas une
déclaration déjà renouvelée.
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


class LandingRefreezeTest(PgTestCase):
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

    def message_bodies(self):
        return [row.get("body") or ""
                for row in self.st.mailbox.unread("decl", 50)]

    def record_member(self, head, unit_sha, source_sha=""):
        self.st.work.record_packet(
            int(self.vehicle["id"]), branch="integration/paquet-3641",
            base_sha=self.base, head_sha=head,
            units=[{"work_item_id": int(self.unit["id"]), "unit_sha": unit_sha,
                    "source_sha": source_sha, "frozen_sha": head}],
            note="composition #3641", actor="mty-amh-impl-07")

    def test_a_refreeze_warns_and_leaves_the_declaration(self):
        self.record_member(self.old, self.old)

        work.milestone(self.db, int(self.vehicle["id"]), "frozen", sha=self.new,
                       repo=str(self.repo), actor="mty-amh-impl-07")

        self.assertEqual(self.pin(), self.old)
        self.assertTrue(any("needs renewal" in note for note in self.vehicle_notes()),
                        self.vehicle_notes())
        self.assertTrue(any("pinned to" in body and "renew it" in body
                            for body in self.message_bodies()), self.message_bodies())

    def test_an_ordinary_removal_never_repins_the_declaration(self):
        """Le témoin statique de la revue : B descend de A et retire U sans revert."""
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
        (self.repo / "u.txt").unlink()
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "remove U")
        removed = _git(self.repo, "rev-parse", "HEAD")

        work.milestone(self.db, int(self.vehicle["id"]), "frozen", sha=removed,
                       repo=str(self.repo), actor="mty-amh-impl-07")

        self.assertEqual(self.pin(), composed)
        self.assertTrue(any("needs renewal" in note for note in self.vehicle_notes()),
                        self.vehicle_notes())

    def test_a_record_replacement_never_decides_the_declaration(self):
        """Un `packet record` refait sans U ne change rien : l'avis ne re-pin pas."""
        self.record_member(self.old, self.old)
        self.st.work.record_packet(
            int(self.vehicle["id"]), branch="integration/paquet-3641",
            base_sha=self.base, head_sha=self.mid, units=[],
            note="composition corrigée sans U", actor="mty-amh-impl-07")

        work.milestone(self.db, int(self.vehicle["id"]), "frozen", sha=self.new,
                       repo=str(self.repo), actor="mty-amh-impl-07")

        self.assertEqual(self.pin(), self.old)
        self.assertTrue(any("needs renewal" in note for note in self.vehicle_notes()),
                        self.vehicle_notes())

    def test_a_delayed_older_freeze_does_not_warn_a_renewed_declaration(self):
        self.record_member(self.old, self.old)
        work.milestone(self.db, int(self.vehicle["id"]), "frozen", sha=self.mid,
                       repo=str(self.repo), actor="mty-amh-impl-07")
        work.milestone(self.db, int(self.vehicle["id"]), "frozen", sha=self.new,
                       repo=str(self.repo), actor="mty-amh-impl-07")
        work_dependencies.land(self.db, int(self.unit["id"]),
                               int(self.vehicle["id"]), until_on=self.until,
                               actor="pilot")
        self.assertEqual(self.pin(), self.new)
        before = len(self.vehicle_notes())
        stale = [dict(self.landing(), pinned_sha=self.old)]

        # Gel `mid` retardé : le gel courant est `new`, l'avis est refusé.
        with mock.patch("ameesh.storage.of", return_value=self.st), \
             mock.patch.object(self.st.work_dependencies, "landings_on_vehicle",
                               return_value=stale):
            result = work_dependencies.warn_landings_after_freeze(
                self.db, int(self.vehicle["id"]), self.mid)
        self.assertEqual((result["notified"], result["skipped"]),
                         ([], [int(self.unit["id"])]))
        self.assertEqual(len(self.vehicle_notes()), before)
        self.assertEqual(self.pin(), self.new)

        # Même gel courant, mais l'ancienne épingle a changé : refus aussi.
        with mock.patch("ameesh.storage.of", return_value=self.st), \
             mock.patch.object(self.st.work_dependencies, "landings_on_vehicle",
                               return_value=stale):
            result = work_dependencies.warn_landings_after_freeze(
                self.db, int(self.vehicle["id"]), self.new)
        self.assertEqual((result["notified"], result["skipped"]),
                         ([], [int(self.unit["id"])]))
        self.assertEqual(len(self.vehicle_notes()), before)
        self.assertEqual(self.pin(), self.new)
