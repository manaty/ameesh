# SPDX-License-Identifier: AGPL-3.0-only
"""#8170 — attestation owner d'un commit antérieur aux liaisons de session.

Huit commits de juillet de manaty-its (#8095) ont été faits sur pc-smichea
avant toute liaison de session : `record --signed` les refuse (« an unbound
session is not evidence of the author »), à raison. `record --signed
--historic` est une attestation d'autorité distincte : clé owner en personne,
SHA complet, auteur déclaré (humain ou agent + harnais), hôte et lot, reçu
`turn = "signed-historic:<approbation>"`.

Témoins : refus sans signature, refus quand une liaison de l'hôte aurait pu
couvrir le commit, refus d'un hôte sans liaison et d'une session liée,
signature consommée une fois, première écriture qui fait foi, et reçu lu par la
lecture d'auteurs (garde de gel, familles par commit) et par la porte.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time

from ameesh import authority, merge_queue, registry, review_authors
from ameesh import review_provenance as prov
from ameesh import signing, storage, work

from .support import PgTestCase
from .test_2447_review_designation_activation import _Repo

FAMILIES = ("claude", "codex", "deepseek")
OWNER = "smichea"
AGENT = "claude-manaty-6598"
HOST = "pc-smichea"
LABEL = "unknown-2026-07-pc-smichea"
#: la première liaison connue de l'hôte, et un commit de juillet avant elle
FIRST_BINDING = 1790000000.0
JULY = 1784600000.0
CRITICAL = "src/ameesh/merge_queue.py"


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))


class _Owner(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute(
            "TRUNCATE provenance_receipts, provenance_revocations,"
            " mesh_approvals, mesh_consumed_nonces, session_bindings"
            " RESTART IDENTITY CASCADE")
        self.seed = signing.backend("pure").generate_seed()
        authority.register_human_key(
            self.db, OWNER, signing.backend("pure").public_from_seed(self.seed),
            role="owner")
        self.key_path = os.path.join(self.tmp, "owner.key")
        with open(self.key_path, "w", encoding="utf-8") as fh:
            fh.write(signing.encode_private(self.seed))
        os.chmod(self.key_path, 0o600)

    def bind(self, *, host=HOST, created=FIRST_BINDING, session="s-first",
             agent="bureau", harness="claude"):
        row = storage.of(self.db).session_bindings.bind(
            host=host, harness=harness, session_id=session, agent=agent,
            pid=None, created_by="human:test")
        self.db.execute(
            "UPDATE session_bindings SET created_at = to_timestamp(%s),"
            " revoked_at = to_timestamp(%s) WHERE id = %s",
            (float(created), float(created) + 3600.0, int(row["id"])))
        return row

    def historic(self, sha, repo, *, actor=AGENT, harness="claude", host=HOST,
                 session=LABEL, **kwargs):
        fields = dict(actor=actor, harness=harness, host=host, session=session,
                      families=FAMILIES, lot=int(self.lot["id"]),
                      approver=OWNER, key_path=self.key_path, cwd=repo)
        fields.update(kwargs)
        return prov.record_historic_commit(self.db, sha, **fields)

    def presigned(self, sha, *, actor=AGENT, harness="claude", host=HOST,
                  session=LABEL, kind=None):
        fields = dict(commit=sha, actor=actor, harness=harness, host=host,
                      session=session, lot=int(self.lot["id"]))
        return authority.create_approval(
            self.db, self.seed, approver=OWNER, action=prov.SIGNED_ACTION,
            artifact_kind=kind or prov.HISTORIC_KIND,
            artifact_hash=prov.historic_digest(**fields),
            meta={"statement": prov.historic_statement(**fields)})


class HistoricRecordTest(_Owner):
    def setUp(self):
        super().setUp()
        registry.upsert(self.db, AGENT, harness="claude", host=HOST,
                        mode="externe")
        self.lot = work.add(self.db, title="huit commits de juillet",
                            app="ameesh", actor="orchestrateur")
        self.repo = tempfile.mkdtemp(prefix="ameesh-8170-", dir=self.tmp)
        subprocess.run(["git", "init", "-q", self.repo], check=True,
                       capture_output=True)
        self.bind()

    def commit(self, ts, message="juillet"):
        with open(os.path.join(self.repo, "f.txt"), "a", encoding="utf-8") as fh:
            fh.write(message + "\n")
        subprocess.run(["git", "add", "f.txt"], cwd=self.repo, check=True,
                       capture_output=True)
        env = dict(os.environ,
                   GIT_AUTHOR_NAME="smichea", GIT_AUTHOR_EMAIL="s@example.invalid",
                   GIT_COMMITTER_NAME="smichea",
                   GIT_COMMITTER_EMAIL="s@example.invalid",
                   GIT_AUTHOR_DATE=iso(ts), GIT_COMMITTER_DATE=iso(ts))
        subprocess.run(["git", "commit", "-q", "-m", message], cwd=self.repo,
                       check=True, capture_output=True, env=env)
        return subprocess.check_output(["git", "rev-parse", "HEAD"],
                                       cwd=self.repo, text=True).strip()

    def reading(self, sha):
        families: dict = {}
        commit_families: dict = {}
        global_families: set = set()
        authors, unknown, _ = review_authors.reading(
            self.db, {"id": int(self.lot["id"]), "assignee": "codex1",
                      "app": "ameesh"},
            None, [sha], repo=self.repo, families=FAMILIES, strict=True,
            commit_families_out=commit_families,
            global_families_out=global_families)
        return authors, unknown, commit_families, global_families

    # -- témoins positifs ---------------------------------------------------
    def test_an_agent_author_reads_the_attested_family(self):
        head = self.commit(JULY)
        row = self.historic(head, self.repo)
        self.assertTrue(row["recorded"])
        receipt = prov.receipt_for_commit(self.db, head)
        self.assertTrue(receipt["turn"].startswith(prov.HISTORIC_TURN_PREFIX))
        self.assertEqual((receipt["actor"], receipt["harness"],
                          receipt["session"]), (AGENT, "claude", LABEL))
        authors, unknown, per_commit, glob = self.reading(head)
        self.assertEqual(unknown, [])
        self.assertIn({"name": AGENT, "family": "claude"}, authors)
        self.assertEqual(per_commit[head], {"claude"})
        self.assertEqual(glob, set())
        # l'approbation porte la borne, hors du texte signé
        approval = storage.of(self.db).approvals.get(
            int(receipt["turn"][len(prov.HISTORIC_TURN_PREFIX):]))
        self.assertEqual(approval["artifact_kind"], prov.HISTORIC_KIND)
        first = (approval.get("meta") or {}).get("first_binding") or {}
        self.assertEqual(float(first.get("created_ts")), FIRST_BINDING)

    def test_a_human_author_excludes_no_family(self):
        head = self.commit(JULY)
        self.historic(head, self.repo, actor="human:alice", harness="")
        receipt = prov.receipt_for_commit(self.db, head)
        self.assertEqual((receipt["actor"], receipt["harness"]),
                         ("human:alice", prov.HUMAN_HARNESS))
        authors, unknown, per_commit, glob = self.reading(head)
        self.assertEqual(unknown, [])
        self.assertEqual(per_commit[head], set())
        self.assertEqual(glob, set())

    def test_a_presigned_approval_is_consumed_once(self):
        head = self.commit(JULY)
        approval = self.presigned(head)
        row = self.historic(head, self.repo, key_path="",
                            approval_id=int(approval["id"]))
        self.assertTrue(row["recorded"])
        # la preuve retirée de la table, l'approbation ne resservira pas
        self.db.execute("DELETE FROM provenance_receipts")
        with self.assertRaisesRegex(prov.ProvenanceError, "already consumed"):
            self.historic(head, self.repo, key_path="",
                          approval_id=int(approval["id"]))
        self.assertIsNone(prov.receipt_for_commit(self.db, head))

    # -- refus ----------------------------------------------------------------
    def test_refused_without_a_signature(self):
        head = self.commit(JULY)
        with self.assertRaisesRegex(prov.ProvenanceError, "--key"):
            self.historic(head, self.repo, key_path="")
        self.assertIsNone(prov.receipt_for_commit(self.db, head))

    def test_an_ordinary_signed_approval_does_not_cover_a_historic_one(self):
        head = self.commit(JULY)
        approval = self.presigned(head, kind=prov.SIGNED_KIND)
        with self.assertRaisesRegex(prov.ProvenanceError,
                                    "not a signed provenance statement"):
            self.historic(head, self.repo, key_path="",
                          approval_id=int(approval["id"]))
        self.assertIsNone(prov.receipt_for_commit(self.db, head))

    def test_refused_when_a_binding_of_the_host_could_have_covered_it(self):
        head = self.commit(FIRST_BINDING + 60.0)
        with self.assertRaisesRegex(prov.ProvenanceError,
                                    "not before the first session binding"):
            self.historic(head, self.repo)
        self.assertIsNone(prov.receipt_for_commit(self.db, head))
        # une liaison plus ancienne sur l'hôte déplace la borne
        self.bind(created=JULY - 3600.0, session="s-older")
        head = self.commit(JULY, "après la liaison ancienne")
        with self.assertRaisesRegex(prov.ProvenanceError,
                                    "not before the first session binding"):
            self.historic(head, self.repo)

    def test_refused_on_a_host_without_any_binding(self):
        head = self.commit(JULY)
        with self.assertRaisesRegex(prov.ProvenanceError,
                                    "no known session binding"):
            self.historic(head, self.repo, host="hote-invente")

    def test_refused_when_the_declared_session_is_bound(self):
        head = self.commit(JULY)
        with self.assertRaisesRegex(prov.ProvenanceError, "has a binding"):
            self.historic(head, self.repo, session="s-first")

    def test_refused_for_an_unregistered_agent_or_a_human_with_a_harness(self):
        head = self.commit(JULY)
        with self.assertRaisesRegex(prov.ProvenanceError, "absent from the registry"):
            self.historic(head, self.repo, actor="inconnu-9")
        with self.assertRaisesRegex(prov.ProvenanceError, "no model harness"):
            self.historic(head, self.repo, actor="human:alice", harness="claude")
        with self.assertRaisesRegex(prov.ProvenanceError, "unknown harness"):
            self.historic(head, self.repo, harness="human")

    def test_the_first_proof_stands(self):
        head = self.commit(JULY)
        self.historic(head, self.repo, actor="human:alice", harness="")
        again = self.historic(head, self.repo, actor="human:alice", harness="")
        self.assertFalse(again["recorded"])
        with self.assertRaisesRegex(prov.ProvenanceError, "first proof stands"):
            self.historic(head, self.repo)
        receipt = prov.receipt_for_commit(self.db, head)
        self.assertEqual(receipt["actor"], "human:alice")
        # un reçu de session ou signé ordinaire existant n'est pas réécrit non plus
        other = self.commit(JULY + 10.0, "reçu existant")
        storage.of(self.db).provenance.add(
            "commit:" + other, other, AGENT, "s-x", "t-1", "claude")
        with self.assertRaisesRegex(prov.ProvenanceError, "first proof stands"):
            self.historic(other, self.repo)

    def test_the_ordinary_signed_path_still_refuses_the_unbound_session(self):
        head = self.commit(JULY)
        with self.assertRaisesRegex(prov.ProvenanceError, "no binding on any host"):
            prov.record_signed_commit(
                self.db, head, actor=AGENT, session=LABEL, harness="claude",
                host=HOST, families=FAMILIES, lot=int(self.lot["id"]),
                approver=OWNER, key_path=self.key_path, cwd=self.repo)


class HistoricGateTest(_Owner):
    """La porte lit le reçu historique comme un delta de sa famille attestée."""

    def setUp(self):
        super().setUp()
        for table in ("turn_costs", "canon_state", "quota_gauge_readings"):
            self.db.execute("DELETE FROM %s" % table)
        self.repo = _Repo(os.path.join(self.tmp, "depot"))
        self.repo.git("symbolic-ref", "HEAD", "refs/heads/main")
        self.repo.write(CRITICAL, "base\n")
        self.base = self.repo.commit("base")
        self.repo.git("checkout", "-q", "-b", "lot/8170-historic")
        self.repo.write(CRITICAL, "juillet\n")
        self.historic_sha = self.repo.commit("juillet")
        self.repo.write(CRITICAL, "deepseek\n")
        self.frozen = self.repo.commit("deepseek delta", agent="ds-author")
        # la première liaison de l'hôte est postérieure aux deux commits
        self.bind(created=time.time() + 86400.0)
        for name, harness in ((AGENT, "claude"), ("ds-author", "deepseek"),
                              ("claude-r", "claude"), ("codex-r", "codex"),
                              ("deepseek-r", "deepseek")):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                            cwd=self.repo.root, status="idle")
            self.db.execute("UPDATE agent_registry SET team = 'ameesh'"
                            " WHERE name = %s", (name,))
        self.lot = work.add(self.db, title="porte historique #8170",
                            app="ameesh", assignee="ds-author",
                            branch="lot/8170-historic", branch_target=self.base)
        work.move(self.db, int(self.lot["id"]), "build")
        work.move(self.db, int(self.lot["id"]), "qa")
        self.receipt("commit:" + self.frozen, sha=self.frozen,
                     actor="ds-author", harness="deepseek")
        work.milestone(self.db, int(self.lot["id"]), "frozen", sha=self.frozen,
                       actor="ds-author")

    def receipt(self, subject, *, sha, actor, harness):
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, 't', %s, %s)",
            (subject, sha, actor, "s-" + harness, harness, int(self.lot["id"])))

    def verdict(self, actor, harness):
        mark = work.milestone(self.db, int(self.lot["id"]), "verdict",
                              sha=self.frozen, actor=actor, verdict="ok")
        self.receipt("milestone:%d" % int(mark["id"]), sha=self.frozen,
                     actor=actor, harness=harness)

    def gate(self):
        lot = int(self.lot["id"])
        klass = {"class": "sensitive", "project": "ameesh",
                 "repo": self.repo.root, "target": self.base,
                 "base_sha": self.base,
                 "files": [{"path": CRITICAL, "class": "sensitive",
                            "rule": [CRITICAL]}]}
        return merge_queue.review_gate(
            self.db, work.milestones(self.db, lot), self.frozen,
            repo=self.repo.root, klass=klass, item=work.get(self.db, lot),
            families=FAMILIES)

    def test_without_the_historic_receipt_the_gate_names_the_unknown_commit(self):
        for actor, harness in (("claude-r", "claude"), ("codex-r", "codex"),
                               ("deepseek-r", "deepseek")):
            self.verdict(actor, harness)
        out = self.gate()
        self.assertFalse(out["ok"], out)
        self.assertIn(self.historic_sha[:12], str(out))

    def test_the_attested_family_closes_only_its_delta(self):
        self.historic(self.historic_sha, self.repo.root)
        self.verdict("codex-r", "codex")
        self.verdict("deepseek-r", "deepseek")
        # codex+deepseek couvrent le delta claude, pas le delta deepseek
        out = self.gate()
        self.assertFalse(out["ok"], out)
        self.verdict("claude-r", "claude")
        out = self.gate()
        self.assertTrue(out["ok"], out)
        rows = {row["sha"]: row for row in out["deltas"]}
        self.assertEqual(rows[self.historic_sha]["families"], ["claude"])

    def test_a_human_historic_author_leaves_every_family_eligible(self):
        self.historic(self.historic_sha, self.repo.root, actor="human:alice",
                      harness="")
        self.verdict("claude-r", "claude")
        self.verdict("codex-r", "codex")
        out = self.gate()
        self.assertTrue(out["ok"], out)
        rows = {row["sha"]: row for row in out["deltas"]}
        self.assertEqual(rows[self.historic_sha]["families"], [])
