# SPDX-License-Identifier: AGPL-3.0-only
"""#4485 — provenance signée d'un auteur hors runner, sans ameesh local.

Le bureau (canon §10) compose parfois des commits sur `bureau-goal`, où ameesh
n'est pas installé : aucun PID local à faire vérifier par la liaison #3985. La
preuve est alors une **attestation signée par une clé `owner`** (le
propriétaire, ou le bureau qui détient sa délégation) : elle couvre le SHA
complet, l'acteur, la session, le harnais, l'hôte et le lot. Le reçu porte
`turn = "signed:<approbation>"` et la porte lit la famille comme pour un tour.

Les témoins couvrent la preuve (clé owner, ou approbation pré-signée), les
refus (agent execute, bail vivant, harnais divergent, clé de rôle agent, sans
approbation, approbation consommée) et le fait que le commit n'a pas à être
HEAD : il a été créé sur un autre hôte.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest

from ameesh import authority, registry, review_authors, review_provenance as prov
from ameesh import signing, work

from .support import PgTestCase, mechanics_canon

FAMILIES = ("claude", "codex", "deepseek")
OWNER = "smichea"
SESSION = "526a0a74-f00e-4639-b4d1-d11066d51f99"
HOST = "bureau-goal"


class SignedProvenanceTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute(
            "TRUNCATE provenance_receipts, mesh_approvals, mesh_consumed_nonces"
            " RESTART IDENTITY CASCADE")
        registry.upsert(self.db, "bureau", harness="claude", host=self.cfg.host,
                        mode="externe")
        self.lot = work.add(self.db, title="un lot signé", app="ameesh",
                            actor="orchestrateur")
        self.seed = signing.backend("pure").generate_seed()
        self.public = signing.backend("pure").public_from_seed(self.seed)
        authority.register_human_key(self.db, OWNER, self.public, role="owner")
        self.key_path = os.path.join(self.tmp, "owner.key")
        with open(self.key_path, "w", encoding="utf-8") as fh:
            fh.write(signing.encode_private(self.seed))
        os.chmod(self.key_path, 0o600)
        self.repo = tempfile.mkdtemp(prefix="ameesh-4485-", dir=self.tmp)
        subprocess.run(["git", "init", "-q", self.repo], check=True,
                       capture_output=True)
        self.git_env = dict(
            os.environ,
            GIT_AUTHOR_NAME="smichea",
            GIT_AUTHOR_EMAIL="sebastien.michea@manaty.net",
            GIT_COMMITTER_NAME="smichea",
            GIT_COMMITTER_EMAIL="sebastien.michea@manaty.net",
        )

    # -- utilitaires ------------------------------------------------------
    def commit(self, message="canon", *, trailer=False):
        path = os.path.join(self.repo, "fiche.md")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(message + "\n")
        subprocess.run(["git", "add", "fiche.md"], cwd=self.repo, check=True,
                       capture_output=True)
        args = ["git", "commit", "-q", "-m", message]
        if trailer:
            args += ["-m", "Co-Authored-By: Claude <noreply@anthropic.com>"]
        subprocess.run(args, cwd=self.repo, check=True, capture_output=True,
                       env=self.git_env)
        return subprocess.check_output(["git", "rev-parse", "HEAD"],
                                       cwd=self.repo, text=True).strip()

    def sign(self, sha=None, *, actor="bureau", session=SESSION,
             harness="claude", host=HOST, lot=True, **kwargs):
        fields = dict(
            actor=actor, session=session, harness=harness, host=host,
            families=FAMILIES, lot=int(self.lot["id"]) if lot else None,
            approver=OWNER, key_path=self.key_path, cwd=self.repo,
        )
        fields.update(kwargs)
        return prov.record_signed_commit(self.db, sha or self.commit(), **fields)

    def approval(self, sha, *, decision="approved", **fields):
        """Une approbation pré-signée pour le même texte canonique."""
        statement = prov.signed_statement(
            commit=sha, actor=fields.get("actor", "bureau"),
            session=fields.get("session", SESSION),
            harness=fields.get("harness", "claude"),
            host=fields.get("host", HOST),
            lot=fields.get("lot", int(self.lot["id"])))
        digest = prov.signed_digest(
            commit=sha, actor=fields.get("actor", "bureau"),
            session=fields.get("session", SESSION),
            harness=fields.get("harness", "claude"),
            host=fields.get("host", HOST),
            lot=fields.get("lot", int(self.lot["id"])))
        return authority.create_approval(
            self.db, self.seed, approver=OWNER, action=prov.SIGNED_ACTION,
            artifact_kind=prov.SIGNED_KIND, artifact_hash=digest,
            decision=decision, meta={"statement": statement})

    # -- la preuve --------------------------------------------------------
    def test_an_owner_signed_commit_is_proven_outside_a_turn(self):
        head = self.commit("canon", trailer=True)
        # Le commit est celui du bureau : auteur/committer humains, pas le
        # `<agent>@ameesh.invalid` d'un tour managé.
        committer = subprocess.check_output(
            ["git", "show", "-s", "--format=%ce", head], cwd=self.repo,
            text=True).strip()
        self.assertEqual(committer, "sebastien.michea@manaty.net")
        row = self.sign(head)
        self.assertTrue(row["recorded"])
        receipt = prov.receipt_for_commit(self.db, head)
        self.assertEqual(receipt["actor"], "bureau")
        self.assertEqual(receipt["session"], SESSION)
        self.assertEqual(receipt["harness"], "claude")
        self.assertTrue(receipt["turn"].startswith("signed:"))
        self.assertEqual(receipt["milestone"], None)
        # Le gel lit la famille de l'attestation, pas un nom Git.
        authors, unknown, _ = review_authors.reading(
            self.db, {"id": int(self.lot["id"]), "assignee": "bureau",
                      "app": "ameesh"},
            None, [head], repo=self.repo, families=FAMILIES, strict=True)
        self.assertEqual(unknown, [])
        self.assertIn({"name": "bureau", "family": "claude"}, authors)

    def test_the_signed_commit_does_not_need_to_be_head(self):
        older = self.commit("premier")
        newer = self.commit("second")
        self.assertNotEqual(older, newer)
        row = self.sign(older)
        self.assertTrue(row["recorded"])
        self.assertEqual(prov.receipt_for_commit(self.db, older)["commit"], older)
        self.assertIsNone(prov.receipt_for_commit(self.db, newer))

    def test_a_pre_signed_approval_also_proves_the_commit(self):
        head = self.commit("pré-signé")
        built = self.approval(head)
        row = prov.record_signed_commit(
            self.db, head, actor="bureau", session=SESSION, harness="claude",
            host=HOST, families=FAMILIES, lot=int(self.lot["id"]),
            approval_id=int(built["id"]), cwd=self.repo)
        self.assertTrue(row["recorded"])
        receipt = prov.receipt_for_commit(self.db, head)
        self.assertEqual(receipt["actor"], "bureau")
        self.assertEqual(receipt["turn"], "signed:%d" % int(built["id"]))

    def test_a_rejected_approval_proves_nothing(self):
        """Un refus signé reste un refus : il ne crée ni reçu ni consommation.

        B P1 de la relecture cx3 (#4485) : `build_approval` accepte
        `decision=rejected`, et `verify_approval` en vérifie la signature sans
        exiger « approved ». La décision doit être contrôlée avant l'écriture."""
        head = self.commit("refusé")
        built = self.approval(head, decision="rejected")
        with self.assertRaises(prov.ProvenanceError) as raised:
            prov.record_signed_commit(
                self.db, head, actor="bureau", session=SESSION,
                harness="claude", host=HOST, families=FAMILIES,
                lot=int(self.lot["id"]), approval_id=int(built["id"]),
                cwd=self.repo)
        self.assertIn("not an approved statement", str(raised.exception))
        self.assertIsNone(prov.receipt_for_commit(self.db, head))
        rows = authority.list_approvals(
            self.db, action=prov.SIGNED_ACTION,
            artifact_hash=built["artifact_hash"], limit=5)
        row = next(r for r in rows if int(r["id"]) == int(built["id"]))
        self.assertIsNone(row.get("consumed_ts"))

    def test_the_statement_is_canonical_and_deterministic(self):
        head = self.commit("canon")
        one = prov.signed_statement(commit=head, actor="bureau", session=SESSION,
                                    harness="claude", host=HOST, lot=42)
        two = prov.signed_statement(commit=head.lower(), actor="bureau",
                                    session=SESSION, harness="claude",
                                    host=HOST, lot=42)
        self.assertEqual(one, two)
        self.assertIn('"version":1', one)
        self.assertEqual(prov.signed_digest(
            commit=head, actor="bureau", session=SESSION, harness="claude",
            host=HOST, lot=42),
            prov.signed_digest(commit=head, actor="bureau", session=SESSION,
                               harness="claude", host=HOST, lot=42))

    # -- les refus nommés --------------------------------------------------
    def test_an_execute_agent_cannot_be_signed_as_an_author(self):
        registry.upsert(self.db, "executeur", harness="claude",
                        host=self.cfg.host, mode="execute")
        head = self.commit()
        with self.assertRaises(prov.ProvenanceError) as raised:
            self.sign(head, actor="executeur")
        self.assertIn("execute agent", str(raised.exception))

    def test_a_live_lease_removes_the_signed_path(self):
        head = self.commit()
        self.db.execute(
            "UPDATE agent_registry SET lease_owner = 'runner-x', lease_epoch = 1,"
            " lease_expires_at = now() + interval '1 hour' WHERE name = 'bureau'")
        with self.assertRaises(prov.ProvenanceError) as raised:
            self.sign(head)
        self.assertIn("live lease", str(raised.exception))

    def test_the_signed_harness_must_match_the_registry(self):
        head = self.commit()
        with self.assertRaises(prov.ProvenanceError) as raised:
            self.sign(head, harness="codex")
        self.assertIn("registered under", str(raised.exception))

    def test_the_signed_harness_must_be_a_compiled_family(self):
        head = self.commit()
        with self.assertRaises(prov.ProvenanceError) as raised:
            self.sign(head, families=("codex", "deepseek"))
        self.assertIn("unknown harness", str(raised.exception))

    def test_no_signature_proves_nothing(self):
        head = self.commit()
        with self.assertRaises(prov.ProvenanceError) as raised:
            prov.record_signed_commit(
                self.db, head, actor="bureau", session=SESSION, harness="claude",
                host=HOST, families=FAMILIES, cwd=self.repo)
        self.assertIn("--key", str(raised.exception))

    def test_an_agent_key_cannot_sign_provenance(self):
        agent_seed = signing.backend("pure").generate_seed()
        agent_public = signing.backend("pure").public_from_seed(agent_seed)
        registry.upsert(self.db, "ameesh-cx3", harness="codex",
                        host=self.cfg.host, mode="externe")
        authority.register_key(self.db, "ameesh-cx3", agent_public)  # rôle agent
        key_path = os.path.join(self.tmp, "agent.key")
        with open(key_path, "w", encoding="utf-8") as fh:
            fh.write(signing.encode_private(agent_seed))
        os.chmod(key_path, 0o600)
        head = self.commit()
        with self.assertRaises(prov.ProvenanceError) as raised:
            self.sign(head, approver="ameesh-cx3", key_path=key_path)
        self.assertIn("owner key", str(raised.exception))

    def test_a_revoked_owner_key_proves_nothing(self):
        head = self.commit()
        built = self.approval(head)
        authority.revoke_key(self.db, OWNER)
        with self.assertRaises(prov.ProvenanceError) as raised:
            prov.record_signed_commit(
                self.db, head, actor="bureau", session=SESSION, harness="claude",
                host=HOST, families=FAMILIES, lot=int(self.lot["id"]),
                approval_id=int(built["id"]), cwd=self.repo)
        self.assertIn("revoked", str(raised.exception))

    def test_an_approval_for_another_statement_is_refused(self):
        head = self.commit("autre")
        built = self.approval(head)
        # Même commit, mais une session différente : l'approbation ne couvre pas.
        with self.assertRaises(prov.ProvenanceError) as raised:
            prov.record_signed_commit(
                self.db, head, actor="bureau", session="autre-session",
                harness="claude", host=HOST, families=FAMILIES,
                lot=int(self.lot["id"]), approval_id=int(built["id"]),
                cwd=self.repo)
        self.assertIn("does not cover", str(raised.exception))

    def test_a_consumed_approval_cannot_be_replayed(self):
        head = self.commit()
        built = self.approval(head)
        prov.record_signed_commit(
            self.db, head, actor="bureau", session=SESSION, harness="claude",
            host=HOST, families=FAMILIES, lot=int(self.lot["id"]),
            approval_id=int(built["id"]), cwd=self.repo)
        # Le reçu existe déjà : la même identité est idempotente, sans relire
        # l'approbation consommée.
        again = prov.record_signed_commit(
            self.db, head, actor="bureau", session=SESSION, harness="claude",
            host=HOST, families=FAMILIES, lot=int(self.lot["id"]),
            approval_id=int(built["id"]), cwd=self.repo)
        self.assertFalse(again["recorded"])
        # La preuve ne se réécrit pas sous une autre identité.
        with self.assertRaises(prov.ProvenanceError) as raised:
            prov.record_signed_commit(
                self.db, head, actor="bureau", session="autre-session",
                harness="claude", host=HOST, families=FAMILIES,
                approval_id=int(built["id"]), cwd=self.repo)
        self.assertIn("already recorded", str(raised.exception))

    def test_the_lot_must_exist(self):
        head = self.commit()
        with self.assertRaises(prov.ProvenanceError) as raised:
            prov.record_signed_commit(
                self.db, head, actor="bureau", session=SESSION,
                harness="claude", host=HOST, families=FAMILIES, lot=999999,
                approver=OWNER, key_path=self.key_path, cwd=self.repo)
        self.assertIn("not found", str(raised.exception))


class SignedProvenanceCliTest(PgTestCase):
    """Le geste réel : `ameesh provenance record --signed` (clé owner en 0600)."""

    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "bureau", harness="claude", host=self.cfg.host,
                        mode="externe")
        self.lot = work.add(self.db, title="un lot signé du CLI", app="ameesh",
                            actor="orchestrateur")
        self.seed = signing.backend("pure").generate_seed()
        self.public = signing.backend("pure").public_from_seed(self.seed)
        authority.register_human_key(self.db, OWNER, self.public, role="owner")
        self.key_path = os.path.join(self.tmp, "owner.key")
        with open(self.key_path, "w", encoding="utf-8") as fh:
            fh.write(signing.encode_private(self.seed))
        os.chmod(self.key_path, 0o600)
        self.repo = tempfile.mkdtemp(prefix="ameesh-4485-cli-", dir=self.tmp)
        subprocess.run(["git", "init", "-q", self.repo], check=True,
                       capture_output=True)
        env = dict(os.environ,
                   GIT_AUTHOR_NAME="smichea",
                   GIT_AUTHOR_EMAIL="sebastien.michea@manaty.net",
                   GIT_COMMITTER_NAME="smichea",
                   GIT_COMMITTER_EMAIL="sebastien.michea@manaty.net")
        with open(os.path.join(self.repo, "f.txt"), "w", encoding="utf-8") as fh:
            fh.write("x\n")
        subprocess.run(["git", "add", "f.txt"], cwd=self.repo, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "canon"], cwd=self.repo,
                       check=True, capture_output=True, env=env)
        self.head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, text=True).strip()

    def test_the_cli_records_a_signed_commit(self):
        canon = mechanics_canon(os.path.join(self.tmp, "canon"))
        env = self.env(AMEESH_CANON=canon, AMEESH_CANON_REF="HEAD")
        out = self.mesh(
            "provenance", "record", "--signed", "--commit", self.head,
            "--actor", "bureau", "--session", SESSION, "--harness", "claude",
            "--host", HOST, "--lot", str(self.lot["id"]),
            "--key", self.key_path, "--as", OWNER, "--repo", self.repo,
            env=env)
        self.assertEqual(out.returncode, 0, out.stderr + out.stdout)
        receipt = prov.receipt_for_commit(self.db, self.head)
        self.assertEqual(receipt["actor"], "bureau")
        self.assertTrue(receipt["turn"].startswith("signed:"))

    def test_the_statement_subcommand_prints_the_digest(self):
        out = self.mesh("provenance", "statement", "--commit", self.head,
                        "--actor", "bureau", "--session", SESSION,
                        "--harness", "claude", "--host", HOST,
                        "--lot", str(self.lot["id"]), "--json")
        self.assertEqual(out.returncode, 0, out.stderr + out.stdout)
        import json
        data = json.loads(out.stdout)
        expected = prov.signed_digest(
            commit=self.head, actor="bureau", session=SESSION,
            harness="claude", host=HOST, lot=int(self.lot["id"]))
        self.assertEqual(data["digest"], expected)


if __name__ == "__main__":
    unittest.main()
