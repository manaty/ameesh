# SPDX-License-Identifier: AGPL-3.0-only
"""L113 : dépôt de travail de l'exécuteur médié (sans base).

Une « forge » locale (dépôt nu), le serveur (`work_repo.WorkDepot`) et
l'appareil (`work_repo.checkout`, `push`, `wipe`) reliés par un faux transport
qui appelle la route du serveur :

* l'archive reproduit l'arbre exact (modes, lien symbolique, fichier
  `export-ignore`), et le commit de base est le même des deux côtés ;
* les commits de l'appareil arrivent sur `agent/<nom>` avec le même arbre,
  le même auteur et le même message ; envois successifs en avance rapide ;
* reprise sous un nouveau bail depuis la branche de l'agent ;
* refus : bail d'un autre exécuteur, base inconnue, commit de fusion,
  sous-module ; le dossier est effacé en fin de bail.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ameesh.db import DbError
from ameesh.mediated_executor import contract as C
from ameesh.mediated_executor import work_repo
from ameesh.mediated_executor.contract import Fence

EXEC = "7f3a9c2e4b1d6058"
OWNER = "exec:%s:anna-portable:4121" % EXEC


def sh(cwd, *args, env=None):
    full = dict(os.environ, GIT_AUTHOR_NAME="dev", GIT_AUTHOR_EMAIL="dev@x",
                GIT_COMMITTER_NAME="dev", GIT_COMMITTER_EMAIL="dev@x", **(env or {}))
    return subprocess.run(["git", "-c", "init.defaultBranch=main", *args], cwd=cwd, env=full,
                          check=True, capture_output=True, text=True).stdout.strip()


class _Transport:
    """Le transport de l'appareil, branché directement sur la route du serveur."""

    def __init__(self, route, principal):
        self.route, self.principal = route, principal

    def _call(self, method, fence, headers, body=b""):
        h = {C.HDR_LEASE_OWNER: fence.owner, C.HDR_LEASE_EPOCH: str(fence.epoch)}
        h.update(headers)
        resp = self.route(None, self.principal, method, "/work/%s/bundle" % fence.agent, h, body)
        if resp.status != 200:
            from ameesh.db import DbError
            err = DbError(str(resp.body))
            err.code = (resp.body or {}).get("error")
            raise err
        return resp

    def get_work(self, fence):
        resp = self._call("GET", fence, {})
        return resp.raw, resp.headers

    def put_work(self, fence, base, bundle):
        return self._call("POST", fence, {C.HDR_BASE: base}, bundle).body


@unittest.skipUnless(shutil.which("git"), "git requis")
class DepotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="l113-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # la forge : un dépôt nu, alimenté depuis un clone
        self.forge = os.path.join(self.tmp, "forge.git")
        sh(self.tmp, "init", "-q", "--bare", self.forge)
        src = os.path.join(self.tmp, "src")
        sh(self.tmp, "clone", "-q", self.forge, src)
        os.makedirs(os.path.join(src, "bin"))
        with open(os.path.join(src, "README.md"), "w") as fh:
            fh.write("site\n")
        with open(os.path.join(src, "bin", "go.sh"), "w") as fh:
            fh.write("#!/bin/sh\necho go\n")
        os.chmod(os.path.join(src, "bin", "go.sh"), 0o755)
        os.symlink("README.md", os.path.join(src, "LISEZMOI"))
        with open(os.path.join(src, ".gitattributes"), "w") as fh:
            fh.write("secret.txt export-ignore\n")
        with open(os.path.join(src, ".gitignore"), "w") as fh:
            fh.write("*.log\n")
        with open(os.path.join(src, "secret.txt"), "w") as fh:
            fh.write("suivi, mais export-ignore\n")
        with open(os.path.join(src, "garde.log"), "w") as fh:
            fh.write("suivi malgré .gitignore\n")
        sh(src, "add", "-A", "-f")
        sh(src, "commit", "-q", "-m", "départ")
        sh(src, "push", "-q", "origin", "HEAD:main")
        self.start = sh(src, "rev-parse", "HEAD")
        self.allowed = {(OWNER, 42)}
        self.route = work_repo.WorkDepot(
            work_repo.RepoMap({"default": {"repo": self.forge, "base": "main"}}),
            os.path.join(self.tmp, "cache"),
            lambda p, agent, owner, epoch: (owner, epoch) in self.allowed)
        self.principal = types.SimpleNamespace(kind="executor", executor_id=EXEC,
                                               host="anna-portable")
        self.transport = _Transport(self.route, self.principal)
        self.fence = Fence("inge-front", OWNER, 42)
        self.work = os.path.join(self.tmp, "vm", "work", "inge-front", "42")

    def _commit(self, name, text, message):
        with open(os.path.join(self.work, name), "w") as fh:
            fh.write(text)
        sh(self.work, "add", name)
        subprocess.run(["git", "commit", "-q", "-m", message], cwd=self.work, check=True,
                       capture_output=True)

    def test_aller_retour(self):
        meta = work_repo.checkout(self.transport, self.fence, self.work)
        self.assertEqual(meta["commit"], self.start)
        self.assertEqual(meta["branch"], "agent/inge-front")
        self.assertTrue(os.access(os.path.join(self.work, "bin", "go.sh"), os.X_OK))
        self.assertEqual(os.readlink(os.path.join(self.work, "LISEZMOI")), "README.md")
        self.assertTrue(os.path.exists(os.path.join(self.work, "secret.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.work, "garde.log")))
        # l'arbre de l'appareil est celui du serveur
        self.assertEqual(sh(self.work, "rev-parse", "HEAD^{tree}"),
                         sh(self.forge, "rev-parse", self.start + "^{tree}"))
        self.assertIsNone(work_repo.push(self.transport, self.fence, self.work))  # rien
        self._commit("page.html", "<h1>accueil</h1>\n", "Page d'accueil\n\nCorps du message.")
        reply = work_repo.push(self.transport, self.fence, self.work)
        self.assertEqual((reply["schema"], reply["commits"]), (C.SCHEMA_BUNDLE, 1))
        head = sh(self.forge, "rev-parse", "refs/heads/agent/inge-front")
        self.assertEqual(head, reply["commit"])
        self.assertEqual(sh(self.forge, "rev-parse", head + "^"), self.start)
        self.assertEqual(sh(self.forge, "log", "-1", "--format=%an|%B", head),
                         "inge-front|Page d'accueil\n\nCorps du message.")
        self.assertIn(EXEC, sh(self.forge, "log", "-1", "--format=%cn", head))
        self.assertEqual(sh(self.forge, "rev-parse", head + "^{tree}"),
                         sh(self.work, "rev-parse", "HEAD^{tree}"))
        # second envoi : avance rapide
        self._commit("page.html", "<h1>accueil v2</h1>\n", "v2")
        with open(os.path.join(self.work, "brouillon.txt"), "w") as fh:
            fh.write("non commité\n")
        reply = work_repo.push(self.transport, self.fence, self.work, commit_pending=True)
        self.assertEqual(reply["commits"], 2)
        head2 = sh(self.forge, "rev-parse", "refs/heads/agent/inge-front")
        self.assertEqual(sh(self.forge, "rev-parse", head2 + "~2"), head)
        self.assertIn("brouillon.txt", sh(self.forge, "ls-tree", "--name-only", head2))
        work_repo.wipe(self.work)
        self.assertFalse(os.path.exists(self.work))
        # nouveau bail : reprise depuis la branche de l'agent
        self.allowed.add((OWNER, 43))
        fence2 = Fence("inge-front", OWNER, 43)
        meta = work_repo.checkout(self.transport, fence2, self.work)
        self.assertEqual(meta["commit"], head2)
        with open(os.path.join(self.work, "page.html")) as fh:
            self.assertEqual(fh.read(), "<h1>accueil v2</h1>\n")

    def test_refus(self):
        other = Fence("inge-front", "exec:autre:h:1", 42)
        with self.assertRaises(Exception) as ctx:
            work_repo.checkout(self.transport, other, self.work)
        self.assertEqual(ctx.exception.code, "forbidden_scope")
        work_repo.checkout(self.transport, self.fence, self.work)
        resp = self.route(None, self.principal, "POST", "/work/inge-front/bundle",
                          {C.HDR_LEASE_OWNER: OWNER, C.HDR_LEASE_EPOCH: "42",
                           C.HDR_BASE: "0" * 40}, b"x")
        self.assertEqual(resp.body["error"], "idempotency_mismatch")
        session = types.SimpleNamespace(kind="session", executor_id=EXEC, host="anna-portable")
        resp = self.route(None, session, "GET", "/work/inge-front/bundle",
                          {C.HDR_LEASE_OWNER: OWNER, C.HDR_LEASE_EPOCH: "42"}, b"")
        self.assertEqual(resp.body["error"], "token_invalid")
        # un commit de fusion est refusé
        base = sh(self.work, "rev-parse", "HEAD")
        sh(self.work, "checkout", "-q", "-b", "a")
        self._commit("a.txt", "a\n", "a")
        sh(self.work, "checkout", "-q", "work")
        self._commit("b.txt", "b\n", "b")
        sh(self.work, "merge", "-q", "--no-edit", "a")
        with self.assertRaises(Exception) as ctx:
            work_repo.push(self.transport, self.fence, self.work)
        self.assertEqual(ctx.exception.code, "bad_args")
        self.assertEqual(work_repo._read_meta(self.work)["pushed"], base)

    def test_publication_rechecks_and_locks_after_git_preparation(self):
        # Exercise the production checker without starting a database. The
        # fake transaction tracks both row locks at the actual forge push.
        class Connection:
            live = True
            revoked = False
            active = False
            executor_locked = False
            agent_locked = False

            @contextlib.contextmanager
            def transaction(conn):
                conn.active = True
                try:
                    yield conn
                finally:
                    conn.active = conn.executor_locked = conn.agent_locked = False

            def query(conn, sql, params):
                if "JOIN executors" in sql:
                    return [{"ok": 1}] if conn.live and not conn.revoked else []
                if "FROM executors" in sql:
                    conn.executor_locked = conn.active and "FOR SHARE" in sql
                    return [] if conn.revoked else [{"ok": 1}]
                if "FOR UPDATE" in sql:
                    conn.agent_locked = conn.active
                return [{"lease_owner": OWNER, "lease_epoch": 42,
                         "host": "anna-portable", "live": conn.live}]

        conn = Connection()
        @contextlib.contextmanager
        def connection():
            yield conn
        self.route.lease_check = work_repo.lease_checker(types.SimpleNamespace(connection=connection))
        work_repo.checkout(self.transport, self.fence, self.work)
        self._commit("page.html", "test\n", "page")
        real_replay = self.route._replay
        real_git = work_repo.git
        pushes = []

        def guarded_git(repo, *args, **kw):
            if args[0] == "push":
                self.assertTrue(conn.active and conn.executor_locked and conn.agent_locked)
                pushes.append(args)
            return real_git(repo, *args, **kw)

        for invalidation in ("expired", "revoked", None):
            conn.live, conn.revoked = True, False
            def replay(*args):
                result = real_replay(*args)
                conn.live = invalidation != "expired"
                conn.revoked = invalidation == "revoked"
                return result
            with self.subTest(invalidation=invalidation), \
                    mock.patch.object(self.route, "_replay", side_effect=replay), \
                    mock.patch.object(work_repo, "git", side_effect=guarded_git):
                if invalidation:
                    with self.assertRaises(Exception) as ctx:
                        work_repo.push(self.transport, self.fence, self.work)
                    self.assertEqual(ctx.exception.code, "forbidden_scope")
                    self.assertEqual(pushes, [])
                    self.assertIsNone(self.route._rev(self.forge, "refs/heads/agent/inge-front"))
                else:
                    work_repo.push(self.transport, self.fence, self.work)
                    self.assertEqual(len(pushes), 1)

    def test_publication_refuses_expiry_while_waiting_for_agent_lock(self):
        # Simulate PostgreSQL evaluating the old SELECT target before its
        # LockRows wait, then acquiring the unchanged row after lease expiry.
        class Connection:
            expired = False

            @contextlib.contextmanager
            def transaction(conn):
                yield conn

            def query(conn, sql, params):
                if "JOIN executors" in sql or "FROM executors" in sql:
                    return [{"ok": 1}]
                if "FOR UPDATE" in sql:
                    live_before_lock = not conn.expired
                    conn.expired = True
                    return [{"lease_owner": OWNER, "lease_epoch": 42,
                             "host": "anna-portable", "live": live_before_lock}]
                return [{"live": not conn.expired}]

        conn = Connection()
        @contextlib.contextmanager
        def connection():
            yield conn
        self.route.lease_check = work_repo.lease_checker(types.SimpleNamespace(connection=connection))
        work_repo.checkout(self.transport, self.fence, self.work)
        self._commit("page.html", "test\n", "page")
        real_git = work_repo.git
        def no_push(repo, *args, **kw):
            self.assertNotEqual(args[0], "push", "publication after lease expiry")
            return real_git(repo, *args, **kw)
        with mock.patch.object(work_repo, "git", side_effect=no_push):
            with self.assertRaises(DbError) as ctx:
                work_repo.push(self.transport, self.fence, self.work)
        self.assertEqual(ctx.exception.code, "forbidden_scope")
        self.assertIsNone(self.route._rev(self.forge, "refs/heads/agent/inge-front"))

    def test_receipts_and_endpoint_mappings_remain_bounded(self):
        work_repo.checkout(self.transport, self.fence, self.work)
        put = self.transport.put_work
        sent = []
        def capture(fence, base, bundle):
            reply = put(fence, base, bundle)
            sent.append((base, bundle, reply))
            return reply
        with mock.patch.object(work_repo, "MAX_UPLOAD_RECEIPTS", 2), \
                mock.patch.object(self.transport, "put_work", side_effect=capture):
            for upload in range(4):
                # More commits than the receipt window: no per-commit state.
                for commit in range(3):
                    self._commit("page.html", "%d-%d\n" % (upload, commit), "page")
                work_repo.push(self.transport, self.fence, self.work)
                state = self.route._load(self.fence.agent)
                self.assertLessEqual(len(state["receipts"]), 2)
                self.assertLessEqual(len(state["map"]), 4)
            with mock.patch.object(self.route, "_replay", side_effect=AssertionError("duplicate")):
                for base, bundle, reply in sent[-2:]:
                    self.assertEqual(put(self.fence, base, bundle), reply)
            with self.assertRaises(Exception) as ctx:
                put(self.fence, sent[0][0], sent[0][1])
            self.assertEqual(ctx.exception.code, "idempotency_mismatch")
            # Lost latest reply followed by new work: its old base still has
            # a receipt, but none of the intermediate prefix commit mappings.
            work_repo._write_meta(self.work, dict(work_repo._read_meta(self.work),
                                                  pushed=sent[-1][0]))
            published = sh(self.forge, "rev-parse", "refs/heads/agent/inge-front")
            self._commit("page.html", "continuation\n", "continuation")
            work_repo.push(self.transport, self.fence, self.work)
            current = sh(self.forge, "rev-parse", "refs/heads/agent/inge-front")
            self.assertEqual(sh(self.forge, "rev-parse", current + "^"), published)
            state = self.route._load(self.fence.agent)
            self.assertEqual(len(state["receipts"]), 2)
            self.assertLessEqual(len(state["map"]), 4)

    def test_lost_upload_reply_is_replayable_and_later_commits_append(self):
        work_repo.checkout(self.transport, self.fence, self.work)
        base = work_repo._read_meta(self.work)["pushed"]
        self._commit("page.html", "v1\n", "v1")
        put = self.transport.put_work
        sent = []

        def lost_reply(fence, old_base, bundle):
            reply = put(fence, old_base, bundle)
            sent.append((old_base, bundle, reply))
            raise OSError("reply lost after forge push")

        with mock.patch.object(self.transport, "put_work", side_effect=lost_reply):
            with self.assertRaises(OSError):
                work_repo.push(self.transport, self.fence, self.work)
        self.assertEqual(work_repo._read_meta(self.work)["pushed"], base)
        head = sh(self.forge, "rev-parse", "refs/heads/agent/inge-front")
        # Receipts survive a server route restart. An identical retry must not
        # publish again; a later bundle from the unacknowledged base must reuse
        # its published prefix rather than duplicate it.
        self.route = work_repo.WorkDepot(self.route.repos, self.route.cache, self.route.lease_check)
        self.transport.route = self.route
        with mock.patch.object(self.route, "_replay", side_effect=AssertionError("replayed twice")):
            self.assertEqual(put(self.fence, sent[0][0], sent[0][1]), sent[0][2])
        self._commit("page.html", "v2\n", "v2")
        reply = work_repo.push(self.transport, self.fence, self.work)
        head2 = sh(self.forge, "rev-parse", "refs/heads/agent/inge-front")
        self.assertEqual(sh(self.forge, "rev-parse", head2 + "^"), head)
        self.assertEqual(reply["device_head"], sh(self.work, "rev-parse", "HEAD"))
        self.assertEqual(put(self.fence, sent[0][0], sent[0][1]), sent[0][2])
        self.assertEqual(sh(self.forge, "rev-parse", "refs/heads/agent/inge-front"), head2)
        self._commit("page.html", "v3\n", "v3")
        work_repo.push(self.transport, self.fence, self.work)
        head3 = sh(self.forge, "rev-parse", "refs/heads/agent/inge-front")
        self.assertEqual(sh(self.forge, "rev-parse", head3 + "^"), head2)

    def test_archive_hostile_refusee(self):
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo("../evasion")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        os.makedirs(self.work)
        with self.assertRaises(work_repo.DepotError):
            work_repo._safe_extract(buf.getvalue(), self.work)


if __name__ == "__main__":
    unittest.main()
