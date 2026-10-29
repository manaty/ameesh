# SPDX-License-Identifier: AGPL-3.0-only
"""#4429 — paquet-candidats écarte un lot dont la migration entre en collision.

Constat du 29/10 : #4174 (0506_pending_decisions.sql) et #4351
(0506_work_validated.sql) étaient tous deux prêts ; un paquet portant les deux
est rouge à `migrate` (« duplicate migration version: 0506 »). Le banc monte
un dépôt jetable dont la cible porte déjà 0505, et des lots légers (le gel
déclaré suffit) qui ajoutent des migrations : numéro sur la cible, numéro
partagé entre deux candidats (le détenteur de la réservation reste), cas
nominal, lot sans migration, projet sans `migrations` déclaré. Depuis #4430,
la couverture par une réservation active du lot passe avant : un candidat
non couvert est écarté et ne départage personne (B1 de dev3, #160568).
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import migrations, packet_candidates, resolutions

from . import test_2404_paquet_candidats as bench
from .support import patch_families
from .test_2404_paquet_candidats import (FakeProgress, FakeWork, _commit, _git, _write,
                                         candidates_script)

MIG = "src/ameesh/migrations"


class Registry(FakeWork):
    """Le registre simulé, avec les réservations L122 actives."""

    def __init__(self, rows, holders):
        super().__init__(rows)
        self.holders = holders
        self.asked = []

    def migration_holder(self, number, repo):
        self.asked.append((number, repo))
        lot = self.holders.get(number)
        return {"id": lot, "title": "lot %d" % lot, "state": "qa"} if lot else None


class MigrationCollisionTest(unittest.TestCase):
    # le banc #2404, sans importer sa classe (ses témoins seraient rejoués ici)
    inventory_of = bench.InventoryTest.inventory
    by_id = bench.InventoryTest.by_id

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-4429-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.name", "banc")
        _git(self.repo, "config", "user.email", "banc@example.invalid")
        _git(self.repo, "config", "commit.gpgsign", "false")
        _write(self.repo, MIG + "/0504_old.sql", "-- 504\n")
        _commit(self.repo, "base")
        self.marks: dict[int, list[dict]] = {}
        self.rows: list[dict] = []
        self.classes: dict[int, str] = {}
        self.files: dict[int, list] = {}
        self.train_reviews: dict[int, int] = {}
        self.head_classes: dict = {}
        self.class_calls = []
        self.families = {"author": "deepseek", "reviewer": "claude"}
        self.role_error = None
        self.role_verdicts: dict[str, bool] = {}

        # 4174 : 0505 ajouté, mais la cible a reçu un autre 0505 depuis
        self.lot(4174, {MIG + "/0505_pending_decisions.sql": "-- a\n"})
        # 4351 et 4352 : le même 0506 ; 4352 est plus ancien dans l'ordre
        self.lot(4351, {MIG + "/0506_work_validated.sql": "-- b\n"}, created=50.0)
        self.lot(4352, {MIG + "/0506_pending.sql": "-- c\n"}, created=10.0)
        # 4400 : nominal, 0507 unique et absent de la cible
        self.lot(4400, {MIG + "/0507_fine.sql": "-- d\n"})
        # 4401 : sans migration
        self.lot(4401, {"src/ameesh/plain.py": "x\n"})
        _write(self.repo, MIG + "/0505_landed.sql", "-- landed\n")
        _commit(self.repo, "main avance : 0505 atterri")

        origin = os.path.join(self.tmp, "origin.git")
        _git(self.tmp, "init", "-q", "--bare", origin)
        _git(self.repo, "remote", "add", "origin", origin)
        _git(self.repo, "push", "-q", "origin", "--all")
        self.clone = resolutions.Replayer(self.repo, os.path.join(self.tmp, "clone"))
        self.holders = {506: 4351}
        # réservations actives : 4351 et 4352 tiennent toutes deux 0506 (le
        # doublon de registre que #4430 a corrigé), pour exercer le départage
        self.reservations = {4174: [505], 4351: [506], 4352: [506], 4400: [507]}
        self.registry = Registry(self.rows, self.holders)
        self.store = mock.Mock(work=self.registry, progress=FakeProgress(self.marks, {}))
        self.store.migration_reservations.for_target.side_effect = self.for_target
        self.store.work_dependencies.for_items.return_value = []
        patch_families(self)

    def lot(self, lot: int, edits: dict, *, created: float | None = None) -> None:
        name = "lot/%d" % lot
        _git(self.repo, "switch", "-q", "-c", name, "main")
        for path, text in edits.items():
            _write(self.repo, path, text)
        tip = _commit(self.repo, "#%d" % lot)
        _git(self.repo, "switch", "-q", "main")
        self.marks[lot] = [{"id": lot * 10, "work_item_id": lot, "kind": "frozen", "sha": tip,
                            "actor": "author", "verdict": None, "at_ts": float(lot)},
                           {"id": lot * 10 + 1, "work_item_id": lot, "kind": "verdict", "sha": tip,
                            "actor": "reviewer", "verdict": "ok", "at_ts": float(lot) + 1}]
        self.classes[lot] = "light"
        self.rows.append({"id": lot, "state": "qa", "app": "ameesh", "branch": name,
                          "branch_target": None, "title": "lot %d" % lot,
                          "assignee": "author", "priority": 2,
                          "created_ts": created if created is not None else float(lot)})

    def for_target(self, repository, branch):
        assert (repository, branch) == ("github.com/manaty/ameesh", "main")
        return [{"work_item_id": lot, "number": number}
                for lot, numbers in self.reservations.items() for number in numbers]

    def inventory(self, project=None, **kwargs):
        project = project or SimpleNamespace(name="ameesh", target="main",
                                             migrations=MIG + "/", github="manaty/ameesh")
        real = packet_candidates.inventory

        # le banc #2404 fixe le projet ; on lui substitue celui du témoin
        def call(db, _project, **kw):
            return real(db, project, **kw)

        # l'origin du banc est un dépôt nu local : #4430 y lit manaty/ameesh
        with mock.patch.object(packet_candidates, "inventory", side_effect=call), \
                mock.patch.object(migrations, "origin_repo", return_value="manaty/ameesh"):
            return self.inventory_of(**kwargs)

    def collision(self, entry):
        return [c for c in entry["causes"] if c["code"] == "migration-collision"]

    def test_number_already_on_the_target_is_excluded(self):
        entry = self.by_id(self.inventory())[4174]
        self.assertFalse(entry["ready"])
        (cause,) = self.collision(entry)
        self.assertEqual(cause["case"], "target")
        self.assertEqual(cause["number"], 505)
        self.assertIn("0505_landed.sql", cause["detail"])
        self.assertIn("0505_pending_decisions.sql", cause["detail"])

    def test_shared_number_keeps_the_reservation_holder(self):
        payload = self.inventory()
        entries = self.by_id(payload)
        self.assertIn(4351, payload["ready"])
        self.assertEqual(self.collision(entries[4351]), [])
        self.assertNotIn(4352, payload["ready"])
        (cause,) = self.collision(entries[4352])
        self.assertEqual((cause["case"], cause["with_lot"]), ("candidates", 4351))
        self.assertIn("holds the active reservation", cause["detail"])
        self.assertIn((506, "manaty/ameesh"), self.registry.asked)

    def test_shared_number_without_holder_keeps_the_first_in_ready_order(self):
        self.holders.clear()
        payload = self.inventory()
        self.assertIn(4352, payload["ready"])
        (cause,) = self.collision(self.by_id(payload)[4351])
        self.assertEqual(cause["with_lot"], 4352)
        self.assertIn("first in ready order", cause["detail"])

    def test_unreadable_registry_puts_both_lots_in_error(self):
        def broken(number, repo):
            raise RuntimeError("offline")
        self.registry.migration_holder = broken
        payload = self.inventory()
        for lot in (4351, 4352):
            self.assertNotIn(lot, payload["ready"])
            self.assertIn("offline", self.by_id(payload)[lot]["error"])
        self.assertEqual(sorted(e["work_item"] for e in payload["errors_by_lot"]),
                         [4351, 4352])

    def test_nominal_and_plain_lots_stay_ready(self):
        payload = self.inventory()
        entries = self.by_id(payload)
        self.assertIn(4400, payload["ready"])
        self.assertEqual(entries[4400]["migrations"], {"507": [MIG + "/0507_fine.sql"]})
        self.assertIn(4401, payload["ready"])
        self.assertEqual(entries[4401]["migrations"], {})

    def test_an_excluded_lot_does_not_arbitrate(self):
        # le détenteur bloqué par un prérequis ne retient pas l'autre lot,
        # tant que celui-ci est couvert par sa propre réservation active
        self.store.work_dependencies.for_items.return_value = [
            {"work_item_id": 4351, "blocker_id": 1, "blocker_state": "intake",
             "blocking": True}]
        payload = self.inventory()
        self.assertIn(4352, payload["ready"])
        self.assertEqual(self.collision(self.by_id(payload)[4352]), [])
        # sans réservation, le numéro tenu par le lot bloqué l'écarte (#4430)
        self.reservations[4352] = []
        entry = self.by_id(self.inventory())[4352]
        self.assertFalse(entry["ready"])
        (cause,) = self.collision(entry)
        self.assertIn("not covered by an active reservation of lot #4352", cause["detail"])

    def test_single_candidate_without_its_reservation_is_excluded(self):
        # témoin de dev3 (#160568) : candidat seul, numéro tenu par un autre
        # lot ouvert hors des prêts, ou par personne
        self.reservations = {4174: [505], 4352: [506], 4400: []}
        self.store.work_dependencies.for_items.return_value = [
            {"work_item_id": 4352, "blocker_id": 1, "blocker_state": "intake",
             "blocking": True}]
        payload = self.inventory()
        entries = self.by_id(payload)
        for lot in (4351, 4400):
            self.assertNotIn(lot, payload["ready"])
            (cause,) = self.collision(entries[lot])
            self.assertIn("not covered by an active reservation of lot #%d" % lot,
                          cause["detail"])
        self.assertIn(4401, payload["ready"])

    def test_project_without_migrations_is_not_read(self):
        payload = self.inventory(SimpleNamespace(name="ameesh", target="main"))
        self.assertIn(4174, payload["ready"])
        self.assertIn(4352, payload["ready"])
        self.assertEqual(self.registry.asked, [])

    def test_reason_in_text_and_json(self):
        payload = self.inventory()
        text = candidates_script.render(payload)
        self.assertIn("migration-collision: migration %s/0506_pending.sql: number 506 "
                      "also added by #4351" % MIG, text)
        self.assertIn("number already on main", text)
        data = json.loads(json.dumps(payload, default=str))
        codes = [c["code"] for c in self.by_id(data)[4174]["causes"]]
        self.assertEqual(codes, ["migration-collision"])


if __name__ == "__main__":
    unittest.main()
