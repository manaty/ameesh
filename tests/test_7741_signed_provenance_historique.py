# SPDX-License-Identifier: AGPL-3.0-only
"""#7741 — attestation owner d'une session historiquement liée à deux agents.

La session Codex du poste `01a186ba-…` a été liée à `bureau` par des lignes
#32–50 (toutes révoquées), puis à `veille` par #54, active pendant les commits
du 17/11. `_signed_identity` parcourait toutes les lignes, révoquées comprises,
et refusait dès qu'un agent différait : `--signed --actor bureau` refusait #54,
`--signed --actor veille` refusait #32–50. La seule issue était de réétiqueter
les commits, ce qui est faux.

#7741 retient la liaison qui **couvrait la date de committer** : un commit fait
sous veille se signe `veille`, un commit fait sous bureau se signe `bureau` ;
un commit hors de tout intervalle, une session liée après le commit (session
artificielle) ou sur un autre hôte sont refusés. #7725 reste entier pour les
reçus de session, et les refus du committer tiers, de la clé déléguée et du
reçu révoqué restent effectifs.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import types
import unittest
from unittest import mock

from ameesh import authority, registry, review_authors, review_provenance as prov
from ameesh import signing, storage, work

from .support import PgTestCase

FAMILIES = ("claude", "codex", "deepseek")
OWNER = "smichea"
BUREAU = "bureau"
VEILLE = "veille"
#: la session Codex du poste (#32–50 bureau, #54 veille)
SESSION = "01a186ba-ae8b-7893-b721-46140003838d"
#: une session « nouvelle » pour le témoin artificiel
NEW_SESSION = "01a186ba-ae8b-7893-b721-461400038399"
#: les six SHA de #7678 sont eux aussi dans l'intervalle #54 ; leurs dates
#: exactes ne sont pas nécessaires au banc, seule la relation d'intervalle l'est.
BUREAU_CREATED = 1794700000.0
BUREAU_REVOKED = 1794750000.0
COMMIT_BUREAU = 1794720000.0
COMMIT_UNBOUND = 1794770000.0
VEILLE_CREATED = 1794800000.0
#: date de committer réelle de d4a8170e (17/11 09:36:13Z), sous #54
COMMIT_VEILLE = 1794908173.0


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))


class SignedMultiBindingTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute(
            "TRUNCATE provenance_receipts, provenance_revocations,"
            " mesh_approvals, mesh_consumed_nonces, session_bindings"
            " RESTART IDENTITY CASCADE")
        registry.upsert(self.db, BUREAU, harness="codex", host=self.cfg.host,
                        mode="externe")
        registry.upsert(self.db, VEILLE, harness="codex", host=self.cfg.host,
                        mode="externe")
        self.lot = work.add(self.db, title="un lot d'une session multiliaison",
                            app="ameesh", actor="orchestrateur")
        self.seed = signing.backend("pure").generate_seed()
        self.public = signing.backend("pure").public_from_seed(self.seed)
        authority.register_human_key(self.db, OWNER, self.public, role="owner")
        self.key_path = os.path.join(self.tmp, "owner.key")
        with open(self.key_path, "w", encoding="utf-8") as fh:
            fh.write(signing.encode_private(self.seed))
        os.chmod(self.key_path, 0o600)
        self.repo = tempfile.mkdtemp(prefix="ameesh-7741-", dir=self.tmp)
        subprocess.run(["git", "init", "-q", self.repo], check=True,
                       capture_output=True)

    # -- utilitaires ------------------------------------------------------
    def bind_row(self, agent, *, created, revoked=None, session=SESSION,
                 harness="codex"):
        """La ligne telle que `mail bind` l'a écrite, puis datée par le banc.

        L'API `sb.bind` refuse `veille` depuis #7725 : la liaison historique
        #54 existe déjà en base, on écrit donc la ligne directement, comme le
        fit le geste du 13/11.
        """
        row = storage.of(self.db).session_bindings.bind(
            host=self.cfg.host, harness=harness, session_id=session,
            agent=agent, pid=None, created_by="human:test")
        self.assertIsNotNone(row)
        if revoked is None:
            self.db.execute(
                "UPDATE session_bindings SET created_at = to_timestamp(%s)"
                " WHERE id = %s", (float(created), int(row["id"])))
        else:
            self.db.execute(
                "UPDATE session_bindings SET created_at = to_timestamp(%s),"
                " revoked_at = to_timestamp(%s) WHERE id = %s",
                (float(created), float(revoked), int(row["id"])))
        row["created_ts"] = float(created)
        row["revoked_ts"] = float(revoked) if revoked is not None else None
        return row

    def history(self):
        """bureau #32–50 révoquée, puis veille #54 active (le cas du poste)."""
        bureau = self.bind_row(BUREAU, created=BUREAU_CREATED,
                               revoked=BUREAU_REVOKED)
        veille = self.bind_row(VEILLE, created=VEILLE_CREATED)
        return bureau, veille

    def commit(self, ts, message="un", *, author=("Codex (PC)",
                                                  "codex-pc@users.noreply.github.com"),
               committer=None):
        path = os.path.join(self.repo, "f.txt")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(message + "\n")
        subprocess.run(["git", "add", "f.txt"], cwd=self.repo, check=True,
                       capture_output=True)
        name, email = committer or author
        env = dict(os.environ,
                   GIT_AUTHOR_NAME=author[0], GIT_AUTHOR_EMAIL=author[1],
                   GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email,
                   GIT_AUTHOR_DATE=iso(ts), GIT_COMMITTER_DATE=iso(ts))
        subprocess.run(["git", "commit", "-q", "-m", message], cwd=self.repo,
                       check=True, capture_output=True, env=env)
        return subprocess.check_output(["git", "rev-parse", "HEAD"],
                                       cwd=self.repo, text=True).strip()

    def sign(self, sha, *, actor=VEILLE, session=SESSION, harness="codex",
             host=None, lot=True, **kwargs):
        fields = dict(
            actor=actor, session=session, harness=harness,
            host=host or self.cfg.host, families=FAMILIES,
            lot=int(self.lot["id"]) if lot else None,
            approver=OWNER, key_path=self.key_path, cwd=self.repo,
        )
        fields.update(kwargs)
        return prov.record_signed_commit(self.db, sha, **fields)

    def approval(self, sha, *, actor=VEILLE, session=SESSION, harness="codex",
                 host=None, lot=True, decision="approved"):
        host = host or self.cfg.host
        lot_value = int(self.lot["id"]) if lot else None
        statement = prov.signed_statement(
            commit=sha, actor=actor, session=session, harness=harness,
            host=host, lot=lot_value)
        digest = prov.signed_digest(
            commit=sha, actor=actor, session=session, harness=harness,
            host=host, lot=lot_value)
        return authority.create_approval(
            self.db, self.seed, approver=OWNER, action=prov.SIGNED_ACTION,
            artifact_kind=prov.SIGNED_KIND, artifact_hash=digest,
            decision=decision, meta={"statement": statement})

    def approval_meta(self, receipt):
        turn = str(receipt.get("turn") or "")
        self.assertTrue(turn.startswith(prov.SIGNED_TURN_PREFIX))
        row = storage.of(self.db).approvals.get(
            int(turn[len(prov.SIGNED_TURN_PREFIX):]))
        self.assertIsNotNone(row)
        return row.get("meta") or {}

    def revoke(self, sha, *, actor="human:test", reason="preuve retirée"):
        nobody = types.SimpleNamespace(name="", ok=False, source="none", reason="")
        with mock.patch("ameesh.identity.resolve_binding", return_value=nobody), \
                mock.patch.dict(os.environ, {"AGENT_MAIL_NAME": ""}):
            return prov.revoke_receipt(self.db, sha, actor=actor, reason=reason)

    # -- témoins positifs : l'agent de l'intervalle -------------------------
    def test_a_commit_made_under_veille_is_signed_as_veille(self):
        _, veille_row = self.history()
        head = self.commit(COMMIT_VEILLE, "sous veille")
        row = self.sign(head, actor=VEILLE)
        self.assertTrue(row["recorded"])
        receipt = prov.receipt_for_commit(self.db, head)
        self.assertEqual(receipt["actor"], VEILLE)
        self.assertEqual(receipt["session"], SESSION)
        self.assertEqual(receipt["harness"], "codex")
        # La porte lit la famille de la signature, pas un nom Git.
        authors, unknown, _ = review_authors.reading(
            self.db, {"id": int(self.lot["id"]), "assignee": VEILLE,
                      "app": "ameesh"},
            None, [head], repo=self.repo, families=FAMILIES, strict=True)
        self.assertEqual(unknown, [])
        self.assertEqual(authors, [{"name": VEILLE, "family": "codex"}])
        # Le diagnostic d'audit nomme la liaison #54, sans changer le texte.
        meta = self.approval_meta(receipt)
        binding = meta.get("binding") or {}
        self.assertEqual(binding.get("agent"), VEILLE)
        self.assertEqual(int(binding.get("id") or 0), int(veille_row["id"]))
        self.assertIsNone(binding.get("revoked_ts"))

    def test_a_commit_made_under_the_revoked_bureau_interval_names_bureau(self):
        bureau_row, _ = self.history()
        head = self.commit(COMMIT_BUREAU, "sous bureau")
        row = self.sign(head, actor=BUREAU)
        self.assertTrue(row["recorded"])
        receipt = prov.receipt_for_commit(self.db, head)
        self.assertEqual(receipt["actor"], BUREAU)
        binding = self.approval_meta(receipt).get("binding") or {}
        self.assertEqual(binding.get("agent"), BUREAU)
        self.assertEqual(int(binding.get("id") or 0), int(bureau_row["id"]))
        self.assertEqual(float(binding.get("revoked_ts")), BUREAU_REVOKED)

    # -- témoins négatifs : ni réattribution ni session artificielle --------
    def test_bureau_cannot_claim_a_commit_made_under_veille(self):
        self.history()
        head = self.commit(COMMIT_VEILLE, "fait sous veille")
        with self.assertRaises(prov.ProvenanceError) as raised:
            self.sign(head, actor=BUREAU)
        self.assertIn("was bound to veille at commit time", str(raised.exception))
        self.assertIn("not to the signed actor bureau", str(raised.exception))
        self.assertIsNone(prov.receipt_for_commit(self.db, head))

    def test_veille_cannot_claim_a_commit_made_under_bureau(self):
        self.history()
        head = self.commit(COMMIT_BUREAU, "fait sous bureau")
        with self.assertRaises(prov.ProvenanceError) as raised:
            self.sign(head, actor=VEILLE)
        self.assertIn("was bound to bureau at commit time", str(raised.exception))
        self.assertIn("not to the signed actor veille", str(raised.exception))
        self.assertIsNone(prov.receipt_for_commit(self.db, head))

    def test_a_commit_after_unbind_has_no_attributable_actor(self):
        self.history()
        head = self.commit(COMMIT_UNBOUND, "entre les deux liaisons")
        for actor in (BUREAU, VEILLE):
            with self.assertRaises(prov.ProvenanceError) as raised:
                self.sign(head, actor=actor)
            self.assertIn("outside every interval", str(raised.exception))
            self.assertIsNone(prov.receipt_for_commit(self.db, head))

    def test_a_new_session_cannot_claim_the_old_commit(self):
        self.history()
        head = self.commit(COMMIT_VEILLE, "session neuve")
        # Aucune liaison : l'hôte utilise ameesh, la session n'est pas prouvée.
        with self.assertRaises(prov.ProvenanceError) as raised:
            self.sign(head, actor=VEILLE, session=NEW_SESSION)
        self.assertIn("has no binding", str(raised.exception))
        # Liée après le commit : elle ne peut pas le revendiquer rétroactivement.
        self.bind_row(VEILLE, created=COMMIT_VEILLE + 3600,
                      session=NEW_SESSION)
        with self.assertRaises(prov.ProvenanceError) as raised:
            self.sign(head, actor=VEILLE, session=NEW_SESSION)
        self.assertIn("outside every interval", str(raised.exception))
        self.assertIsNone(prov.receipt_for_commit(self.db, head))

    def test_x_then_y_then_x_does_not_reclaim_the_y_commit(self):
        """#7719 B1 étendu à la voie signée : le retour à X ne récupère pas le
        commit de l'époque Y."""
        self.bind_row(BUREAU, created=1794600000, revoked=1794650000)
        self.bind_row(VEILLE, created=1794700000, revoked=1794750000)
        self.bind_row(BUREAU, created=1794800000)
        under_y = self.commit(1794720000, "sous veille, entre deux bureaux")
        with self.assertRaises(prov.ProvenanceError) as raised:
            self.sign(under_y, actor=BUREAU)
        self.assertIn("was bound to veille at commit time", str(raised.exception))
        # Le commit fait après le second lien bureau reste, lui, attribuable.
        under_x_again = self.commit(1794820000, "bureau à nouveau")
        self.assertTrue(self.sign(under_x_again, actor=BUREAU)["recorded"])

    def test_the_host_of_the_binding_is_part_of_the_proof(self):
        self.history()
        head = self.commit(COMMIT_VEILLE, "autre hôte")
        with self.assertRaises(prov.ProvenanceError) as raised:
            self.sign(head, actor=VEILLE, host="bureau-goal")
        self.assertIn("is bound on", str(raised.exception))
        self.assertIn("not on bureau-goal", str(raised.exception))
        self.assertIsNone(prov.receipt_for_commit(self.db, head))

    # -- les gardes conservées --------------------------------------------
    def test_a_third_party_committer_is_still_refused(self):
        self.history()
        head = self.commit(COMMIT_VEILLE, "committer tiers",
                           committer=(BUREAU, BUREAU + "@ameesh.invalid"))
        with self.assertRaises(prov.ProvenanceError) as raised:
            self.sign(head, actor=VEILLE)
        self.assertIn("was committed by bureau, not by veille",
                      str(raised.exception))
        self.assertIsNone(prov.receipt_for_commit(self.db, head))

    def test_the_delegated_key_still_cannot_attest(self):
        self.history()
        head = self.commit(COMMIT_VEILLE, "clé déléguée")
        authority.register_key(self.db, BUREAU, self.public, role="owner")
        with self.assertRaises(prov.ProvenanceError) as raised:
            self.sign(head, actor=VEILLE)
        self.assertIn("also held by agent bureau", str(raised.exception))
        self.assertIsNone(prov.receipt_for_commit(self.db, head))

    def test_a_revoked_receipt_is_not_resurrected(self):
        self.history()
        head = self.commit(COMMIT_VEILLE, "à révoquer")
        self.assertTrue(self.sign(head, actor=VEILLE)["recorded"])
        self.revoke(head)
        self.assertIsNone(prov.receipt_for_commit(self.db, head))
        with self.assertRaises(prov.ProvenanceError) as raised:
            self.sign(head, actor=VEILLE)
        self.assertIn("revoked", str(raised.exception))

    def test_7725_still_refuses_a_session_receipt(self):
        """#7725 n'est pas contourné : le même commit est refusé en reçu de
        session, et seule la signature owner l'atteste."""
        self.history()
        head = self.commit(COMMIT_VEILLE, "veille sans boîte lue")
        with self.assertRaises(prov.ProvenanceError) as raised:
            prov.record_session_commit(
                self.db, head, session=SESSION, harness="codex",
                families=FAMILIES, cfg=self.cfg, cwd=self.repo)
        self.assertIn("no read mailbox", str(raised.exception))
        self.assertIsNone(prov.receipt_for_commit(self.db, head))
        self.assertTrue(self.sign(head, actor=VEILLE)["recorded"])

    def test_show_prints_the_historical_binding(self):
        _, veille_row = self.history()
        head = self.commit(COMMIT_VEILLE, "affiché")
        self.assertTrue(self.sign(head, actor=VEILLE)["recorded"])
        shown = self.mesh("provenance", "show", "--commit", head, "--json")
        self.assertEqual(shown.returncode, 0, shown.stderr + shown.stdout)
        data = json.loads(shown.stdout)
        self.assertEqual(data["binding"]["agent"], VEILLE)
        self.assertEqual(int(data["binding"]["id"]), int(veille_row["id"]))
        text = self.mesh("provenance", "show", "--commit", head)
        self.assertEqual(text.returncode, 0, text.stderr + text.stdout)
        self.assertIn("historical binding: #%d veille/codex"
                      % int(veille_row["id"]), text.stdout)

    def test_a_pre_signed_approval_keeps_the_historical_binding(self):
        """L'owner peut signer avant la consommation ; la liaison retenue est
        écrite au moment de la consommation, sans toucher au texte signé."""
        self.history()
        head = self.commit(COMMIT_VEILLE, "pré-signé")
        built = self.approval(head)
        row = prov.record_signed_commit(
            self.db, head, actor=VEILLE, session=SESSION, harness="codex",
            host=self.cfg.host, families=FAMILIES, lot=int(self.lot["id"]),
            approval_id=int(built["id"]), cwd=self.repo)
        self.assertTrue(row["recorded"])
        self.assertEqual(prov.receipt_for_commit(self.db, head)["actor"], VEILLE)
        after = storage.of(self.db).approvals.get(int(built["id"]))
        self.assertEqual(after["artifact_hash"], built["artifact_hash"])
        self.assertEqual(after["signed_payload"], built["signed_payload"])
        binding = (after.get("meta") or {}).get("binding") or {}
        self.assertEqual(binding.get("agent"), VEILLE)


if __name__ == "__main__":
    unittest.main()
