# SPDX-License-Identifier: AGPL-3.0-only
"""Correctif du suivi des lots (constat du 2026-10-10).

Des lots fusionnés en local sur la branche d'intégration, sans PR ni gel ni
branche déclarée, restaient en `intake` : ni `sync-merges`, ni `sync-github`,
ni le relevé des branches ne les fermaient, et `stale_lot` les disait
« sans activité ». Les événements écrits par `ameesh work` sans `--actor`
avaient un acteur vide ; `ameesh projects` montrait comme lot en cours un lot
périmé ; un `promoted` posé par erreur ne se corrigeait pas.

* `ameesh work merged <id> --sha S` : tout état ouvert → `merged` ;
* `ameesh work move <id> merged --correct "raison"` : un `promoted` à tort,
  réservé aux humains et aux orchestrateurs ou agents de conception ;
* relevé des fusions : `ameesh-work: <id>` dans un commit de fusion, `#<id>`
  dans son titre si le dépôt l'active (`git config ameesh.lotRef hash`),
  cible `git config ameesh.target` (introuvable : une erreur) ;
* acteur par défaut : l'identité liée, sinon « inconnu » (averti) ;
* lot en cours : le dernier lot ouvert cité par l'agent, jamais un lot fermé.
"""
from __future__ import annotations

import os
import subprocess
import time
import unittest
from unittest import mock

from ameesh import (assignments, exploitation, land, mail, plan_git, process_verifiers,
                    projects, registry, review_lot, storage, sous_utilisation, work)

from .support import PgTestCase, hold_for_owner

SHA = "0123456789abcdef0123456789abcdef01234567"
#: l'identité de la session qui lance les tests ne doit pas fuir dans ceux qui
#: résolvent l'identité dans le processus même
_IDENTITE = ("AGENT_MAIL_NAME", "AMEESH_RUNNER_ID", "AGENT_MESH_RUNNER_ID",
             "AMEESH_LEASE_EPOCH", "AGENT_MESH_LEASE_EPOCH")


def identite(name: str | None = None):
    """Environnement du processus avec, pour seule identité, `name` (ou aucune)."""
    env = {k: v for k, v in os.environ.items() if k not in _IDENTITE}
    if name:
        env["AGENT_MAIL_NAME"] = name
    return mock.patch.dict(os.environ, env, clear=True)


def _git(repo, *args, env=None):
    base = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    base.update(env or {})
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, env=base)
    if proc.returncode != 0:
        raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


class _Base(PgTestCase):
    def agent(self, name, **kw):
        registry.upsert(self.db, name, harness="claude", host=kw.pop("host", self.cfg.host),
                        **kw)

    def promoted(self, title="livré"):
        lot = work.add(self.db, title=title, actor="orch")
        for state in ("build", "qa", "merged", "promoted"):
            work.move(self.db, lot["id"], state, actor="orch")
        return lot

    def merged_milestone(self, lot_id):
        return next(m for m in work.milestones(self.db, lot_id) if m["kind"] == "merged")


class MergedCommandTest(_Base):
    """`ameesh work merged <id> --sha S`."""

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "declared-repo")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "remote", "add", "origin", "git@github.com:acme/web.git")

    def proof(self, lot):
        self.db.execute("UPDATE work_items SET issue_ref = %s WHERE id = %s",
                        ("acme/web#9", lot["id"]))
        _git(self.repo, "commit", "--allow-empty", "-qm",
             "Deliver\n\nameesh-work: %d" % lot["id"])
        return _git(self.repo, "rev-parse", "HEAD")

    def test_tout_etat_ouvert_passe_merged_en_une_fois(self):
        for state in ("intake", "build", "qa", "blocked", "waiting_human"):
            lot = work.add(self.db, title="lot %s" % state, actor="orch")
            if state == "qa":
                work.move(self.db, lot["id"], "build")
            if state == "waiting_human":
                # L233 : waiting_human only behind an open owner request
                hold_for_owner(self.db, lot["id"])
            elif state != "intake":
                work.move(self.db, lot["id"], state)
            sha = self.proof(lot)
            done = work.merged(self.db, lot["id"], sha=sha.upper(), actor="claude1", repo=self.repo,
                               note="fusion locale sur develop")
            self.assertEqual((done["result"], done["item"]["state"]), ("merged", "delivered"),
                             state)
            jalon = self.merged_milestone(lot["id"])
            self.assertEqual((jalon["sha"], jalon["actor"]), (sha, "claude1"))
            note = work.events(self.db, lot["id"])[0]
            self.assertEqual(note["actor"], "claude1")
            self.assertEqual(note["note"], "merge declared by claude1 (commit %s) — fusion "
                                           "locale sur develop [via git-merge]" % sha[:12])

    def test_idempotent_et_jamais_de_reouverture(self):
        lot = work.add(self.db, title="lot")
        sha = self.proof(lot)
        work.merged(self.db, lot["id"], sha=sha, actor="claude1", repo=self.repo)
        again = work.merged(self.db, lot["id"], sha="abcdef1", actor="claude2")
        self.assertEqual(again["result"], "already")
        self.assertEqual(self.merged_milestone(lot["id"])["sha"], sha)   # rien de réécrit
        ferme = work.add(self.db, title="abandonné")
        work.close(self.db, ferme["id"], abandoned=True)
        with self.assertRaises(work.WorkError) as ctx:
            work.merged(self.db, ferme["id"], sha=SHA, actor="claude1")
        self.assertIn("not reopened", str(ctx.exception))
        for bad in ("", "xyz1234", "abc12", "g" * 40):
            with self.assertRaises(work.WorkError):
                work.merged(self.db, lot["id"], sha=bad, actor="claude1")

    def test_cli_acteur_lie_par_defaut(self):
        lot = work.add(self.db, title="lot")
        sha = self.proof(lot)
        proc = self.mesh("work", "merged", str(lot["id"]), "--sha", sha[:12], "--repo", self.repo,
                         "--note", "à la main",
                         env=self.env(AGENT_MAIL_NAME="claude1"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("lot #%d → merged (commit %s, declared by claude1)"
                      % (lot["id"], sha[:12]), proc.stdout)
        self.assertEqual(self.merged_milestone(lot["id"])["sha"], sha)
        self.assertEqual(self.merged_milestone(lot["id"])["actor"], "claude1")
        proc = self.mesh("work", "merged", str(lot["id"]), "--sha", SHA,
                         env=self.env(AGENT_MAIL_NAME="claude1"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("already delivered", proc.stdout)
        proc = self.mesh("work", "merged", str(lot["id"]))          # --sha obligatoire
        self.assertEqual(proc.returncode, 2)

    def test_le_constat_est_affiche_avant_l_ecriture(self):
        # #1400 : lot/app/titre et sujet du commit sont montrés avant que le
        # lot ne change d'état ; la branche ENREGISTRÉE du lot est reconnue
        # dans un vrai commit de fusion GitHub (propriétaire canonique).
        lot = work.add(self.db, title="déclaration de fusion", app="nexlink",
                       issue_ref="acme/web#12")
        self.db.execute("UPDATE work_items SET branch = %s WHERE id = %s",
                        ("agent/cx3-declaration", lot["id"]))
        _git(self.repo, "commit", "--allow-empty", "-qm", "Base")
        _git(self.repo, "checkout", "-qb", "agent/cx3-declaration")
        with open(os.path.join(self.repo, "livre.txt"), "w") as fh:
            fh.write("work\n")
        _git(self.repo, "add", "livre.txt")
        _git(self.repo, "commit", "-qm", "Work")
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "merge", "--no-ff", "agent/cx3-declaration", "-qm",
             "Merge pull request #12 from acme/agent/cx3-declaration")
        sha = _git(self.repo, "rev-parse", "HEAD")
        proc = self.mesh("work", "merged", str(lot["id"]), "--sha", sha, "--repo", self.repo,
                         env=self.env(AGENT_MAIL_NAME="claude1"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("declaring merge: lot #%d (nexlink: déclaration de fusion)" % lot["id"],
                      proc.stdout)
        self.assertIn("commit %s (recorded branch): Merge pull request #12 from "
                      "acme/agent/cx3-declaration" % sha[:12], proc.stdout)
        self.assertLess(proc.stdout.index("declaring merge"),
                        proc.stdout.index("→ merged"))
        self.assertEqual(self.merged_milestone(lot["id"])["sha"], sha)

    def test_commit_d_un_autre_lot_ne_modifie_ni_etat_ni_jalon(self):
        mine = work.add(self.db, title="redemption", app="nexlink", issue_ref="acme/web#9")
        other = work.add(self.db, title="git identity")
        sha = self.proof(other)
        before = work.events(self.db, mine["id"])
        milestones = work.milestones(self.db, mine["id"])
        proc = self.mesh("work", "merged", str(mine["id"]), "--sha", sha,
                         "--repo", self.repo)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("nexlink: redemption", proc.stderr)
        self.assertEqual(work.get(self.db, mine["id"])["state"], "intake")
        self.assertEqual(work.events(self.db, mine["id"]), before)
        self.assertEqual(work.milestones(self.db, mine["id"]), milestones)


class CorrectionTest(_Base):
    """`ameesh work move <id> <état> --correct "raison"` : un état posé par
    erreur — `promoted` à tort revient en `merged`, `merged` à tort (PR d'une
    étape fermée trop tôt, #759) revient en `build`."""

    def setUp(self):
        super().setUp()
        for name in ("orch", "dev1"):
            self.agent(name)
        self.lot = self.promoted()

    def correct(self, *extra, **env):
        return self.mesh("work", "move", str(self.lot["id"]), "merged", "--correct",
                         "promu par erreur : pas encore en production", *extra,
                         env=self.env(**env))

    def test_sans_correct_la_transition_reste_refusee_avec_la_marche_a_suivre(self):
        with self.assertRaises(work.WorkError) as ctx:
            work.move(self.db, self.lot["id"], "merged")
        self.assertIn("--correct", str(ctx.exception))

    def test_orchestrateur(self):
        proc = self.correct(AGENT_MAIL_NAME="orch", AMEESH_ALERT_ORCHESTRATORS="orch")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("correction recorded in the log, by orch", proc.stdout)
        item = work.get(self.db, self.lot["id"])
        self.assertEqual(item["state"], "delivered")
        self.assertIsNone(item["closed_ts"])
        event = work.events(self.db, self.lot["id"])[0]
        self.assertEqual((event["state"], event["actor"]), ("merged", "orch"))
        self.assertEqual(event["note"], "correction: promoted → merged — promu par erreur : "
                                        "pas encore en production")
        # un seul jalon de fusion : la correction n'en invente pas un second
        self.assertEqual(sum(1 for m in work.milestones(self.db, self.lot["id"])
                             if m["kind"] == "merged"), 1)
        # ensuite, les transitions habituelles
        self.assertEqual(work.move(self.db, self.lot["id"], "blocked")["state"], "blocked")

    def test_humain_qui_se_nomme(self):
        proc = self.correct("--actor", "human:proprio")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.events(self.db, self.lot["id"])[0]["actor"], "human:proprio")

    def test_refus(self):
        cas = [
            ((), {}, "say who corrects"),                                  # humain anonyme
            ((), {"AGENT_MAIL_NAME": "dev1"}, "correction refused: dev1"),   # agent ordinaire
            (("--actor", "human:proprio"), {"AGENT_MAIL_NAME": "dev1"},
             "this session belongs to dev1"),                            # au nom d'un autre
            (("--actor", "dev1"), {}, "correction refused: dev1"),
        ]
        for extra, env, attendu in cas:
            proc = self.correct(*extra, **env)
            self.assertEqual(proc.returncode, 1, (extra, env, proc.stdout))
            self.assertIn(attendu, proc.stderr, (extra, env))
        self.assertEqual(work.get(self.db, self.lot["id"])["state"], "delivered")
        # seules corrections possibles : promoted → merged et merged → build
        autre = work.add(self.db, title="autre")
        with self.assertRaises(work.WorkError) as ctx:
            work.move(self.db, autre["id"], "merged", actor="orch", correct="raison")
        self.assertIn("correction refused: intake → merged", str(ctx.exception))
        with self.assertRaises(work.WorkError):
            work.move(self.db, self.lot["id"], "merged", actor="orch", correct="  ")

    def test_759_merged_a_tort_revient_en_build(self):
        # #759 : une PR d'étape a fermé le lot trop tôt ; l'orchestrateur le
        # corrige en `build`, le journal le dit, et le lot repart.
        lot = work.add(self.db, title="fermé trop tôt")
        work.close_merged(self.db, lot["id"], sha=SHA, actor="orch")
        with self.assertRaises(work.WorkError) as ctx:
            work.move(self.db, lot["id"], "build", actor="orch")
        self.assertIn("--correct", str(ctx.exception))          # la marche à suivre
        proc = self.mesh("work", "move", str(lot["id"]), "build", "--correct",
                         "PR d'étape fusionnée trop tôt (#759)",
                         env=self.env(AGENT_MAIL_NAME="orch",
                                      AMEESH_ALERT_ORCHESTRATORS="orch"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        item = work.get(self.db, lot["id"])
        self.assertEqual(item["state"], "build")
        self.assertEqual(work.events(self.db, lot["id"])[0]["note"],
                         "correction: merged → build — PR d'étape fusionnée trop tôt (#759)")
        # le jalon de fusion reste : la fusion a bien eu lieu (l'historique
        # n'est pas réécrit)…
        self.assertEqual(sum(1 for m in work.milestones(self.db, lot["id"])
                             if m["kind"] == "merged"), 1)
        # … mais la correction l'annule à la lecture : il ne date plus le lot
        # tant qu'aucune fusion postérieure n'a écrit son propre jalon.
        self.assertIsNone(work.timeline(self.db, lot["id"])["merged_ts"])
        # et le lot repart dans la machine à états
        self.assertEqual(work.move(self.db, lot["id"], "qa")["state"], "qa")
        # la vraie fusion écrit SON jalon, daté par elle
        self.assertEqual(work.move(self.db, lot["id"], "merged")["state"], "delivered")
        jalons = [m for m in work.milestones(self.db, lot["id"])
                  if m["kind"] == "merged"]
        self.assertEqual(len(jalons), 2)
        vrai = max(jalons, key=lambda m: m["at_ts"])
        self.assertEqual(work.timeline(self.db, lot["id"])["merged_ts"],
                         vrai["at_ts"])

    def test_1693_jalon_annule_reste_mais_toutes_les_lectures_l_ecartent(self):
        # #1693 (N4), variante sans reprise destructive : le jalon de la
        # fusion annulée reste dans `work_item_milestones` (l'historique est
        # préservé) ; ce sont les lectures qui calculent le jalon effectif
        # depuis le journal — affichage, délais, en-tête d'issue et mesures.
        lot = work.add(self.db, title="fusion annulée", actor="orch")
        work.set_branch(self.db, lot["id"], "lot/%d-etape" % lot["id"], actor="orch",
                        source="test")
        self.db.execute("UPDATE work_items SET issue_ref = %s WHERE id = %s",
                        ("acme/web#9", lot["id"]))
        work.close_merged(self.db, lot["id"], sha=SHA, actor="orch")
        self.assertIsNotNone(work.timeline(self.db, lot["id"])["merged_ts"])
        work.move(self.db, lot["id"], "build", actor="orch",
                  correct="PR d'étape fusionnée trop tôt (#759)")
        # l'historique est intact : un seul jalon, celui de la fusion annulée
        jalons = [m for m in work.milestones(self.db, lot["id"])
                  if m["kind"] == "merged"]
        self.assertEqual(len(jalons), 1)
        # aucune lecture ne le retient comme fusion effective
        self.assertIsNone(work.timeline(self.db, lot["id"])["merged_ts"])
        self.assertIsNone(work.delays(self.db, ids=[lot["id"]])[0]["merged_ts"])
        feed = next(r for r in storage.of(self.db).work.issue_feed(200)
                    if int(r["id"]) == int(lot["id"]))
        self.assertIsNone(feed["merged_ts"])
        self.assertFalse(any(int(r["id"]) == int(lot["id"])
                             for r in storage.of(self.db).work.estimate_history(
                                 app=None, limit=200)))
        self.assertFalse(any(int(r["id"]) == int(lot["id"])
                             for r in storage.of(self.db).work.measure_candidates(
                                 ids=[int(lot["id"])], retry_diff_s=3600, since_s=None,
                                 limit=10)))
        # le jalon effectif revient à la vraie fusion, postérieure à la
        # correction ; l'ancien reste dans la table, la lecture prend le dernier
        work.move(self.db, lot["id"], "qa", actor="orch")
        work.move(self.db, lot["id"], "merged", actor="orch")
        jalons = [m for m in work.milestones(self.db, lot["id"])
                  if m["kind"] == "merged"]
        self.assertEqual(len(jalons), 2)
        vrai = max(jalons, key=lambda m: m["at_ts"])
        self.assertEqual(work.timeline(self.db, lot["id"])["merged_ts"],
                         vrai["at_ts"])
        self.assertEqual(work.delays(self.db, ids=[lot["id"]])[0]["merged_ts"],
                         vrai["at_ts"])
        feed = next(r for r in storage.of(self.db).work.issue_feed(200)
                    if int(r["id"]) == int(lot["id"]))
        self.assertEqual(feed["merged_ts"], vrai["at_ts"])
        self.assertTrue(any(int(r["id"]) == int(lot["id"])
                            for r in storage.of(self.db).work.estimate_history(
                                app=None, limit=200)))

    def test_1693_retour_de_blocked_ne_redate_pas_la_fusion(self):
        # R1 dev2 (B1) : `merged → blocked → merged` n'est pas une nouvelle
        # fusion ; sans index unique, le déclencheur ne doit pas écrire un
        # second jalon qui redaterait le lot et perdrait le SHA.
        lot = work.add(self.db, title="aller-retour", actor="orch")
        work.close_merged(self.db, lot["id"], sha=SHA, actor="orch")
        avant = work.timeline(self.db, lot["id"])["merged_ts"]
        work.move(self.db, lot["id"], "blocked", actor="orch")
        work.move(self.db, lot["id"], "merged", actor="orch")
        jalons = [m for m in work.milestones(self.db, lot["id"])
                  if m["kind"] == "merged"]
        self.assertEqual(len(jalons), 1)
        self.assertEqual(jalons[0]["sha"], SHA)
        self.assertEqual(work.timeline(self.db, lot["id"])["merged_ts"], avant)

    def test_2317_une_note_libre_du_meme_prefixe_n_annule_pas_la_fusion(self):
        # #2317 (N2) : la correction se reconnaît à la TRANSITION du journal,
        # pas au texte de sa note. Une note libre qui commence par le même
        # préfixe ne doit donc pas annuler la fusion.
        lot = work.add(self.db, title="note libre", actor="orch")
        work.close_merged(self.db, lot["id"], sha=SHA, actor="orch")
        work.note(self.db, lot["id"], "correction: merged → build — note libre",
                  actor="orch")
        jalons = [m for m in work.milestones(self.db, lot["id"])
                  if m["kind"] == "merged"]
        self.assertEqual(len(jalons), 1)
        self.assertFalse(jalons[0]["annulled"])
        self.assertIsNotNone(work.timeline(self.db, lot["id"])["merged_ts"])
        # une VRAIE correction, elle, annule le jalon (transition merged→build)
        work.move(self.db, lot["id"], "build", actor="orch", correct="vraie correction")
        jalons = [m for m in work.milestones(self.db, lot["id"])
                  if m["kind"] == "merged"]
        self.assertTrue(jalons[0]["annulled"])

    def test_2317_le_jalon_est_date_a_l_insertion_pas_au_debut_de_transaction(self):
        # #2317 (N1) : now() = début de transaction ; un close_merged retenu
        # par le verrou consultatif datait sa fusion avant une correction
        # concurrente. Le jalon est daté par clock_timestamp().
        lot = work.add(self.db, title="horloge", actor="orch")
        with self.db.transaction() as tx:
            debut = tx.query("SELECT extract(epoch from now())::float8 AS ts")[0]["ts"]
            time.sleep(0.05)
            work.close_merged(tx, lot["id"], sha=SHA, actor="orch")
            row = tx.query(
                "SELECT extract(epoch from at)::float8 AS ts FROM work_item_milestones"
                " WHERE work_item_id = %s AND kind = 'merged'", (lot["id"],))[0]
            self.assertGreater(row["ts"], debut)

    def test_2317_correction_apres_fusion_dans_transaction_englobante(self):
        # Both operations may share an outer transaction. Its start precedes
        # the merge milestone; the later correction must still annul it.
        lot = work.add(self.db, title="correction transactionnelle", actor="orch")
        with self.db.transaction() as tx:
            work.close_merged(tx, lot["id"], sha=SHA, actor="orch")
            work.move(tx, lot["id"], "build", actor="orch", correct="trop tôt")
            merged = [m for m in work.milestones(tx, lot["id"])
                      if m["kind"] == "merged"]
            self.assertTrue(merged[0]["annulled"])
            self.assertIsNone(work.timeline(tx, lot["id"])["merged_ts"])
            work.close_merged(tx, lot["id"], sha="f" * 40, actor="orch")
            self.assertEqual(review_lot.merged_sha(tx, lot["id"]), "f" * 40)

    def test_2317_les_lecteurs_de_fusion_ignorent_le_jalon_annule(self):
        # #2317 (N5) : land, review_lot et le registre Process ne retiennent
        # pas le sha d'une fusion annulée.
        lot = work.add(self.db, title="lecteurs", actor="orch")
        work.close_merged(self.db, lot["id"], sha=SHA, actor="orch")
        self.assertEqual(land._merged_sha(self.db, lot["id"]), SHA)
        self.assertEqual(review_lot.merged_sha(self.db, lot["id"]), SHA)
        work.move(self.db, lot["id"], "build", actor="orch", correct="trop tôt")
        self.assertEqual(land._merged_sha(self.db, lot["id"]), "")
        self.assertEqual(review_lot.merged_sha(self.db, lot["id"]), "")
        rows = storage.of(self.db).work.milestones(lot["id"], None)
        self.assertEqual(process_verifiers.merged_of(rows), "")
        # la vraie fusion, elle, redonne son sha
        work.move(self.db, lot["id"], "qa", actor="orch")
        work.close_merged(self.db, lot["id"], sha="f" * 40, actor="orch")
        self.assertEqual(land._merged_sha(self.db, lot["id"]), "f" * 40)
        self.assertEqual(review_lot.merged_sha(self.db, lot["id"]), "f" * 40)

    def test_2317_estimate_history_garde_la_premiere_fin(self):
        # #2317 (N6) : la fin d'un lot reste la PREMIÈRE livraison (fusion,
        # sinon livraison hors cible). Un jalon `delivered` postérieur ne doit
        # pas faire glisser la mesure (le max de #1693 le faisait).
        lot = work.add(self.db, title="mesure", actor="orch")
        work.close_merged(self.db, lot["id"], sha=SHA, actor="orch")
        fusion = work.timeline(self.db, lot["id"])["merged_ts"]
        time.sleep(0.02)
        storage.of(self.db).work.add_milestone(
            lot["id"], "delivered", sha=SHA, actor="orch", verdict=None,
            note="delivered")
        row = next(r for r in storage.of(self.db).work.estimate_history(
            app=None, limit=500) if int(r["id"]) == int(lot["id"]))
        self.assertAlmostEqual(row["merged_ts"], fusion, places=3)

    def test_2317_corrections_bornees_aux_lots_candidats(self):
        # #2317 (N4) : la lecture des corrections est bornée aux lots
        # candidats, pas un LIKE sur tout le journal.
        a = work.add(self.db, title="un", actor="orch")
        b = work.add(self.db, title="deux", actor="orch")
        for lot in (a, b):
            work.close_merged(self.db, lot["id"], sha=SHA, actor="orch")
            work.move(self.db, lot["id"], "build", actor="orch", correct="trop tôt")
        st = storage.of(self.db).work
        self.assertEqual(set(st.corrections([a["id"]])), {int(a["id"])})
        self.assertEqual(set(st.corrections([a["id"], b["id"]])),
                         {int(a["id"]), int(b["id"])})
        self.assertEqual(st.corrections([]), {})

    def test_role_de_conception_au_canon(self):
        class Fiche:
            def __init__(self, data):
                self.data = data

        class Agent:
            def __init__(self, title, roles):
                self.title, self.fiche = title, Fiche({"roles": roles})

        class Canon:
            agents = [Agent("dev1", ["conception"])]

        listing = [{"name": "dev1"}]
        self.assertEqual(sous_utilisation.orchestrators(self.cfg, self.db, listing,
                                                        canons=[Canon()]), [])
        self.assertEqual(sous_utilisation.orchestrators(self.cfg, self.db, listing,
                                                        canons=[Canon()],
                                                        roles=work.CORRECTOR_ROLES), ["dev1"])
        with identite(), mock.patch.object(
                assignments, "is_orchestrator", side_effect=lambda cfg, db, name, roles=None, history=True:
                name == "dev1" and roles == work.CORRECTOR_ROLES and history):
            self.assertEqual(work.corrector(self.cfg, self.db, "dev1"), "dev1")
        with identite("dev1"), mock.patch.object(
                assignments, "is_orchestrator", side_effect=lambda cfg, db, name, roles=None, history=True:
                name == "dev1" and roles == work.CORRECTOR_ROLES and history):
            self.assertEqual(work.corrector(self.cfg, self.db), "dev1")


class ActorTest(_Base):
    """L'acteur des commandes `work` : `--actor`, sinon l'identité liée, sinon « inconnu »."""

    def test_identite_liee_puis_inconnu(self):
        proc = self.mesh("work", "add", "--title", "par l'orchestrateur",
                         env=self.env(AGENT_MAIL_NAME="orch"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lot = work.list_items(self.db)[0]
        self.assertEqual(work.events(self.db, lot["id"])[0]["actor"], "orch")
        proc = self.mesh("work", "note", str(lot["id"]), "une note sans identité")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("unknown actor", proc.stderr)
        self.assertEqual(work.events(self.db, lot["id"])[0]["actor"], work.UNKNOWN_ACTOR)
        proc = self.mesh("work", "move", str(lot["id"]), "build", "--actor", "human:proprio",
                         env=self.env(AGENT_MAIL_NAME="orch"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.events(self.db, lot["id"])[0]["actor"], "human:proprio")
        proc = self.mesh("work", "milestone", str(lot["id"]), "frozen", "--sha", SHA,
                         env=self.env(AGENT_MAIL_NAME="dev1"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.milestones(self.db, lot["id"])[0]["actor"], "dev1")
        proc = self.mesh("work", "close", str(lot["id"]), "--abandoned",
                         env=self.env(AGENT_MAIL_NAME="orch"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("warning", proc.stderr)
        self.assertEqual(work.events(self.db, lot["id"])[0]["actor"], "orch")
        self.assertFalse([e for e in work.events(self.db, lot["id"]) if not e["actor"]])

    def test_resolve_actor(self):
        with identite("claude1"):
            self.assertEqual(work.resolve_actor(self.cfg, self.db), ("claude1", None))
            self.assertEqual(work.resolve_actor(self.cfg, self.db, " x "), ("x", None))
        with identite():
            actor, warning = work.resolve_actor(self.cfg, self.db)
            self.assertEqual(actor, "inconnu")
            self.assertIn("--actor", warning)


class MergeSweepTest(_Base):
    """Le relevé des fusions ferme un lot SANS branche que désigne un commit de fusion."""

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "depot")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        self.commit("socle")
        _git(self.repo, "checkout", "-q", "-b", "develop")
        _git(self.repo, "config", plan_git.TARGET_GIT_KEY, "develop")
        self.agent("claude1", cwd=self.repo)
        for state in (plan_git._pr_body_backoff, plan_git._pr_body_cache):
            state.clear()                           # état du processus
            self.addCleanup(state.clear)

    def commit(self, name):
        with open(os.path.join(self.repo, name), "w") as fh:
            fh.write(name + "\n")
        _git(self.repo, "add", name)
        _git(self.repo, "commit", "-q", "-m", "ajoute %s" % name)

    def merge(self, branch, message, env=None):
        """Une branche de travail fusionnée en local sur develop, sans PR (`--no-ff`)."""
        _git(self.repo, "checkout", "-q", "-b", branch, "develop")
        self.commit(branch.replace("/", "-"))
        _git(self.repo, "checkout", "-q", "develop")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", message, branch, env=env)
        return _git(self.repo, "rev-parse", "HEAD")

    def lot(self, title="lot sans branche"):
        return work.add(self.db, title=title, assignee="claude1", actor="orch",
                        force="test: several lots for one agent (#540)")

    def sync(self, **kw):
        return plan_git.sync_branches(self.db, host=self.cfg.host, **kw)

    def test_ligne_ameesh_work_dans_le_commit_de_fusion(self):
        lot = self.lot()
        autre = self.lot("autre lot")
        report = {r["work_item"]: r for r in self.sync()["results"]}
        self.assertEqual(report[lot["id"]]["result"], "open")
        self.assertIn("ameesh-work: %d" % lot["id"], report[lot["id"]]["detail"])
        sha = self.merge("feature/x", "merge(desktop): DESKTOP-X\n\nameesh-work: %d" % lot["id"])
        self.assertEqual({r["work_item"]: r["result"] for r in self.sync(dry_run=True)["results"]},
                         {lot["id"]: "would-merge", autre["id"]: "open"})
        report = self.sync()
        self.assertEqual(report["merged"], 1)
        item = work.get(self.db, lot["id"])
        self.assertEqual(item["state"], "delivered")
        jalon = self.merged_milestone(lot["id"])
        self.assertEqual(jalon["sha"], sha)
        self.assertIn("merge sweep, merge commit of develop that carries "
                      "\"ameesh-work: %d\"" % lot["id"], jalon["note"])
        self.assertEqual(work.get(self.db, autre["id"])["state"], "intake")
        self.assertEqual([r["work_item"] for r in self.sync()["results"]], [autre["id"]])

    def test_diese_seulement_si_le_depot_l_active(self):
        lot = self.lot()
        sha = self.merge("feature/y", "Merge #%d COMPUTE-IDLE" % lot["id"])
        self.sync()
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")  # n° de PR ailleurs
        _git(self.repo, "config", plan_git.LOT_REF_GIT_KEY, "hash")
        report = self.sync()
        self.assertEqual((report["merged"], report["results"][0]["how"]), (1, "#id"))
        self.assertEqual(self.merged_milestone(lot["id"])["sha"], sha)
        self.assertIn("cites #%d in its title (ameesh.lotRef hash)" % lot["id"],
                      self.merged_milestone(lot["id"])["note"])

    def test_formes_voisines_et_corps_ignores(self):
        _git(self.repo, "config", plan_git.LOT_REF_GIT_KEY, "hash")
        lot = self.lot()
        n = lot["id"]
        self.merge("feature/a", "Merge PR#%d" % n)
        self.merge("feature/b", "Merge depot#%d et ##%d" % (n, n))
        self.merge("feature/c", "Merge feature/c\n\n* suite de #%d" % n)
        self.sync()
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")

    def test_diese_nie_dans_un_titre_ne_ferme_pas_le_lot(self):
        # #2259, cas Nexlink #598 (commit 84c68e9d4) : un montage de paquet
        # cite « sans #598 » dans son titre ; la branche du lot reste en
        # avance, sa tête n'est pas dans la cible, le lot reste ouvert.
        _git(self.repo, "config", plan_git.LOT_REF_GIT_KEY, "hash")
        lot = self.lot()
        n = lot["id"]
        branch = "lot/%d-ouvert" % n
        _git(self.repo, "checkout", "-q", "-b", branch, "develop")
        self.commit("travail-du-lot")
        _git(self.repo, "checkout", "-q", "develop")
        work.set_branch(self.db, n, branch, "develop", actor="orch")
        self.merge("feature/paquet-52-v2",
                   "paquet 52 v2 : montage #2221 sur develop — recomposition sans "
                   "#%d ni correctif .github (décision pilote (c), courrier #97932) "
                   "[skip ci]" % n)
        report = {r["work_item"]: r for r in self.sync()["results"]}
        self.assertEqual(work.get(self.db, n)["state"], "intake")
        self.assertEqual(report[n]["result"], "open")
        self.assertFalse([m for m in work.milestones(self.db, n) if m["kind"] == "merged"])

    def test_trailer_d_un_autre_lot_l_emporte_sur_le_titre(self):
        # #5110, cas Nexlink 281c5be4c3d2 (paquet 278) : le titre
        # « #4820 B8 (#659) » cite #659 en contexte, le trailer désigne 4820 ;
        # seul 4820 est livré, #659 reste ouvert.
        _git(self.repo, "config", plan_git.LOT_REF_GIT_KEY, "hash")
        livre, cite = self.lot(), self.lot()
        self.merge("feature/paquet-278",
                   "Paquet 278 : #%d B8 (#%d) : useThread.ts sans garde "
                   "contentHintsEnabled\n\nameesh-work: %d"
                   % (livre["id"], cite["id"], livre["id"]))
        self.sync()
        self.assertEqual(work.get(self.db, livre["id"])["state"], "delivered")
        self.assertEqual(work.get(self.db, cite["id"])["state"], "intake")
        self.assertFalse([m for m in work.milestones(self.db, cite["id"])
                          if m["kind"] == "merged"])

    def test_cited_lots_designation_explicite_fait_foi(self):
        # #5110 : tout trailer, ou la fusion d'une branche de lot, coupe les
        # `#<id>` du titre ; sans désignation explicite, le titre cite encore.
        cited = plan_git.cited_lots
        self.assertEqual(cited("Paquet 278 : #4820 B8 (#659)\n\nameesh-work: 4820",
                               hash_refs=True), {4820: "ameesh-work"})
        self.assertEqual(cited("#12 et #13\n\nameesh-work-partial: 12",
                               hash_refs=True), {})
        self.assertEqual(cited("Merge pull request #8 from manaty/lot/7-x", hash_refs=True),
                         {7: "lot-branch"})
        self.assertEqual(cited("Paquet 278 : #4820 B8 (#659)", hash_refs=True),
                         {4820: "#id", 659: "#id"})

    def test_cible_du_projet_plutot_que_celle_du_clone(self):
        # #5110 : sans `ameesh.target` dans le clone (cible par défaut main),
        # la cible déclarée du projet du lot (`develop` pour Nexlink) fait foi :
        # la fusion sur develop ferme sans « off the default target », une
        # fusion sur main n'est qu'un résidu.
        from ameesh import intake_neighbors
        _git(self.repo, "config", "--unset", plan_git.TARGET_GIT_KEY)
        lot = work.add(self.db, title="lot nexlink", app="nexlink", assignee="claude1",
                       actor="orch", force="test: several lots for one agent (#540)")
        ailleurs = work.add(self.db, title="lot nexlink sur main", app="nexlink",
                            assignee="claude1", actor="orch",
                            force="test: several lots for one agent (#540)")
        self.merge("feature/n", "merge n\n\nameesh-work: %d" % lot["id"])
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "merge", "-q", "--no-ff", "-m",
             "merge m\n\nameesh-work: %d" % ailleurs["id"], "feature/n")
        _git(self.repo, "checkout", "-q", "develop")
        targets = {"nexlink": "develop"}
        with mock.patch.object(intake_neighbors, "project_target",
                               side_effect=lambda cfg, app, slug="": targets.get(app, "")):
            report = {r["work_item"]: r for r in self.sync()["results"]}
        self.assertEqual(report[lot["id"]]["result"], "merged")
        self.assertEqual(report[lot["id"]]["target"], "develop")
        self.assertIsNone(report[lot["id"]].get("off_target"))
        self.assertNotIn("off the default target", self.merged_milestone(lot["id"])["note"])
        self.assertEqual(work.get(self.db, ailleurs["id"])["state"], "intake")

    def test_diese_parenthese_nie_ne_ferme_pas_le_lot(self):
        # R Codex #2259 : la parenthèse ne sépare pas « sans » du lot exclu.
        _git(self.repo, "config", plan_git.LOT_REF_GIT_KEY, "hash")
        lot = self.lot()
        n = lot["id"]
        self.merge("feature/paquet-sans-parenthese", "Merge sans (#%d)" % n)
        self.sync()
        self.assertEqual(work.get(self.db, n)["state"], "intake")
        self.assertFalse([m for m in work.milestones(self.db, n) if m["kind"] == "merged"])

    def test_lot_livre_avant_sans_reste_cite(self):
        # Précision impl-08 : « #N livré sans (#M) » livre N, jamais M.
        _git(self.repo, "config", plan_git.LOT_REF_GIT_KEY, "hash")
        livre, exclu = self.lot(), self.lot()
        self.merge("feature/paquet-positif-avant-sans", "paquet : #%d livré sans (#%d)"
                   % (livre["id"], exclu["id"]))
        self.sync()
        self.assertEqual(work.get(self.db, livre["id"])["state"], "delivered")
        self.assertEqual(work.get(self.db, exclu["id"])["state"], "intake")

    def test_diese_retire_apres_le_numero_ne_ferme_pas_le_lot(self):
        # #2259 : « Merge #N retiré » cite un retrait, pas une livraison.
        # Restreindre la négation aux numéros d'après le mot fermait encore
        # le lot (régression du 17/10) ; le participe nie aussi ce qui précède.
        _git(self.repo, "config", plan_git.LOT_REF_GIT_KEY, "hash")
        lot = self.lot()
        n = lot["id"]
        self.merge("feature/retrait", "Merge #%d retiré" % n)
        self.sync()
        self.assertEqual(work.get(self.db, n)["state"], "intake")
        self.assertFalse([m for m in work.milestones(self.db, n) if m["kind"] == "merged"])

    # --- L118b : la fusion d'une branche lot/<id>-… ferme le lot -----------

    def test_fusion_github_d_une_branche_lot_ferme_le_lot(self):
        lot = self.lot()
        n = lot["id"]
        sha = self.merge("lot/%d-garde" % n,
                         "Merge pull request #7 from manaty/lot/%d-garde\n\nL1: garde" % n)
        _git(self.repo, "branch", "-q", "-D", "lot/%d-garde" % n)   # supprimée après la PR
        lus = []
        report = self.sync(pr_body=lambda repo, num: lus.append(num) or "Lot #%d." % n)
        self.assertEqual((report["merged"], report["results"][0]["how"]), (1, "lot-branch"))
        self.assertEqual(lus, [7])
        self.assertEqual(self.merged_milestone(n)["sha"], sha)
        self.assertIn("merges the branch lot/%d-…" % n, self.merged_milestone(n)["note"])

    def test_pr_partielle_le_lot_reste_ouvert_jusqu_a_la_derniere(self):
        lot = self.lot("parapluie")
        n = lot["id"]
        corps = {7: "Part one.\n\nameesh-work-partial: %d" % n, 8: "The rest."}
        self.merge("lot/%d-un" % n, "Merge pull request #7 from manaty/lot/%d-un" % n)
        report = self.sync(pr_body=lambda repo, num: corps[num])
        self.assertEqual(report["results"][0]["how"], "partial")
        self.assertEqual(work.get(self.db, n)["state"], "intake")
        sha = self.merge("lot/%d-deux" % n, "Merge pull request #8 from manaty/lot/%d-deux" % n)
        report = self.sync(pr_body=lambda repo, num: corps[num])
        self.assertEqual(report["merged"], 1)
        self.assertEqual(self.merged_milestone(n)["sha"], sha)

    def test_corps_illisible_aucune_fermeture(self):
        lot = self.lot()
        n = lot["id"]
        self.merge("lot/%d-x" % n, "Merge pull request #9 from manaty/lot/%d-x" % n)

        def illisible(repo, num):
            raise plan_git.GitError("gh: not logged in")

        report = self.sync(pr_body=illisible)
        self.assertEqual(report["results"][0]["result"], "open")
        self.assertIn("unreadable", report["results"][0]["detail"])
        self.assertEqual(work.get(self.db, n)["state"], "intake")

    def test_fusion_locale_d_une_branche_lot(self):
        lot = self.lot()
        n = lot["id"]

        def jamais(repo, num):
            raise AssertionError("no PR to read")

        self.merge("lot/%d-local" % n, "Merge branch 'lot/%d-local' into develop" % n)
        self.assertEqual(self.sync(pr_body=jamais)["merged"], 1)

    # --- #2260 : une citation `#<id>` exige une preuve de livraison ---------

    def test_diese_dementie_par_la_branche_en_avance(self):
        # cas Nexlink #598 cité positivement : la PR du lot reste ouverte, sa
        # tête n'est pas dans la cible ; le doute se trace une fois
        _git(self.repo, "config", plan_git.LOT_REF_GIT_KEY, "hash")
        n = self.lot()["id"]
        branch = "lot/%d-ouvert" % n
        _git(self.repo, "checkout", "-q", "-b", branch, "develop")
        self.commit("travail-du-lot")
        _git(self.repo, "checkout", "-q", "develop")
        work.set_branch(self.db, n, branch, "develop", actor="orch")
        sha = self.merge("feature/paquet-53", "paquet 53 : montage #%d" % n)
        tip = _git(self.repo, "rev-parse", branch)[:12]
        report = {r["work_item"]: r for r in self.sync()["results"]}
        self.assertEqual((report[n]["result"], report[n]["how"], report[n]["ref"]),
                         ("open", "unproven", sha))
        self.assertIn("the tip %s of %s is not in develop" % (tip, branch),
                      report[n]["detail"])
        # au relevé suivant, la pointe est devenue la dernière tête vue
        report = {r["work_item"]: r for r in self.sync()["results"]}
        self.assertEqual(work.get(self.db, n)["state"], "intake")
        self.assertIn("the last head seen %s is not in develop" % tip, report[n]["detail"])
        notes = [e["note"] for e in work.events(self.db, n, 40)
                 if "a mention is not a proof of delivery" in (e.get("note") or "")]
        self.assertEqual(len(notes), 1, notes)

    def test_diese_dementie_par_le_gel_hors_cible(self):
        _git(self.repo, "config", plan_git.LOT_REF_GIT_KEY, "hash")
        n = self.lot()["id"]
        _git(self.repo, "checkout", "-q", "-b", "agent/gel", "develop")
        self.commit("gel")
        gel = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "-q", "develop")
        _git(self.repo, "branch", "-q", "-D", "agent/gel")
        work.milestone(self.db, n, "frozen", sha=gel, actor="ouvrier")
        self.merge("feature/paquet-54", "paquet 54 : #%d" % n)
        report = {r["work_item"]: r for r in self.sync()["results"]}
        self.assertEqual(work.get(self.db, n)["state"], "qa")
        self.assertEqual(report[n]["how"], "unproven")
        self.assertIn("the last freeze %s is not in develop" % gel[:12], report[n]["detail"])

    def test_diese_prouvee_par_le_gel_ancetre(self):
        _git(self.repo, "config", plan_git.LOT_REF_GIT_KEY, "hash")
        n = self.lot()["id"]
        _git(self.repo, "checkout", "-q", "-b", "agent/livre", "develop")
        self.commit("livre")
        gel = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "-q", "develop")
        work.milestone(self.db, n, "frozen", sha=gel, actor="ouvrier")
        sha = _git(self.repo, "merge", "-q", "--no-ff", "-m", "paquet 55 : #%d" % n,
                   "agent/livre") or _git(self.repo, "rev-parse", "HEAD")
        report = self.sync()
        self.assertEqual((report["merged"], report["results"][0]["how"]), (1, "#id"))
        self.assertEqual(self.merged_milestone(n)["sha"], sha)

    def test_diese_ancienne_tete_livree_ne_prouve_pas_le_nouveau_gel(self):
        _git(self.repo, "config", plan_git.LOT_REF_GIT_KEY, "hash")
        n = self.lot()["id"]
        ancien = self.merge("feature/ancien", "ancienne livraison")
        branch = "agent/nouveau-gel"
        _git(self.repo, "checkout", "-q", "-b", branch, "develop")
        self.commit("nouveau-gel")
        nouveau = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "-q", "develop")
        work.set_branch(self.db, n, branch, "develop", actor="orch")
        storage.of(self.db).work.set_branch_head(n, branch, ancien)
        work.milestone(self.db, n, "frozen", sha=nouveau, actor="ouvrier")
        self.merge("feature/citation", "paquet : #%d" % n)
        entry = self.sync()["results"][0]
        self.assertEqual((entry["result"], entry["how"]), ("open", "unproven"))
        self.assertEqual(work.get(self.db, n)["state"], "qa")

    def test_diese_regel_concurrent_ne_ferme_pas(self):
        _git(self.repo, "config", plan_git.LOT_REF_GIT_KEY, "hash")
        n = self.lot()["id"]
        # #2786 (5) : le gel est posé AVANT la fusion ; un gel sur un commit
        # déjà dans la cible (aucun commit propre) est refusé à l'écriture.
        _git(self.repo, "checkout", "-q", "-b", "agent/livre", "develop")
        self.commit("livre")
        livre = _git(self.repo, "rev-parse", "HEAD")
        work.milestone(self.db, n, "frozen", sha=livre, actor="ouvrier")
        _git(self.repo, "checkout", "-q", "develop")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "paquet 55 : #%d" % n,
             "agent/livre")
        _git(self.repo, "checkout", "-q", "-b", "agent/regel", "develop")
        self.commit("regel")
        nouveau = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "-q", "develop")
        close = work.close_merged

        def regeler(db, lot, **kw):
            work.milestone(db, lot, "frozen", sha=nouveau, actor="ouvrier")
            return close(db, lot, **kw)

        with mock.patch.object(work, "close_merged", side_effect=regeler):
            entry = self.sync()["results"][0]
        self.assertEqual(entry["result"], "refrozen")
        self.assertEqual(work.get(self.db, n)["state"], "qa")

    def test_branche_implicite_en_avance_rapide(self):
        lot = self.lot()
        n = lot["id"]
        _git(self.repo, "checkout", "-q", "-b", "lot/%d-ff" % n, "develop")
        self.commit("ff")
        _git(self.repo, "checkout", "-q", "develop")
        report = self.sync()                       # la pointe en avance est retenue
        self.assertEqual(report["results"][0]["branch"], "lot/%d-ff" % n)
        self.assertEqual(work.get(self.db, n)["state"], "intake")
        _git(self.repo, "merge", "-q", "--ff-only", "lot/%d-ff" % n)
        self.assertEqual(work.get(self.db, n)["branch"], "lot/%d-ff" % n)
        self.assertEqual(self.sync()["merged"], 1)

    def test_formes_lot_branch(self):
        cite = plan_git.cited_lots
        self.assertEqual(cite("Merge pull request #3 from o/lot/42-x"), {42: "lot-branch"})
        self.assertEqual(cite("Merge pull request #3 from o/lot/42"), {42: "lot-branch"})
        self.assertEqual(cite("Merge branch 'lot/42-x'"), {42: "lot-branch"})
        self.assertEqual(cite("Merge remote-tracking branch 'origin/main' into lot/42-x"), {})
        self.assertEqual(cite("Merge pull request #3 from o/lot/42x"), {})
        self.assertEqual(cite("Merge pull request #3 from o/agent/lot/42-x"), {})
        self.assertEqual(cite("Merge branch 'lot/42-x'\n\nameesh-work-partial: 42"), {})
        self.assertEqual(plan_git.merge_pr_number("Merge pull request #3 from o/lot/42-x"), 3)
        self.assertIsNone(plan_git.merge_pr_number("Merge branch 'lot/42-x'"))

    # --- #312 r1 --------------------------------------------------------

    def test_r1_branche_enregistree_seule_compte(self):
        # P1 : le lot porte agent/courante, non fusionnée ; la fusion d'une
        # autre lot/<id>-old ne le ferme pas
        lot = self.lot()
        n = lot["id"]
        work.set_branch(self.db, n, "agent/courante", actor="orch", source="test")
        _git(self.repo, "checkout", "-q", "-b", "agent/courante", "develop")
        self.commit("courante")
        _git(self.repo, "checkout", "-q", "develop")
        self.merge("lot/%d-old" % n, "Merge branch 'lot/%d-old'" % n)
        report = self.sync(pr_body=lambda repo, num: "")
        self.assertEqual(report["results"][0]["result"], "open")
        self.assertEqual(work.get(self.db, n)["state"], "intake")
        # la branche enregistrée elle-même, fusionnée par son titre : fermé
        lot2 = self.lot("deux")
        work.set_branch(self.db, lot2["id"], "origin/lot/%d-bon" % lot2["id"], actor="orch",
                        source="test")
        self.merge("lot/%d-bon" % lot2["id"],
                   "Merge pull request #4 from manaty/lot/%d-bon" % lot2["id"])
        self.sync(pr_body=lambda repo, num: "")
        self.assertEqual(work.get(self.db, lot2["id"])["state"], "delivered")

    def test_r1_fusion_locale_partielle_retient_la_branche_implicite(self):
        # P2 : `ameesh-work-partial` dans le message d'une fusion locale
        # retient aussi la preuve par la branche implicite
        lot = self.lot("parapluie")
        n = lot["id"]
        self.merge("lot/%d-un" % n, "Merge branch 'lot/%d-un'\n\nameesh-work-partial: %d"
                   % (n, n))
        report = self.sync()
        self.assertEqual(report["results"][0]["how"], "partial")
        self.assertEqual(work.get(self.db, n)["state"], "intake")

    def test_r1_corps_illisible_puis_pr_complete_lisible(self):
        # P2 : le corps toujours illisible de #7 ne cache pas #8, complète
        lot = self.lot()
        n = lot["id"]
        self.merge("lot/%d-a" % n, "Merge pull request #7 from manaty/lot/%d-a" % n)
        sha = self.merge("lot/%d-b" % n, "Merge pull request #8 from manaty/lot/%d-b" % n)
        lus = []

        def lire(repo, num):
            lus.append(num)
            if num == 7:
                raise plan_git.GitError("gh: HTTP 404")
            return "The rest."

        report = self.sync(pr_body=lire)          # un échec de gh par relevé
        self.assertIn("unreadable", report["results"][0]["detail"])
        self.assertEqual(work.get(self.db, n)["state"], "intake")
        self.assertEqual(self.sync(pr_body=lire)["merged"], 1)   # #7 recule : #8 lu
        self.assertEqual(lus, [7, 8])
        self.assertEqual(self.merged_milestone(n)["sha"], sha)

    def test_r1_recul_et_plafond_des_lectures_de_gh(self):
        # P2 : au plus un échec de gh par dépôt et par relevé ; une PR en échec
        # recule (délai doublé à chaque échec) ; au plus MAX_PR_BODY_READS
        # lectures par relevé
        lots = [self.lot("l%d" % i) for i in range(3)]
        for i, lot in enumerate(lots):
            self.merge("lot/%d-x" % lot["id"],
                       "Merge pull request #%d from manaty/lot/%d-x" % (10 + i, lot["id"]))
        appels = []

        def en_panne(repo, num):
            appels.append(num)
            raise plan_git.GitError("gh: timeout")

        for _ in range(4):
            self.sync(pr_body=en_panne)
        self.assertEqual(appels, [10, 11, 12])     # rien pendant les reculs
        key = (self.repo, 10)
        self.assertEqual(plan_git._pr_body_backoff[key][1], plan_git.PR_BODY_BACKOFF_S)
        plan_git._pr_body_backoff[key] = (0.0, plan_git._pr_body_backoff[key][1])
        self.sync(pr_body=en_panne)
        self.assertEqual(appels, [10, 11, 12, 10])
        self.assertEqual(plan_git._pr_body_backoff[key][1], 2 * plan_git.PR_BODY_BACKOFF_S)
        # plafond : une lecture par relevé ; une lecture réussie oublie le recul
        plan_git._pr_body_backoff.clear()
        lus = []
        with mock.patch.object(plan_git, "MAX_PR_BODY_READS", 1):
            report = self.sync(pr_body=lambda repo, num: lus.append(num) or "")
        self.assertEqual((len(lus), report["merged"]), (1, 1))
        self.assertTrue(any("bodies read in this sweep" in r["detail"]
                            for r in report["results"]))

    def test_r2_corps_lus_gardes_pas_de_famine(self):
        # P2 : un corps déjà lu ne reprend pas de lecture : le lot suivant
        # est lu au relevé suivant, même avec un plafond d'une lecture
        a, b = self.lot("parapluie"), self.lot("complet")
        self.merge("lot/%d-x" % a["id"], "Merge pull request #30 from manaty/lot/%d-x" % a["id"])
        self.merge("lot/%d-y" % b["id"], "Merge pull request #31 from manaty/lot/%d-y" % b["id"])
        corps = {30: "ameesh-work-partial: %d" % a["id"], 31: "Done."}
        lus = []

        def lire(repo, num):
            lus.append(num)
            return corps[num]

        with mock.patch.object(plan_git, "MAX_PR_BODY_READS", 1):
            self.sync(pr_body=lire)
            self.sync(pr_body=lire)
            self.sync(pr_body=lire)
        self.assertEqual(lus, [30, 31])
        self.assertEqual(work.get(self.db, b["id"])["state"], "delivered")
        self.assertEqual(work.get(self.db, a["id"])["state"], "intake")

    def test_r2_seule_la_derniere_fusion_partielle_compte(self):
        # P2 : fusion locale partielle, puis la suite de la même branche
        # entre dans la cible en avance rapide : le lot est fermé
        lot = self.lot("parapluie")
        n = lot["id"]
        branch = "lot/%d-un" % n
        self.merge(branch, "Merge branch '%s'\n\nameesh-work-partial: %d" % (branch, n))
        self.assertEqual(self.sync()["results"][0]["how"], "partial")
        _git(self.repo, "checkout", "-q", branch)
        _git(self.repo, "merge", "-q", "--ff-only", "develop")
        self.commit("la-suite")
        _git(self.repo, "checkout", "-q", "develop")
        self.assertEqual(self.sync()["results"][0]["how"], "partial")   # vue en avance
        _git(self.repo, "merge", "-q", "--ff-only", branch)
        self.assertEqual(self.sync()["merged"], 1)

    def pointe_puis_fusion_partielle(self, *, antidate=None):
        """S vu en avance, la branche ramenée à P (poussée forcée) et fusionnée
        partiellement (M, sa date de committer `antidate`), puis S fusionné
        dans la cible par une autre branche, et la branche du lot avancée
        jusqu'à la cible. Le numéro du lot."""
        n = self.lot("parapluie")["id"]
        self.db.execute("UPDATE work_items SET created_at = now() - interval '1 hour'"
                        " WHERE id = %s", (n,))
        branch = "lot/%d-un" % n
        _git(self.repo, "checkout", "-q", "-b", branch, "develop")
        self.commit("p")
        p = _git(self.repo, "rev-parse", "HEAD")
        self.commit("s")
        _git(self.repo, "checkout", "-q", "develop")
        self.sync()                                               # S vu en avance
        s = work.get(self.db, n)["branch_head"]
        _git(self.repo, "branch", "-q", "-f", "autre", s)
        _git(self.repo, "branch", "-q", "-f", branch, p)          # poussée forcée à P
        _git(self.repo, "merge", "-q", "--no-ff", "-m",
             "Merge branch '%s'\n\nameesh-work-partial: %d" % (branch, n), branch,
             env={"GIT_COMMITTER_DATE": antidate} if antidate else None)
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "Merge branch 'autre'", "autre")
        _git(self.repo, "branch", "-q", "-f", branch, "develop")  # avance rapide
        return n

    def test_l118d_r1_pointe_vue_avant_la_fusion_partielle(self):
        # #346 r1 P1 : la pointe S, vue avant la fusion partielle, ne la lève pas
        n = self.pointe_puis_fusion_partielle()
        for _ in range(2):
            report = self.sync()
            self.assertEqual((report["merged"], report["results"][0]["how"]), (0, "partial"))
        self.assertEqual(work.get(self.db, n)["state"], "intake")

    def test_l118d_r2_fusion_partielle_antidatee(self):
        # #346 r2 P1 : la date de committer de M, antidatée de 10 minutes
        # (au-delà de CLOCK_SLACK_S), ne compte pas : seul l'instant où le
        # relevé a vu M, horloge de la base, compte
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 600))
        n = self.pointe_puis_fusion_partielle(antidate=stamp)
        for _ in range(2):                   # M vue, puis relevé suivant
            report = self.sync()
            self.assertEqual((report["merged"], report["results"][0]["how"]), (0, "partial"))
        row = work.get(self.db, n)
        self.assertLess(row["branch_head_ts"], row["partial_seen_ts"])

    def test_l118d_r2_pointe_inchangee_relevee_apres_la_fusion(self):
        # #346 r2 (suite) : S vu en avance, P seul fusionné partiellement, la
        # branche toujours à S : S est relevée de nouveau après M, puis entre
        # dans la cible en avance rapide : le lot est fermé
        n = self.lot("parapluie")["id"]
        branch = "lot/%d-un" % n
        _git(self.repo, "checkout", "-q", "-b", branch, "develop")
        self.commit("p")
        p = _git(self.repo, "rev-parse", "HEAD")
        self.commit("s")
        _git(self.repo, "checkout", "-q", "develop")
        self.sync()                                               # S vu en avance
        _git(self.repo, "merge", "-q", "--no-ff", "-m",
             "Merge branch '%s'\n\nameesh-work-partial: %d" % (branch, n), p)
        self.assertEqual(self.sync()["results"][0]["how"], "partial")   # S relevée après M
        row = work.get(self.db, n)
        self.assertGreater(row["branch_head_ts"], row["partial_seen_ts"])
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "Merge branch 'autre'", branch)
        self.assertEqual(self.sync()["merged"], 1)

    def test_403_dry_run_deuxieme_fusion_partielle(self):
        # #403 : M1 partielle relevée, S vu en avance après M1, puis une
        # deuxième fusion partielle M2 (de P seul) et S entré dans la cible
        # par une autre branche : le dry-run juge comme le vrai relevé, M2
        # pas encore relevée ne se compare pas à l'instant de M1
        n = self.lot("parapluie")["id"]
        self.db.execute("UPDATE work_items SET created_at = now() - interval '1 hour'"
                        " WHERE id = %s", (n,))
        branch = "lot/%d-un" % n
        _git(self.repo, "checkout", "-q", "-b", branch, "develop")
        self.commit("p0")
        _git(self.repo, "checkout", "-q", "develop")
        partielle = "Merge branch '%s'\n\nameesh-work-partial: %d" % (branch, n)
        _git(self.repo, "merge", "-q", "--no-ff", "-m", partielle, branch)
        self.assertEqual(self.sync()["results"][0]["how"], "partial")   # M1 relevée
        m1 = work.get(self.db, n)["partial_merge"]
        _git(self.repo, "checkout", "-q", branch)
        self.commit("p")
        p = _git(self.repo, "rev-parse", "HEAD")
        self.commit("s")
        _git(self.repo, "checkout", "-q", "develop")
        self.sync()                                               # S vu en avance après M1
        s = work.get(self.db, n)["branch_head"]
        _git(self.repo, "branch", "-q", "-f", "autre", s)
        _git(self.repo, "branch", "-q", "-f", branch, p)          # poussée forcée à P
        _git(self.repo, "merge", "-q", "--no-ff", "-m", partielle, branch)   # M2
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "Merge branch 'autre'", "autre")
        _git(self.repo, "branch", "-q", "-f", branch, "develop")  # avance rapide
        report = self.sync(dry_run=True)
        self.assertEqual((report["results"][0]["result"], report["results"][0]["how"]),
                         ("open", "partial"))
        self.assertEqual(work.get(self.db, n)["partial_merge"], m1)   # rien d'écrit
        report = self.sync()
        self.assertEqual((report["merged"], report["results"][0]["how"]), (0, "partial"))
        self.assertEqual(work.get(self.db, n)["state"], "intake")

    def test_l118d_r1_erreur_git_garde_le_lot_ouvert(self):
        # #346 r1 P2 : une ascendance illisible (code 128) n'est pas une preuve
        merge = {"sha": "m" * 40, "ts": 0.0, "parents": ["a" * 40, "b" * 40]}
        codes = iter([128, 128, 0])
        with mock.patch.object(plan_git, "_git", side_effect=lambda *a: subprocess.
                               CompletedProcess(a, next(codes))):
            self.assertFalse(plan_git._moved_past(self.repo, "s" * 40, 10_000.0, "s" * 40,
                                                  merge, 0.0))

    def test_l118d_avance_jusqu_a_la_cible_sans_travail_nouveau(self):
        # #312 r3 : après une fusion partielle, la branche est avancée jusqu'à
        # develop sans travail nouveau (et develop a avancé entre-temps) : le
        # lot reste ouvert
        lot = self.lot("parapluie")
        n = lot["id"]
        branch = "lot/%d-un" % n
        self.merge(branch, "Merge branch '%s'\n\nameesh-work-partial: %d" % (branch, n))
        self.assertEqual(self.sync()["results"][0]["how"], "partial")
        _git(self.repo, "checkout", "-q", branch)
        _git(self.repo, "merge", "-q", "--ff-only", "develop")      # la fusion elle-même
        _git(self.repo, "checkout", "-q", "develop")
        report = self.sync()
        self.assertEqual((report["merged"], report["results"][0]["how"]), (0, "partial"))
        self.commit("travail-d-un-autre")                         # develop avance
        _git(self.repo, "checkout", "-q", branch)
        _git(self.repo, "merge", "-q", "--ff-only", "develop")
        _git(self.repo, "checkout", "-q", "develop")
        report = self.sync()
        self.assertEqual((report["merged"], report["results"][0]["how"]), (0, "partial"))
        self.assertEqual(work.get(self.db, n)["state"], "intake")

    def test_cited_lots(self):
        cas = {
            "merge(desktop): DESKTOP-MAC-UNIVERSAL (#91) …": {91: "#id"},
            "Merge #93 COMPUTE-IDLE": {93: "#id"},
            "Merge pull request #45 from org/branche": {45: "#id"},
            "Merge #7.": {7: "#id"},
            "Merge PR#9, depot#9, ##9, #9a, &#93;": {},
            # #2259 : une mention niée ne désigne pas le lot. Le titre du
            # commit 84c68e9d4 (Nexlink, paquet 52 v2) fermait #598 alors que
            # sa PR restait ouverte et sa tête hors de develop. Le courrier
            # cité dans la parenthèse du retrait ne prouve pas une livraison.
            "paquet 52 v2 : montage #2221 (cd8b0483a) sur develop e5b4f300a — "
            "recomposition sans #598 ni correctif .github (décision pilote (c), "
            "courrier #97932) [skip ci]": {2221: "#id"},
            "Merge sans #12": {},
            "Merge retiré #12": {},
            "Merge hors #12": {},
            "Merge without #12": {},
            "Revert #12": {},
            "Merge sans #12 / #13 livré": {13: "#id"},
            "Merge sans #12, puis #12 livré": {12: "#id"},
            "Merge sans (#12)": {},
            "Merge without (#12, (#13)), #14 livré": {14: "#id"},
            "Merge #14 (sans #12)": {14: "#id"},
            "paquet : #2221 sans #598": {2221: "#id"},
            "#598 livré sans #601": {598: "#id"},
            "Merge #14 sans (#12 ni (#13))": {14: "#id"},
            "Merge sans #12 ni #13": {},
            "Merge exclues #12": {},
            "Merge #12 retiré": {},
            "Merge (#12) retiré": {},
            "Merge #12 reverted": {},
            "Merge #12 sans #13": {12: "#id"},
            # #2262 : un groupe ouvert sur un contraste est une proposition
            # positive ; la négation du parent ne l'atteint pas.
            "Merge sans #12 (mais #13 livré)": {13: "#id"},
            "Merge sans (#12) (mais #13 livré)": {13: "#id"},
            "Merge sans #12 (mais #13 livré sans #14)": {13: "#id"},
            "Merge #12 retiré (mais #13 livré)": {13: "#id"},
            # Un retrait après le groupe porte encore sur ce groupe.
            "Merge (mais #13) retiré": {},
            "Merge #12 (mais #13 aussi) reverted": {},
            "Merge (mais #13) retiré (mais #14 livré)": {14: "#id"},
            "Merge #12 without #13": {12: "#id"},
            "Merge #12 hors #13": {12: "#id"},
            "paquet : tout sauf #12": {},
            "Merge #12 except #13": {12: "#id"},
            "Merge x\n\nameesh-work: #12\n  ameesh-work: 13": {12: "ameesh-work",
                                                               13: "ameesh-work"},
            "Merge x\n\nvoir ameesh-work: 14 plus tard": {},
        }
        for message, attendu in cas.items():
            self.assertEqual(plan_git.cited_lots(message, hash_refs=True), attendu, message)
        self.assertEqual(plan_git.cited_lots("Merge #93 COMPUTE-IDLE"), {})
        self.assertEqual(plan_git.cited_lots("Merge x\n\nameesh-work: 5"), {5: "ameesh-work"})

    def test_fusion_anterieure_au_lot_ignoree(self):
        lot = self.lot()
        self.merge("feature/old", "Merge\n\nameesh-work: %d" % lot["id"],
                   env={"GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z"})
        self.sync()
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")

    def test_cible_configuree_introuvable(self):
        lot = self.lot()
        _git(self.repo, "config", plan_git.TARGET_GIT_KEY, "origin/develop")
        entry = self.sync()["results"][0]
        self.assertEqual((entry["work_item"], entry["result"]), (lot["id"], "error"))
        self.assertIn("ameesh.target = origin/develop not found", entry["detail"])

    def test_stale_lot_dit_l_action_puis_se_tait_apres_le_releve(self):
        lot = self.lot()
        for sql in ("UPDATE work_items SET created_at = now() - interval '8 hours',"
                    " updated_at = now() - interval '7 hours'",
                    "UPDATE work_item_events SET created_at = now() - interval '7 hours'",
                    "UPDATE work_item_milestones SET at = now() - interval '7 hours'"):
            self.db.execute(sql)
        stale = [a for a in exploitation.alerts(self.cfg, self.db) if a["type"] == "stale_lot"]
        self.assertEqual([a["lot"] for a in stale], [lot["id"]])
        self.assertIn("merged? close it: ameesh work merged %d --sha <merge commit>; "
                      "delivered outside the target? close it: ameesh work close %d "
                      "--delivered --sha <reviewed head>; otherwise nudge claude1"
                      % (lot["id"], lot["id"]), stale[0]["detail"])
        self.assertEqual(stale[0]["action"], exploitation.stale_action(stale[0] | {
            "id": lot["id"], "assignee": "claude1"}))
        self.merge("feature/z", "Merge #%d\n\nameesh-work: %d" % (lot["id"], lot["id"]))
        self.assertEqual(self.sync()["merged"], 1)
        self.assertFalse([a for a in exploitation.alerts(self.cfg, self.db)
                          if a["type"] == "stale_lot"])

    def test_cli_sync_branches(self):
        lot = self.lot()
        self.merge("feature/cli", "Merge\n\nameesh-work: %d" % lot["id"])
        proc = self.mesh("work", "sync-branches")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("(no branch)", proc.stdout)
        self.assertIn("CLOSED", proc.stdout)
        self.assertEqual(work.get(self.db, lot["id"])["state"], "delivered")

    # --- #1825 : une fusion annulée par un revert n'est pas une livraison ---

    def test_1825_fusion_annulee_par_revert_reste_ouverte(self):
        """La pointe de la branche reste ancêtre de develop après le revert de
        sa fusion, mais le contenu n'y est plus : le lot n'est pas fermé."""
        lot = self.lot()
        n = lot["id"]
        self.merge("lot/%d-garde" % n, "Merge branch 'lot/%d-garde' into develop" % n)
        _git(self.repo, "revert", "--no-edit", "-m", "1", "HEAD")
        entry = self.sync()["results"][0]
        self.assertEqual(entry["result"], "open")
        self.assertEqual(entry["how"], "reverted")
        self.assertIn("revert", entry["detail"])
        self.assertEqual(work.get(self.db, n)["state"], "intake")

    def test_1825_revert_annule_puis_reapplique_ferme(self):
        """Le revert du revert remet le contenu dans develop : la livraison
        est de nouveau constatée."""
        lot = self.lot()
        n = lot["id"]
        self.merge("lot/%d-garde" % n, "Merge branch 'lot/%d-garde' into develop" % n)
        tip = _git(self.repo, "rev-parse", "lot/%d-garde" % n)
        _git(self.repo, "revert", "--no-edit", "-m", "1", "HEAD")
        _git(self.repo, "revert", "--no-edit", "HEAD")
        report = self.sync()
        self.assertEqual(report["merged"], 1)
        self.assertEqual(self.merged_milestone(n)["sha"], tip)
        self.assertEqual(work.get(self.db, n)["state"], "delivered")

    def test_1825_revert_d_un_autre_lot_ne_retient_pas(self):
        """Un revert étranger ne retient pas un lot livré avant lui."""
        premier = self.lot("premier")
        second = self.lot("second")
        self.merge("lot/%d-un" % premier["id"],
                   "Merge branch 'lot/%d-un' into develop" % premier["id"])
        self.merge("lot/%d-deux" % second["id"],
                   "Merge branch 'lot/%d-deux' into develop" % second["id"])
        _git(self.repo, "revert", "--no-edit", "-m", "1", "HEAD")
        report = {r["work_item"]: r for r in self.sync()["results"]}
        self.assertEqual(report[premier["id"]]["result"], "merged")
        self.assertEqual(report[second["id"]]["result"], "open")
        self.assertEqual(work.get(self.db, premier["id"])["state"], "delivered")
        self.assertEqual(work.get(self.db, second["id"])["state"], "intake")

    def test_1825_fusion_citante_annulee_ne_ferme_pas(self):
        """La PR citante a bien fusionné la branche, mais la fusion a été
        annulée : la citation seule ne ferme plus (#1825)."""
        lot = self.lot()
        n = lot["id"]
        self.merge("lot/%d-garde" % n,
                   "Merge pull request #7 from manaty/lot/%d-garde" % n)
        _git(self.repo, "revert", "--no-edit", "-m", "1", "HEAD")
        _git(self.repo, "branch", "-q", "-D", "lot/%d-garde" % n)
        entry = self.sync(pr_body=lambda repo, num: "Livraison complète.")["results"][0]
        self.assertEqual(entry["result"], "open")
        self.assertIn("reverted", entry["detail"])
        self.assertEqual(work.get(self.db, n)["state"], "intake")

    def test_1825_branche_disparue_fusion_annulee_ne_ferme_pas(self):
        """La branche enregistrée n'existe plus ; la fusion qui la cite a été
        annulée : la citation seule ne ferme pas non plus (#1825)."""
        lot = self.lot()
        n = lot["id"]
        nom = "lot/%d-garde" % n
        self.merge(nom, "Merge pull request #7 from manaty/%s" % nom)
        _git(self.repo, "revert", "--no-edit", "-m", "1", "HEAD")
        _git(self.repo, "branch", "-q", "-D", nom)
        work.set_branch(self.db, n, nom, actor="orch", source="test")
        entry = self.sync(pr_body=lambda repo, num: "Livraison complète.")["results"][0]
        self.assertEqual(entry["result"], "open")
        self.assertIn("reverted", entry["detail"])
        self.assertEqual(work.get(self.db, n)["state"], "intake")


class RealTargetTest(_Base):
    """#746 : la cible RÉELLE du dépôt (`develop`) est suivie quand ni le lot
    ni le dépôt n'en déclarent, et qu'`origin/HEAD` (figé au clone) dit main.

    Liste blanche fermée : `main`, `master`, `develop`, `release/*`, sur le
    remote `origin` seul. Preuve : une fusion citante (trailer, `#<id>` si le
    dépôt l'active, ou nom entier de la branche), jamais le seul voisinage ;
    un lot à branche enregistrée exige que la fusion ait porté SA branche
    (deuxième parent = sa pointe). Une cible déclarée ne ferme jamais
    ailleurs : le résidu est signalé, sans fermeture."""

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "depot746")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        self.commit("socle")
        _git(self.repo, "checkout", "-q", "-b", "develop")
        # ni `ameesh.target` ni `origin/HEAD` : la cible par défaut est main
        _git(self.repo, "update-ref", "refs/remotes/origin/main",
             _git(self.repo, "rev-parse", "HEAD"))
        self.agent("claude1", cwd=self.repo)
        for state in (plan_git._pr_body_backoff, plan_git._pr_body_cache):
            state.clear()                           # état du processus
            self.addCleanup(state.clear)

    def commit(self, name):
        with open(os.path.join(self.repo, name), "w") as fh:
            fh.write(name + "\n")
        _git(self.repo, "add", name)
        _git(self.repo, "commit", "-q", "-m", "ajoute %s" % name)

    def merge(self, branch, message, env=None):
        """Une branche fusionnée en `--no-ff` sur develop (la cible réelle)."""
        _git(self.repo, "checkout", "-q", "-b", branch, "develop")
        self.commit(branch.replace("/", "-"))
        _git(self.repo, "checkout", "-q", "develop")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", message, branch, env=env)
        return _git(self.repo, "rev-parse", "HEAD")

    def publish(self, ref, sha=None):
        _git(self.repo, "update-ref", "refs/remotes/origin/" + ref,
             sha or _git(self.repo, "rev-parse", "HEAD"))

    def lot(self, title="lot sans branche"):
        # #540 : le témoin #990 crée deux lots pour claude1 ; le second doit
        # donc forcer la file d'équipe (même motif que MergeSweepTest.lot).
        return work.add(self.db, title=title, assignee="claude1", actor="orch",
                        force="test: several lots for one agent (#540)")

    def sync(self, **kw):
        return plan_git.sync_branches(self.db, host=self.cfg.host, **kw)

    def test_746_liste_blanche_et_ordre_des_candidats(self):
        self.publish("develop")
        self.publish("lot/12-travail")
        self.publish("review/12-r1")
        self.publish("release/1.0")
        self.assertEqual(plan_git.target_candidates(self.repo, "origin/main"),
                         ["origin/develop", "origin/release/1.0"])

    def test_746_citation_sur_develop_ferme_le_lot(self):
        lot = self.lot()
        sha = self.merge("agent/libre", "merge(desktop): X\n\nameesh-work: %d" % lot["id"])
        self.publish("develop")
        report = self.sync()
        entry = report["results"][0]
        self.assertEqual(entry["result"], "merged")
        self.assertEqual(entry["target"], "origin/develop")
        self.assertEqual(entry["off_target"], "origin/main")
        self.assertIn("ameesh.target", entry["detail"])
        self.assertEqual(work.get(self.db, lot["id"])["state"], "delivered")
        jalon = self.merged_milestone(lot["id"])
        self.assertEqual(jalon["sha"], sha)
        self.assertIn("origin/develop", jalon["note"])
        self.assertIn("declare `git config ameesh.target", jalon["note"])

    def test_746_citation_sur_une_branche_de_travail_ne_ferme_pas(self):
        lot = self.lot()
        base = _git(self.repo, "rev-parse", "develop")
        sha = self.merge("lot/autre-lot", "Merge branch 'lot/autre-lot' into develop\n\n"
                         "ameesh-work: %d" % lot["id"])
        self.publish("develop", base)          # develop n'a pas la fusion
        self.publish("lot/autre-lot", sha)     # la branche de travail, elle, l'a
        report = self.sync()
        self.assertEqual(report["results"][0]["result"], "open")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")

    def test_746_cible_declaree_citation_develop_note_sans_fermeture(self):
        lot = self.lot()
        _git(self.repo, "config", plan_git.TARGET_GIT_KEY, "origin/main")
        sha = self.merge("agent/libre", "merge(desktop): X\n\nameesh-work: %d" % lot["id"])
        self.publish("develop")
        entry = self.sync()["results"][0]
        self.assertEqual(entry["result"], "off-target")
        self.assertIn("origin/develop", entry["detail"])
        self.assertIn("declared target origin/main", entry["detail"])
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")
        notes = [e["note"] for e in work.events(self.db, lot["id"])]
        self.assertTrue(any("origin/develop" in n and sha[:12] in n for n in notes), notes)
        # le relevé suivant ne répète pas le signalement
        self.sync()
        self.assertEqual([e["note"] for e in work.events(self.db, lot["id"])], notes)

    def test_746_fusion_partielle_sur_develop_ne_ferme_pas(self):
        lot = self.lot()
        n = lot["id"]
        self.merge("lot/%d-un" % n, "Merge pull request #7 from manaty/lot/%d-un" % n)
        self.publish("develop")
        report = self.sync(pr_body=lambda repo, num: "part one\n\nameesh-work-partial: %d" % n)
        self.assertEqual(report["results"][0]["result"], "open")
        self.assertIn("partial", report["results"][0]["detail"])
        self.assertEqual(work.get(self.db, n)["state"], "intake")

    def test_746_branche_enregistree_sans_deuxieme_parent_ne_ferme_pas(self):
        lot = work.add(self.db, title="lot à branche", assignee="claude1", actor="orch",
                       branch="agent/x-746")
        self.merge("agent/x-746", "Merge branch 'agent/x-746' into develop")
        self.publish("develop")
        _git(self.repo, "checkout", "-q", "agent/x-746")
        self.commit("suite")                    # la pointe n'est plus le second parent
        _git(self.repo, "checkout", "-q", "develop")
        report = self.sync()
        self.assertEqual(report["results"][0]["result"], "open")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")

    def test_746_branche_enregistree_avec_deuxieme_parent_ferme(self):
        lot = work.add(self.db, title="lot à branche", assignee="claude1", actor="orch",
                       branch="agent/x-746")
        sha = self.merge("agent/x-746", "Merge branch 'agent/x-746' into develop")
        self.publish("develop")
        report = self.sync()
        self.assertEqual(report["results"][0]["result"], "merged")
        self.assertEqual(self.merged_milestone(lot["id"])["sha"], sha)

    def test_746_ref_illisible_signalee_sans_fermeture(self):
        lot = self.lot()
        self.publish("develop")
        vrai = plan_git.merge_commits

        def casse(repo, target, since_ts=None, **kw):
            if plan_git.short_branch(target) == "develop":
                raise plan_git.MergeProbeError("cannot read origin/develop")
            return vrai(repo, target, since_ts, **kw)

        with mock.patch.object(plan_git, "merge_commits", side_effect=casse):
            entry = self.sync()["results"][0]
            notes = [e["note"] for e in work.events(self.db, lot["id"])]
            self.sync()                    # le résidu ne se répète pas
            self.assertEqual([e["note"] for e in work.events(self.db, lot["id"])], notes)
        self.assertEqual(entry["result"], "off-target")
        self.assertIn("unreadable integration ref", entry["detail"])
        self.assertIn("origin/develop", entry["detail"])
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")
        self.assertTrue(any("origin/develop" in (n or "") for n in notes))

    def test_990_cache_des_candidats_par_cible_et_non_par_depot(self):
        """#990 (P2 de la relecture de #746) : la liste des candidats dépend de
        la cible primaire, propre au lot. Un lot à cible déclarée examiné en
        premier ne doit pas figer la liste pour les lots suivants, sans cible,
        du même dépôt : ils resteraient ouverts alors qu'une fusion citante les
        livre sur `origin/develop`."""
        premier = work.add(self.db, title="cible déclarée en premier",
                           assignee="claude1", actor="orch", branch="agent/a-990",
                           branch_target="origin/develop")
        suivant = self.lot("sans cible, livré sur develop")
        self.merge("agent/libre",
                   "merge(desktop): X\n\nameesh-work: %d" % suivant["id"])
        self.publish("develop")
        by_id = {r["work_item"]: r for r in self.sync()["results"]}
        self.assertEqual(by_id[premier["id"]]["result"], "open")
        self.assertEqual(by_id[suivant["id"]]["result"], "merged",
                         "le lot suivant, sans cible, est livré sur develop : "
                         "la liste des candidats ne doit pas être figée par le "
                         "lot précédent, qui déclarait origin/develop")
        self.assertEqual(work.get(self.db, suivant["id"])["state"], "delivered")


class ContentDeliveryTest(_Base):
    """#1005 : un lot livré sur la branche d'intégration (`develop`) par une
    fusion `--no-ff` qui ne cite pas le lot (cas #560 et #905, restés
    « waiting for merge ») est fermé par le relevé automatique de l'exécuteur
    (dernier gel ancêtre de la ref) et par `sync-merges` sans `--target`, qui
    suivent la cible réelle du clone au lieu d'`origin/main` par défaut.

    Le relevé automatique borne la preuve à l'ancêtre (une commande git) ; le
    patch-id exact (cherry-pick, rebase, squash) reste le travail de
    `sync-merges`. Une cible déclarée ne ferme jamais ailleurs."""

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "depot1005")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        self.commit("socle")
        _git(self.repo, "checkout", "-q", "-b", "develop")
        # ni `ameesh.target` ni `origin/HEAD` : la cible par défaut est main
        _git(self.repo, "update-ref", "refs/remotes/origin/main",
             _git(self.repo, "rev-parse", "HEAD"))
        self.agent("claude1", cwd=self.repo)
        for state in (plan_git._pr_body_backoff, plan_git._pr_body_cache):
            state.clear()                           # état du processus
            self.addCleanup(state.clear)

    def commit(self, name):
        with open(os.path.join(self.repo, name), "w") as fh:
            fh.write(name + "\n")
        _git(self.repo, "add", name)
        _git(self.repo, "commit", "-q", "-m", "ajoute %s" % name)
        return _git(self.repo, "rev-parse", "HEAD")

    def livrer(self, branch, message):
        """Une branche fusionnée en `--no-ff` sur develop (la cible réelle),
        sans trailer : le message d'une PR GitHub qui ne cite pas le lot."""
        _git(self.repo, "checkout", "-q", "-b", branch, "develop")
        sha = self.commit(branch.replace("/", "-"))
        _git(self.repo, "checkout", "-q", "develop")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", message, branch)
        _git(self.repo, "update-ref", "refs/remotes/origin/develop",
             _git(self.repo, "rev-parse", "HEAD"))
        return sha

    def livrer_imbrique(self):
        """B2 R2 (cx3) : le gel entre dans develop par une fusion IMBRIQUÉE —
        une fusion locale `--no-ff` sur la branche porteuse `agent/carrier`,
        puis la fusion de cette branche (la PR GitHub #627). La fusion locale
        n'est PAS sur la chaîne première-parente de develop : le porteur est
        la PR #627."""
        _git(self.repo, "checkout", "-q", "-b", "frozen", "develop")
        sha = self.commit("frozen")
        _git(self.repo, "checkout", "-q", "-b", "agent/carrier", "develop")
        self.commit("carrier")
        _git(self.repo, "merge", "-q", "--no-ff", "-m",
             "Merge branch 'frozen' into agent/carrier", "frozen")
        _git(self.repo, "checkout", "-q", "develop")
        _git(self.repo, "merge", "-q", "--no-ff", "-m",
             "Merge pull request #627 from manaty/agent/carrier", "agent/carrier")
        self.publish_develop()
        return sha

    def publish_develop(self):
        _git(self.repo, "update-ref", "refs/remotes/origin/develop",
             _git(self.repo, "rev-parse", "develop"))

    def gel_vide(self, branch, message="composition"):
        """#1776 : une branche dont le gel est un commit vide
        (`--allow-empty`) — son diff propre ne porte rien, le contenu du lot
        est dans ses ancêtres. Le gel est fusionné en `--no-ff` sur la cible
        (develop), publiée."""
        _git(self.repo, "checkout", "-q", "-b", branch, "develop")
        self.commit(branch.replace("/", "-"))
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "gel vide")
        sha = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "-q", "develop")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", message, branch)
        self.publish_develop()
        return sha

    def lot(self, sha):
        """Un lot gelé en qa, sans branche ni cible déclarée (#905)."""
        item = work.add(self.db, title="lot sans branche", assignee="claude1", actor="orch")
        for state in ("build", "qa"):
            work.move(self.db, item["id"], state)
        # #2786 (5) : les tests livrent dans `develop` AVANT de geler. La
        # marque, écrite ici au stockage, dit ce que le gel réel aurait écrit :
        # `develop` ne portait pas encore le commit, donc la livraison
        # constatée reste prouvable. `work.milestone` ignore désormais une
        # marque d'appelant (avis dev4) : le scénario se pose au stockage.
        storage.of(self.db).work.add_milestone(
            item["id"], "frozen", sha=sha, actor="ouvrier", verdict=None,
            note="ameesh-freeze-absent: develop")
        return item

    def sync(self, **kw):
        return plan_git.sync_branches(self.db, host=self.cfg.host, **kw)

    def test_1005_gel_ancetre_de_develop_ferme_le_lot(self):
        sha = self.livrer("agent/deepseek6-905-timestamp-precision",
                          "Merge pull request #627 from manaty/"
                          "agent/deepseek6-905-timestamp-precision")
        lot = self.lot(sha)
        lus = []
        entry = self.sync(pr_body=lambda repo, num:
                          lus.append(num) or "Livraison complète.")["results"][0]
        self.assertEqual(entry["result"], "merged")
        self.assertEqual(entry["target"], "origin/develop")
        self.assertEqual(entry["off_target"], "origin/main")
        self.assertEqual(entry["how"], "content-ancestor")
        self.assertEqual(lus, [627])   # le corps de la PR PORTEUSE est lu (B2)
        self.assertEqual(work.get(self.db, lot["id"])["state"], "delivered")
        jalon = self.merged_milestone(lot["id"])
        self.assertEqual(jalon["sha"], sha)
        self.assertIn("origin/develop", jalon["note"])
        self.assertIn("declare `git config ameesh.target", jalon["note"])

    def test_1005_corps_porteur_partiel_retient_le_lot(self):
        """B2 : la PR porteuse du gel (PR627, branche agent/…) déclare le lot
        partiel : le contenu est bien ancêtre, mais le lot reste ouvert."""
        sha = self.livrer("agent/deepseek6-905-timestamp-precision",
                          "Merge pull request #627 from manaty/"
                          "agent/deepseek6-905-timestamp-precision")
        lot = self.lot(sha)
        entry = self.sync(pr_body=lambda repo, num:
                          "Première partie.\n\nameesh-work-partial: %d"
                          % lot["id"])["results"][0]
        self.assertEqual(entry["how"], "partial")
        self.assertIn("PR #627", entry["detail"])
        self.assertIn("ameesh-work-partial: %d" % lot["id"], entry["detail"])
        self.assertEqual(work.get(self.db, lot["id"])["state"], "qa")

    def test_1005_corps_porteur_illisible_retient_le_lot(self):
        """B2 : sans citation, le corps de la PR porteuse est illisible : on ne
        ferme pas sans savoir."""
        sha = self.livrer("agent/deepseek6-905-timestamp-precision",
                          "Merge pull request #627 from manaty/"
                          "agent/deepseek6-905-timestamp-precision")
        lot = self.lot(sha)

        def illisible(repo, num):
            raise plan_git.GitError("gh: not logged in")

        entry = self.sync(pr_body=illisible)["results"][0]
        self.assertEqual(entry["result"], "open")
        self.assertIn("PR #627", entry["detail"])
        self.assertIn("unreadable", entry["detail"])
        self.assertEqual(work.get(self.db, lot["id"])["state"], "qa")

    def test_1005_fusion_imbriquee_porteur_partiel_retient(self):
        """B2 R2 (cx3) : le gel entre par une fusion IMBRIQUÉE (fusion locale
        `--no-ff` dans la branche porteuse, puis PR GitHub #627). Le porteur
        est la fusion de la CIBLE (chaîne première-parente), pas la fusion
        locale : le corps de la PR #627 est lu, et partiel il retient le lot
        (avant correctif, la fusion locale était retenue, son message local
        n'a pas de PR, et le lot fermait indûment)."""
        sha = self.livrer_imbrique()
        lot = self.lot(sha)
        lus = []
        entry = self.sync(pr_body=lambda repo, num: lus.append(num)
                          or "ameesh-work-partial: %d" % lot["id"])["results"][0]
        self.assertEqual(lus, [627])           # la PR porteuse, pas la fusion locale
        self.assertEqual(entry["result"], "open")
        self.assertEqual(entry["how"], "partial")
        self.assertIn("PR #627", entry["detail"])
        self.assertIn("ameesh-work-partial: %d" % lot["id"], entry["detail"])
        self.assertEqual(work.get(self.db, lot["id"])["state"], "qa")

    def test_1005_fusion_imbriquee_porteur_complet_ferme(self):
        """B2 R2 : même fusion imbriquée, corps de la PR porteuse complet : la
        preuve par le contenu est retenue APRÈS lecture de #627, et le gel
        examiné est bien celui qui ferme (B1)."""
        sha = self.livrer_imbrique()
        lot = self.lot(sha)
        lus = []
        entry = self.sync(pr_body=lambda repo, num: lus.append(num)
                          or "Livraison complète.")["results"][0]
        self.assertEqual(lus, [627])
        self.assertEqual((entry["result"], entry["how"]), ("merged", "content-ancestor"))
        self.assertEqual(work.get(self.db, lot["id"])["state"], "delivered")
        self.assertEqual(self.merged_milestone(lot["id"])["sha"], sha)

    def test_1005_fusion_imbriquee_porteur_illisible_retient(self):
        """B2 R2 : la PR porteuse d'une fusion imbriquée est illisible : on ne
        ferme pas sans savoir, même si la fusion locale n'a pas de PR."""
        sha = self.livrer_imbrique()
        lot = self.lot(sha)

        def illisible(repo, num):
            raise plan_git.GitError("gh: not logged in")

        entry = self.sync(pr_body=illisible)["results"][0]
        self.assertEqual(entry["result"], "open")
        self.assertIn("PR #627", entry["detail"])
        self.assertIn("unreadable", entry["detail"])
        self.assertEqual(work.get(self.db, lot["id"])["state"], "qa")

    def test_1005_porteur_hors_fenetre_citations_est_lu(self):
        """B2 R2 (cx3, scène old-carrier) : le porteur est cherché sur la cible
        SANS le `since_all` des citations ; une PR porteuse antérieure à la
        fenêtre des citations est quand même lue (partielle : lot ouvert)."""
        vieux = "2020-01-02T03:04:05+00:00"
        env = {"GIT_AUTHOR_DATE": vieux, "GIT_COMMITTER_DATE": vieux}
        _git(self.repo, "checkout", "-q", "-b", "agent/vieux", "develop")
        sha = self.commit("vieux")
        _git(self.repo, "checkout", "-q", "develop")
        _git(self.repo, "merge", "-q", "--no-ff", "-m",
             "Merge pull request #627 from manaty/agent/vieux", "agent/vieux", env=env)
        self.publish_develop()
        lot = self.lot(sha)                    # création du lot : bien après la PR
        lus = []
        entry = self.sync(pr_body=lambda repo, num: lus.append(num)
                          or "ameesh-work-partial: %d" % lot["id"])["results"][0]
        self.assertEqual(lus, [627])
        self.assertEqual(entry["how"], "partial")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "qa")

    def test_1005_poussee_directe_ferme_sans_lecture(self):
        """B2 R2 contrôle : un gel poussé directement sur develop (sans fusion)
        n'a pas de porteur : aucun corps à lire, la preuve par le contenu vaut."""
        sha = self.commit("direct")            # commit direct sur develop
        self.publish_develop()
        lot = self.lot(sha)
        lus = []
        entry = self.sync(pr_body=lambda repo, num: lus.append(num) or "x")["results"][0]
        self.assertEqual(lus, [])
        self.assertEqual((entry["result"], entry["how"]), ("merged", "content-ancestor"))
        self.assertEqual(work.get(self.db, lot["id"])["state"], "delivered")

    def test_1005_avance_rapide_ferme_sans_lecture(self):
        """B2 R2 contrôle : une avance rapide reste fermée, même quand la
        chaîne première-parente porte des fusions ANTÉRIEURES au gel : elles
        ne le portent pas, aucune n'est le porteur. Une fenêtre tronquée reste
        concluante ici : si la fusion la plus récente ne porte pas le gel,
        aucune fusion plus ancienne ne le porte (monotonie)."""
        self.livrer("agent/pre", "Merge pull request #1 from manaty/agent/pre")
        self.livrer("agent/pre2", "Merge pull request #2 from manaty/agent/pre2")
        _git(self.repo, "checkout", "-q", "-b", "agent/ff", "develop")
        sha = self.commit("ff")
        _git(self.repo, "checkout", "-q", "develop")
        _git(self.repo, "merge", "-q", "--ff-only", "agent/ff")
        self.publish_develop()
        lot = self.lot(sha)
        lus = []
        with mock.patch.object(plan_git, "MAX_CARRIER_SCAN", 1):
            entry = self.sync(pr_body=lambda repo, num: lus.append(num) or "x")["results"][0]
        self.assertEqual(lus, [])
        self.assertEqual((entry["result"], entry["how"]), ("merged", "content-ancestor"))
        self.assertEqual(work.get(self.db, lot["id"])["state"], "delivered")

    def test_1005_porteur_hors_fenetre_lecture_reste_ouvert(self):
        """B2 R2 : la fenêtre de lecture de la chaîne première-parente est
        bornée (limite + 1, pour voir la troncature) ; quand elle s'arrête
        avant le porteur, le lot reste OUVERT au lieu de conclure à l'absence
        de PR — puis la fenêtre complète lit #627 et ferme."""
        sha = self.livrer("agent/carrier",
                          "Merge pull request #627 from manaty/agent/carrier")
        for rang in range(3):                  # trois fusions plus récentes
            self.livrer("agent/apres%d" % rang,
                        "Merge pull request #%d from manaty/agent/apres%d"
                        % (630 + rang, rang))
        lot = self.lot(sha)
        lus = []
        with mock.patch.object(plan_git, "MAX_CARRIER_SCAN", 2):
            entry = self.sync(pr_body=lambda repo, num: lus.append(num)
                              or "Livraison complète.")["results"][0]
        self.assertEqual(lus, [])              # rien de lu : porteur indéterminé
        self.assertEqual(entry["result"], "open")
        self.assertIn("carrier", entry["detail"])
        self.assertEqual(work.get(self.db, lot["id"])["state"], "qa")
        # fenêtre complète : le porteur #627 est lu, le lot ferme
        entry = self.sync(pr_body=lambda repo, num: lus.append(num)
                          or "Livraison complète.")["results"][0]
        self.assertEqual(lus, [627])
        self.assertEqual(entry["result"], "merged")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "delivered")

    def test_1005_regel_concurrent_ne_ferme_pas(self):
        """B1 : un gel déclaré pendant le relevé (après la lecture, avant
        l'écriture) n'a pas été examiné : la fermeture est refusée."""
        sha = self.livrer("agent/libre", "Merge pull request #7 from manaty/agent/libre")
        lot = self.lot(sha)
        _git(self.repo, "checkout", "-q", "-b", "agent/regel", "develop")
        nouveau = self.commit("regel")          # jamais fusionné
        _git(self.repo, "checkout", "-q", "develop")

        def lire(repo, num):
            work.milestone(self.db, lot["id"], "frozen", sha=nouveau, actor="ouvrier")
            return "Livraison complète."

        entry = self.sync(pr_body=lire)["results"][0]
        self.assertEqual(entry["result"], "refrozen")
        self.assertIn("new freeze declared in the meantime", entry["detail"])
        self.assertEqual(work.get(self.db, lot["id"])["state"], "qa")
        # le nouveau gel n'est pas ancêtre : le relevé suivant laisse ouvert
        self.assertEqual(self.sync()["results"][0]["result"], "open")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "qa")

    def test_1005_gel_non_livre_laisse_le_lot_ouvert(self):
        _git(self.repo, "checkout", "-q", "-b", "agent/jamais", "develop")
        sha = self.commit("jamais")
        _git(self.repo, "checkout", "-q", "develop")
        self.publish_develop()
        lot = self.lot(sha)
        entry = self.sync()["results"][0]
        self.assertEqual(entry["result"], "open")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "qa")

    def test_1005_cible_declaree_contenu_develop_note_sans_fermeture(self):
        """B2 R2 contrôle : sur une ref qui ne ferme pas (cible déclarée), la
        preuve par le contenu n'est qu'un résidu signalé : le corps du porteur
        n'est pas lu (rien à qualifier)."""
        _git(self.repo, "config", plan_git.TARGET_GIT_KEY, "origin/main")
        sha = self.livrer("agent/libre", "Merge pull request #7 from manaty/agent/libre")
        lot = self.lot(sha)
        lus = []
        entry = self.sync(pr_body=lambda repo, num: lus.append(num) or "x")["results"][0]
        self.assertEqual(lus, [])              # closing=False : aucune lecture
        self.assertEqual(entry["result"], "off-target")
        self.assertIn("origin/develop", entry["detail"])
        self.assertIn("declared target origin/main", entry["detail"])
        self.assertEqual(work.get(self.db, lot["id"])["state"], "qa")
        notes = [e["note"] for e in work.events(self.db, lot["id"])]
        self.assertTrue(any("origin/develop" in (n or "") for n in notes), notes)
        self.sync()                    # le résidu ne se répète pas
        self.assertEqual([e["note"] for e in work.events(self.db, lot["id"])], notes)

    def test_1005_sync_merges_sans_target_suit_develop(self):
        sha = self.livrer("agent/libre", "Merge pull request #7 from manaty/agent/libre")
        lot = self.lot(sha)
        report = plan_git.sync_merges(self.db, self.repo)
        self.assertTrue(report["auto"])
        self.assertEqual(report["target"], "origin/main")   # cible par défaut du clone
        entry = {r["work_item"]: r for r in report["results"]}[lot["id"]]
        self.assertEqual((entry["result"], entry["how"]), ("merged", "ancestor"))
        self.assertEqual(entry["target"], "origin/develop")
        self.assertEqual(entry["off_target"], "origin/main")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "delivered")
        jalon = self.merged_milestone(lot["id"])
        self.assertEqual((jalon["sha"], jalon["actor"]), (sha, "git:origin/develop"))

    def test_1005_sync_merges_target_explicite_reste_seul(self):
        sha = self.livrer("agent/libre", "Merge pull request #7 from manaty/agent/libre")
        lot = self.lot(sha)
        entry = plan_git.sync_merges(self.db, self.repo, target="origin/main")["results"][0]
        self.assertEqual(entry["result"], "open")
        self.assertIn("missing from origin/main", entry["detail"])
        self.assertEqual(work.get(self.db, lot["id"])["state"], "qa")
        with self.assertRaises(plan_git.MergeProbeError):
            plan_git.sync_merges(self.db, self.repo, target="origin/nulle-part")

    def test_1005_patch_id_reste_a_sync_merges(self):
        # cherry-pick : le gel n'est pas ancêtre de develop ; le relevé
        # automatique (borné à l'ancêtre) laisse le lot ouvert, `sync-merges`
        # le ferme par le patch-id exact
        _git(self.repo, "checkout", "-q", "-b", "agent/cherry", "develop")
        sha = self.commit("cherry")
        _git(self.repo, "checkout", "-q", "develop")
        self.commit("socle2")          # develop avance : le cherry-pick change de parent
        _git(self.repo, "cherry-pick", sha)
        self.publish_develop()
        lot = self.lot(sha)
        self.assertEqual(self.sync()["results"][0]["result"], "open")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "qa")
        entry = plan_git.sync_merges(self.db, self.repo)["results"][0]
        self.assertEqual((entry["result"], entry["how"]), ("merged", "patch-id"))
        self.assertEqual(work.get(self.db, lot["id"])["state"], "delivered")

    # --- #1825 : une fusion annulée par un revert n'est pas une livraison ---

    def test_1825_revert_de_la_fusion_du_gel_ne_ferme_pas(self):
        """Le gel est entré dans develop par une fusion --no-ff, puis la
        fusion a été annulée : la tête reste ancêtre de la cible, mais le
        contenu n'y est plus ; le lot reste ouvert, et la PR porteuse n'est
        pas lue (il n'y a rien à qualifier)."""
        sha = self.livrer("agent/annule",
                          "Merge pull request #627 from manaty/agent/annule")
        lot = self.lot(sha)
        _git(self.repo, "revert", "--no-edit", "-m", "1", "HEAD")
        self.publish_develop()
        lus = []
        entry = self.sync(pr_body=lambda repo, num: lus.append(num)
                          or "Livraison complète.")["results"][0]
        self.assertEqual(entry["result"], "open")
        self.assertEqual(entry["how"], "reverted")
        self.assertIn("reverts", entry["detail"])
        self.assertEqual(lus, [])
        self.assertEqual(work.get(self.db, lot["id"])["state"], "qa")

    def test_1825_revert_annule_puis_reapplique_ferme(self):
        """Le revert du revert remet le contenu dans develop : la livraison
        par le contenu est de nouveau constatée."""
        sha = self.livrer("agent/annule",
                          "Merge pull request #627 from manaty/agent/annule")
        lot = self.lot(sha)
        _git(self.repo, "revert", "--no-edit", "-m", "1", "HEAD")
        _git(self.repo, "revert", "--no-edit", "HEAD")
        self.publish_develop()
        entry = self.sync(pr_body=lambda repo, num: "Livraison complète.")["results"][0]
        self.assertEqual((entry["result"], entry["how"]), ("merged", "content-ancestor"))
        self.assertEqual(work.get(self.db, lot["id"])["state"], "delivered")

    def test_1825_revert_direct_du_gel_ne_ferme_pas(self):
        """Poussée directe du gel sur develop, puis revert de ce commit : le
        gel a disparu de la cible, le lot reste ouvert."""
        sha = self.commit("direct")
        self.publish_develop()
        lot = self.lot(sha)
        _git(self.repo, "revert", "--no-edit", "HEAD")
        self.publish_develop()
        entry = self.sync()["results"][0]
        self.assertEqual(entry["result"], "open")
        self.assertEqual(entry["how"], "reverted")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "qa")

    def test_1825_patch_id_annule_par_revert_ne_ferme_pas(self):
        """Le gel cherry-pické sur develop puis annulé : son patch-id est
        encore dans l'historique, mais le contenu n'y est plus."""
        _git(self.repo, "checkout", "-q", "-b", "agent/cherry", "develop")
        sha = self.commit("cherry")
        _git(self.repo, "checkout", "-q", "develop")
        self.commit("socle2")
        _git(self.repo, "cherry-pick", sha)
        _git(self.repo, "revert", "--no-edit", "HEAD")
        self.publish_develop()
        lot = self.lot(sha)
        entry = plan_git.sync_merges(self.db, self.repo)["results"][0]
        self.assertEqual(entry["result"], "open")
        self.assertEqual(entry["how"], "reverted")
        self.assertEqual(entry["target"], "origin/develop")
        self.assertIn("reverts", entry["detail"])
        self.assertEqual(work.get(self.db, lot["id"])["state"], "qa")

    def test_1825_fusion_citante_annulee_ne_ferme_pas(self):
        """La branche `lot/<id>-…` est entrée par une PR qui la cite, puis la
        fusion a été annulée : la pointe reste ancêtre, le lot reste ouvert."""
        _git(self.repo, "checkout", "-q", "-b", "agent/tmp", "develop")
        sha = self.commit("annule")
        lot = self.lot(sha)
        nom = "lot/%d-annule" % lot["id"]
        _git(self.repo, "branch", nom, sha)
        _git(self.repo, "checkout", "-q", "develop")
        _git(self.repo, "branch", "-q", "-D", "agent/tmp")
        _git(self.repo, "merge", "-q", "--no-ff", "-m",
             "Merge pull request #627 from manaty/%s" % nom, nom)
        self.publish_develop()
        _git(self.repo, "revert", "--no-edit", "-m", "1", "HEAD")
        self.publish_develop()
        entry = self.sync(pr_body=lambda repo, num: "Livraison complète.")["results"][0]
        self.assertEqual(entry["result"], "open")
        self.assertEqual(entry["how"], "reverted")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "qa")

    def test_1005_cli_sync_merges_cible_auto(self):
        sha = self.livrer("agent/libre", "Merge pull request #7 from manaty/agent/libre")
        lot = self.lot(sha)
        proc = self.mesh("work", "sync-merges", "--git-dir", self.repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("closed by content", proc.stdout)
        self.assertEqual(work.get(self.db, lot["id"])["state"], "delivered")

    # --- #1776 : le gel vide et le paquet imbriqué, jugés par le contenu ---

    def test_1776_gel_vide_reverte_ne_ferme_pas(self):
        """Un gel vide (`commit --allow-empty`) dont la fusion est annulée par
        un revert : la tête reste ancêtre de la cible, mais le contenu n'y est
        plus — le lot reste ouvert (probe et sync-merges)."""
        sha = self.gel_vide("agent/1776")
        self.assertTrue(plan_git.probe(self.repo, sha, "develop")["merged"])
        _git(self.repo, "revert", "--no-edit", "-m", "1", "HEAD")
        self.publish_develop()
        found = plan_git.probe(self.repo, sha, "origin/develop")
        self.assertFalse(found["merged"])
        self.assertEqual(found["how"], "reverted")
        self.assertIn("revert", found["detail"])
        lot = self.lot(sha)
        entry = plan_git.sync_merges(self.db, self.repo)["results"][0]
        self.assertEqual(entry["result"], "open")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "qa")

    def test_1776_gel_vide_conserve_reste_fusionne(self):
        """Le contenu du lot est toujours là, l'arbre a évolué depuis : pas de
        retrait — l'ascendance reste la preuve."""
        sha = self.gel_vide("agent/1776")
        with open(os.path.join(self.repo, "agent-1776"), "a") as fh:
            fh.write("suite\n")
        _git(self.repo, "commit", "-q", "-am", "suite sur le meme fichier")
        self.publish_develop()
        found = plan_git.probe(self.repo, sha, "origin/develop")
        self.assertTrue(found["merged"])
        self.assertEqual(found["how"], "ancestor")

    def test_1776_gel_vide_dans_paquet_reverte(self):
        """Le lot entre dans une branche de paquet, elle-même fusionnée, puis
        la fusion du paquet est annulée : le gel vide reste ancêtre, le
        contenu a disparu — la livraison n'est pas constatée."""
        sha = self.gel_vide("agent/1776-nested")
        _git(self.repo, "branch", "-m", "develop", "packet1776")
        _git(self.repo, "checkout", "-q", "-b", "develop", "main")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "paquet", "packet1776")
        self.assertTrue(plan_git.probe(self.repo, sha, "develop")["merged"])
        _git(self.repo, "revert", "--no-edit", "-m", "1", "HEAD")
        self.publish_develop()
        self.assertFalse(plan_git.probe(self.repo, sha, "origin/develop")["merged"])


class ProbePartialCloneTest(_Base):
    """#1772 : dans un clone partiel, le commit d'un gel absent localement est
    rapatrié par un fetch explicite borné, au lieu de se lire « not in the
    repository » (#1749 coupe le fetch paresseux des sondes de présence).

    Hors clone partiel (aucun remote *promisor*), aucun fetch n'est tenté :
    la cible reste lue telle que le clone la connaît."""

    def setUp(self):
        super().setUp()
        # #3822 : un bac par test. `canon._git` retient ses lectures de
        # contenu par (chemin réel, arguments) ; un chemin réutilisé d'un test
        # à l'autre (le `self.tmp` est celui de la classe) pouvait rendre
        # présent le commit d'un test précédent, quand les deux commits —
        # même contenu, même auteur, même seconde — portaient le même SHA.
        # Le clone reste neuf et le témoin intact.
        self.bac = os.path.join(self.tmp, self._testMethodName)
        os.makedirs(self.bac)
        self.remote = os.path.join(self.bac, "remote1772")
        os.makedirs(self.remote)
        _git(self.remote, "init", "-q", "-b", "main")
        self.commit(self.remote, "socle")
        # le serveur local doit servir le filtre (clone partiel) et un objet
        # par son sha (fetch explicite d'un commit)
        _git(self.remote, "config", "uploadpack.allowFilter", "true")
        _git(self.remote, "config", "uploadpack.allowAnySHA1InWant", "true")
        self.repo = os.path.join(self.bac, "clone1772")
        _git(self.tmp, "clone", "-q", "--filter=blob:none", "--no-local",
             "file://" + self.remote, self.repo)
        # un second clone partiel, intact : les têtes publiées ensuite n'y sont
        # pas encore (preuve du best-effort sans toucher au premier clone)
        self.autre = os.path.join(self.bac, "clone1772-bis")
        _git(self.tmp, "clone", "-q", "--filter=blob:none", "--no-local",
             "file://" + self.remote, self.autre)
        # #3822 : le témoin n'existe que sur un clone réellement partiel
        # (remote promisor). Un git ou un serveur sans filtre ne peut pas le
        # produire : le dire, plutôt que d'échouer sur une assertion trompeuse.
        if plan_git._out(self.repo, "config", "--bool",
                         "remote.origin.promisor") != "true":
            self.skipTest("clone partiel indisponible (remote.origin.promisor "
                          "absent) : le témoin #1772 ne peut pas s'exercer")
        self.agent("claude1", cwd=self.repo)

    def commit(self, repo, name):
        with open(os.path.join(repo, name), "w") as fh:
            fh.write(name + "\n")
        _git(repo, "add", name)
        _git(repo, "commit", "-q", "-m", "ajoute %s" % name)
        return _git(repo, "rev-parse", "HEAD")

    def publish(self):
        """Une tête publiée sur une branche du remote, PAS rapatriée par le
        clone : la branche existe, le commit est absent localement."""
        _git(self.remote, "checkout", "-q", "-b", "agent/libre", "main")
        sha = self.commit(self.remote, "libre")
        _git(self.remote, "checkout", "-q", "main")
        return sha

    def test_1772_gel_absent_rapatrie_par_un_fetch_explicite(self):
        sha = self.publish()
        # #3832 : la prémisse est « absent des objets LOCAUX ». `_commit` s'en
        # remet à GIT_NO_LAZY_FETCH, que git 2.43 n'honore pas toujours : sous
        # charge, un fetch paresseux pouvait aboutir et le témoin devenait
        # « présent » (rouge h-32). `local_commit` lit un magasin d'objets sans
        # remote, donc sans fetch possible ; la sonde suivante prouve le
        # rapatriement explicite.
        self.assertIsNone(plan_git.local_commit(self.repo, sha))  # absent du clone
        found = plan_git.probe(self.repo, sha, "origin/main")
        self.assertFalse(found["merged"], found)
        self.assertNotIn("not in the repository", found["detail"])
        self.assertIn("missing from origin/main", found["detail"])
        self.assertIsNotNone(plan_git._commit(self.repo, sha))   # rapatrié
        self.assertEqual(plan_git._promisor_remote(self.repo, "origin/main"), "origin")

    def test_1772_sha_introuvable_reste_une_absence_dite(self):
        found = plan_git.probe(self.repo, "c" * 40, "origin/main")
        self.assertIn("not in the repository", found["detail"])
        self.assertIn("explicit fetch from origin brought nothing", found["detail"])

    def test_1772_clone_complet_ne_fetch_pas(self):
        complet = os.path.join(self.bac, "complet1772")
        _git(self.tmp, "clone", "-q", "--no-local", "file://" + self.remote, complet)
        sha = self.publish()
        found = plan_git.probe(complet, sha, "origin/main")
        self.assertIn("not in the repository", found["detail"])
        self.assertNotIn("explicit fetch", found["detail"])
        self.assertIsNone(plan_git._commit(complet, sha))

    def test_1772_commit_present_aucun_acces_au_remote(self):
        # une sonde sur un commit présent ne touche pas le remote : même avec
        # une URL cassée elle conclut, le fetch paresseux compris
        head = _git(self.repo, "rev-parse", "origin/main")
        _git(self.repo, "remote", "set-url", "origin",
             "file://" + os.path.join(self.tmp, "nulle-part"))
        found = plan_git.probe(self.repo, head, "origin/main")
        self.assertTrue(found["merged"], found)

    def test_1772_fetch_borne_et_best_effort(self):
        sha = self.publish()
        real = plan_git._git
        vus = []

        def espion(repo, *args, **kw):
            vus.append((args, kw))
            return real(repo, *args, **kw)

        with mock.patch.object(plan_git, "_git", side_effect=espion):
            plan_git.probe(self.repo, sha, "origin/main")
        fetches = [kw for args, kw in vus if args and args[0] == "fetch"]
        self.assertEqual(len(fetches), 1)
        self.assertEqual(fetches[0].get("timeout"), plan_git.FETCH_TIMEOUT)

        # un fetch qui échoue (réseau) ne casse pas la sonde : l'absence est dite
        def sans_reseau(repo, *args, **kw):
            if args and args[0] == "fetch":
                raise plan_git.GitError("git fetch: timed out")
            return real(repo, *args, **kw)

        with mock.patch.object(plan_git, "_git", side_effect=sans_reseau):
            found = plan_git.probe(self.autre, sha, "origin/main")
        self.assertIn("not in the repository", found["detail"])
        self.assertIn("explicit fetch from origin brought nothing", found["detail"])


class CurrentLotTest(_Base):
    """`ameesh projects` : le lot en cours est le dernier lot OUVERT cité."""

    def setUp(self):
        super().setUp()
        for name in ("claude1", "claude2", "orch"):
            self.agent(name, chantier="nx")
        self.vieux = work.add(self.db, title="COMPUTE-IDLE : veille", assignee="claude1")
        self.relu = work.add(self.db, title="REV-118 : relecture", assignee="claude2")

    def lot_of(self, name):
        view = projects.snapshot(self.db, paid_harnesses=())
        return next(a for p in view["projects"] for a in p["agents"] if a["name"] == name)["lot"]

    def test_lot_cite_par_l_agent_puis_jamais_un_lot_ferme(self):
        self.db.execute("UPDATE agent_registry SET session_work_item = %s WHERE name = 'claude1'",
                        (str(self.vieux["id"]),))
        self.assertEqual(self.lot_of("claude1")["id"], self.vieux["id"])
        # claude1 relit le lot de claude2 et le dit par une étiquette : le
        # numéro est enregistré (tout expéditeur), le lot en cours suit
        proc = self.cli("send", "orch", "Relecture en cours.", "--from", "claude1",
                        "--lot", "REV-118")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = self.db.query("SELECT work_item_id FROM agent_mailbox WHERE sender = 'claude1'")
        self.assertEqual(row[0]["work_item_id"], str(self.relu["id"]))
        self.assertEqual(work.get(self.db, self.relu["id"])["assignee"], "claude2")  # pas volé
        self.assertEqual(self.lot_of("claude1"), {"id": self.relu["id"],
                                                  "title": "REV-118 : relecture",
                                                  "state": "intake", "source": "mail"})
        # le lot cité est fusionné : le lot de session (ouvert) reprend la main
        work.close_merged(self.db, self.relu["id"], sha=SHA, actor="claude2")
        self.assertEqual(self.lot_of("claude1")["source"], "session")
        # le lot de session est fusionné à son tour : jamais affiché
        work.close_merged(self.db, self.vieux["id"], sha=SHA, actor="claude1")
        self.assertIsNone(self.lot_of("claude1"))

    def test_etiquettes_resolues_dans_la_vue(self):
        # une étiquette restée telle quelle dans le courrier (message ancien)
        mail.send(self.db, "claude1", "orch", "Sur la veille.", work_item_id="compute-idle")
        lot = self.lot_of("claude1")
        self.assertEqual((lot["id"], lot["source"]), (self.vieux["id"], "mail"))
        # une étiquette ambiguë ne désigne rien
        work.add(self.db, title="DUP : un")
        work.add(self.db, title="DUP : deux")
        mail.send(self.db, "claude2", "orch", "Doublon.", work_item_id="DUP")
        lot = self.lot_of("claude2")
        self.assertEqual((lot["id"], lot["source"]), (self.relu["id"], "assigned"))
        # lot de session donné par une étiquette
        self.db.execute("UPDATE agent_registry SET session_work_item = 'REV-118'"
                        " WHERE name = 'orch'")
        lot = self.lot_of("orch")
        self.assertEqual((lot["id"], lot["source"]), (self.relu["id"], "session"))

    def test_ameesh_list_sans_lot_de_session_ferme(self):
        self.db.execute("UPDATE agent_registry SET session_work_item = %s WHERE name = 'claude1'",
                        (str(self.vieux["id"]),))
        autre = work.add(self.db, title="autre", assignee="claude1",
                         force="test: second lot of claude1 (#540)")
        work.close_merged(self.db, self.vieux["id"], sha=SHA, actor="claude1")
        rows = exploitation.annotate(self.cfg, self.db, [{"name": "claude1"}])
        self.assertEqual((rows[0]["lot"]["id"], rows[0]["lot"]["source"]),
                         (str(autre["id"]), "assigned"))


if __name__ == "__main__":
    unittest.main()
