# SPDX-License-Identifier: AGPL-3.0-only
"""#8340 (manaty/ameesh#6580) : `ameesh canon refuse`.

Un module qui a refusé le canon à un commit épinglé enregistre cet état
`invalid` avec un motif borné, sans import interne. L'état ferme la
réclamation des agents du canon ; une synchro réussie le remplace. Le motif
qui ressemble à un secret est refusé, jamais stocké, et l'appel est idempotent.

Classe sensible (canon, admission des workers) : avis d'une autre famille et
CI verte.
"""
from __future__ import annotations

import json
import os
import shutil
import types
from unittest import mock

from ameesh import canon as canon_mod
from ameesh import canon_refusal, canon_sync, placement, registry, storage
from ameesh.config import CanonEntry

from .support import PgTestCase
from .test_canon import EXAMPLE

HOST = "atelier"
OLD = "a" * 40
NEW = "b" * 40


class RefusalTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM canon_state")
        self.root = os.path.realpath(os.path.join(self.tmp, "canon8340"))
        shutil.copytree(EXAMPLE, self.root)
        self.cfg = types.SimpleNamespace(
            host=HOST, canon=self.root, canon_ref="", canon_untrusted=True)
        self.store = storage.of(self.db).canon

    # -- helpers ---------------------------------------------------------
    def _canon(self):
        return canon_mod.load(self.root, untrusted=True)

    def _sync(self):
        return canon_sync.sync(self.db, self._canon(), HOST)

    def _refuse(self, commit=None, reason="package pin mismatch", **kw):
        return canon_refusal.refuse(self.db, self.cfg, commit, reason, **kw)

    def _state(self, key: str = "") -> dict | None:
        return self.store.state(HOST, key)

    def _seed_ok(self, commit: str = OLD) -> None:
        self.store.record_state(
            HOST, "ok", root=self.root, source="root @ %s (main)" % commit[:12],
            commit=commit, good=True, diagnostic="", canon="")

    # -- recording -------------------------------------------------------
    def test_refusal_records_the_commit_without_touching_last_good(self):
        self._seed_ok()
        before = self._state()
        result = self._refuse(NEW, "the source pin does not match the package")
        self.assertTrue(result.ok, result)
        self.assertTrue(result.written)
        self.assertEqual((result.status, result.commit), ("invalid", NEW))
        state = self._state()
        self.assertEqual(state["status"], "invalid")
        self.assertEqual(state["refused_commit"], NEW)
        self.assertEqual(state["last_good_commit"], OLD)
        self.assertEqual(state["last_good_ts"], before["last_good_ts"])
        self.assertEqual(state["diagnostic"],
                         canon_refusal.DIAGNOSTIC_PREFIX
                         + "the source pin does not match the package")
        self.assertEqual(os.path.realpath(state["root"]), self.root)
        self.assertFalse(placement.canon_fresh(state)[0])

    def test_an_unpinned_refusal_is_recorded_without_a_commit(self):
        result = self._refuse(None, "document synchronization admission unavailable")
        self.assertTrue(result.ok, result)
        state = self._state()
        self.assertEqual(state["status"], "invalid")
        self.assertIsNone(state["refused_commit"])
        self.assertEqual(state["diagnostic"],
                         canon_refusal.DIAGNOSTIC_PREFIX
                         + "document synchronization admission unavailable")

    def test_the_same_refusal_is_idempotent(self):
        first = self._refuse(NEW, "package pin mismatch")
        before = self._state()
        again = self._refuse(NEW, "package pin mismatch")
        self.assertTrue(first.written)
        self.assertTrue(again.ok)
        self.assertFalse(again.written)
        self.assertEqual(self._state(), before)
        # Un motif différent, ou un autre commit, réécrit l'état.
        changed = self._refuse(NEW, "another module reason")
        self.assertTrue(changed.written)
        self.assertIn("another module reason", self._state()["diagnostic"])
        other = self._refuse(OLD, "another module reason")
        self.assertTrue(other.written)
        self.assertEqual(self._state()["refused_commit"], OLD)

    def test_a_successful_sync_replaces_the_refusal(self):
        self._sync()
        self._refuse(NEW, "package pin mismatch")
        self.assertEqual(self._state()["status"], "invalid")
        report = self._sync()
        self.assertEqual(report.status, canon_sync.CANON_OK)
        state = self._state()
        self.assertEqual(state["status"], "ok")
        self.assertIsNone(state["refused_commit"])

    def test_a_losing_ok_cas_preserves_the_refused_commit(self):
        # #8340 (verdict B, P2) : un `ok` fondé sur une lecture périmée perd le
        # CAS ; le statut, le diagnostic et le commit refusé du gagnant restent
        # intacts (seul un `ok` qui gagne le CAS efface le refus).
        self._seed_ok(OLD)
        self._refuse(NEW, "package pin mismatch")
        stale = "c" * 40
        self.store.record_state(
            HOST, "ok", root=self.root, source="root @ %s (main)" % stale[:12],
            commit=stale, good=True, diagnostic="", canon="", expected=stale)
        state = self._state()
        self.assertEqual(state["status"], "invalid")
        self.assertEqual(state["refused_commit"], NEW)
        self.assertEqual(state["last_good_commit"], OLD)
        self.assertEqual(state["diagnostic"],
                         canon_refusal.DIAGNOSTIC_PREFIX + "package pin mismatch")

    def test_an_old_writer_ok_clears_the_refused_commit(self):
        # #8340 (verdict B, P2, N-1) : pendant la vague, un écrivain resté sur
        # la version précédente ne connaît pas `refused_commit` ; son `ok`
        # (forme de `record_state` à a81519f7) doit quand même laisser un état
        # `ok` sans commit refusé. L'invariant est tenu par le schéma (0613).
        self._refuse(NEW, "package pin mismatch")
        n1_ok = ("UPDATE canon_state SET status = 'ok', diagnostic = '', "
                 "last_good_commit = %s, last_good_at = now(), root = %s, "
                 "source = %s, checked_at = now() "
                 "WHERE host = %s AND canon = ''")
        self.previous_write(
            n1_ok, (OLD, self.root, "root @ %s (main)" % OLD[:12], HOST),
            added=["refused_commit"])
        state = self._state()
        self.assertEqual(state["status"], "ok")
        self.assertEqual(state["diagnostic"], "")
        self.assertEqual(state["last_good_commit"], OLD)
        self.assertIsNone(state["refused_commit"])

    def test_the_refusal_blocks_claims_until_a_new_sync(self):
        self._sync()
        self.assertTrue(registry.claimable(self.db, HOST, require_responsible=True))
        self._refuse(NEW, "package pin mismatch")
        self.assertEqual(registry.claimable(self.db, HOST, require_responsible=True), [])
        self._sync()
        self.assertTrue(registry.claimable(self.db, HOST, require_responsible=True))

    # -- governance ------------------------------------------------------
    def test_a_secret_or_unreadable_reason_is_refused_and_not_stored(self):
        for reason, code in (
                ("", "empty-reason"),
                ("   ", "empty-reason"),
                ("x" * (canon_refusal.MAX_REASON + 1), "reason-too-long"),
                ("bad\x00reason", "reason-unreadable"),
                ("password=hunter2", "reason-secret"),
                ("token ghp_" + "a" * 30, "reason-secret"),
                ("-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----",
                 "reason-secret")):
            with self.subTest(reason=reason[:30]):
                result = self._refuse(None, reason)
                self.assertFalse(result.ok)
                self.assertEqual(result.code, code)
                self.assertIsNone(self._state())

    def test_a_value_from_a_secret_environment_variable_is_refused(self):
        with mock.patch.dict(os.environ, {"MODULE_8340_TOKEN": "s3cr3t-value-8340"}):
            result = self._refuse(None, "the value s3cr3t-value-8340 leaked")
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "reason-secret")
        self.assertIsNone(self._state())

    def test_an_invalid_commit_is_refused(self):
        for commit in ("abc", "HEAD", "0" * 39, "z" * 40):
            with self.subTest(commit=commit):
                result = self._refuse(commit, "package pin mismatch")
                self.assertFalse(result.ok)
                self.assertEqual(result.code, "invalid-commit")
                self.assertIsNone(self._state())

    def test_an_unconfigured_canon_is_refused(self):
        result = self._refuse(None, "package pin mismatch",
                              canon=os.path.join(self.tmp, "elsewhere"))
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "unknown-canon")
        self.assertIsNone(self._state())

    def test_no_configured_canon_is_refused(self):
        self.cfg.canon = ""
        result = self._refuse(None, "package pin mismatch")
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "no-canon")

    def test_a_duplicate_canon_id_is_ambiguous(self):
        left = os.path.join(self.tmp, "left", "same")
        right = os.path.join(self.tmp, "right", "same")
        for root in (left, right):
            os.makedirs(os.path.dirname(root), exist_ok=True)
            shutil.copytree(EXAMPLE, root)
        self.cfg.canon_entries = (CanonEntry(left, "", True),
                                  CanonEntry(right, "", True))
        result = self._refuse(None, "package pin mismatch", canon="same")
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "ambiguous-canon")
        self.assertIsNone(self._state())

    def test_a_host_without_identity_is_refused(self):
        self.cfg.host = ""
        result = self._refuse(None, "package pin mismatch")
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "no-host")

    def test_a_second_configured_canon_is_written_under_its_own_key(self):
        self._seed_ok()
        other = os.path.join(self.tmp, "canon-other")
        shutil.copytree(EXAMPLE, other)
        self.cfg.canon_entries = (CanonEntry(self.root, "", True),
                                  CanonEntry(other, "", True))
        loaded = canon_mod.from_config(self.cfg, entry=self.cfg.canon_entries[1])
        result = self._refuse(None, "package pin mismatch", canon=other)
        self.assertTrue(result.ok, result)
        self.assertEqual(result.canon, loaded.id)
        self.assertEqual(self._state("")["status"], "ok")  # untouched default
        self.assertEqual(self._state(loaded.id)["status"], "invalid")

    # -- public CLI ------------------------------------------------------
    def _cli_env(self) -> dict:
        return self.env(AMEESH_CANON=self.root, AMEESH_CANON_UNTRUSTED="1",
                        AMEESH_HOST=HOST)

    def test_the_cli_records_a_refusal_and_canon_check_sees_it(self):
        proc = self.mesh("canon", "refuse", "--reason", "package pin mismatch",
                         "--commit", NEW, "--json", env=self._cli_env())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        data = json.loads(proc.stdout)
        self.assertTrue(data["ok"])
        self.assertTrue(data["written"])
        self.assertEqual((data["host"], data["canon"], data["commit"]),
                         (HOST, "", NEW))
        self.assertEqual(data["status"], "invalid")
        self.assertEqual(data["diagnostic"],
                         canon_refusal.DIAGNOSTIC_PREFIX + "package pin mismatch")
        # `canon check --json` lit l'état enregistré, avec le commit refusé.
        proc = self.mesh("canon", "check", "--json", env=self._cli_env())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        checked = json.loads(proc.stdout)
        self.assertEqual(checked["registry_state"]["status"], "invalid")
        self.assertEqual(checked["registry_state"]["refused_commit"], NEW)
        self.assertEqual(checked["registry_state"]["diagnostic"],
                         canon_refusal.DIAGNOSTIC_PREFIX + "package pin mismatch")
        # La vue humaine nomme le refus et le commit.
        proc = self.mesh("canon", "check", env=self._cli_env())
        self.assertIn("not claimable", proc.stdout)
        self.assertIn("refused commit " + NEW[:12], proc.stdout)

    def test_the_cli_refuses_a_secret_without_echoing_it(self):
        proc = self.mesh("canon", "refuse", "--reason", "password=hunter2",
                         "--json", env=self._cli_env())
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("hunter2", proc.stdout + proc.stderr)
        data = json.loads(proc.stdout)
        self.assertFalse(data["ok"])
        self.assertEqual(data["code"], "reason-secret")
        self.assertIsNone(self._state())

    def test_the_cli_refuses_a_foreign_canon_root(self):
        proc = self.mesh("canon", "refuse", "--reason", "package pin mismatch",
                         "--canon", os.path.join(self.tmp, "elsewhere"),
                         env=self._cli_env())
        self.assertEqual(proc.returncode, 1)
        self.assertIn("unknown-canon", proc.stderr)
        self.assertIsNone(self._state())
