# SPDX-License-Identifier: AGPL-3.0-only
"""#4572 — « review GO → merge » lit la cause du candidat avant d'escalader.

Constat (29/10 13:25Z) : `paquet-candidats` écartait #4384 pour
`freeze-not-head` (gel `1dd33c0e` ≠ tête publiée `949e195b`), #4385 et #4383
pour `conflict` ; l'escalade SLO disait pourtant « the composer/pilot carries
it, and reassigning or nudging its holder does not advance it ». Le composeur
ne pouvait rien : seul l'auteur débloque, et le lot dormait.

Attendu : l'escalade lit la cause avec les **mêmes codes** que
`packet-candidates` (`freeze-not-head`, `conflict`, …) ; quand elle est côté
auteur, elle nomme l'auteur et le geste (regeler, résoudre le conflit), et
l'assigné reste la cible du niveau 1. Le texte « le composeur le porte » reste
réservé à un lot réellement prêt (`ready`). `ameesh debit` (qa_merge) affiche
la même cause.

Témoin : un lot dont le gel n'est pas la tête publiée → escalade adressée à
l'assigné, avec « freeze-not-head ».
"""
from __future__ import annotations

import os
import subprocess
import unittest
from unittest import mock

from ameesh import (alert_briefs, debit, exploitation, packet_candidates,
                    plan_git, registry, storage, work)

from .support import PgTestCase

HEURE = 3600.0
ENV_GIT = dict(os.environ, GIT_AUTHOR_NAME="dev1",
               GIT_AUTHOR_EMAIL="dev1@ameesh.invalid",
               GIT_COMMITTER_NAME="dev1", GIT_COMMITTER_EMAIL="dev1@ameesh.invalid")


def git(repo, *args):
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                          text=True, env=ENV_GIT)
    if proc.returncode != 0:
        raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


def write_commit(repo, path, text, message):
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as stream:
        stream.write(text)
    git(repo, "add", path)
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def repo_with_branch(root, *, conflict=False):
    """Un dépôt : `main` avec `f`, puis une branche `lot/4572-x`.

    `conflict` change `f` des deux côtés (conflit réel à la fusion) ; sinon la
    branche ajoute `g`, sans conflit. Les refs `origin/*` sont posées à la
    main : la lecture est locale, aucun réseau."""
    os.makedirs(root, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    write_commit(root, "f", "base\n", "base")
    write_commit(root, "f", "base2\n", "base2")
    if conflict:
        # les deux côtés modifient la même ligne : conflit à la fusion
        git(root, "checkout", "-q", "-b", "lot/4572-x")
        frozen = write_commit(root, "f", "branche\n", "branche")
        git(root, "checkout", "-q", "main")
        published = write_commit(root, "f", "main\n", "main")
    else:
        git(root, "checkout", "-q", "-b", "lot/4572-x")
        frozen = write_commit(root, "g", "x\n", "branche")
        git(root, "checkout", "-q", "main")
        published = git(root, "rev-parse", "HEAD")
    git(root, "update-ref", "refs/remotes/origin/main", published)
    git(root, "update-ref", "refs/remotes/origin/lot/4572-x", frozen)
    return frozen, published


class RefusalOfTest(unittest.TestCase):
    """Le moteur de cause : mêmes codes que `paquet-candidats`, sans réseau."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="test-4572-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.repo = os.path.join(self.tmp, "repo")

    def _refusal(self, row=None, **kw):
        row = row or {"id": 4572, "branch": "lot/4572-x",
                      "branch_target": "main", "assignee": "dev1"}
        base = kw.pop("base", None)
        if base is None:
            base = plan_git._commit(self.repo, "refs/remotes/origin/main")
        return packet_candidates.refusal_of(row, repo=self.repo, base=base, **kw)

    def test_freeze_not_head_est_une_cause_auteur(self):
        frozen, published = repo_with_branch(self.repo)
        old = plan_git._commit(self.repo, "HEAD~1") or published
        cause = self._refusal(frozen=frozen, raw_freeze=frozen,
                              published=old, published_source="origin")
        self.assertEqual(cause["code"], "freeze-not-head")
        self.assertEqual(cause["owner"], "author")
        self.assertIn("freeze again", cause["gesture"])
        self.assertFalse(cause["ready"])

    def test_conflit_avec_la_cible_est_une_cause_auteur(self):
        frozen, published = repo_with_branch(self.repo, conflict=True)
        cause = self._refusal(frozen=frozen, raw_freeze=frozen,
                              published=frozen, published_source="origin",
                              target="main")
        self.assertEqual(cause["code"], "conflict")
        self.assertEqual(cause["owner"], "author")
        self.assertIn("resolve the conflict", cause["gesture"])
        self.assertIn("f", cause["detail"])

    def test_un_lot_pret_ne_porte_aucune_cause(self):
        frozen, published = repo_with_branch(self.repo)
        cause = self._refusal(frozen=frozen, raw_freeze=frozen,
                              published=frozen, published_source="origin",
                              target="main")
        self.assertEqual(cause["code"], "")
        self.assertTrue(cause["ready"])
        self.assertFalse(cause["unknown"])

    def test_une_observation_ne_prouve_jamais_la_tete_publiee(self):
        # `branch_head` est une observation : elle peut nommer un refus, jamais
        # conclure « prêt » (plan_git.freeze_candidate).
        frozen, _published = repo_with_branch(self.repo)
        cause = self._refusal(frozen=frozen, raw_freeze=frozen,
                              published=frozen, published_source="observed")
        self.assertEqual(cause["code"], "")
        self.assertFalse(cause["ready"])
        self.assertTrue(cause["unknown"])

    def test_sans_depot_la_lecture_ne_conclut_pas(self):
        frozen, published = repo_with_branch(self.repo)
        cause = packet_candidates.refusal_of(
            {"id": 4572, "branch": "lot/4572-x"}, frozen=frozen,
            raw_freeze=frozen, published=frozen)
        self.assertEqual(cause["code"], "")
        self.assertTrue(cause["unknown"])


def _seuils(**kw):
    args = exploitation.build_parser().parse_args(["alerts"])
    out = exploitation.thresholds(args)
    out.update(kw)
    return out


class _SloBase(PgTestCase):
    GO = 1_792_950_000.0          # un instant absolu, loin des bords

    def setUp(self):
        super().setUp()
        for name in ("dev1", "orch", "bureau", "auditeur"):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host)
        self.lot = work.add(self.db, title="un lot gelé", assignee="dev1",
                            app="ameesh", branch="lot/4572-x",
                            branch_target="main", actor="dev1")
        work.move(self.db, self.lot["id"], "build")
        work.move(self.db, self.lot["id"], "qa")
        created = self.GO - 2 * HEURE
        self.db.execute("UPDATE work_items SET created_at = to_timestamp(%s)"
                        " WHERE id = %s", (created, self.lot["id"]))
        self.db.execute(
            "UPDATE work_item_events SET created_at = to_timestamp(%s)"
            " WHERE work_item_id = %s AND state = 'build'",
            (created + 1800.0, self.lot["id"]))
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, actor)"
            " VALUES (%s, 'frozen', to_timestamp(%s), 'dev1')",
            (self.lot["id"], self.GO - 1))
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, actor, verdict)"
            " VALUES (%s, 'verdict', to_timestamp(%s), 'rel', 'ok')",
            (self.lot["id"], self.GO))
        self.now = self.GO + 1801.0

    def alert(self, **kw):
        self.now = kw.pop("now", self.now)
        alerts = exploitation.alerts(self.cfg, self.db, now=self.now,
                                     **_seuils(orchestrators_declared=["orch"]))
        return [row for row in alerts
                if row["type"] == "slo_breach"
                and row["step"] == "verdict_merge"]

    def briefs(self, alert):
        listing = [{"name": n, "team": ""}
                   for n in ("dev1", "orch", "bureau", "auditeur")]
        return alert_briefs.briefs(alert, listing, orchestrator_names=("orch",),
                                   auditor_names=("auditeur",))


class EscaladeAuteurTest(_SloBase):
    """La cause côté auteur change la cible et le texte, pas la mesure."""

    CAUSE = {"code": "freeze-not-head", "owner": "author",
             "detail": "freeze 1dd33c0e17c3 is not the published head 949e195be64c",
             "gesture": "freeze again on the published head, then have it reviewed",
             "ready": False, "unknown": False}

    def test_niveau_1_vise_l_assigne_et_nomme_la_cause(self):
        # Témoin du lot : gel ≠ tête publiée → l'escalade est adressée à
        # l'assigné, avec le code que le composeur lit.
        with mock.patch.object(packet_candidates, "candidate_refusal",
                               return_value=dict(self.CAUSE)):
            alerts = self.alert()
        self.assertEqual(len(alerts), 1, alerts)
        alert = alerts[0]
        self.assertEqual((alert["level"], alert["targets"]), (1, ["assignee"]))
        self.assertEqual(alert["agent"], "dev1")
        self.assertEqual((alert["waiting"] or {}).get("candidate", {}).get("code"),
                         "freeze-not-head")
        briefs = self.briefs(alert)
        self.assertEqual(set(briefs), {"dev1"})
        self.assertIn("freeze-not-head", briefs["dev1"])
        self.assertIn("author dev1", briefs["dev1"])
        self.assertNotIn("the composer/pilot carries it", briefs["dev1"])

    def test_niveau_3_nomme_encore_l_auteur_et_le_geste(self):
        with mock.patch.object(packet_candidates, "candidate_refusal",
                               return_value=dict(self.CAUSE)):
            self.now = self.GO + 3 * 1800 + 1
            alerts = self.alert()
        self.assertEqual([(a["level"], a["targets"]) for a in alerts],
                         [(3, ["bureau", "auditeur"])])
        text = self.briefs(alerts[0])["bureau"]
        self.assertIn("freeze-not-head", text)
        self.assertIn("author dev1", text)
        self.assertIn("do not wait for the composer", text)

    def test_un_conflit_nomme_le_geste_de_l_auteur(self):
        cause = dict(self.CAUSE, code="conflict", detail="conflict with main: f",
                     gesture="resolve the conflict with the target, then freeze")
        with mock.patch.object(packet_candidates, "candidate_refusal",
                               return_value=cause):
            alerts = self.alert()
        text = self.briefs(alerts[0])["dev1"]
        self.assertIn("conflict", text)
        self.assertIn("resolve the conflict", text)

    def test_un_lot_pret_garde_le_texte_du_composeur(self):
        ready = {"code": "", "owner": "", "detail": "", "gesture": "",
                 "ready": True, "unknown": False}
        with mock.patch.object(packet_candidates, "candidate_refusal",
                               return_value=ready):
            alerts = self.alert()
        self.assertEqual([(a["level"], a["targets"]) for a in alerts],
                         [(1, ["pilot"])])
        text = self.briefs(alerts[0])["orch"]
        self.assertIn("composer/pilot carries it", text)
        self.assertNotIn("freeze-not-head", text)

    def test_la_cle_de_dedoublonnage_change_avec_la_cause(self):
        # Un brief déjà parti au pilote ne doit pas rester le seul reçu quand
        # la cause côté auteur apparaît : la clé porte le code.
        ready = {"code": "", "owner": "", "detail": "", "gesture": "",
                 "ready": True, "unknown": False}
        with mock.patch.object(packet_candidates, "candidate_refusal",
                               return_value=dict(ready)):
            prêt = self.alert()[0]
        with mock.patch.object(packet_candidates, "candidate_refusal",
                               return_value=dict(self.CAUSE)):
            cause = self.alert()[0]
        self.assertEqual(prêt["reason"], "verdict_merge/level-1")
        self.assertIn("freeze-not-head", cause["reason"])
        self.assertNotEqual(prêt["reason"], cause["reason"])

    def test_une_lecture_incomplete_ne_nomme_personne_au_hasard(self):
        unknown = {"code": "", "owner": "", "detail": "", "gesture": "",
                   "ready": False, "unknown": True}
        with mock.patch.object(packet_candidates, "candidate_refusal",
                               return_value=unknown):
            alerts = self.alert()
        text = self.briefs(alerts[0])["orch"]
        self.assertIn("scripts/paquet-candidats.py", text)
        self.assertNotIn("composer/pilot carries it", text)


class EscaladeIntegrationTest(_SloBase):
    """Le chemin réel : dépôt, refs `origin`, gel périmé, aucune doublure."""

    def setUp(self):
        super().setUp()
        root = os.path.join(self.tmp, "repo")
        os.makedirs(root, exist_ok=True)
        git(root, "init", "-q", "-b", "main")
        write_commit(root, "f", "base\n", "base")
        write_commit(root, "f", "base2\n", "base2")
        git(root, "checkout", "-q", "-b", "lot/4572-x")
        frozen = write_commit(root, "g", "x\n", "branche")
        published = write_commit(root, "h", "y\n", "après gel")
        git(root, "checkout", "-q", "main")
        main_tip = git(root, "rev-parse", "HEAD")
        git(root, "update-ref", "refs/remotes/origin/main", main_tip)
        git(root, "update-ref", "refs/remotes/origin/lot/4572-x", published)
        self.repo = root
        registry.upsert(self.db, "dev1", harness="claude", host=self.cfg.host,
                        cwd=root)
        self.db.execute(
            "UPDATE work_item_milestones SET sha = %s"
            " WHERE work_item_id = %s AND kind = 'frozen'",
            (frozen, self.lot["id"]))
        self.db.execute(
            "UPDATE work_item_milestones SET sha = %s"
            " WHERE work_item_id = %s AND kind = 'verdict'",
            (frozen, self.lot["id"]))

    def test_le_gel_perime_est_lu_sur_les_refs_du_depot(self):
        # Témoin du constat : le gel n'est plus la tête publiée ; l'escalade
        # doit aller à l'assigné avec « freeze-not-head », sans doublure.
        # La porte des avis (exigence de provenance, #2928) est ouverte par le
        # test : elle a ses propres témoins, la cause du candidat est ici le
        # sujet. Tout le reste est le chemin réel (dépôt, refs, jalons).
        with mock.patch.object(exploitation, "_review_gate_open",
                               return_value=True):
            alerts = self.alert()
        self.assertEqual(len(alerts), 1, alerts)
        alert = alerts[0]
        self.assertEqual((alert["level"], alert["targets"]), (1, ["assignee"]))
        self.assertEqual((alert["waiting"] or {}).get("candidate", {}).get("code"),
                         "freeze-not-head")
        text = self.briefs(alert)["dev1"]
        self.assertIn("freeze-not-head", text)
        self.assertIn("author dev1", text)


class DebitCauseTest(_SloBase):
    """`ameesh debit` (qa_merge) affiche la même cause et le même geste."""

    def test_qa_merge_nomme_la_cause_de_l_auteur(self):
        self.db.execute(
            "UPDATE work_item_milestones SET at = to_timestamp(%s)"
            " WHERE work_item_id = %s AND kind = 'frozen'",
            (self.now - 3 * HEURE, self.lot["id"]))
        cause = {"code": "freeze-not-head", "owner": "author",
                 "detail": "freeze 1dd33c0e17c3 is not the published head 949e195be64c",
                 "gesture": "freeze again on the published head, then have it reviewed",
                 "ready": False, "unknown": False}
        with mock.patch.object(packet_candidates, "candidate_refusal",
                               return_value=dict(cause)):
            report = debit.read(self.db, cfg=self.cfg, now=self.now, jobs=False,
                                wait_s=2 * HEURE)
        rows = [row for row in report["slow_waits"] if row["kind"] == "qa_merge"]
        self.assertEqual(len(rows), 1, report["slow_waits"])
        self.assertEqual(rows[0]["cause"]["code"], "freeze-not-head")
        self.assertEqual(rows[0]["who"], "dev1")
        self.assertIn("freeze-not-head", rows[0]["gesture"])
        self.assertIn("freeze again", rows[0]["gesture"])
        text = debit.format_text(report)
        self.assertIn("freeze-not-head", text)
