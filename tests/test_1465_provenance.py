# SPDX-License-Identifier: AGPL-3.0-only
"""#1465 — le fournisseur exact d'origine d'un commit et d'un jalon.

Deux niveaux : le contrat pur (sujets, SHA complets, capture d'identité) sans
base, puis la lecture réelle sur Postgres (migration 0445) : la preuve jointe
au fait, l'absence qui rend `unknown`, la divergence refusée, l'ajout seul.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ameesh import review_provenance as prov, storage, work

from .support import PgTestCase

SHA1 = "a" * 40
SHA1_FULL = "b" * 40
SHA256 = "c" * 64


class ContractTest(unittest.TestCase):
    """Le contrat seul : ce qui est refusé avant toute base."""

    def test_full_sha_and_subjects(self):
        self.assertEqual(prov.full_sha("A" * 40), "a" * 40)
        self.assertEqual(prov.full_sha(SHA256), SHA256)
        self.assertEqual(prov.subject_of_commit(SHA1), "commit:" + SHA1)
        self.assertEqual(prov.subject_of_milestone(42), "milestone:42")
        self.assertEqual(prov.subject_of_milestone("42"), "milestone:42")
        for bad in ("", "abc", "a" * 39, "z" * 40, None):
            with self.assertRaises(prov.ProvenanceError):
                prov.full_sha(bad)
        for bad in (0, -3, "x", None):
            with self.assertRaises(prov.ProvenanceError):
                prov.subject_of_milestone(bad)

    def test_capture_refuses_outside_a_managed_turn(self):
        env = {"AGENT_MAIL_NAME": "mty-amh-impl-07", "AMEESH_HARNESS": "deepseek"}
        with self.assertRaises(prov.ProvenanceError):
            prov.capture(env)  # pas de tour
        env["AMEESH_TURN_ID"] = "t" * 32
        self.assertEqual(prov.capture(env),
                         {"actor": "mty-amh-impl-07", "turn": "t" * 32,
                          "harness": "deepseek"})
        # un tour ne signe pas la preuve d'un autre agent (relecture cx3)
        with self.assertRaises(prov.ProvenanceError):
            prov.capture(env, actor="autre-agent")
        env["AMEESH_HARNESS"] = "autre"
        with self.assertRaises(prov.ProvenanceError):
            prov.capture(env)  # famille inconnue
        del env["AMEESH_HARNESS"]
        with self.assertRaises(prov.ProvenanceError):
            prov.capture(env)  # pas de harnais
        del env["AGENT_MAIL_NAME"]
        env["AMEESH_HARNESS"] = "claude"
        with self.assertRaises(prov.ProvenanceError):
            prov.capture(env)  # pas d'acteur


    def test_intent_outside_a_turn_writes_nothing(self):
        """Le hook `pre-commit` hors tour managé n'atteste rien (jamais bloquant)."""
        repo = tempfile.mkdtemp(prefix="ameesh-1465-intent-")
        try:
            subprocess.run(["git", "init", "-q", repo], check=True, capture_output=True)
            self.assertIsNone(prov.prepare_commit_intent(repo, environ={}))
            self.assertIsNone(prov.read_commit_intent(repo))
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_creation_outside_a_turn_writes_nothing(self):
        """Le hook `post-commit` hors tour managé ne constate rien (jamais bloquant)."""
        repo = tempfile.mkdtemp(prefix="ameesh-1465-created-")
        try:
            subprocess.run(["git", "init", "-q", repo], check=True, capture_output=True)
            self.assertIsNone(prov.prepare_commit_creation(repo, environ={}))
            self.assertIsNone(prov.read_commit_creation(repo))
        finally:
            shutil.rmtree(repo, ignore_errors=True)


class PgProvenanceTest(PgTestCase):
    """La lecture réelle : la preuve jointe au fait, ou `unknown`."""

    def setUp(self):
        super().setUp()
        self.ops = storage.of(self.db).provenance
        self.lot = work.add(self.db, title="un lot à preuve", app="ameesh",
                            actor="orchestrateur")

    def _receipt(self, subject, sha, *, actor="mty-amh-impl-07", session="s-1",
                 turn="t" * 32, harness="deepseek", work_item_id=None):
        return self.ops.add(subject, sha, actor, session, turn, harness,
                            work_item_id=work_item_id)

    def test_commit_receipt_is_exact_or_unknown(self):
        self.assertIsNone(prov.receipt_for_commit(self.db, SHA1))
        self._receipt("commit:" + SHA1, SHA1, work_item_id=int(self.lot["id"]))
        recu = prov.receipt_for_commit(self.db, SHA1)
        self.assertEqual(recu["subject"], "commit:" + SHA1)
        self.assertEqual(recu["commit"], SHA1)
        self.assertEqual(recu["actor"], "mty-amh-impl-07")
        self.assertEqual(recu["session"], "s-1")
        self.assertEqual(recu["turn"], "t" * 32)
        self.assertEqual(recu["harness"], "deepseek")
        self.assertEqual(recu["record"], "provenance_receipts:commit:" + SHA1)
        self.assertIsNone(recu["milestone"])
        # une abréviation n'est pas un commit pour le fournisseur
        with self.assertRaises(prov.ProvenanceError):
            prov.receipt_for_commit(self.db, SHA1[:12])
        # un autre commit reste inconnu : pas de voisin, pas de repli
        self.assertIsNone(prov.receipt_for_commit(self.db, SHA1_FULL))

    def test_milestone_receipt_copies_the_declared_facts(self):
        mark = work.milestone(self.db, int(self.lot["id"]), "verdict", sha=SHA1[:12],
                              actor="mty-amh-impl-07", verdict="ok")
        self.assertIsNone(prov.receipt_for_milestone(self.db, mark["id"]))
        self._receipt("milestone:%d" % mark["id"], SHA1, work_item_id=int(self.lot["id"]))
        recu = prov.receipt_for_milestone(self.db, mark["id"])
        self.assertEqual(recu["subject"], "milestone:%d" % mark["id"])
        # la preuve porte le commit COMPLET que le sha déclaré résout
        self.assertEqual(recu["commit"], SHA1)
        # les faits recopiés sont ceux du jalon, à l'identique
        self.assertEqual(recu["milestone"], {
            "id": mark["id"], "actor": mark["actor"], "at_ts": mark["at_ts"],
            "sha": mark["sha"], "verdict": mark["verdict"]})
        # et ils coïncident avec la lecture des jalons du lot
        lu = [m for m in work.milestones(self.db, int(self.lot["id"]))
              if m["id"] == mark["id"]][0]
        self.assertEqual(recu["milestone"],
                         {key: lu[key] for key in ("id", "actor", "at_ts", "sha", "verdict")})
        self.assertEqual(recu["harness"], "deepseek")

    def test_milestone_proof_must_match_the_mark(self):
        mark = work.milestone(self.db, int(self.lot["id"]), "verdict", sha=SHA1[:12],
                              actor="mty-amh-impl-07", verdict="ok")
        # acteur de la preuve différent de l'acteur du jalon : refus
        self._receipt("milestone:%d" % mark["id"], SHA1, actor="autre-agent")
        with self.assertRaises(prov.ProvenanceError):
            prov.receipt_for_milestone(self.db, mark["id"])
        # une abréviation qui ne résout pas le commit enregistré : refus
        self.db.execute("DELETE FROM provenance_receipts WHERE subject = %s",
                        ("milestone:%d" % mark["id"],))
        self._receipt("milestone:%d" % mark["id"], SHA1_FULL)
        with self.assertRaises(prov.ProvenanceError):
            prov.receipt_for_milestone(self.db, mark["id"])

    def test_milestone_proof_without_mark_is_unknown(self):
        self._receipt("milestone:999999", SHA1)
        self.assertIsNone(prov.receipt_for_milestone(self.db, 999999))

    def test_a_receipt_is_append_only(self):
        first = self._receipt("commit:" + SHA1, SHA1)
        self.assertTrue(first["recorded"])
        again = self._receipt("commit:" + SHA1, SHA1_FULL, actor="quelqu-un")
        self.assertFalse(again["recorded"])
        self.assertEqual(again["actor"], "mty-amh-impl-07")
        self.assertEqual(again["sha"], SHA1)
        recu = prov.receipt_for_commit(self.db, SHA1)
        self.assertEqual(recu["commit"], SHA1)

    def test_a_divergent_commit_row_is_refused_not_unknown(self):
        # une ligne existe pour le sujet, mais enregistre un AUTRE commit :
        # ce n'est pas une origine absente (None), c'est une preuve corrompue
        self._receipt("commit:" + SHA1, SHA1_FULL)
        with self.assertRaises(prov.ProvenanceError):
            prov.receipt_for_commit(self.db, SHA1)

    def test_milestone_declared_in_a_turn_carries_its_proof(self):
        """Le jalon et sa preuve naissent dans la même transaction (0445)."""
        storage.of(self.db).agents.upsert_unleased(
            "mty-amh-impl-07", chantier=None, harness="deepseek", host="h",
            cwd="/tmp", session_id="s-1")
        turn = "t" * 32
        env = {"AGENT_MAIL_NAME": "mty-amh-impl-07", "AMEESH_TURN_ID": turn,
               "AMEESH_HARNESS": "deepseek"}
        # le sha déclaré est une abréviation ; le dépôt sous la main la résout
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(prov, "resolve_commit", return_value=SHA1):
            mark = work.milestone(self.db, int(self.lot["id"]), "frozen",
                                  sha=SHA1[:12], actor="mty-amh-impl-07")
        self.assertEqual(mark["sha"], SHA1[:12], "le fait garde le sha déclaré")
        recu = prov.receipt_for_milestone(self.db, mark["id"])
        self.assertEqual(recu["commit"], SHA1, "la preuve porte le commit complet")
        self.assertEqual(recu["session"], "s-1")
        self.assertEqual(recu["turn"], turn)
        self.assertEqual(recu["harness"], "deepseek")
        self.assertEqual(recu["milestone"], {
            "id": mark["id"], "actor": mark["actor"], "at_ts": mark["at_ts"],
            "sha": mark["sha"], "verdict": mark["verdict"]})

    def test_milestone_outside_a_turn_stays_unknown(self):
        """Hors tour managé, le jalon s'écrit, l'origine reste inconnue."""
        with mock.patch.dict(os.environ, {"AMEESH_TURN_ID": "", "AMEESH_HARNESS": ""},
                             clear=False):
            mark = work.milestone(self.db, int(self.lot["id"]), "verdict",
                                  sha=SHA1, actor="mty-amh-impl-07", verdict="ok")
        self.assertIsNone(prov.receipt_for_milestone(self.db, mark["id"]))

    def _repo(self, *, actor="mty-amh-impl-07", name="ameesh-1465-repo-"):
        """Un dépôt git jetable et son premier commit, sous l'identité donnée."""
        repo = tempfile.mkdtemp(prefix=name, dir=self.tmp)
        subprocess.run(["git", "init", "-q", repo], check=True, capture_output=True)
        env = dict(os.environ,
                   GIT_AUTHOR_NAME=actor, GIT_AUTHOR_EMAIL="%s@ameesh.invalid" % actor,
                   GIT_COMMITTER_NAME=actor, GIT_COMMITTER_EMAIL="%s@ameesh.invalid" % actor)
        self._commit(repo, env, "un")
        return repo, env

    def _stage(self, repo, message):
        with open(os.path.join(repo, "f.txt"), "a", encoding="utf-8") as fh:
            fh.write(message + "\n")
        subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True, capture_output=True)

    def _commit_only(self, repo, env, message):
        subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True,
                       capture_output=True, env=env)
        return self._head(repo)

    def _commit(self, repo, env, message):
        self._stage(repo, message)
        return self._commit_only(repo, env, message)

    def _head(self, repo):
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo,
                                       text=True).strip()

    def _attest(self, repo, *, actor="mty-amh-impl-07", turn="t" * 32,
                harness="deepseek"):
        """Le hook `pre-commit` : l'attestation écrite avant le commit."""
        return prov.prepare_commit_intent(repo, environ={
            "AGENT_MAIL_NAME": actor, "AMEESH_TURN_ID": turn,
            "AMEESH_HARNESS": harness})

    def _created(self, repo, *, actor="mty-amh-impl-07", turn="t" * 32,
                 harness="deepseek"):
        """Le hook `post-commit` : le fait de création de l'objet né."""
        return prov.prepare_commit_creation(repo, environ={
            "AGENT_MAIL_NAME": actor, "AMEESH_TURN_ID": turn,
            "AMEESH_HARNESS": harness})

    def _commit_attested(self, repo, env, message, **kw):
        """Le tour réel : index prêt, attestation, commit, fait de création."""
        self._stage(repo, message)
        self._attest(repo, **kw)
        head = self._commit_only(repo, env, message)
        self._created(repo, **kw)
        return head

    def test_an_inherited_head_is_unknown_not_attributed_to_the_new_turn(self):
        """Témoin cx3 15/10 04:37 : un HEAD hérité n'est pas une création.

        Le commit a été créé sous un tour antérieur, par le même acteur (donc
        même committer Git) ; il est resté HEAD. Le tour suivant ne peut pas se
        l'approprier : sans attestation de création dans CE tour, l'origine
        reste inconnue (`None`), jamais devinée."""
        repo, env = self._repo()  # commit unique, créé sous l'ancien tour
        inherited = self._head(repo)
        ident = {"actor": "mty-amh-impl-07", "session": "s-new", "turn": "n" * 32,
                 "harness": "codex"}
        with self.assertRaises(prov.ProvenanceError):
            prov.record_commit(self.db, inherited, identity=ident, cwd=repo)
        self.assertIsNone(prov.receipt_for_commit(self.db, inherited))

    def test_an_attestation_written_after_the_commit_attests_nothing(self):
        """Une attestation posée sur le HEAD déjà né ne le crée pas."""
        repo, env = self._repo()
        head = self._head(repo)
        self._attest(repo)  # head attesté == le commit lui-même
        with self.assertRaises(prov.ProvenanceError):
            prov.record_commit(self.db, head, identity={
                "actor": "mty-amh-impl-07", "session": "s-1", "turn": "t" * 32,
                "harness": "deepseek"}, cwd=repo)
        self.assertIsNone(prov.receipt_for_commit(self.db, head))

    def test_an_attestation_from_another_turn_is_refused(self):
        repo, env = self._repo()
        head = self._commit_attested(repo, env, "deux", turn="a" * 32)
        with self.assertRaises(prov.ProvenanceError):
            prov.record_commit(self.db, head, identity={
                "actor": "mty-amh-impl-07", "session": "s-1", "turn": "b" * 32,
                "harness": "deepseek"}, cwd=repo)
        self.assertIsNone(prov.receipt_for_commit(self.db, head))

    def test_an_amended_commit_is_the_attested_creation(self):
        ident = {"actor": "mty-amh-impl-07", "session": "s-1", "turn": "t" * 32,
                 "harness": "deepseek"}
        repo, env = self._repo()
        first = self._head(repo)
        self._commit_attested(repo, env, "deux")
        amended_from = self._head(repo)
        # l'amend part du HEAD attesté : mêmes parents que lui
        self._attest(repo)
        subprocess.run(["git", "commit", "-q", "--amend", "-m", "deux-amende"],
                       cwd=repo, check=True, capture_output=True, env=env)
        amended = self._head(repo)
        self.assertNotEqual(amended, amended_from)
        self.assertNotEqual(amended, first)
        self.assertIsNotNone(self._created(repo))
        row = prov.record_commit(self.db, amended, identity=ident, cwd=repo)
        self.assertTrue(row["recorded"])
        self.assertEqual(prov.receipt_for_commit(self.db, amended)["session"], "s-1")

    def test_a_replayed_old_object_is_refused_despite_the_attested_shape(self):
        """B1 cx2/Codex 15/10 12:12 : un objet ancien restauré n'est pas une création.

        Le scénario rejoué : C est créé sous un tour antérieur (même
        committer), HEAD revient sur son parent A, l'index est préparé à
        l'identique de l'arbre(C), l'attestation du nouveau tour est écrite,
        puis HEAD est remis sur C. La forme attestée est satisfaite, mais un
        `reset` n'écrit aucun fait de création : `record_commit` refuse et
        l'origine reste inconnue."""
        ident = {"actor": "mty-amh-impl-07", "session": "s-1", "turn": "t" * 32,
                 "harness": "deepseek"}
        repo, env = self._repo()
        first = self._head(repo)
        old = self._commit(repo, env, "ancien")  # C, créé sous un tour antérieur
        subprocess.run(["git", "reset", "-q", "--hard", first], cwd=repo,
                       check=True, capture_output=True, env=env)
        self._stage(repo, "ancien")  # index identique à l'arbre(C)
        self._attest(repo)
        subprocess.run(["git", "reset", "-q", "--hard", old], cwd=repo,
                       check=True, capture_output=True, env=env)
        self.assertIsNone(prov.read_commit_creation(repo))
        self.assertTrue(prov._attests_creation(repo, old, prov.read_commit_intent(repo)),
                        "la forme attestée seule serait acceptée : c'est le défaut B1")
        with self.assertRaises(prov.ProvenanceError):
            prov.record_commit(self.db, old, identity=ident, cwd=repo)
        self.assertIsNone(prov.receipt_for_commit(self.db, old))

    def test_a_creation_record_covers_only_the_object_it_names(self):
        """Le fait de création d'un commit ne couvre pas un autre objet."""
        ident = {"actor": "mty-amh-impl-07", "session": "s-1", "turn": "t" * 32,
                 "harness": "deepseek"}
        repo, env = self._repo()
        old = self._head(repo)
        self._commit_attested(repo, env, "deux")  # vrai commit du tour
        subprocess.run(["git", "reset", "-q", "--hard", old], cwd=repo,
                       check=True, capture_output=True, env=env)
        # le fait de création du commit "deux" existe encore, mais il nomme
        # un autre objet : il ne couvre pas C
        self.assertIsNotNone(prov.read_commit_creation(repo))
        with self.assertRaises(prov.ProvenanceError):
            prov.record_commit(self.db, old, identity=ident, cwd=repo)
        self.assertIsNone(prov.receipt_for_commit(self.db, old))

    def test_a_reset_is_not_a_creation(self):
        """Le reflog d'un `reset` n'est pas un `git commit` : rien n'est constaté."""
        repo, env = self._repo()
        first = self._head(repo)
        self._commit(repo, env, "deux")
        self._attest(repo)
        subprocess.run(["git", "reset", "-q", "--hard", first], cwd=repo,
                       check=True, capture_output=True, env=env)
        self.assertIsNone(self._created(repo))

    def test_an_abandoned_attestation_writes_no_creation(self):
        """Une attestation sans commit ne produit aucun fait de création ; le
        commit qui suit, dans le même tour, produit bien les deux."""
        ident = {"actor": "mty-amh-impl-07", "session": "s-1", "turn": "t" * 32,
                 "harness": "deepseek"}
        repo, env = self._repo()
        self._stage(repo, "deux")
        self.assertIsNotNone(self._attest(repo))
        self.assertIsNone(self._created(repo))
        head = self._commit_only(repo, env, "deux")
        self.assertIsNotNone(self._created(repo))
        row = prov.record_commit(self.db, head, identity=ident, cwd=repo)
        self.assertTrue(row["recorded"])

    def test_record_commit_accepts_the_creation_attested_by_this_turn(self):
        ident = {"actor": "mty-amh-impl-07", "session": "s-1", "turn": "t" * 32,
                 "harness": "deepseek"}
        repo, env = self._repo()
        old = self._head(repo)  # hérité, sans attestation de ce tour
        # un ancien commit, même complet et présent, n'est pas enregistrable
        with self.assertRaises(prov.ProvenanceError):
            prov.record_commit(self.db, old, identity=ident, cwd=repo)
        head = self._commit_attested(repo, env, "deux")
        row = prov.record_commit(self.db, head, identity=ident, cwd=repo,
                                 work_item_id=int(self.lot["id"]))
        self.assertTrue(row["recorded"])
        self.assertEqual(prov.receipt_for_commit(self.db, head)["session"], "s-1")
        # l'attestation a été consommée : rejouer la MÊME identité ne lève pas
        again = prov.record_commit(self.db, head, identity=ident, cwd=repo)
        self.assertFalse(again["recorded"])
        # une autre identité pour le même commit est refusée
        with self.assertRaises(prov.ProvenanceError):
            prov.record_commit(self.db, head, identity=dict(ident, actor="autre"),
                               cwd=repo)
        with self.assertRaises(prov.ProvenanceError):
            prov.record_commit(self.db, "pas-un-sha", identity=ident, cwd=repo)
        # un commit dont le committer est un autre agent est refusé
        other, _ = self._repo(actor="autre-agent", name="ameesh-1465-autre-")
        other_head = self._head(other)
        with self.assertRaises(prov.ProvenanceError):
            prov.record_commit(self.db, other_head, identity=ident, cwd=other)
        self.assertIsNone(prov.receipt_for_commit(self.db, other_head))


class HookInstallTest(unittest.TestCase):
    """Le hook post-commit : posé une fois, jamais à la place d'un étranger."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-1465-hook-")
        subprocess.run(["git", "init", "-q", self.tmp], check=True,
                       capture_output=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_install_is_idempotent_and_keeps_a_foreign_hook(self):
        out = prov.install_hook(self.tmp)
        self.assertTrue(out["installed"], out)
        self.assertEqual(out["path"], os.path.join(self.tmp, ".git", "hooks", "post-commit"))
        for name in ("pre-commit", "post-commit"):
            path = os.path.join(self.tmp, ".git", "hooks", name)
            with open(path, encoding="utf-8") as fh:
                self.assertIn("ameesh #1465", fh.read())
            self.assertTrue(os.access(path, os.X_OK))
        # le `post-commit` constate l'objet né AVANT d'écrire la preuve
        with open(out["path"], encoding="utf-8") as fh:
            post = fh.read()
        self.assertIn("provenance created", post)
        self.assertIn("provenance record", post)
        self.assertTrue(prov.install_hook(self.tmp)["installed"])
        foreign = "#!/bin/sh\necho autre\n"
        with open(out["path"], "w", encoding="utf-8") as fh:
            fh.write(foreign)
        refused = prov.install_hook(self.tmp)
        self.assertFalse(refused["installed"])
        self.assertIn("foreign", refused["reason"])
        self.assertIn("post-commit", refused["reason"])
        with open(out["path"], encoding="utf-8") as fh:
            self.assertEqual(fh.read(), foreign)
        # un `pre-commit` étranger n'est pas écrasé non plus : la paire manque
        os.unlink(out["path"])
        pre = os.path.join(self.tmp, ".git", "hooks", "pre-commit")
        with open(pre, "w", encoding="utf-8") as fh:
            fh.write(foreign)
        refused = prov.install_hook(self.tmp)
        self.assertFalse(refused["installed"])
        self.assertIn("pre-commit", refused["reason"])
        with open(pre, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), foreign)

    def test_runner_installs_the_hook_pair_and_skips_mediated_sessions(self):
        from ameesh import runner as runner_mod
        fake = types.SimpleNamespace(
            runner=types.SimpleNamespace(mediated=False), name="mty-amh-impl-07")
        runner_mod.AgentWorker._installe_hook_preuve(fake, self.tmp)
        hooks = [os.path.join(self.tmp, ".git", "hooks", name)
                 for name in ("pre-commit", "post-commit")]
        for hook in hooks:
            self.assertTrue(os.path.exists(hook))
            os.unlink(hook)
        fake.runner.mediated = True
        runner_mod.AgentWorker._installe_hook_preuve(fake, self.tmp)
        for hook in hooks:
            self.assertFalse(os.path.exists(hook), "pas de hook sur une VM médiée")


if __name__ == "__main__":
    unittest.main()
