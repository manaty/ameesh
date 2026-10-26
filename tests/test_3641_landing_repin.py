# SPDX-License-Identifier: AGPL-3.0-only
"""#3641 : un regel de véhicule à unités constantes garde les atterrissages.

Constat (mesh-design #141541, 26/10 03:22Z) : les déclarations d'atterrissage
visaient la tête gelée 011c4073 du véhicule #3627 ; le regel sur 67aaf293, avec
les mêmes 17 unités, les a fait tomber sans prévenir le pilote, et l'étape SLO
`verdict_merge` de #3619 est passée au niveau 3 alors que le lot attendait
seulement son paquet.

Une tête qui avance (nouvelle tête descendante de la tête épinglée) reporte la
déclaration ; une tête réécrite ou d'une autre lignée ne la reporte pas et le
déclarant est prévenu.
"""
from __future__ import annotations

import datetime
import subprocess
import tempfile
import unittest
from pathlib import Path

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
        (self.repo / "a.txt").write_text("old\n")
        _git(self.repo, "commit", "-qam", "vehicle old")
        self.old = _git(self.repo, "rev-parse", "HEAD")
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
        self.vehicle = work.add(self.db, title="vehicule", app="ameesh",
                                actor="human:op")
        self.unit = work.add(self.db, title="unite", app="ameesh", actor="human:op")
        work.milestone(self.db, int(self.vehicle["id"]), "frozen", sha=self.old,
                       repo=str(self.repo), actor="mty-amh-impl-07")
        work.milestone(self.db, int(self.unit["id"]), "frozen", sha=self.old,
                       repo=str(self.repo), actor="mty-amh-impl-07")
        work.milestone(self.db, int(self.unit["id"]), "verdict", verdict="ok",
                       sha=self.old, repo=str(self.repo), actor="mty-amh-impl-07")
        until = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        work_dependencies.land(self.db, int(self.unit["id"]),
                               int(self.vehicle["id"]), until_on=until, actor="decl")

    def landing(self):
        rows = storage.of(self.db).work_dependencies.landings_for([int(self.unit["id"])])
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def test_a_forward_refreeze_keeps_the_declaration_on_the_new_head(self):
        work.milestone(self.db, int(self.vehicle["id"]), "frozen", sha=self.new,
                       repo=str(self.repo), actor="mty-amh-impl-07")

        self.assertEqual(self.landing()["pinned_sha"], self.new)
        notes = [row["note"] for row in work.events(self.db, int(self.unit["id"]))]
        self.assertTrue(any("Landing re-pinned" in note for note in notes), notes)

    def test_a_rewritten_refreeze_invalidates_and_warns_the_declarant(self):
        work.milestone(self.db, int(self.vehicle["id"]), "frozen", sha=self.other,
                       repo=str(self.repo), actor="mty-amh-impl-07")

        self.assertEqual(self.landing()["pinned_sha"], self.old)
        notes = [row["note"] for row in work.events(self.db, int(self.vehicle["id"]))]
        self.assertTrue(any("Landing declaration of lot #%d invalidated"
                            % int(self.unit["id"]) in note for note in notes), notes)
        messages = storage.of(self.db).mailbox.unread("decl", 20)
        self.assertTrue(any("re-frozen on" in (row.get("body") or "")
                            for row in messages), messages)
