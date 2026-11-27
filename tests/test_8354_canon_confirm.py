# SPDX-License-Identifier: AGPL-3.0-only
"""#8354 (manaty/ameesh#6596) : `ameesh canon confirm --commit SHA`.

Un canon inchangé rafraîchit la seule fraîcheur (`checked_at`) sans charger
ni valider de fiche ; un commit différent, inconnu, une source avancée, un
état non `ok` ou une application complète trop ancienne sont refusés sans
rien écrire. Témoin clé : une invalidation concurrente au même commit garde
la porte fermée.

Classe sensible (canon) : avis de deux autres familles.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import types
from unittest import mock

from ameesh import canon as canon_mod
from ameesh import canon_confirm, placement, storage

from .support import PgTestCase

HOST = "host8354"


def _git(repo: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, *args], check=True, capture_output=True, text=True,
        env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                 GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"),
    ).stdout.strip()


class ConfirmTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM canon_state")
        self.repo = os.path.join(self.tmp, "canon8354")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        self.sha = self._commit("one")
        self.hcfg = types.SimpleNamespace(canon=self.repo, canon_ref="",
                                         canon_untrusted=False)
        self.store = storage.of(self.db).canon
        self._record(self.sha)
        # une fraîcheur périmée : seule la confirmation peut la rendre
        self.db.execute("UPDATE canon_state SET checked_at = now() - interval '10 minutes'")

    def _commit(self, text: str) -> str:
        with open(os.path.join(self.repo, "f.txt"), "w") as fh:
            fh.write(text)
        _git(self.repo, "add", "f.txt")
        _git(self.repo, "commit", "-q", "-m", text)
        return _git(self.repo, "rev-parse", "HEAD")

    def _record(self, sha: str, key: str = "", root: str | None = None) -> None:
        self.store.record_state(HOST, "ok", root=root or self.repo,
                                source="root @ %s (main)" % sha[:12], commit=sha,
                                good=True, diagnostic="", canon=key)

    def _state(self, key: str = "") -> dict:
        return self.store.state(HOST, key)

    def test_unchanged_canon_refreshes_only_checked_at(self):
        before = self._state()
        self.assertFalse(placement.canon_fresh(before)[0])
        with mock.patch.object(canon_mod, "load", side_effect=AssertionError("loaded")), \
                mock.patch.object(canon_mod, "validate", side_effect=AssertionError("validated")):
            done = canon_confirm.confirm(self.db, self.hcfg, HOST, self.sha)
        self.assertTrue(done.ok, done)
        after = self._state()
        self.assertTrue(placement.canon_fresh(after)[0])
        self.assertGreater(after["checked_ts"], before["checked_ts"])
        for column in ("status", "root", "source", "last_good_commit", "last_good_ts",
                       "diagnostic", "process_commit", "canon_id"):
            self.assertEqual(after[column], before[column], column)

    def _refused(self, code: str, commit: str | None = None, **kw) -> None:
        before = self._state()
        done = canon_confirm.confirm(self.db, self.hcfg, HOST, commit or self.sha, **kw)
        self.assertFalse(done.ok)
        self.assertEqual(done.code, code, done.reason)
        self.assertEqual(self._state(), before)

    def test_other_or_malformed_commit_is_refused(self):
        self._refused("commit-mismatch", "0" * 40)
        self._refused("invalid-commit", self.sha[:12])

    def test_non_ok_state_is_refused(self):
        self.db.execute("UPDATE canon_state SET status = 'invalid'")
        self._refused("not-ok")

    def test_moved_source_is_refused(self):
        self._commit("two")
        self._refused("source-moved")

    def test_old_full_apply_is_refused(self):
        self.db.execute("UPDATE canon_state SET last_good_at = now() - interval '2 hours'")
        self._refused("too-old")

    def test_unconfigured_root_is_refused(self):
        self.hcfg.canon = os.path.join(self.tmp, "elsewhere")
        self._refused("not-configured")

    def test_untrusted_canon_is_refused(self):
        self.hcfg.canon_untrusted = True
        self._refused("untrusted")

    def test_named_canon_uses_its_own_row(self):
        other = os.path.join(self.tmp, "second")
        _git(self.tmp, "clone", "-q", self.repo, other)
        from ameesh.config import CanonEntry
        self.hcfg.canon_entries = (CanonEntry(self.repo), CanonEntry(other))
        self._record(self.sha, key="second", root=other)
        self.db.execute("UPDATE canon_state SET checked_at = now() - interval '10 minutes'")
        done = canon_confirm.confirm(self.db, self.hcfg, HOST, self.sha, canon="second")
        self.assertTrue(done.ok, done)
        self.assertTrue(placement.canon_fresh(self._state("second"))[0])
        self.assertFalse(placement.canon_fresh(self._state(""))[0])

    def test_concurrent_invalidation_at_same_commit_keeps_the_gate_closed(self):
        holder = self.connect()
        self.addCleanup(holder.close)
        results = []
        with holder.transaction() as tx:
            tx.execute("UPDATE canon_state SET status = 'invalid', checked_at = now() "
                       "WHERE host = %s", (HOST,))
            worker_db = self.connect()
            self.addCleanup(worker_db.close)
            thread = threading.Thread(target=lambda: results.append(
                storage.of(worker_db).canon.confirm_fresh(
                    HOST, "", commit=self.sha, root=self.repo, max_span_s=3600)))
            thread.start()
            time.sleep(0.3)         # la confirmation attend le verrou de la ligne
        thread.join(30)
        self.assertEqual(results, [None])
        state = self._state()
        self.assertEqual(state["status"], "invalid")
        self.assertFalse(placement.canon_fresh(state)[0])

    def test_cli_confirm(self):
        env = self.env(AMEESH_CANON=self.repo, AMEESH_HOST=HOST)
        proc = self.mesh("canon", "confirm", "--commit", self.sha, "--json", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)["ok"])
        proc = self.mesh("canon", "confirm", "--commit", "f" * 40, env=env)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("commit-mismatch", proc.stderr)
