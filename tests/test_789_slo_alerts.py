# SPDX-License-Identifier: AGPL-3.0-only
"""#789 tranche 2 : l'alerte `slo_breach` (canon §9).

`exploitation.alerts()` mesure les étapes du §9 sur les lots ouverts et lève
une alerte par `(lot, étape, niveau)` ; `alert_briefs` l'adresse au niveau
atteint — 1× l'assigné, 2× le pilote, 3× le bureau et l'auditeur, l'owner en
plus pour un P1. Un lot en attente planifiée (dépendance ouverte) n'est pas en
retard : il est tu.
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from ameesh import (alert_briefs, decisions, exploitation, lot_class, merge_queue,
                    notify, packet_guard, planned_waits, registry, slo, stagnation,
                    storage, work, work_dependencies)
from ameesh.storage.postgres.operations import Operations

from .support import FAKEBIN, PgTestCase

HEURE = 3600.0
FAKE_NOTIFY_SEND = os.path.join(FAKEBIN, "notify-send")


def _seuils(**kw):
    args = exploitation.build_parser().parse_args(["alerts"])
    out = exploitation.thresholds(args)
    out.update(kw)
    return out


#: #2739 : les dépôts du banc doivent committer sous une identité d'agent ; le
#: runner impose `ameesh-dev3` dans l'environnement, qui écraserait la config
#: locale du dépôt. Sans identité d'agent, la porte ne peut pas attribuer la
#: plage gelée et refuse en échec fermé.
_GIT_DEV1 = dict(os.environ, GIT_AUTHOR_NAME="dev1",
                 GIT_AUTHOR_EMAIL="dev1@ameesh.invalid",
                 GIT_COMMITTER_NAME="dev1", GIT_COMMITTER_EMAIL="dev1@ameesh.invalid")


class _Base(PgTestCase):
    def setUp(self):
        super().setUp()
        for name in ("orch", "dev1", "bureau", "auditeur"):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host)
        # #3036 : un brief n'est plus déposé à un destinataire sans bail
        # vivant ; ces agents simulent un runner qui les tient (régime normal).
        self.db.execute("UPDATE agent_registry SET lease_owner = 'runner-' || name,"
                        " lease_expires_at = now() + interval '1 hour'")
        # le lot reste en intake : « prêt, pas démarré » — l'étape qui court
        self.lot = work.add(self.db, title="un lot SLO", assignee="dev1")
        self.now = time.time()

    def age(self, seconds, lot=None):
        """Le lot est prêt (`created_at`) depuis `seconds`."""
        self.db.execute(
            "UPDATE work_items SET created_at = now() - make_interval(secs => %s)"
            " WHERE id = %s", (float(seconds), (lot or self.lot)["id"]))

    def slo(self, **kw):
        kw.setdefault("orchestrators_declared", ["orch"])
        return [a for a in exploitation.alerts(self.cfg, self.db, now=self.now,
                                               **_seuils(**kw))
                if a["type"] == "slo_breach"]

    def start(self, since_s=None):
        """Le lot démarre (build) ; `since_s` recule la transition."""
        work.move(self.db, self.lot["id"], "build")
        if since_s is not None:
            self.db.execute(
                "UPDATE work_item_events SET created_at = now() - make_interval(secs => %s)"
                " WHERE work_item_id = %s AND state = 'build'",
                (float(since_s), self.lot["id"]))

    def qa(self, verdict_s=3000.0):
        """Le lot passe en qa avec un verdict `ok` posé il y a `verdict_s`."""
        work.move(self.db, self.lot["id"], "build")
        work.move(self.db, self.lot["id"], "qa")
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, actor, verdict)"
            " VALUES (%s, 'verdict', now() - make_interval(secs => %s), 'rel', 'ok')",
            (self.lot["id"], float(verdict_s)))
        self.now = time.time()

    def qa_at(self, verdict_ts, *, created_s=7200.0, lot=None):
        """qa avec un verdict `ok` à l'instant absolu `verdict_ts` (#2335).

        Les autres étapes restent tenues : création `created_s` avant le
        verdict, build 30 min après la création (cible 1 h)."""
        lot = self.lot if lot is None else lot
        work.move(self.db, lot["id"], "build")
        work.move(self.db, lot["id"], "qa")
        created = float(verdict_ts) - float(created_s)
        self.db.execute("UPDATE work_items SET created_at = to_timestamp(%s)"
                        " WHERE id = %s", (created, lot["id"]))
        self.db.execute(
            "UPDATE work_item_events SET created_at = to_timestamp(%s)"
            " WHERE work_item_id = %s AND state = 'build'",
            (created + 1800.0, lot["id"]))
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, actor, verdict)"
            " VALUES (%s, 'verdict', to_timestamp(%s), 'rel', 'ok')",
            (lot["id"], float(verdict_ts)))
        self.now = float(verdict_ts)

    def packet(self, body="", *, branch="integration/paquet-test", head=None):
        """Un porteur de paquet ouvert (#2335) : branche `integration/**`."""
        carrier = work.add(self.db, title="porteur de paquet test", body=body,
                           branch=branch, actor="test")
        if head:
            self.db.execute("UPDATE work_items SET branch_head = %s WHERE id = %s",
                            (head, carrier["id"]))
        return carrier

    def briefs(self, alert):
        listing = [{"name": n, "team": ""} for n in ("dev1", "orch", "bureau", "auditeur")]
        return alert_briefs.briefs(alert, listing, orchestrator_names=("orch",),
                                   auditor_names=("auditeur",))


class AlerteTest(_Base):
    def test_intake_reporte_ne_reprend_pas_les_jours_avant_le_debut(self):
        from ameesh.plan import day_start_ts
        self.age(8 * 24 * HEURE)
        self.db.execute("UPDATE work_items SET planned_start = '2026-10-19' WHERE id = %s",
                        (self.lot["id"],))
        planned = day_start_ts("2026-10-19")
        # Une origine ancienne, comme les lots reportés #114/#65/#313.
        self.db.execute("UPDATE work_items SET created_at = to_timestamp(%s) WHERE id = %s",
                        (planned - 8 * 24 * HEURE, self.lot["id"]))
        for offset, levels in ((-1, []), (180, []), (3600, [1]), (10800, [3])):
            with self.subTest(offset=offset):
                self.now = planned + offset
                alerts = [a for a in self.slo() if a["step"] == "intake_started"]
                self.assertEqual([a["level"] for a in alerts], levels)

    def test_niveau_1_assigne(self):
        self.age(1.5 * HEURE)
        alerts = self.slo()
        self.assertEqual([(a["step"], a["level"], a["targets"], a["reason"]) for a in alerts],
                         [("intake_started", 1, ["assignee"], "intake_started/level-1")])
        alert = alerts[0]
        self.assertEqual((alert["lot"], alert["agent"], alert["critical"], alert["urgent"]),
                         (self.lot["id"], "dev1", False, False))
        self.assertEqual(alert["threshold"], 3600)
        self.assertAlmostEqual(alert["value"], 1.5 * HEURE - 3600, delta=5)
        self.assertIn("slo_breach", exploitation.ALERT_TYPES)
        self.assertIn("slo_breach", notify.TYPE_LABELS)
        self.assertIn("slo_breach", alert_briefs.BRIEFED_TYPES)

    def test_niveau_2_pilote(self):
        self.age(2.5 * HEURE)
        alerts = self.slo()
        self.assertEqual([(a["level"], a["targets"]) for a in alerts],
                         [(2, ["pilot"])])
        self.assertEqual(alerts[0]["orchestrators"], ["orch"])

    def test_niveau_2_ne_prend_pas_un_assigneur_pour_pilote(self):
        # #1982 : l'historique L94 (agents ayant confié des lots) ne fait pas
        # un pilote — l'heuristique `history=True` briefait des agents de
        # chantier sans mandat ; le niveau 2 vise le rôle (déclaré ou canon).
        registry.upsert(self.db, "dist", harness="claude", host=self.cfg.host)
        registry.upsert(self.db, "dev2", harness="claude", host=self.cfg.host)
        autre = work.add(self.db, title="à distribuer")
        work.assign(self.db, int(autre["id"]), "dev2", actor="dist")
        self.assertIn("dist", storage.of(self.db).operations.assigners(
            since_s=30 * 24 * HEURE))
        self.age(2.5 * HEURE)
        alerts = self.slo()
        self.assertEqual([(a["level"], a["targets"]) for a in alerts],
                         [(2, ["pilot"])])
        self.assertEqual(alerts[0]["orchestrators"], ["orch"])

    def test_p1_niveau_3_bureau_auditeur_et_urgent(self):
        self.db.execute("UPDATE work_items SET priority = 1 WHERE id = %s",
                        (self.lot["id"],))
        self.age(50 * 60)
        alerts = self.slo()
        self.assertEqual(len(alerts), 1, alerts)
        alert = alerts[0]
        self.assertEqual((alert["level"], alert["targets"]),
                         (3, ["bureau", "auditeur"]))
        self.assertEqual(alert["threshold"], 900)          # délai court du P1
        self.assertTrue(alert["critical"])
        self.assertTrue(alert["urgent"])

    def test_etape_tenue_sans_alerte(self):
        self.age(60)
        self.assertEqual(self.slo(), [])

    def test_attente_planifiee_exclue(self):
        bloqueur = work.add(self.db, title="bloqueur")
        work_dependencies.add(self.db, self.lot["id"], bloqueur["id"], actor="test")
        self.age(4 * HEURE)
        self.assertEqual(self.slo(), [])

    def test_bloquant_fusionne_le_delai_part_de_la_levee(self):
        # #2551 : #2198/#2498 — un lot assigné depuis 13 h mais retenu par
        # #2184 ne doit pas escalader au niveau 3 à la seconde où le bloqueur
        # fusionne : la mesure d'intake part de la fusion.
        bloqueur = work.add(self.db, title="bloqueur", assignee="human:proprio")
        work_dependencies.add(self.db, self.lot["id"], bloqueur["id"], actor="test")
        self.age(13 * HEURE)
        self.assertEqual(self.slo(), [])
        self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s",
                        (bloqueur["id"],))
        merged = time.time()
        self.now = merged + 60
        self.assertEqual(self.slo(), [])
        self.now = merged + 1.5 * HEURE
        alerts = self.slo()
        self.assertEqual([(a["step"], a["level"], a["targets"]) for a in alerts],
                         [("intake_started", 1, ["assignee"])])
        self.assertAlmostEqual(alerts[0]["since"], merged, delta=5)
        self.assertAlmostEqual(alerts[0]["value"], 1800, delta=5)

    def test_bloquant_clos_le_delai_part_de_la_cloture(self):
        # #2551 : la fermeture d'un prérequis (abandon, remplacement) le rend
        # non bloquant, comme la fusion : le délai part de sa clôture.
        bloqueur = work.add(self.db, title="bloqueur", assignee="human:proprio")
        work_dependencies.add(self.db, self.lot["id"], bloqueur["id"], actor="test")
        self.age(13 * HEURE)
        self.assertEqual(self.slo(), [])
        work.close(self.db, bloqueur["id"], abandoned=True, actor="test")
        closed = time.time()
        self.now = closed + 60
        self.assertEqual(self.slo(), [])
        self.now = closed + 1.5 * HEURE
        alerts = self.slo()
        self.assertEqual([(a["step"], a["level"]) for a in alerts],
                         [("intake_started", 1)])
        self.assertAlmostEqual(alerts[0]["since"], closed, delta=5)

    def test_etapes_tenues_puis_verdict_recent(self):
        # créé il y a 4 h, démarré 30 min plus tard, verdict ok il y a 5 min :
        # aucune étape en dépassement, seule la fusion reste à faire
        self.age(4 * HEURE)
        self.start(3.5 * HEURE)
        self.db.execute("UPDATE work_items SET state = 'qa' WHERE id = %s",
                        (self.lot["id"],))
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, actor, verdict)"
            " VALUES (%s, 'verdict', now() - interval '5 minutes', 'rel', 'ok')",
            (self.lot["id"],))
        self.assertEqual(self.slo(), [])


class ReviewGateRepositoryTest(_Base):
    """#2559 : la porte des avis se lit avec le dépôt du gel retrouvé sur
    l'hôte qui lit le lot (pas seulement par ses réglages radar). Un premier
    avis sur un chemin critique ne fait pas le GO, et l'alerte GO → merge ne
    part pas."""

    def _frozen_repo(self) -> tuple[str, str]:
        """Un dépôt avec un commit de base puis la tête gelée qui touche
        `src/ameesh/db.py` (SQL, deux avis). Rend `(repo, sha)`, lecture Git
        réelle sans réseau."""
        base = tempfile.mkdtemp(prefix="ameesh-slo-2559-")
        self.addCleanup(subprocess.run, ["rm", "-rf", base])
        repo = os.path.join(base, "repo")
        subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
        # #2739 : le committer doit être un agent pour que la porte lise la
        # plage gelée sans inventer d'auteur ; l'assigné du lot est `dev1`.
        for key, value in (("user.email", "dev1@ameesh.invalid"), ("user.name", "dev1")):
            subprocess.run(["git", "-C", repo, "config", key, value], check=True)
        os.makedirs(os.path.join(repo, "docs"))
        with open(os.path.join(repo, "docs", "notes.md"), "w") as fh:
            fh.write("base\n")
        subprocess.run(["git", "-C", repo, "add", "."], check=True)
        subprocess.run(["git", "-C", repo, "commit", "-q", "-m", "base"], check=True,
                       env=_GIT_DEV1)
        subprocess.run(["git", "-C", repo, "checkout", "-q", "-b", "lot"], check=True)
        os.makedirs(os.path.join(repo, "src", "ameesh"))
        with open(os.path.join(repo, "src", "ameesh", "db.py"), "w") as fh:
            fh.write("# critique\n")
        subprocess.run(["git", "-C", repo, "add", "."], check=True)
        subprocess.run(["git", "-C", repo, "commit", "-q", "-m", "gel"], check=True,
                       env=_GIT_DEV1)
        sha = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                             check=True, capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "-C", repo, "checkout", "-q", "main"], check=True)
        return repo, sha

    def _freeze_and_qa(self, lot, sha, *, verdict_s=3000.0):
        """Le lot passe en qa, gelé sur `sha`, avec un verdict `ok` ancien."""
        work.move(self.db, lot["id"], "build")
        work.move(self.db, lot["id"], "qa")
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, sha, actor)"
            " VALUES (%s, 'frozen', now() - make_interval(secs => 4000), %s, 'dev1')",
            (lot["id"], sha))
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, sha, actor, verdict)"
            " VALUES (%s, 'verdict', now() - make_interval(secs => %s), %s, 'cx', 'ok')",
            (lot["id"], float(verdict_s), sha))

    def test_depot_du_gel_retrouve_par_un_autre_agent_local(self):
        """Le dépôt n'est pas dans le radar de l'hôte : le dossier de travail
        d'un agent du registre qui contient le commit suffit (#2559)."""
        repo, sha = self._frozen_repo()
        registry.upsert(self.db, "orch", harness="claude", host=self.cfg.host,
                        cwd=repo)
        item = work.get(self.db, self.lot["id"])
        self.assertFalse((registry.get(self.db, "dev1") or {}).get("cwd"))
        self.assertEqual(lot_class.repo_for_freeze(self.db, self.cfg, item, sha),
                         (os.path.realpath(repo), sha))

    def test_le_gel_abrege_est_resolu_avant_la_classe(self):
        """#2564 : `repo_for_freeze` rend le SHA complet, et `of_lot` reçoit ce
        SHA — jamais le préfixe saisi dans le jalon de gel."""
        repo, sha = self._frozen_repo()
        registry.upsert(self.db, "orch", harness="claude", host=self.cfg.host,
                        cwd=repo)
        registry.upsert(self.db, "cx", harness="codex", host=self.cfg.host)
        item = work.get(self.db, self.lot["id"])
        self.assertEqual(lot_class.repo_for_freeze(self.db, self.cfg, item, sha[:12]),
                         (os.path.realpath(repo), sha))
        marks = [{"kind": "frozen", "sha": sha[:12], "actor": "dev1", "at_ts": 1},
                 {"kind": "verdict", "sha": sha[:12], "actor": "cx", "verdict": "ok",
                  "at_ts": 2}]
        with mock.patch("ameesh.lot_class.of_lot", wraps=lot_class.of_lot) as classe:
            gate = merge_queue.review_gate_for_item(self.db, self.cfg, item, marks)
        self.assertIsNotNone(gate)
        self.assertEqual(classe.call_args.kwargs["head"], sha)

    def test_une_passe_sur_deux_lots_lit_le_registre_une_fois(self):
        """#2564 : la porte est interrogée pour chaque lot en attente de
        fusion, mais `registry.overview` n'est lu qu'une fois par passe."""
        repo, sha = self._frozen_repo()
        registry.upsert(self.db, "orch", harness="claude", host=self.cfg.host,
                        cwd=repo)
        registry.upsert(self.db, "cx", harness="codex", host=self.cfg.host)
        lots = [self.lot, work.add(self.db, title="un second lot SLO",
                                   assignee="dev1")]
        for lot in lots:
            self._freeze_and_qa(lot, sha)
        self.now = time.time()
        listing = storage.of(self.db).operations.listing()
        calls = []
        real = registry.overview

        def counting(db):
            calls.append(db)
            return real(db)

        with mock.patch("ameesh.registry.overview", side_effect=counting), \
                mock.patch("ameesh.exploitation._review_gate_open",
                           wraps=exploitation._review_gate_open) as gate_open:
            exploitation._slo_breaches(self.cfg, self.db, listing, now=self.now)
        self.assertEqual(gate_open.call_count, 2)
        self.assertEqual(len(calls), 1)

    def test_un_avis_sur_chemin_critique_ne_fait_pas_le_go(self):
        """Un avis indépendant sur `db.py` ne suffit pas : la porte exige deux
        familles, la ligne GO → merge ne part pas, et le doute n'est pas
        nécessaire (dépôt du gel lisible)."""
        repo, sha = self._frozen_repo()
        registry.upsert(self.db, "orch", harness="claude", host=self.cfg.host,
                        cwd=repo)
        registry.upsert(self.db, "cx", harness="codex", host=self.cfg.host)
        self.age(2 * HEURE)
        self.start(1.5 * HEURE)
        work.move(self.db, self.lot["id"], "qa")
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, sha, actor)"
            " VALUES (%s, 'frozen', now() - make_interval(secs => 4000), %s, 'dev1')",
            (self.lot["id"], sha))
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, sha, actor, verdict)"
            " VALUES (%s, 'verdict', now() - make_interval(secs => 3000), %s, 'cx', 'ok')",
            (self.lot["id"], sha))
        self.now = time.time()
        marks = storage.of(self.db).progress.lot_milestones([self.lot["id"]])
        row = work.get(self.db, self.lot["id"])
        gate = merge_queue.review_gate_for_item(self.db, self.cfg, row, marks)
        self.assertIsNotNone(gate, "le dépôt du gel doit être retrouvé")
        self.assertFalse(gate["ok"])
        self.assertEqual(gate["required"], 2)
        self.assertFalse(exploitation._review_gate_open(
            self.cfg, self.db, row, marks, {"verdict_required": True}))
        self.assertEqual(self.slo(), [])

    def test_main_local_en_retard_ne_rajoute_pas_un_avis(self):
        """Un changement SQL déjà sur origin/main n'appartient plus au lot."""
        repo, merged = self._frozen_repo()
        subprocess.run(["git", "-C", repo, "update-ref", "refs/remotes/origin/main",
                        merged], check=True)
        subprocess.run(["git", "-C", repo, "checkout", "-q", "lot"], check=True)
        with open(os.path.join(repo, "docs", "notes.md"), "a") as fh:
            fh.write("suite\n")
        subprocess.run(["git", "-C", repo, "commit", "-qam", "notes"], check=True,
                       env=_GIT_DEV1)
        sha = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                             check=True, capture_output=True, text=True).stdout.strip()
        registry.upsert(self.db, "orch", harness="claude", host=self.cfg.host,
                        cwd=repo)
        registry.upsert(self.db, "cx", harness="codex", host=self.cfg.host)
        row = dict(work.get(self.db, self.lot["id"]), branch_target="main")
        # #2928 : un avis sensible ne compte que sur son reçu exact, lu sur le
        # vrai jalon ; l'avis est donc posé en base avant d'être relu.
        mark = work.milestone(self.db, self.lot["id"], "verdict", sha=sha,
                              actor="cx", verdict="ok", repo=repo)
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (subject) DO NOTHING",
            ("milestone:%d" % int(mark["id"]), sha, "cx", "s-codex", "t",
             "codex", self.lot["id"]))
        marks = [{"kind": "frozen", "sha": sha, "actor": "dev1", "at_ts": 1},
                 {"kind": "verdict", "id": int(mark["id"]), "sha": sha,
                  "actor": "cx", "verdict": "ok", "at_ts": 2}]
        gate = merge_queue.review_gate_for_item(self.db, self.cfg, row, marks)
        self.assertIsNotNone(gate)
        self.assertEqual(gate["required"], 1)
        self.assertTrue(gate["ok"], gate["why"])


class PaquetTest(_Base):
    """#2335 : un paquet en cours est une attente planifiée, et l'horloge
    GO → fusion ne part qu'au premier créneau de paquet après le GO. #2919 :
    cible p80 du paquet sur le chemin normal, aucune escalade propre pour une
    unité de paquet ouvert."""

    GO = datetime.datetime(2026, 10, 17, 23, 5, tzinfo=datetime.timezone.utc).timestamp()

    def setUp(self):
        super().setUp()
        # Le banc ne vide pas `ci_runs` ; chaque témoin de p80 part d'une
        # mesure propre (les runs d'un test ne comptent pas dans le suivant).
        self.db.execute("TRUNCATE ci_test_failures, ci_runs")

    def test_lot_dans_un_paquet_ouvert_est_une_attente_planifiee(self):
        self.qa_at(self.GO)
        self.packet(body="unités : #%d @ abcdef0 #999999 @ cafebabe" % self.lot["id"])
        self.now = self.GO + 75 * 60
        self.assertEqual(self.slo(), [])
        rows = storage.of(self.db).operations.open_lots_activity(None)
        described = stagnation.describe(self.db, rows, now=self.now, open_items=rows)
        info = described[self.lot["id"]]
        self.assertEqual((info["planned_wait"] or {}).get("what"), "packet")
        self.assertEqual((info["waiting"] or {}).get("what"), "packet")
        self.assertIn("packet merge", info["waiting"]["label"])
        self.assertIn("packet merge", work_dependencies.wait_label(info["planned_wait"]))
        self.assertIsNone(info["stale"])

    def test_porteur_sans_liste_lie_par_les_trailers(self):
        # #2599 : le corps des porteurs h-2300/h-0100 n'a pas la liste
        # « #N <tête> » ; les trailers des commits de composition lient
        # l'unité au paquet, et le SLO est tu.
        self.qa_at(self.GO)
        carrier = self.packet(body="aucune liste d'unités dans ce corps")
        with mock.patch.object(planned_waits, "packet_attachments",
                               return_value={carrier["id"]: {self.lot["id"]}}):
            self.now = self.GO + 75 * 60
            self.assertEqual(self.slo(), [])
            rows = storage.of(self.db).operations.open_lots_activity(None)
            described = stagnation.describe(self.db, rows, now=self.now, open_items=rows)
        info = described[self.lot["id"]]
        self.assertEqual((info["planned_wait"] or {}).get("what"), "packet")
        self.assertIn("packet merge", info["waiting"]["label"])
        self.assertIsNone(info["stale"])

    def test_membre_d_un_vehicule_ouvert_ne_leve_pas_le_slo_et_revient_s_il_est_retire(self):
        # #2837 : la composition se lit sur un clone du registre quand l'hôte
        # n'a pas les réglages radar ; l'unité d'un véhicule ouvert est tue,
        # et une recomposition qui la retire rend l'alerte.
        self.qa_at(self.GO)
        repo = tempfile.mkdtemp(prefix="paquet-2837-")
        self.addCleanup(shutil.rmtree, repo, True)

        def git(*args):
            return subprocess.run(
                ["git", "-C", repo, *args], check=True, capture_output=True,
                text=True, env=_GIT_DEV1).stdout.strip()

        git("init", "-q", "-b", "main")
        with open(os.path.join(repo, "README"), "w", encoding="utf-8") as fh:
            fh.write("base\n")
        git("add", "README")
        git("commit", "-q", "-m", "base")
        git("checkout", "-q", "-b", "integration/paquet-test")
        with open(os.path.join(repo, "note"), "w", encoding="utf-8") as fh:
            fh.write("member\n")
        git("add", "note")
        git("commit", "-q", "-m", "vehicule : #%d abcdef0\n\nameesh-work: %d"
            % (self.lot["id"], self.lot["id"]))
        carrier = self.packet(body="composition sans liste",
                              head=git("rev-parse", "HEAD"))
        registry.upsert(self.db, "dev1", harness="claude", host=self.cfg.host,
                        cwd=repo)
        self.now = self.GO + 125 * 60
        self.assertEqual(self.slo(), [])
        # Recomposition sans le lot : le trailer disparaît, l'alerte revient.
        git("checkout", "-q", "-b", "observed", "main")
        with open(os.path.join(repo, "note"), "w", encoding="utf-8") as fh:
            fh.write("other\n")
        git("add", "note")
        git("commit", "-q", "-m",
            "vehicule : #999999 abcdef0\n\nameesh-work: 999999")
        self.db.execute("UPDATE work_items SET branch_head = %s WHERE id = %s",
                        (git("rev-parse", "HEAD"), carrier["id"]))
        self.assertEqual([a["lot"] for a in self.slo()], [self.lot["id"]])

    def test_lot_exclu_en_prose_reste_signale_meme_avec_trailers(self):
        # #2599 : un lot cité en prose n'est pas attaché ; même quand la
        # lecture des trailers rend d'autres unités, son alerte reste due.
        self.qa_at(self.GO)
        exclu = work.add(self.db, title="exclu du paquet", assignee="dev1")
        self.qa_at(self.GO, lot=exclu)
        carrier = self.packet(body="paquet ; #%d exclu (verdict manquant)"
                              % exclu["id"])
        with mock.patch.object(planned_waits, "packet_attachments",
                               return_value={carrier["id"]: {self.lot["id"]}}):
            self.now = self.GO + 125 * 60
            alerts = self.slo()
        self.assertEqual([a["lot"] for a in alerts], [exclu["id"]], alerts)
        self.assertEqual(alerts[0]["step"], "verdict_merge")

    def test_lot_exclu_en_prose_reste_signale(self):
        # B1 (relecture dev7) : un lot seulement cité (« exclu », « garder »)
        # n'est pas transporté ; son alerte ne doit pas être tue par le
        # porteur. Seule une unité nommée avec sa tête l'est.
        self.qa_at(self.GO)
        exclu = work.add(self.db, title="exclu du paquet", assignee="dev1")
        self.qa_at(self.GO, lot=exclu)
        self.packet(body="unités : #%d @ abcdef0 ; #%d exclu (verdict manquant)"
                    % (self.lot["id"], exclu["id"]))
        self.now = self.GO + 125 * 60         # 01:10, après le créneau 00:00
        alerts = self.slo()
        self.assertEqual([a["lot"] for a in alerts], [exclu["id"]], alerts)
        self.assertEqual(alerts[0]["step"], "verdict_merge")
        self.assertIn("pilot", alerts[0]["targets"])

    def test_non_inclus_avant_le_creneau_ne_signale_rien(self):
        # GO 23:05, premier créneau 00:00 : à 00:20, 20 min < cible 30 min.
        self.qa_at(self.GO)
        self.packet(body="unités : #999999 @ cafebabe")
        self.now = self.GO + 75 * 60          # 00:20
        self.assertEqual(self.slo(), [])

    def test_non_inclus_creneau_manque_signale_au_niveau_1(self):
        # 01:10 : le créneau 00:00 est passé et la cible de 30 min est
        # dépassée ; c'est le pilote qui porte la fusion dans le paquet.
        self.qa_at(self.GO)
        self.packet(body="unités : #999999 @ cafebabe")
        self.now = self.GO + 125 * 60         # 01:10
        alerts = self.slo()
        self.assertEqual(len(alerts), 1, alerts)
        self.assertEqual(alerts[0]["step"], "verdict_merge")
        self.assertGreaterEqual(alerts[0]["level"], 1)
        self.assertIn("pilot", alerts[0]["targets"])

    def test_les_mentions_en_prose_ne_sont_pas_des_unites(self):
        # Les quatre faux positifs réels de la relecture dev7 : un lot cité
        # dans une phrase (« exclu », « garder ») n'est pas transporté.
        rows = [{"id": i, "state": "qa"} for i in (2203, 1976, 2062, 1024)]
        carrier = {"id": 9001, "state": "qa", "branch": "integration/paquet-test",
                   "body": "Paquet ; #2203 exclu ; #1976 admis mais exclu du paquet ; "
                           "Périmètre enregistré au lot #2062 ; garder #1024."}
        self.assertEqual(planned_waits.packet_waits(rows, rows + [carrier]), {})

    def test_cadence_du_canon_sans_porteur_observe(self):
        # B2 (relecture dev7) : entre deux paquets (porteur précédent fusionné,
        # suivant pas encore ouvert), la cadence déclarée au canon s'applique
        # quand même ; l'horloge ne repart pas du GO.
        class Cadence:
            @staticmethod
            def next_packet(at):
                return float(at) + 3600.0
        with mock.patch.object(exploitation, "_packet_calendar", return_value=Cadence()):
            self.assertEqual(exploitation._packet_slot(
                None, self.GO, "ameesh", observed=False), self.GO + 3600.0)

    def test_creneau_horaire_utc_seulement_si_paquet_observe(self):
        self.assertEqual(exploitation._packet_slot(
            None, self.GO, "ameesh", observed=True), self.GO + 55 * 60)
        self.assertIsNone(exploitation._packet_slot(
            None, self.GO, "ameesh", observed=False))

    def _ci_run(self, minutes, *, run_id):
        """Une suite complète conclue autour de `GO`, pour le p80 mesuré."""
        self.db.execute(
            "INSERT INTO ci_runs (repo, run_id, head_sha, branch, event, base,"
            " status, url, first_seen_at, concluded_at)"
            " VALUES ('manaty/ameesh', %s, %s, 'integration/paquet-test', 'push',"
            " 'main', 'success', 'u', to_timestamp(%s), to_timestamp(%s))",
            (run_id, "0" * 40, self.GO - 600.0,
             self.GO - 600.0 + float(minutes) * 60.0))

    def test_unite_de_paquet_mesuree_contre_le_p80_sans_alerte(self):
        # #2919 : la cible est le p80 réel de la CI du paquet (7 jours) ; la
        # ligne est mesurée, jamais alertée (aucune cible d'escalade propre).
        self.qa_at(self.GO)
        self.packet(body="unités : #%d @ abcdef0" % self.lot["id"])
        for run_id, minutes in enumerate((10, 20, 30, 40), start=1):
            self._ci_run(minutes, run_id=run_id)
        self.now = self.GO + 75 * 60
        calls = []
        real = slo.lot_steps

        def spy(*args, **kwargs):
            calls.append(kwargs)
            return real(*args, **kwargs)

        with mock.patch.object(exploitation, "_project_github",
                               return_value="manaty/ameesh"), \
                mock.patch.object(slo, "lot_steps", side_effect=spy):
            self.assertEqual(self.slo(), [])
        packet_calls = [kw for kw in calls if kw.get("packet")]
        self.assertEqual(len(packet_calls), 1, calls)
        # [600, 1200, 1800, 2400] : p80 interpolé à 2040 s.
        self.assertEqual(packet_calls[0]["p80_s"], 2040.0)

    def test_chemin_normal_p1_p80_40_min_pas_d_alerte_puis_niveau_1(self):
        # Témoin du N auditeur (#2919) : P1, GO courant hors paquet ouvert,
        # p80 40 min ; 20 min après le créneau → tenu, 41 min → niveau 1.
        self.qa_at(self.GO)
        self.db.execute("UPDATE work_items SET priority = 1 WHERE id = %s",
                        (self.lot["id"],))
        # Un porteur ouvert observe la cadence : le créneau est 00:00 ; le lot
        # n'y est pas, il garde donc l'escalade du chemin normal.
        self.packet(body="unités : #999999 @ cafebabe")
        for run_id, minutes in enumerate((20, 40, 40, 40), start=1):
            self._ci_run(minutes, run_id=run_id)
        with mock.patch.object(exploitation, "_project_github",
                               return_value="manaty/ameesh"):
            self.now = self.GO + 75 * 60          # 00:20, 20 min après le créneau
            self.assertEqual(self.slo(), [])
            self.now = self.GO + 96 * 60          # 00:41, 41 min après le créneau
            alerts = self.slo()
        self.assertEqual([(a["step"], a["level"], a["threshold"]) for a in alerts],
                         [("verdict_merge", 1, 2400)])
        self.assertIn("pilot", alerts[0]["targets"])

    def test_chemin_normal_p1_sans_mesure_garde_le_repli_30_min(self):
        # Témoin cx2 (#15024) : P1 normal sans mesure CI → repli 30 min, pas
        # la cible P1 de 15 min (réservée à une exception déclarée).
        self.qa_at(self.GO)
        self.db.execute("UPDATE work_items SET priority = 1 WHERE id = %s",
                        (self.lot["id"],))
        self.packet(body="unités : #999999 @ cafebabe")
        with mock.patch.object(exploitation, "_project_github",
                               return_value="manaty/ameesh"):
            self.now = self.GO + 75 * 60          # 00:20, 20 min après le créneau
            self.assertEqual(self.slo(), [])
            self.now = self.GO + 96 * 60          # 00:41, 41 min après le créneau
            alerts = self.slo()
        self.assertEqual([(a["step"], a["level"], a["threshold"]) for a in alerts],
                         [("verdict_merge", 1, 1800)])
        self.assertIn("pilot", alerts[0]["targets"])

    def test_exception_p1_garde_15_min_depuis_le_go(self):
        # Hors paquet déclaré : la cible de table (15 min en P1) court depuis
        # le GO, sans le report au créneau de paquet.
        self.qa_at(self.GO)
        self.db.execute("UPDATE work_items SET priority = 1 WHERE id = %s",
                        (self.lot["id"],))
        head = "c" * 40
        self.db.execute("UPDATE work_items SET branch_head = %s WHERE id = %s",
                        (head, self.lot["id"]))
        packet_guard.declare(self.db, self.lot["id"], "unblock",
                             "CI bloquée", head, actor="dev1")
        self.now = self.GO + 16 * 60
        alerts = self.slo()
        self.assertEqual([(a["step"], a["level"], a["threshold"]) for a in alerts],
                         [("verdict_merge", 1, 900)])
        self.assertEqual(alerts[0]["since"], self.GO)
        self.assertEqual(alerts[0]["value"], 60)

    def test_exception_declaree_sort_du_paquet_et_alerte(self):
        # #2919 : une exception `packet-exception incident` pour la tête
        # courante ramène la cible de la table (30 min) et l'escalade.
        self.qa_at(self.GO)
        head = "a" * 40
        self.db.execute("UPDATE work_items SET branch_head = %s WHERE id = %s",
                        (head, self.lot["id"]))
        self.packet(body="unités : #%d @ %s" % (self.lot["id"], head))
        packet_guard.declare(self.db, self.lot["id"], "incident",
                             "cible rouge", head, actor="dev1")
        self.now = self.GO + 50 * 60
        alerts = self.slo()
        self.assertEqual([(a["step"], a["level"], a["targets"]) for a in alerts],
                         [("verdict_merge", 1, ["assignee"])])
        self.assertIsNone(alerts[0]["waiting"])
        self.assertEqual(alerts[0]["threshold"], 1800)

    def test_exception_d_une_autre_tete_ne_sort_pas_du_paquet(self):
        # Une exception ne couvre que sa tête : le nouveau gel reste dans le
        # paquet, mesuré au p80 et sans alerte.
        self.qa_at(self.GO)
        head = "b" * 40
        self.db.execute("UPDATE work_items SET branch_head = %s WHERE id = %s",
                        (head, self.lot["id"]))
        self.packet(body="unités : #%d @ %s" % (self.lot["id"], head))
        packet_guard.declare(self.db, self.lot["id"], "incident",
                             "ancienne tête", "a" * 40, actor="dev1")
        self.now = self.GO + 125 * 60
        with mock.patch.object(exploitation, "_project_github",
                               return_value="manaty/ameesh"):
            self.assertEqual(self.slo(), [])


class BriefsTest(_Base):
    def test_niveau_1_assigne_seul(self):
        self.age(1.5 * HEURE)
        out = self.briefs(self.slo()[0])
        self.assertEqual(sorted(out), ["dev1"])
        self.assertIn("SLO breach, level 1", out["dev1"])
        self.assertIn("late by 30 min (target 1 h)", out["dev1"])
        self.assertIn("ameesh work note %d" % self.lot["id"], out["dev1"])

    def test_niveau_2_pilote_seul(self):
        self.age(2.5 * HEURE)
        out = self.briefs(self.slo()[0])
        self.assertEqual(sorted(out), ["orch"])
        self.assertIn("pilot: reassign", out["orch"])
        self.assertIn("late by 1 h 30 (target 1 h)", out["orch"])

    def test_niveau_3_bureau_et_auditeur(self):
        self.db.execute("UPDATE work_items SET priority = 1 WHERE id = %s",
                        (self.lot["id"],))
        self.age(50 * 60)
        out = self.briefs(self.slo()[0])
        self.assertEqual(sorted(out), ["auditeur", "bureau"])
        self.assertIn("bureau or auditor", out["bureau"])
        for text in out.values():
            self.assertIn("late by 35 min (target 15 min)", text)
        # #2395 : le texte annonce la vraie cible, pas une notification owner.
        self.assertIn("bureau handles it with the auditor", out["bureau"])
        self.assertIn("no SLO delay is notified to the owner", out["bureau"])


class AttentePorteurTest(_Base):
    """#2231 : quand le blocage n'est pas chez le porteur (attente de paquet,
    lot `blocked`), l'alerte vise le pilote/le composeur et ne prescrit ni
    relance ni réassignation du porteur. L'horloge GO → merge ne part qu'une
    fois tous les avis requis acquis sur la tête gelée."""

    def test_attente_paquet_vise_le_pilote(self):
        self.qa(verdict_s=50 * 60)
        alerts = self.slo()
        self.assertEqual([(a["step"], a["level"], a["targets"]) for a in alerts],
                         [("verdict_merge", 1, ["pilot"])])
        alert = alerts[0]
        self.assertEqual((alert["waiting"] or {}).get("what"), "merge")
        self.assertEqual(alert["orchestrators"], ["orch"])
        out = self.briefs(alert)
        self.assertEqual(sorted(out), ["orch"])
        self.assertIn("merge vehicle/package", out["orch"])
        self.assertIn("do not reassign or nudge its holder", out["orch"])
        self.assertNotIn("ameesh work assign", out["orch"])

    def test_attente_paquet_niveau_3_pousse_le_pilote(self):
        self.qa(verdict_s=100 * 60)
        alerts = self.slo()
        self.assertEqual([(a["level"], a["targets"]) for a in alerts],
                         [(3, ["pilot", "bureau", "auditeur"])])
        out = self.briefs(alerts[0])
        self.assertEqual(sorted(out), ["auditeur", "bureau", "orch"])
        for text in out.values():
            self.assertIn("merge vehicle/package", text)
            self.assertIn("push the pilot/composer", text)
            self.assertNotIn("reassign it now", text)

    def test_fusion_canon_vise_le_bureau(self):
        """#2511 : une branche `canon/…` cible manaty/home, qu'un paquet ameesh
        ne porte pas ; l'alerte va au bureau qui fusionne, pas au pilote."""
        self.qa(verdict_s=50 * 60)
        work.set_branch(self.db, self.lot["id"], "canon/2511-test")
        alerts = self.slo()
        self.assertEqual([(a["step"], a["level"], a["targets"]) for a in alerts],
                         [("verdict_merge", 1, ["bureau"])])
        alert = alerts[0]
        self.assertEqual((alert["waiting"] or {}).get("what"), "merge")
        self.assertTrue((alert["waiting"] or {}).get("canon"))
        out = self.briefs(alert)
        self.assertEqual(sorted(out), ["bureau"])
        self.assertIn("merge it directly", out["bureau"])
        self.assertIn("manaty/home", out["bureau"])
        self.assertNotIn("push the pilot/composer", out["bureau"])
        self.assertNotIn("include it in the next package", out["bureau"])

    def test_fusion_canon_niveau_3_bureau_et_auditeur(self):
        """#2511 : au triple, le bureau fusionne (l'auditeur veille) ; le
        composeur de paquets ameesh n'est plus nommé."""
        self.qa(verdict_s=100 * 60)
        work.set_branch(self.db, self.lot["id"], "canon/2511-test")
        alerts = self.slo()
        self.assertEqual([(a["level"], a["targets"]) for a in alerts],
                         [(3, ["bureau", "auditeur"])])
        out = self.briefs(alerts[0])
        self.assertEqual(sorted(out), ["auditeur", "bureau"])
        for text in out.values():
            self.assertIn("merge it directly", text)
            self.assertNotIn("push the pilot/composer", text)

    def test_lot_bloque_vise_le_pilote(self):
        self.qa(verdict_s=50 * 60)
        work.move(self.db, self.lot["id"], "blocked",
                  note="attente d'un 2e avis hors DeepSeek/Claude (Codex 22/10)")
        self.now = time.time()
        alerts = self.slo()
        self.assertEqual([(a["step"], a["targets"]) for a in alerts],
                         [("verdict_merge", ["pilot"])])
        self.assertEqual((alerts[0]["waiting"] or {}).get("what"), "unblock")
        out = self.briefs(alerts[0])
        self.assertIn("`blocked`", out["orch"])
        self.assertIn("unblock", out["orch"])
        self.assertNotIn("reassign it now", out["orch"])

    def test_premier_avis_ne_fait_pas_le_go(self):
        self.qa(verdict_s=50 * 60)
        klass = {"class": "sensitive", "doubt": False, "frozen_sha": "a" * 40,
                 "repo": None}
        with mock.patch("ameesh.lot_class.of_lot", return_value=klass), \
                mock.patch("ameesh.merge_queue.review_gate",
                           return_value={"ok": False, "required": 2, "accepted": [1]}):
            self.assertEqual(self.slo(), [])
        with mock.patch("ameesh.lot_class.of_lot", return_value=klass), \
                mock.patch("ameesh.merge_queue.review_gate",
                           return_value={"ok": True, "required": 2,
                                         "accepted": [1, 2]}):
            alerts = self.slo()
        self.assertEqual([(a["step"], a["targets"]) for a in alerts],
                         [("verdict_merge", ["pilot"])])

    def test_classe_douteuse_ne_tait_pas_l_alerte(self):
        self.qa(verdict_s=50 * 60)
        klass = {"class": "sensitive", "doubt": True, "frozen_sha": "", "repo": None}
        with mock.patch("ameesh.lot_class.of_lot", return_value=klass):
            self.assertEqual(len(self.slo()), 1)


    def test_unblock_outside_verdict_merge_keeps_the_assignee_text(self):
        # The brief follows `_slo_targets`: outside `verdict_merge` the targets
        # stay the assignee's, so the text is not the pilot's.
        alert = {"lot": self.lot["id"], "level": 1, "step": "signal_assigned",
                 "waiting": {"what": "unblock"}, "agent": "dev1",
                 "value": 5400, "threshold": 3600}
        text = alert_briefs._slo_breach(alert)
        self.assertIn("assignee: act on it", text)
        self.assertNotIn("do not reassign its holder", text)


class AttenteDecisionTest(_Base):
    """#2378 : un lot `waiting_human` à demande de décision ouverte attend son
    décideur. Non échue, c'est une attente prévue (aucune alerte) ; échue,
    l'alerte vise le pilote et prescrit de vérifier l'échéance avec le
    demandeur, jamais de réassigner le porteur ni de relancer l'owner (canon
    §9). Sans demande, rien ne change."""

    def ask(self, due_in_s=2 * HEURE):
        view = decisions.ask(self.cfg, self.db, requester="dev1",
                             question="Quel critère pour la porte des avis ?",
                             options=["a=union", "b=par auteur"],
                             lot=str(self.lot["id"]), due_ts=time.time() + due_in_s)
        self.now = time.time()
        return view

    def expire(self, view, ago_s=10 * 60):
        """La demande est échue depuis `ago_s` (l'échéance passe en base)."""
        self.db.execute(
            "UPDATE agent_mailbox SET payload = jsonb_set(payload, '{decision,due_ts}',"
            " to_jsonb(%s::float8)) WHERE id = %s",
            (time.time() - ago_s, view["id"]))
        self.now = time.time()

    def test_demande_ouverte_non_echue_tait_l_alerte(self):
        self.qa(verdict_s=100 * 60)
        self.assertEqual([a["level"] for a in self.slo()], [3])
        view = self.ask()
        self.assertEqual(work.get(self.db, self.lot["id"])["state"], "waiting_human")
        self.assertEqual(self.slo(), [])
        self.assertEqual([a for a in exploitation.alerts(self.cfg, self.db, now=self.now,
                                                          **_seuils(stale_lot_s=60))
                          if a["type"] == "stale_lot" and a.get("lot") == self.lot["id"]],
                         [])
        wait = stagnation_wait(self.db, self.lot, self.now)
        self.assertEqual((wait["what"], wait["decision"]), ("decision", view["id"]))
        self.assertIn("decision #%d" % view["id"], work_dependencies.wait_label(wait))

    def test_demande_echue_vise_le_pilote_sans_reassignation(self):
        self.qa(verdict_s=100 * 60)
        view = self.ask()
        self.expire(view)
        alerts = self.slo()
        self.assertEqual([(a["step"], a["level"], a["targets"]) for a in alerts],
                         [("verdict_merge", 3, ["pilot", "bureau", "auditeur"])])
        waiting = alerts[0]["waiting"]
        self.assertEqual((waiting["what"], waiting["decision"]), ("decision", view["id"]))
        out = self.briefs(alerts[0])
        self.assertEqual(sorted(out), ["auditeur", "bureau", "orch"])
        for text in out.values():
            self.assertIn("decision #%d" % view["id"], text)
            self.assertIn("check the deadline with the requester", text)
            self.assertNotIn("remind the decider", text)
            self.assertIn("deadline", text)
            self.assertNotIn("reassign it now", text)
            self.assertNotIn("ameesh work assign", text)

    def test_demande_echue_niveau_1_vise_le_pilote(self):
        self.qa(verdict_s=50 * 60)
        self.expire(self.ask())
        alerts = self.slo()
        self.assertEqual([(a["level"], a["targets"]) for a in alerts], [(1, ["pilot"])])
        out = self.briefs(alerts[0])
        self.assertEqual(sorted(out), ["orch"])
        self.assertIn("pilot: the lot is `waiting_human` on decision", out["orch"])
        self.assertIn("do not reassign its holder", out["orch"])

    def test_waiting_human_sans_demande_inchange(self):
        # ligne antérieure à L233 : `work.move` refuserait la transition
        self.qa(verdict_s=50 * 60)
        self.db.execute("UPDATE work_items SET state = 'waiting_human' WHERE id = %s",
                        (self.lot["id"],))
        self.now = time.time()
        alerts = self.slo()
        self.assertEqual([(a["level"], a["targets"]) for a in alerts], [(1, ["assignee"])])
        self.assertNotIn("decision", alerts[0]["waiting"])
        self.assertIn("assignee: act on it", self.briefs(alerts[0])["dev1"])


def stagnation_wait(db, lot, now):
    from ameesh import stagnation
    row = work.get(db, lot["id"])
    return stagnation.describe(db, [row], now=now)[int(lot["id"])]["planned_wait"]


class RetourAttenteHumaineTest(_Base):
    """#2916 : au retour en intake après une décision owner, le délai intake
    repart de ce retour. Les quatre alertes de niveau 3 du 21/10 venaient de
    lots sortis de `waiting_human` : leur porteur reprenait le travail, une
    réattribution aurait cassé ce tour."""

    def answer_after_two_days(self):
        """Le lot attend une décision owner 2 jours, puis elle est rendue."""
        held_s = 2 * 24 * HEURE
        self.age(held_s)
        view = decisions.ask(self.cfg, self.db, requester="dev1",
                             question="Quel critère pour la porte des avis ?",
                             options=["a=union", "b=par auteur"],
                             lot=str(self.lot["id"]))
        self.assertEqual(work.get(self.db, self.lot["id"])["state"], "waiting_human")
        # la demande a tenu le lot depuis son début (journal)
        self.db.execute(
            "UPDATE work_item_events SET created_at = now() - make_interval(secs => %s)"
            " WHERE work_item_id = %s AND state = 'waiting_human'",
            (float(held_s) - 60.0, self.lot["id"]))
        result = decisions.answer(self.cfg, self.db, view["id"], "a",
                                 human="human:alice", channel="cli")
        self.assertEqual(work.get(self.db, self.lot["id"])["state"], "intake")
        self.assertEqual(result["lot"]["state"], "intake")
        return time.time()

    def test_retour_en_intake_repart_de_la_decision(self):
        answered = self.answer_after_two_days()
        for offset, levels in ((300, []), (3660, [1])):
            with self.subTest(offset=offset):
                self.now = answered + offset
                alerts = [a for a in self.slo() if a["step"] == "intake_started"]
                self.assertEqual([a["level"] for a in alerts], levels)

    def test_hold_de_pilote_repart_de_la_levee(self):
        # #2916/#1225 : le cas réel nexlink #1225 — un lot parqué deux jours
        # sous un hold de pilote ne demande pas de réattribution à la levée.
        self.age(2 * 24 * HEURE)
        work.note(self.db, self.lot["id"], "hold: attente des gestes owner",
                  actor="human:pilote")
        # la retenue date du début du lot (journal)
        self.db.execute(
            "UPDATE work_item_events SET created_at = now() - make_interval(secs => %s)"
            " WHERE work_item_id = %s AND note LIKE 'hold:%%'",
            (2 * 24 * HEURE - 60.0, self.lot["id"]))
        self.now = time.time()
        self.assertEqual([a for a in self.slo() if a["step"] == "intake_started"], [])
        lifted = time.time()
        work.note(self.db, self.lot["id"], "unhold: gestes owner faits",
                  actor="human:pilote")
        for offset, levels in ((300, []), (3660, [1])):
            with self.subTest(offset=offset):
                self.now = lifted + offset
                alerts = [a for a in self.slo() if a["step"] == "intake_started"]
                self.assertEqual([a["level"] for a in alerts], levels)


class NotifyLivreTest(_Base):
    """B1 (#1035 R1) : la livraison RÉELLE par le Notifier — depuis #2395,
    aucun `slo_breach` n'est poussé à l'owner (canon §9, décision owner 18/10),
    même un P1 et même si son type est configuré ; le courrier d'agent porte
    l'urgence du P1, et la clé de dédoublonnage change quand le niveau monte."""

    def setUp(self):
        super().setUp()
        self.desk = os.path.join(self.tmp, "desktop-789-%s.log" % self._testMethodName)
        self.logs: list = []
        self.now = time.time()

    def notifier(self, **raw):
        raw.setdefault("default", ["desktop"])
        raw.setdefault("default_human", "human:owner")
        return notify.Notifier(
            self.cfg, notify.parse_config(raw), None, log=self.logs.append,
            clock=lambda: self.now,
            sender=notify.Sender(env={
                # #3042 : le canal desktop suit l'OS ; le faux binaire doit
                # couvrir les deux backends du banc (Linux notify-send, macOS
                # osascript), qui journalisent tous deux argv dans `desk`.
                "AMEESH_NOTIFY_SEND_BIN": FAKE_NOTIFY_SEND,
                "AMEESH_OSASCRIPT_BIN": FAKE_NOTIFY_SEND,
                "AMEESH_FAKE_NOTIFY_LOG": self.desk,
                "PATH": os.environ.get("PATH", "")}))

    def desktop(self):
        if not os.path.exists(self.desk):
            return []
        with open(self.desk, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def payload(self, recipient):
        rows = self.db.query("SELECT payload FROM agent_mailbox WHERE recipient = %s"
                             " ORDER BY id DESC LIMIT 1", (recipient,))
        self.assertTrue(rows, "aucun courrier pour %s" % recipient)
        return rows[0]["payload"] or {}

    def p1(self):
        self.db.execute("UPDATE work_items SET priority = 1 WHERE id = %s",
                        (self.lot["id"],))
        self.age(50 * 60)
        alerts = [a for a in self.slo() if a["critical"]]
        self.assertEqual(len(alerts), 1, alerts)
        return alerts

    def test_p1_jamais_livre_a_l_owner_meme_type_hors_notify_types(self):
        alerts = self.p1()
        records = self.notifier(types=["long_turn"]).run_pass(self.db, current=alerts)
        raised = [r for r in records if r["event"] == "raised" and r["type"] == "slo_breach"]
        self.assertEqual(raised, [])
        self.assertEqual(self.desktop(), [])

    def test_slo_breach_configure_ne_livre_pas_a_l_owner(self):
        self.age(1.5 * HEURE)
        alerts = self.slo()
        self.assertFalse(alerts[0]["critical"])
        records = self.notifier(types=["long_turn"]).run_pass(self.db, current=alerts)
        self.assertEqual([r for r in records if r["event"] == "raised"], [])
        self.assertEqual(self.desktop(), [])
        records = self.notifier(types=["long_turn", "slo_breach"]).run_pass(
            self.db, current=alerts)
        raised = [r for r in records if r["event"] == "raised"]
        self.assertEqual([(r["type"], r["human"], r["delivery"]) for r in raised],
                         [("slo_breach", None, "sans_destinataire")])
        self.assertEqual(self.desktop(), [])

    def test_slo_breach_part_a_un_responsable_nomme(self):
        # Un humain explicitement responsable n'est pas l'owner : il reçoit
        # toujours l'alerte ; seule la route par défaut vers l'owner est close.
        self.db.execute("UPDATE agent_registry SET responsible = 'human:pilote'"
                        " WHERE name = 'dev1'")
        self.age(1.5 * HEURE)
        records = self.notifier(types=["slo_breach"]).run_pass(
            self.db, current=self.slo())
        raised = [r for r in records if r["event"] == "raised"]
        self.assertEqual([(r["human"], r["source"], r["delivery"]) for r in raised],
                         [("human:pilote", "agent", "envoyee")])
        self.assertEqual(len(self.desktop()), 1)

    def test_courrier_agent_urgent_pour_un_p1_et_ordinaire_sinon(self):
        self.p1()
        self.notifier(types=["long_turn"]).run_pass(self.db, current=self.slo())
        self.assertTrue(self.payload("bureau").get("urgent"))
        self.db.execute("UPDATE agent_mailbox SET delivered_at = now()")
        self.db.execute("DELETE FROM agent_mailbox")
        self.db.execute("UPDATE work_items SET priority = 2 WHERE id = %s",
                        (self.lot["id"],))
        self.age(1.5 * HEURE)
        self.notifier(types=["long_turn"]).run_pass(self.db, current=self.slo())
        self.assertNotIn("urgent", self.payload("dev1"))

    def test_resolution_slo_ancienne_ne_part_pas_a_l_owner(self):
        # #2395 : une entrée persistée par l'ancienne politique (livraison ou
        # résolution en vol vers l'owner) n'est ni retentée ni résolue vers
        # lui ; elle s'arrête sans envoi.
        alert = {"type": "slo_breach", "agent": "dev1", "lot": self.lot["id"],
                 "since": 1000.0, "value": 600, "threshold": 900,
                 "detail": "work item: step late", "critical": True,
                 "targets": ["owner"], "reason": "build_quiet/level-3"}
        notifier = self.notifier(types=["slo_breach"])
        notifier.state["active"]["old"] = {
            "alert": alert, "human": "human:owner", "source": "defaut",
            "status": "envoi", "raised_ts": 1000.0,
            "channels": {"desktop": {"ok": True, "attempts": 1, "error": None}}}
        notifier.state["resolved"]["oldr"] = {
            "alert": alert, "human": "human:owner", "source": "defaut",
            "status": "envoi", "raised_ts": 1000.0,
            "channels": {"desktop": {"ok": False, "attempts": 0, "error": None}}}
        records = notifier.run_pass(self.db, current=[])
        self.assertFalse([r for r in records if r["delivery"] == "envoyee"])
        self.assertEqual(self.desktop(), [])

    def test_ancien_resume_slo_owner_n_est_pas_envoye(self):
        # #2395 B1 : un bucket de résumé persisté par l'ancienne politique
        # (`human:owner`, raised slo_breach) ne part pas au desktop owner ; il
        # est filtré, pas seulement les entrées active/resolved.
        notifier = self.notifier(types=["slo_breach"])
        notifier.state["summary"]["human:owner"] = {
            "raised": {"slo_breach": 1}, "resolved": 0, "attempts": 0}
        records = notifier.run_pass(self.db, current=[])
        self.assertEqual([r for r in records if r["event"] == "summary"], [])
        self.assertEqual(self.desktop(), [])
        self.assertEqual(notifier.state["summary"], {})

    def test_ancien_resume_owner_garde_les_autres_types(self):
        # le filtrage est ciblé : les autres alertes du même bucket owner
        # restent rendues (sans le slo_breach).
        notifier = self.notifier(types=["slo_breach"])
        notifier.state["summary"]["human:owner"] = {
            "raised": {"slo_breach": 1, "stopped_with_mail": 2},
            "resolved": 0, "attempts": 0}
        records = notifier.run_pass(self.db, current=[])
        resumes = [r for r in records if r["event"] == "summary"]
        self.assertEqual(len(resumes), 1)
        self.assertIn("2 stopped_with_mail", resumes[0]["text"])
        self.assertNotIn("slo_breach", resumes[0]["text"])
        self.assertEqual(len(self.desktop()), 1)
        self.assertNotIn("slo_breach", self.desktop()[0][-1])
        self.assertEqual(notifier.state["summary"], {})

    def test_ancien_resume_owner_sans_type_ne_part_pas(self):
        # Un compte de résolutions de l'ancien format ne porte pas le type :
        # impossible de garantir l'absence de slo_breach, donc il ne part pas
        # vers l'owner (règle owner 18/10).
        notifier = self.notifier(types=["slo_breach"])
        notifier.state["summary"]["human:owner"] = {
            "raised": {}, "resolved": 1, "attempts": 0}
        records = notifier.run_pass(self.db, current=[])
        self.assertEqual([r for r in records if r["event"] == "summary"], [])
        self.assertEqual(self.desktop(), [])
        self.assertEqual(notifier.state["summary"], {})

    def test_cle_de_dedoublonnage_change_avec_le_niveau(self):
        self.age(1.5 * HEURE)
        bas = self.slo()[0]
        self.age(2.5 * HEURE)
        haut = self.slo()[0]
        self.assertEqual((bas["reason"], haut["reason"]),
                         ("intake_started/level-1", "intake_started/level-2"))
        self.assertNotEqual(notify.key_of(bas), notify.key_of(haut))


class LotIllisibleTest(_Base):
    """#1107 (n2 P3 de #1035) : un lot illisible n'éteint plus les alertes.

    Un instant qui n'en est pas un fait lever `float()` dans le lecteur partagé
    (`stagnation.describe`) ; sans garde, ce seul lot faisait tomber **toute**
    la passe d'alertes. Le lot fautif est signalé (`lot_unreadable`), sans
    horodatage inventé ; les lots sains gardent leurs alertes. La ligne
    malformée est injectée à la lecture (`open_lots_activity`) : le calcul des
    jalons, de l'attente et du SLO reste le vrai."""

    def _illisible(self, malsain, **champs):
        vrai = Operations.open_lots_activity

        def lire(this, limit):
            return [dict(row, **champs) if int(row["id"]) == int(malsain["id"]) else row
                    for row in vrai(this, limit)]
        return mock.patch.object(Operations, "open_lots_activity", lire)

    def _lots(self):
        self.age(1.5 * HEURE)                           # lot sain, en retard
        # #540 : dev1 a déjà un lot non démarré, un second irait à la file
        # d'équipe (sans assigné) ; le lot illisible a donc son propre agent,
        # pour vérifier que le signal va bien à l'assigné.
        registry.upsert(self.db, "dev3", harness="claude", host=self.cfg.host)
        malsain = work.add(self.db, title="lot illisible", assignee="dev3")
        self.age(1.5 * HEURE, lot=malsain)
        return malsain

    def _alertes(self, malsain, **champs):
        with self._illisible(malsain, **champs):
            # 7 h plus tard : les deux lots sont aussi stagnants (seuil 6 h)
            return exploitation.alerts(self.cfg, self.db, now=self.now + 7 * HEURE,
                                       **_seuils(orchestrators_declared=["orch"]))

    def _verifie(self, alerts, malsain, valeur, slo):
        par_type = lambda t: [a["lot"] for a in alerts if a["type"] == t]
        self.assertEqual(par_type("slo_breach"), slo)
        self.assertEqual(par_type("stale_lot"), [self.lot["id"]])
        signaux = [a for a in alerts if a["type"] == "lot_unreadable"]
        self.assertEqual([a["lot"] for a in signaux], [malsain["id"]])  # un seul signal
        self.assertEqual(signaux[0]["agent"], "dev3")
        self.assertIsNone(signaux[0]["since"])           # aucun instant inventé
        self.assertIn("ValueError", signaux[0]["detail"])
        self.assertIn(valeur, signaux[0]["detail"])
        # un type d'alerte non enregistré ferait tomber le tri d'`alerts()`
        self.assertIn("lot_unreadable", exploitation.ALERT_TYPES)
        self.assertIn("lot_unreadable", notify.TYPE_LABELS)

    def test_created_ts_malforme(self):
        malsain = self._lots()
        alerts = self._alertes(malsain, created_ts="pas-un-instant")
        self._verifie(alerts, malsain, "pas-un-instant", [self.lot["id"]])

    def test_last_activity_ts_malforme(self):
        malsain = self._lots()
        alerts = self._alertes(malsain, last_activity_ts="jamais")
        # le SLO ne lit pas `last_activity_ts` : le lot garde son échéance
        self._verifie(alerts, malsain, "jamais", [self.lot["id"], malsain["id"]])


if __name__ == "__main__":
    unittest.main()
