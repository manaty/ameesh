# SPDX-License-Identifier: AGPL-3.0-only
"""SQL witnesses for train intent CAS and the shared landing reservation."""
import types
import unittest
from unittest import mock

from ameesh import storage, train_store
from ameesh.train import TrainError
from .support import PgTestCase


class TargetIdentityTest(unittest.TestCase):
    def test_alias_cannot_create_an_independent_lock_for_the_same_remote_branch(self):
        for alias in ("origin/main", "refs/remotes/origin/main", "refs/heads/main"):
            with self.subTest(alias=alias), mock.patch.object(storage, "of") as connect:
                with self.assertRaisesRegex(TrainError, "canonical target"):
                    train_store.Ledger(object(), types.SimpleNamespace(name="shared"),
                                       alias, "runner")
                connect.assert_not_called()


class TrainStorageTest(PgTestCase):
    def test_train_and_single_lot_land_share_reservation_with_distinct_owner(self):
        ledger = train_store.Ledger(self.db, types.SimpleNamespace(name="shared"),
                                    "main", "runner")
        epoch = ledger.acquire(None)
        self.assertIsNone(storage.of(self.db).work.land_lock("shared:main", "runner", 60))
        doc = ledger.save({"phase": "ci-intent"}, 0, epoch)
        self.assertEqual(ledger.load(), doc)
        ledger.release(epoch)
        self.assertIsNotNone(storage.of(self.db).work.land_lock("shared:main", "runner", 60))

    def test_revision_and_epoch_survive_holder_restart(self):
        ops = storage.of(self.db).work
        epoch = ops.land_lock("train:main", "one", 60)
        self.assertIsNone(ops.train_read("train:main"))
        doc = ops.train_save("train:main", {"phase": "ci-intent"}, revision=0,
                              owner="one", epoch=epoch)
        self.assertEqual(doc["revision"], 1)
        self.assertIsNone(ops.train_save("train:main", {}, revision=0, owner="one", epoch=epoch))
        self.assertIsNone(ops.train_save("train:main", {}, revision=2, owner="one", epoch=epoch))
        self.assertIsNone(ops.land_lock("train:main", "two", 60))
        epoch2 = ops.land_lock("train:main", "one", 60)
        self.assertIsNone(ops.train_save("train:main", {}, revision=1, owner="one", epoch=epoch))
        self.assertEqual(ops.train_read("train:main"), doc)
        doc2 = ops.train_save("train:main", {"phase": "ci"}, revision=1,
                               owner="one", epoch=epoch2)
        self.assertEqual(doc2, {"phase": "ci", "revision": 2})

    def test_expired_lease_cannot_save_or_overwrite_intent(self):
        ops = storage.of(self.db).work
        epoch = ops.land_lock("expiry:main", "one", 60)
        ops.train_save("expiry:main", {"phase": "promoting"}, revision=0,
                        owner="one", epoch=epoch)
        self.db.execute("UPDATE merge_locks SET expires_at = clock_timestamp() - interval '1 second'"
                        " WHERE target = 'expiry:main'")
        self.assertIsNone(ops.train_save("expiry:main", {}, revision=1, owner="one", epoch=epoch))
        epoch2 = ops.land_lock("expiry:main", "two", 60)
        self.assertGreater(epoch2, epoch)
        self.assertIsNone(ops.train_save("expiry:main", {}, revision=1, owner="one", epoch=epoch))
        self.assertEqual(ops.train_read("expiry:main")["phase"], "promoting")
