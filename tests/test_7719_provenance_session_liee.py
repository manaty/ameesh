# SPDX-License-Identifier: AGPL-3.0-only
"""#7719 — un commit d'une session native liée reçoit un reçu de provenance.

Le 17/11, trois commits de #7705 et six de #7678/#7715 portaient l'identité
Git native `Codex (PC) <codex-pc@users.noreply.github.com>` : aucun reçu,
auteur absent du registre, `record_commit` refusait le committer (il exige
`<agent>@ameesh.invalid`) et `by_ancestry` ne retrouvait pas la session
native. La liaison `ameesh mail bind` (session `01a186ba-…`, harnais codex,
hôte pc-smichea) connaissait pourtant l'agent ET la famille.

#7725 : une session ne se lie pas à `veille`, identité d'ÉMISSION sans boîte
lue ; la session du poste est liée à l'agent qu'elle porte (`bureau`, codex
sur le PC), et le reçu porte cet acteur. Une liaison historique à `veille` ne
donne plus d'identité : ni reçu à l'écriture, ni preuve à la lecture. Un reçu
ne couvre qu'un commit **fait pendant** une liaison de la session à cet agent
(active ou révoquée) : une session reliée à X, puis à Y, puis de nouveau à X
ne réclame pas sous X un commit de l'époque Y ; les commits faits sous
`veille` relèvent de l'attestation owner, pas d'une réparation.

Le reçu est écrit par la session liée, à la création (hooks, identité de
session dans l'environnement) ou à la poussée (`ameesh provenance record
--session`) : la liaison donne acteur/harnais/session, le commit doit être
fait pendant un intervalle de cette liaison, porté par une ref locale, et son
committer ne peut pas être un autre agent enregistré. Une preuve déjà écrite pour une autre
identité est refusée, et un reçu de session dont la liaison n'existe pas est
tenu pour incohérent par la porte.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest

from ameesh import registry, review_authors, review_provenance as prov
from ameesh import session_bindings as sb, storage, work

from .support import PgTestCase, mechanics_canon

FAMILIES = ("claude", "codex", "deepseek")
CODEX = ("codex-pc", "codex-pc@users.noreply.github.com")
BUREAU = "bureau"
SESSION = "s-bureau"


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class SessionReceiptTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE session_bindings RESTART IDENTITY")
        self.lot = work.add(self.db, title="un lot d'une session native",
                            app="ameesh", actor="orchestrateur")

    # -- utilitaires ------------------------------------------------------
    def bind(self, *, agent=BUREAU, session=SESSION, harness="codex", pid=None):
        return sb.bind(self.cfg, self.db, agent, session_id=session,
                       harness=harness, pid=pid, by="human:test")

    def unbind(self, *, session=SESSION, harness="codex"):
        return sb.unbind(self.cfg, self.db, session_id=session, harness=harness,
                         by="human:test")

    def legacy_binding(self, *, agent="veille", session=SESSION,
                       harness="codex", created=None):
        """La liaison #54 d'avant #7725 : l'API refuse de la créer aujourd'hui.

        On écrit donc la ligne directement, comme le fit `mail bind veille`
        le 13/11 — c'est cette ligne active qui ne doit plus donner d'identité.
        """
        row = storage.of(self.db).session_bindings.bind(
            host=self.cfg.host, harness=harness, session_id=session,
            agent=agent, pid=None, created_by="human:test")
        self.assertIsNotNone(row)
        self.age_binding(row, created=created or time.time() - 4 * 86400)
        return row

    def age_binding(self, row, *, created, revoked=None):
        self.db.execute(
            "UPDATE session_bindings SET created_at = to_timestamp(%s) WHERE id = %s",
            (float(created), int(row["id"])))
        if revoked is not None:
            self.db.execute(
                "UPDATE session_bindings SET revoked_at = to_timestamp(%s)"
                " WHERE id = %s", (float(revoked), int(row["id"])))

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

    def record(self, head, path, *, session=SESSION, harness="codex"):
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
        self.assertEqual(receipt["actor"], BUREAU)
        self.assertEqual(receipt["session"], SESSION)
        self.assertEqual(receipt["turn"], "session:" + SESSION)
        self.assertEqual(receipt["harness"], "codex")
        # La porte lit la famille du harnais de la liaison ; le nom Git
        # `codex-pc` n'est ni deviné ni crédité.
        authors, unknown, _ = review_authors.reading(
            self.db, {"id": int(self.lot["id"]), "assignee": BUREAU,
                      "app": "ameesh"},
            None, [head], repo=path, families=FAMILIES, strict=True)
        self.assertEqual(unknown, [])
        self.assertIn({"name": BUREAU, "family": "codex"}, authors)

    def test_the_creation_hooks_accept_the_session_identity(self):
        """La paire de hooks lit `CODEX_SESSION_ID` et enregistre sans PID."""
        self.bind()
        path, env = self.repo()
        hook_env = {"CODEX_SESSION_ID": SESSION, "AMEESH_HARNESS": "codex"}
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
        self.assertEqual(ident["actor"], BUREAU)
        self.assertEqual(ident["session"], SESSION)
        self.assertEqual(ident["turn"], "session:" + SESSION)
        prov.record_commit(self.db, head, identity=ident, cwd=path)
        receipt = prov.receipt_for_commit(self.db, head)
        self.assertEqual(receipt["actor"], BUREAU)
        self.assertEqual(receipt["harness"], "codex")

    def test_a_commit_before_the_binding_is_refused(self):
        path, env = self.repo(date="2026-11-16T00:00:00Z")
        head = self.commit(path, env, "avant la liaison")
        self.bind()
        self.assertIn("outside every interval", self.refused(head, path))

    def test_a_commit_of_another_registered_agent_is_refused(self):
        registry.upsert(self.db, "auditeur", harness="claude",
                        host=self.cfg.host, mode="externe")
        self.bind(agent=BUREAU)
        path, env = self.repo(name="auditeur", email="auditeur@ameesh.invalid")
        head = self.commit(path, env)
        message = self.refused(head, path)
        self.assertIn("committed by auditeur", message)
        self.assertIn("not by " + BUREAU, message)
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
        self.unbind()
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
            prov.capture({"CODEX_SESSION_ID": SESSION,
                          "AMEESH_HARNESS": "codex"},
                         families=FAMILIES, db=self.db, cfg=self.cfg)
        self.assertIn("session binding", str(raised.exception))

    def test_the_ambient_identity_accepts_its_own_pid_binding(self):
        """La garde B1 ne casse pas la session dont le PID est bien ancêtre."""
        self.bind(pid=os.getpid())
        ident = prov.capture({"CODEX_SESSION_ID": SESSION,
                              "AMEESH_HARNESS": "codex"},
                             families=FAMILIES, db=self.db, cfg=self.cfg)
        self.assertEqual(ident["actor"], BUREAU)
        self.assertEqual(ident["source"], "session")

    def test_the_hooks_do_not_record_a_commit_before_the_binding(self):
        """B1 #7719 : la voie des hooks borne aussi la date à la liaison."""
        self.bind()
        path, env = self.repo(date="2026-11-16T00:00:00Z")
        hook_env = {"CODEX_SESSION_ID": SESSION, "AMEESH_HARNESS": "codex"}
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
        self.assertIn("outside every interval", str(raised.exception))
        self.assertIsNone(prov.receipt_for_commit(self.db, head))

    def test_another_binding_cannot_claim_an_already_received_commit(self):
        """Témoin (3) : le reçu d'une liaison ne se réattribue pas à une autre."""
        self.bind(agent=BUREAU, session=SESSION)
        self.bind(agent="codex2", session="s-codex2")
        path, env = self.repo()
        head = self.commit(path, env)
        self.record(head, path, session=SESSION)
        message = self.refused(head, path, session="s-codex2")
        self.assertIn("already recorded for %s/session:%s" % (BUREAU, SESSION),
                      message)
        receipt = prov.receipt_for_commit(self.db, head)
        self.assertEqual(receipt["actor"], BUREAU)

    def test_a_session_receipt_without_its_binding_is_refused(self):
        """Témoin (3) : un reçu forgé, dont la session n'est pas liée."""
        self.bind()
        path, env = self.repo()
        head = self.commit(path, env)
        storage.of(self.db).provenance.add(
            "commit:" + head, head, BUREAU, "s-ghost", "session:s-ghost",
            "codex")
        with self.assertRaises(prov.ProvenanceError) as raised:
            prov.receipt_for_commit(self.db, head)
        self.assertIn("no binding links", str(raised.exception))

    def test_a_receipt_of_another_agents_binding_is_refused(self):
        self.bind(agent=BUREAU, session=SESSION)
        path, env = self.repo()
        head = self.commit(path, env)
        storage.of(self.db).provenance.add(
            "commit:" + head, head, "auditeur", SESSION, "session:" + SESSION,
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

    # -- #7725 : veille n'est pas une identité de session ------------------
    def test_a_legacy_watch_binding_writes_no_receipt(self):
        """La ligne #54 restée sur `veille` ne contourne pas #7725 à l'écriture."""
        self.legacy_binding()
        path, env = self.repo()
        head = self.commit(path, env)
        message = self.refused(head, path)
        self.assertIn("no read mailbox", message)
        self.assertIn("agent it carries", message)
        self.assertIsNone(prov.receipt_for_commit(self.db, head))

    def test_a_legacy_watch_receipt_is_not_proof(self):
        """Un reçu `session:` au nom de veille n'est pas une preuve (#7725)."""
        self.legacy_binding()
        path, env = self.repo()
        head = self.commit(path, env)
        storage.of(self.db).provenance.add(
            "commit:" + head, head, "veille", SESSION, "session:" + SESSION,
            "codex")
        with self.assertRaises(prov.ProvenanceError) as raised:
            prov.receipt_for_commit(self.db, head)
        self.assertIn("no read mailbox", str(raised.exception))

    def test_a_rebinding_does_not_cover_a_commit_made_under_another_agent(self):
        """B1 #43280 : X → Y → X ne réclame pas un commit de l'époque Y.

        La session a été liée à bureau, puis à veille, puis de nouveau à
        bureau. Un commit fait pendant l'intervalle veille n'est couvert par
        aucune liaison à bureau : le premier lien au même agent ne suffit pas.
        """
        now = time.time()
        first = self.bind().row
        self.unbind()
        self.age_binding(first, created=now - 10 * 86400,
                         revoked=now - 9 * 86400)
        second = self.legacy_binding(created=now - 8 * 86400)
        self.unbind()
        self.age_binding(second, created=now - 8 * 86400,
                         revoked=now - 3 * 86400)
        self.bind()
        path, env = self.repo(date=iso(now - 6 * 86400))
        head = self.commit(path, env, "commit de l'époque veille")
        self.assertIn("outside every interval", self.refused(head, path))
        self.assertIsNone(prov.receipt_for_commit(self.db, head))

    def test_a_rebinding_does_not_cover_the_commits_made_under_veille(self):
        """#7705 : les commits faits sous #54/veille ne sont pas couverts.

        La réparation vers bureau crée une liaison plus récente que les
        commits ; aucun intervalle bureau ne contient leur date. La voie juste
        pour ces commits est l'attestation owner, pas une nouvelle liaison.
        """
        now = time.time()
        self.legacy_binding(created=now - 4 * 86400)
        path, env = self.repo(date=iso(now - 3600))
        head = self.commit(path, env, "commit du poste sous la liaison veille")
        self.unbind()
        self.bind()
        self.assertIn("outside every interval", self.refused(head, path))
        self.assertIsNone(prov.receipt_for_commit(self.db, head))

    def test_a_commit_made_while_bound_to_the_same_agent_stays_provable(self):
        """Un commit de l'intervalle bureau, enregistré après un rebind, reste
        couvert : c'est l'intervalle au moment du commit qui fait foi."""
        now = time.time()
        first = self.bind().row
        self.unbind()
        self.age_binding(first, created=now - 10 * 86400,
                         revoked=now - 9 * 86400)
        self.legacy_binding(created=now - 8 * 86400)
        path, env = self.repo(date=iso(now - 9.5 * 86400))
        head = self.commit(path, env, "commit de l'époque bureau")
        self.unbind()
        self.bind()
        row = self.record(head, path)
        self.assertTrue(row["recorded"])
        receipt = prov.receipt_for_commit(self.db, head)
        self.assertEqual(receipt["actor"], BUREAU)
        self.assertEqual(receipt["session"], SESSION)


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
        sb.bind(self.cfg, self.db, BUREAU, session_id="s-cli-7719",
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
        self.assertTrue(all(row["actor"] == BUREAU for row in receipts))
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
