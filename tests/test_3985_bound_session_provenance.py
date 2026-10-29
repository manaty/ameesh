# SPDX-License-Identifier: AGPL-3.0-only
"""#3985 — une session liée (`ameesh mail bind`) prouve sa famille hors tour.

Le bureau (canon §10) compose hors tour managé : aucun `AMEESH_TURN_ID`, donc
les hooks de provenance n'écrivaient rien et la porte lisait ses commits de
résolution comme « author provenance unknown ». La liaison de session est la
preuve : PID ancêtre vérifié (L46/L63), agent `externe` sans bail, harnais
concordant avec le registre. Le reçu porte un `turn` synthétique
`session:<id>` ; il ne se fait jamais passer pour un tour managé.

Les témoins couvrent aussi les refus nommés : agent mené par l'exécuteur,
harnais divergent, liaison révoquée, sans PID. Un commit de résolution d'une
session liée donne au gel une famille connue ; sinon l'origine reste inconnue.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

from ameesh import review_authors, review_provenance as prov
from ameesh import registry, session_bindings as sb, work

from .support import PgTestCase, mechanics_canon

FAMILIES = ("claude", "codex", "deepseek")


class BoundSessionProvenanceTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE session_bindings RESTART IDENTITY")
        self.lot = work.add(self.db, title="un lot de session liée", app="ameesh",
                            actor="orchestrateur")

    # -- utilitaires ------------------------------------------------------
    def bind(self, *, agent="bureau", session="s-bureau-1", harness="codex",
             with_pid=True, force=False):
        return sb.bind(self.cfg, self.db, agent, session_id=session,
                       harness=harness,
                       pid=os.getpid() if with_pid else None,
                       by="human:test", force=force)

    def repo(self, *, actor="bureau"):
        repo = tempfile.mkdtemp(prefix="ameesh-3985-", dir=self.tmp)
        subprocess.run(["git", "init", "-q", repo], check=True,
                       capture_output=True)
        env = dict(os.environ,
                   GIT_AUTHOR_NAME=actor,
                   GIT_AUTHOR_EMAIL="%s@ameesh.invalid" % actor,
                   GIT_COMMITTER_NAME=actor,
                   GIT_COMMITTER_EMAIL="%s@ameesh.invalid" % actor)
        return repo, env

    def stage(self, repo, message="un"):
        with open(os.path.join(repo, "f.txt"), "a", encoding="utf-8") as fh:
            fh.write(message + "\n")
        subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True,
                       capture_output=True)

    def commit(self, repo, env, message="un", *, staged=False):
        if not staged:
            self.stage(repo, message)
        subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo,
                       check=True, capture_output=True, env=env)
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo,
                                       text=True).strip()

    def bound_commit(self, repo, env):
        """Le geste complet des hooks, sans tour managé."""
        self.stage(repo, "résolution de paquet")
        self.assertIsNotNone(prov.prepare_commit_intent(
            repo, environ={}, db=self.db))
        head = self.commit(repo, env, "résolution de paquet", staged=True)
        self.assertIsNotNone(prov.prepare_commit_creation(
            repo, environ={}, db=self.db))
        ident = prov.proof_for_current_turn(self.db, cwd=repo)
        self.assertIsNotNone(ident)
        prov.record_commit(self.db, head, identity=ident, cwd=repo)
        return head, ident

    # -- la preuve --------------------------------------------------------
    def test_a_bound_session_proves_the_commit_and_its_family(self):
        self.bind()
        repo, env = self.repo()
        head, ident = self.bound_commit(repo, env)
        self.assertEqual(ident["actor"], "bureau")
        self.assertEqual(ident["harness"], "codex")
        self.assertEqual(ident["session"], "s-bureau-1")
        self.assertEqual(ident["turn"], "session:s-bureau-1")
        receipt = prov.receipt_for_commit(self.db, head)
        self.assertEqual(receipt["actor"], "bureau")
        self.assertEqual(receipt["session"], "s-bureau-1")
        self.assertEqual(receipt["turn"], "session:s-bureau-1")
        self.assertEqual(receipt["harness"], "codex")
        # Le gel lit la famille du reçu, sans repli sur un nom Git.
        authors, unknown, _ = review_authors.reading(
            self.db, {"id": int(self.lot["id"]), "assignee": "bureau",
                      "app": "ameesh"},
            None, [head], repo=repo, families=FAMILIES, strict=True)
        self.assertEqual(unknown, [])
        self.assertIn({"name": "bureau", "family": "codex"}, authors)

    def test_a_bound_session_proves_a_milestone(self):
        self.bind()
        repo, env = self.repo()
        head = self.commit(repo, env)
        mark = work.milestone(self.db, int(self.lot["id"]), "verdict",
                              sha=head, actor="bureau", verdict="ok",
                              repo=repo)
        receipt = prov.receipt_for_milestone(self.db, mark["id"])
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["actor"], "bureau")
        self.assertEqual(receipt["harness"], "codex")
        self.assertEqual(receipt["session"], "s-bureau-1")
        self.assertEqual(receipt["turn"], "session:s-bureau-1")

    def test_the_managed_turn_still_wins_over_a_binding(self):
        self.bind()
        ident = prov.capture(
            {"AGENT_MAIL_NAME": "mty-amh-impl-07", "AMEESH_TURN_ID": "t" * 32,
             "AMEESH_HARNESS": "deepseek"},
            families=FAMILIES, db=self.db)
        self.assertEqual(ident["turn"], "t" * 32)
        self.assertNotIn("session", ident)

    # -- #4485 : le nom annoncé ne masque plus la liaison ------------------
    def test_an_announced_identity_is_covered_by_a_valid_binding(self):
        """Un shell qui a gardé AGENT_MAIL_NAME ne masque pas sa liaison.

        C'était l'écart du bureau : `_identity` refusait dès qu'un nom était
        annoncé sans tour, sans jamais consulter la liaison. Une liaison
        valide couvre désormais le nom annoncé, quand c'est le même agent."""
        self.bind()
        ident = prov.capture({"AGENT_MAIL_NAME": "bureau"}, families=FAMILIES,
                             db=self.db)
        self.assertEqual(ident["actor"], "bureau")
        self.assertEqual(ident["session"], "s-bureau-1")
        self.assertEqual(ident["turn"], "session:s-bureau-1")
        self.assertEqual(ident["harness"], "codex")

    def test_an_announced_identity_does_not_borrow_another_binding(self):
        """La liaison doit nommer l'agent annoncé, pas n'importe qui."""
        self.bind()
        with self.assertRaises(prov.ProvenanceError) as raised:
            prov.capture({"AGENT_MAIL_NAME": "ameesh-cx3"},
                         families=FAMILIES, db=self.db)
        self.assertIn("announced but nothing proves it", str(raised.exception))
        self.assertIn("not the agent of this bound session", str(raised.exception))

    def test_an_announced_identity_without_binding_still_refuses(self):
        with self.assertRaises(prov.ProvenanceError) as raised:
            prov.capture({"AGENT_MAIL_NAME": "bureau"}, families=FAMILIES,
                         db=self.db)
        self.assertIn("announced but nothing proves it", str(raised.exception))
        self.assertIn("session binding", str(raised.exception))

    # -- les refus nommés --------------------------------------------------
    def test_an_execute_agent_cannot_prove_a_family_by_binding(self):
        registry.upsert(self.db, "executeur", harness="codex",
                        host=self.cfg.host, mode="execute")
        self.bind(agent="executeur", session="s-exec", force=True)
        with self.assertRaises(prov.ProvenanceError) as raised:
            prov.capture(environ={}, families=FAMILIES, db=self.db)
        self.assertIn("execute agent", str(raised.exception))

    def test_a_registry_harness_mismatch_is_refused(self):
        self.bind()
        self.db.execute(
            "UPDATE agent_registry SET harness = 'claude' WHERE name = 'bureau'")
        with self.assertRaises(prov.ProvenanceError) as raised:
            prov.capture(environ={}, families=FAMILIES, db=self.db)
        self.assertIn("registered under", str(raised.exception))

    def test_a_revoked_binding_proves_nothing(self):
        self.bind()
        sb.unbind(self.cfg, self.db, session_id="s-bureau-1",
                  harness="codex", by="human:test")
        with self.assertRaises(prov.ProvenanceError) as raised:
            prov.capture(environ={}, families=FAMILIES, db=self.db)
        self.assertIn("session binding", str(raised.exception))

    def test_a_binding_without_pid_is_not_a_proof(self):
        self.bind(with_pid=False)
        with self.assertRaises(prov.ProvenanceError) as raised:
            prov.capture(environ={}, families=FAMILIES, db=self.db)
        self.assertIn("rebind with --pid", str(raised.exception))

    def test_a_bound_harness_must_be_a_compiled_family(self):
        self.bind(harness="codex")
        with self.assertRaises(prov.ProvenanceError) as raised:
            prov.capture(environ={}, families=("claude", "deepseek"), db=self.db)
        self.assertIn("unknown harness", str(raised.exception))

    def test_a_live_lease_removes_the_binding(self):
        self.bind()
        self.db.execute(
            "UPDATE agent_registry SET lease_owner = 'runner-x', lease_epoch = 1,"
            " lease_expires_at = now() + interval '1 hour' WHERE name = 'bureau'")
        with self.assertRaises(prov.ProvenanceError) as raised:
            prov.capture(environ={}, families=FAMILIES, db=self.db)
        self.assertIn("does not hold", str(raised.exception))

    def test_an_unbound_commit_stays_unknown(self):
        repo, env = self.repo()
        # Sans liaison, les hooks n'attestent rien : le commit reste inconnu.
        self.assertIsNone(prov.prepare_commit_intent(
            repo, environ={}, db=self.db))
        head = self.commit(repo, env)
        self.assertIsNone(prov.prepare_commit_creation(
            repo, environ={}, db=self.db))
        self.assertIsNone(prov.receipt_for_commit(self.db, head))

    def test_no_database_keeps_the_old_turn_only_capture(self):
        with self.assertRaises(prov.ProvenanceError) as raised:
            prov.capture(environ={}, families=FAMILIES)
        self.assertIn("outside a turn", str(raised.exception))


class BoundHookCliTest(PgTestCase):
    """Les commandes des hooks (`ameesh provenance intent|created`) hors tour.

    Les scripts installés sont couverts par `InstalledHookBoundSessionTest` :
    un vrai `git commit` dans une session liée passe par eux."""

    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE session_bindings RESTART IDENTITY")

    def test_the_hook_pair_records_a_bound_commit(self):
        sb.bind(self.cfg, self.db, "bureau", session_id="s-cli",
                harness="codex", pid=os.getpid(), by="human:test")
        repo = tempfile.mkdtemp(prefix="ameesh-3985-cli-", dir=self.tmp)
        subprocess.run(["git", "init", "-q", repo], check=True,
                       capture_output=True)
        env = dict(os.environ,
                   GIT_AUTHOR_NAME="bureau",
                   GIT_AUTHOR_EMAIL="bureau@ameesh.invalid",
                   GIT_COMMITTER_NAME="bureau",
                   GIT_COMMITTER_EMAIL="bureau@ameesh.invalid")

        def hook(*args):
            return subprocess.run(
                [sys.executable, "-m", "ameesh.main",
                 "provenance", *args],
                cwd=repo, capture_output=True, text=True, env=self.env(),
                timeout=60)

        with open(os.path.join(repo, "f.txt"), "w", encoding="utf-8") as fh:
            fh.write("x\n")
        subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True,
                       capture_output=True)
        intent = hook("intent")
        self.assertEqual(intent.returncode, 0, intent.stderr)
        subprocess.run(["git", "commit", "-q", "-m", "résolution"], cwd=repo,
                       check=True, capture_output=True, env=env)
        created = hook("created")
        self.assertEqual(created.returncode, 0, created.stderr)
        # `record` hors canon n'est pas rejoué ici : la famille du canon est
        # épinglée par le banc ; le chemin de capture, lui, se lit ci-dessous.
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo,
                                       text=True).strip()
        ident = prov.proof_for_current_turn(self.db, cwd=repo)
        self.assertIsNotNone(ident)
        self.assertEqual(ident["turn"], "session:s-cli")
        prov.record_commit(self.db, head, identity=ident, cwd=repo)
        receipt = prov.receipt_for_commit(self.db, head)
        self.assertEqual(receipt["actor"], "bureau")
        self.assertEqual(receipt["session"], "s-cli")
        self.assertEqual(receipt["turn"], "session:s-cli")
        self.assertEqual(receipt["harness"], "codex")


class InstalledHookBoundSessionTest(PgTestCase):
    """Le vrai `git commit` d'une session liée, à travers les hooks installés.

    C'est le geste du bureau (§10) : `paquet-compose` laisse un conflit, le
    composeur résout puis `git commit` — aucun `AMEESH_TURN_ID`. Les hooks
    installés doivent appeler la commande de provenance, qui retrouve la
    liaison, au lieu de sortir avant elle (l'écart corrigé ici).
    """

    def test_installed_hooks_record_a_manual_commit_in_a_bound_session(self):
        sb.bind(self.cfg, self.db, "bureau", session_id="s-hook",
                harness="codex", pid=os.getpid(), by="human:test")
        repo = tempfile.mkdtemp(prefix="ameesh-3985-hook-", dir=self.tmp)
        subprocess.run(["git", "init", "-q", repo], check=True,
                       capture_output=True)
        installed = prov.install_hook(repo)
        self.assertTrue(installed["installed"], installed)

        # `ameesh` du hook doit être la source du lot (le wrapper du poste
        # porterait le paquet installé, pas la correction).
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir, exist_ok=True)
        wrapper = os.path.join(bindir, "ameesh")
        with open(wrapper, "w", encoding="utf-8") as fh:
            fh.write("#!%s\nimport sys\nfrom ameesh.main import main\n"
                     "sys.exit(main())\n" % sys.executable)
        os.chmod(wrapper, 0o755)
        canon = mechanics_canon(os.path.join(self.tmp, "canon"))
        env = self.env(AMEESH_CANON=canon, AMEESH_CANON_REF="HEAD",
                       PATH=bindir + os.pathsep + os.environ.get("PATH", ""))
        env.update(GIT_AUTHOR_NAME="bureau",
                   GIT_AUTHOR_EMAIL="bureau@ameesh.invalid",
                   GIT_COMMITTER_NAME="bureau",
                   GIT_COMMITTER_EMAIL="bureau@ameesh.invalid")
        with open(os.path.join(repo, "f.txt"), "w", encoding="utf-8") as fh:
            fh.write("x\n")
        subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True,
                       capture_output=True)
        commit = subprocess.run(["git", "commit", "-q", "-m", "résolution"],
                                cwd=repo, capture_output=True, text=True, env=env)
        self.assertEqual(commit.returncode, 0, commit.stderr)
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo,
                                       text=True).strip()
        receipt = prov.receipt_for_commit(self.db, head)
        self.assertIsNotNone(receipt, "les hooks installés n'ont pas écrit la preuve")
        self.assertEqual(receipt["actor"], "bureau")
        self.assertEqual(receipt["session"], "s-hook")
        self.assertEqual(receipt["turn"], "session:s-hook")
        self.assertEqual(receipt["harness"], "codex")


if __name__ == "__main__":
    unittest.main()
