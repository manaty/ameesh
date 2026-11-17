# SPDX-License-Identifier: AGPL-3.0-only
"""#7719 — un commit d'une session native liée reçoit un reçu de provenance.

Le 17/11, trois commits de #7705 et six de #7678/#7715 portaient l'identité
Git native `Codex (PC) <codex-pc@users.noreply.github.com>` : aucun reçu,
auteur absent du registre, `record_commit` refusait le committer (il exige
`<agent>@ameesh.invalid`) et `by_ancestry` ne retrouvait pas la session
native. La liaison `ameesh mail bind` (agent `veille`, harnais codex, hôte
pc-smichea, session 01a186ba-…) connaissait pourtant l'agent ET la famille.

Le reçu est écrit par la session liée, à la création (hooks, identité de
session dans l'environnement) ou à la poussée (`ameesh provenance record
--session`) : la liaison donne acteur/harnais/session, le commit doit être
postérieur à la liaison, porté par une ref locale, et son committer ne peut
pas être un autre agent enregistré. Une preuve déjà écrite pour une autre
identité est refusée, et un reçu de session dont la liaison n'existe pas est
tenu pour incohérent par la porte.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest

from ameesh import registry, review_authors, review_provenance as prov
from ameesh import session_bindings as sb, storage, work

from .support import PgTestCase, mechanics_canon

FAMILIES = ("claude", "codex", "deepseek")
CODEX = ("codex-pc", "codex-pc@users.noreply.github.com")


class SessionReceiptTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE session_bindings RESTART IDENTITY")
        self.lot = work.add(self.db, title="un lot d'une session native",
                            app="ameesh", actor="orchestrateur")

    # -- utilitaires ------------------------------------------------------
    def bind(self, *, agent="veille", session="s-veille", harness="codex",
             pid=None):
        return sb.bind(self.cfg, self.db, agent, session_id=session,
                       harness=harness, pid=pid, by="human:test")

    def repo(self, *, name="codex-pc", email=CODEX[1], committer=None,
             committer_email=None, date=None):
        path = tempfile.mkdtemp(prefix="ameesh-7719-", dir=self.tmp)
        subprocess.run(["git", "init", "-q", path], check=True,
                       capture_output=True)
        env = dict(os.environ,
                   GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email,
                   GIT_COMMITTER_NAME=committer or name,
                   GIT_COMMITTER_EMAIL=committer_email or email)
        if date:
            env["GIT_AUTHOR_DATE"] = date
            env["GIT_COMMITTER_DATE"] = date
        return path, env

    def commit(self, path, env, message="un", *, staged=False):
        if not staged:
            with open(os.path.join(path, "f.txt"), "a", encoding="utf-8") as fh:
                fh.write(message + "\n")
            subprocess.run(["git", "add", "f.txt"], cwd=path, check=True,
                           capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", message], cwd=path,
                       check=True, capture_output=True, env=env)
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path,
                                       text=True).strip()

    def record(self, head, path, *, session="s-veille", harness="codex"):
        return prov.record_session_commit(
            self.db, head, session=session, harness=harness,
            families=FAMILIES, cfg=self.cfg, cwd=path)

    def refused(self, head, path, **kwargs):
        with self.assertRaises(prov.ProvenanceError) as raised:
            self.record(head, path, **kwargs)
        return str(raised.exception)

    # -- la preuve --------------------------------------------------------
    def test_a_native_session_commit_gets_a_binding_receipt(self):
        """Témoin (1) : commit `codex-pc` d'une session liée, famille connue."""
        self.bind()
        path, env = self.repo()
        head = self.commit(path, env)
        row = self.record(head, path)
        self.assertTrue(row["recorded"])
        receipt = prov.receipt_for_commit(self.db, head)
        self.assertEqual(receipt["actor"], "veille")
        self.assertEqual(receipt["session"], "s-veille")
        self.assertEqual(receipt["turn"], "session:s-veille")
        self.assertEqual(receipt["harness"], "codex")
        # La porte lit la famille du harnais de la liaison ; le nom Git
        # `codex-pc` n'est ni deviné ni crédité.
        authors, unknown, _ = review_authors.reading(
            self.db, {"id": int(self.lot["id"]), "assignee": "veille",
                      "app": "ameesh"},
            None, [head], repo=path, families=FAMILIES, strict=True)
        self.assertEqual(unknown, [])
        self.assertIn({"name": "veille", "family": "codex"}, authors)

    def test_the_creation_hooks_accept_the_session_identity(self):
        """La paire de hooks lit `CODEX_SESSION_ID` et enregistre sans PID."""
        self.bind()
        path, env = self.repo()
        hook_env = {"CODEX_SESSION_ID": "s-veille", "AMEESH_HARNESS": "codex"}
        with open(os.path.join(path, "f.txt"), "w", encoding="utf-8") as fh:
            fh.write("x\n")
        subprocess.run(["git", "add", "f.txt"], cwd=path, check=True,
                       capture_output=True)
        intent = prov.prepare_commit_intent(path, environ=hook_env,
                                            db=self.db, cfg=self.cfg)
        self.assertIsNotNone(intent)
        head = self.commit(path, env, "création native", staged=True)
        creation = prov.prepare_commit_creation(path, environ=hook_env,
                                                db=self.db, cfg=self.cfg)
        self.assertIsNotNone(creation)
        ident = prov.capture(hook_env, families=FAMILIES, db=self.db,
                             cfg=self.cfg)
        self.assertEqual(ident["actor"], "veille")
        self.assertEqual(ident["session"], "s-veille")
        self.assertEqual(ident["turn"], "session:s-veille")
        prov.record_commit(self.db, head, identity=ident, cwd=path)
        receipt = prov.receipt_for_commit(self.db, head)
        self.assertEqual(receipt["actor"], "veille")
        self.assertEqual(receipt["harness"], "codex")

    def test_a_commit_before_the_binding_is_refused(self):
        path, env = self.repo(date="2026-11-16T00:00:00Z")
        head = self.commit(path, env, "avant la liaison")
        self.bind()
        self.assertIn("before its session binding",
                      self.refused(head, path))

    def test_a_commit_of_another_registered_agent_is_refused(self):
        registry.upsert(self.db, "bureau", harness="codex",
                        host=self.cfg.host, mode="externe")
        self.bind(agent="veille")
        path, env = self.repo(name="bureau", email="bureau@ameesh.invalid")
        head = self.commit(path, env)
        message = self.refused(head, path)
        self.assertIn("committed by bureau", message)
        self.assertIn("not by veille", message)
        self.assertIsNone(prov.receipt_for_commit(self.db, head))

    def test_a_commit_not_carried_by_a_ref_is_refused(self):
        self.bind()
        path, env = self.repo()
        self.commit(path, env)
        tree = subprocess.check_output(["git", "write-tree"], cwd=path,
                                       text=True).strip()
        dangling = subprocess.check_output(
            ["git", "commit-tree", tree, "-m", "objet pendant"], cwd=path,
            text=True, env=env).strip()
        self.assertIn("not carried by a local ref",
                      self.refused(dangling, path))

    def test_a_revoked_binding_proves_nothing(self):
        self.bind()
        path, env = self.repo()
        head = self.commit(path, env)
        sb.unbind(self.cfg, self.db, session_id="s-veille", harness="codex",
                  by="human:test")
        self.assertIn("no active session binding", self.refused(head, path))

    def test_a_pid_binding_is_not_bypassed_by_the_explicit_path(self):
        self.bind(pid=999999)
        path, env = self.repo()
        head = self.commit(path, env)
        self.assertIn("not its descendant", self.refused(head, path))

    def test_the_ambient_identity_does_not_bypass_a_pid_binding(self):
        """B1 #7719 : un PID lié n'est pas revendiqué par la seule variable."""
        self.bind(pid=999999)
        with self.assertRaises(prov.ProvenanceError) as raised:
            prov.capture({"CODEX_SESSION_ID": "s-veille",
                          "AMEESH_HARNESS": "codex"},
                         families=FAMILIES, db=self.db, cfg=self.cfg)
        self.assertIn("session binding", str(raised.exception))

    def test_the_ambient_identity_accepts_its_own_pid_binding(self):
        """La garde B1 ne casse pas la session dont le PID est bien ancêtre."""
        self.bind(pid=os.getpid())
        ident = prov.capture({"CODEX_SESSION_ID": "s-veille",
                              "AMEESH_HARNESS": "codex"},
                             families=FAMILIES, db=self.db, cfg=self.cfg)
        self.assertEqual(ident["actor"], "veille")
        self.assertEqual(ident["source"], "session")

    def test_the_hooks_do_not_record_a_commit_before_the_binding(self):
        """B1 #7719 : la voie des hooks borne aussi la date à la liaison."""
        self.bind()
        path, env = self.repo(date="2026-11-16T00:00:00Z")
        hook_env = {"CODEX_SESSION_ID": "s-veille", "AMEESH_HARNESS": "codex"}
        with open(os.path.join(path, "f.txt"), "w", encoding="utf-8") as fh:
            fh.write("x\n")
        subprocess.run(["git", "add", "f.txt"], cwd=path, check=True,
                       capture_output=True)
        self.assertIsNotNone(prov.prepare_commit_intent(
            path, environ=hook_env, db=self.db, cfg=self.cfg))
        head = self.commit(path, env, "avant la liaison", staged=True)
        self.assertIsNotNone(prov.prepare_commit_creation(
            path, environ=hook_env, db=self.db, cfg=self.cfg))
        ident = prov.capture(hook_env, families=FAMILIES, db=self.db,
                             cfg=self.cfg)
        with self.assertRaises(prov.ProvenanceError) as raised:
            prov.record_commit(self.db, head, identity=ident, cwd=path)
        self.assertIn("before its session binding", str(raised.exception))
        self.assertIsNone(prov.receipt_for_commit(self.db, head))

    def test_another_binding_cannot_claim_an_already_received_commit(self):
        """Témoin (3) : le reçu d'une liaison ne se réattribue pas à une autre."""
        self.bind(agent="veille", session="s-veille")
        self.bind(agent="codex2", session="s-codex2")
        path, env = self.repo()
        head = self.commit(path, env)
        self.record(head, path, session="s-veille")
        message = self.refused(head, path, session="s-codex2")
        self.assertIn("already recorded for veille/session:s-veille", message)
        receipt = prov.receipt_for_commit(self.db, head)
        self.assertEqual(receipt["actor"], "veille")

    def test_a_session_receipt_without_its_binding_is_refused(self):
        """Témoin (3) : un reçu forgé, dont la session n'est pas liée."""
        self.bind()
        path, env = self.repo()
        head = self.commit(path, env)
        storage.of(self.db).provenance.add(
            "commit:" + head, head, "veille", "s-ghost", "session:s-ghost",
            "codex")
        with self.assertRaises(prov.ProvenanceError) as raised:
            prov.receipt_for_commit(self.db, head)
        self.assertIn("no binding links", str(raised.exception))

    def test_a_receipt_of_another_agents_binding_is_refused(self):
        self.bind(agent="bureau", session="s-bureau")
        path, env = self.repo()
        head = self.commit(path, env)
        storage.of(self.db).provenance.add(
            "commit:" + head, head, "veille", "s-bureau", "session:s-bureau",
            "codex")
        with self.assertRaises(prov.ProvenanceError) as raised:
            prov.receipt_for_commit(self.db, head)
        self.assertIn("no binding links", str(raised.exception))

    def test_an_unlinked_commit_stays_unknown(self):
        """Témoin (2) : sans liaison, le chemin refuse et rien n'est écrit."""
        path, env = self.repo()
        head = self.commit(path, env)
        self.assertIn("no active session binding", self.refused(head, path))
        self.assertIsNone(prov.receipt_for_commit(self.db, head))
        with self.assertRaises(prov.ProvenanceError) as raised:
            prov.capture({"CODEX_SESSION_ID": "s-inconnue",
                          "AMEESH_HARNESS": "codex"},
                         families=FAMILIES, db=self.db, cfg=self.cfg)
        self.assertIn("session binding", str(raised.exception))


class SessionReceiptCliTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE session_bindings RESTART IDENTITY")
        # La commande lit les familles de relecture dans le canon : le banc en
        # fournit un, comme `InstalledHookBoundSessionTest` (#3985).
        self.canon = mechanics_canon(os.path.join(self.tmp, "canon"))

    def cli_env(self) -> dict:
        return self.env(AMEESH_CANON=self.canon, AMEESH_CANON_REF="HEAD")

    def test_the_cli_records_a_push_range_from_a_linked_session(self):
        sb.bind(self.cfg, self.db, "veille", session_id="s-cli-7719",
                harness="codex", pid=None, by="human:test")
        path = tempfile.mkdtemp(prefix="ameesh-7719-cli-", dir=self.tmp)
        subprocess.run(["git", "init", "-q", path], check=True,
                       capture_output=True)
        env = dict(os.environ,
                   GIT_AUTHOR_NAME=CODEX[0], GIT_AUTHOR_EMAIL=CODEX[1],
                   GIT_COMMITTER_NAME=CODEX[0], GIT_COMMITTER_EMAIL=CODEX[1])
        for index in (0, 1, 2):
            with open(os.path.join(path, "f.txt"), "a", encoding="utf-8") as fh:
                fh.write("%d\n" % index)
            subprocess.run(["git", "add", "f.txt"], cwd=path, check=True,
                           capture_output=True)
            subprocess.run(["git", "commit", "-q", "-m", "commit %d" % index],
                           cwd=path, check=True, capture_output=True, env=env)
        out = self.mesh(
            "provenance", "record", "--session", "s-cli-7719",
            "--harness", "codex", "--range", "HEAD~2..HEAD",
            "--repo", path, env=self.cli_env())
        self.assertEqual(out.returncode, 0, out.stderr + out.stdout)
        self.assertIn("provenance recorded (session s-cli-7719)", out.stdout)
        receipts = [prov.receipt_for_commit(self.db, sha)
                    for sha in subprocess.check_output(
                        ["git", "rev-list", "HEAD~2..HEAD"], cwd=path,
                        text=True).split()]
        self.assertTrue(all(receipts))
        self.assertTrue(all(row["actor"] == "veille" for row in receipts))
        self.assertTrue(all(row["harness"] == "codex" for row in receipts))
        self.assertTrue(all(row["session"] == "s-cli-7719" for row in receipts))

    def test_the_cli_refuses_an_unlinked_session(self):
        path = tempfile.mkdtemp(prefix="ameesh-7719-unlinked-", dir=self.tmp)
        subprocess.run(["git", "init", "-q", path], check=True,
                       capture_output=True)
        env = dict(os.environ, GIT_AUTHOR_NAME=CODEX[0],
                   GIT_AUTHOR_EMAIL=CODEX[1], GIT_COMMITTER_NAME=CODEX[0],
                   GIT_COMMITTER_EMAIL=CODEX[1])
        with open(os.path.join(path, "f.txt"), "w", encoding="utf-8") as fh:
            fh.write("x\n")
        subprocess.run(["git", "add", "f.txt"], cwd=path, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "un"], cwd=path,
                       check=True, capture_output=True, env=env)
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path,
                                       text=True).strip()
        out = self.mesh(
            "provenance", "record", "--session", "s-not-bound",
            "--commit", head, "--repo", path, env=self.cli_env())
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("no active session binding", out.stderr)


if __name__ == "__main__":
    unittest.main()
