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
import unittest
from unittest import mock

from ameesh import (assignments, exploitation, mail, plan_git, projects, registry,
                    sous_utilisation, work)

from .support import PgTestCase

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

    def test_tout_etat_ouvert_passe_merged_en_une_fois(self):
        for state in ("intake", "build", "qa", "blocked", "waiting_human"):
            lot = work.add(self.db, title="lot %s" % state, actor="orch")
            if state == "qa":
                work.move(self.db, lot["id"], "build")
            if state != "intake":
                work.move(self.db, lot["id"], state)
            done = work.merged(self.db, lot["id"], sha=SHA.upper(), actor="claude1",
                               note="fusion locale sur develop")
            self.assertEqual((done["result"], done["item"]["state"]), ("merged", "merged"),
                             state)
            jalon = self.merged_milestone(lot["id"])
            self.assertEqual((jalon["sha"], jalon["actor"]), (SHA, "claude1"))
            note = work.events(self.db, lot["id"])[0]
            self.assertEqual(note["actor"], "claude1")
            self.assertEqual(note["note"], "fusion déclarée par claude1 (commit %s) — fusion "
                                           "locale sur develop" % SHA[:12])

    def test_idempotent_et_jamais_de_reouverture(self):
        lot = work.add(self.db, title="lot")
        work.merged(self.db, lot["id"], sha=SHA, actor="claude1")
        again = work.merged(self.db, lot["id"], sha="abcdef1", actor="claude2")
        self.assertEqual(again["result"], "already")
        self.assertEqual(self.merged_milestone(lot["id"])["sha"], SHA)   # rien de réécrit
        ferme = work.add(self.db, title="abandonné")
        work.close(self.db, ferme["id"], abandoned=True)
        with self.assertRaises(work.WorkError) as ctx:
            work.merged(self.db, ferme["id"], sha=SHA, actor="claude1")
        self.assertIn("aucune réouverture", str(ctx.exception))
        for bad in ("", "xyz1234", "abc12", "g" * 40):
            with self.assertRaises(work.WorkError):
                work.merged(self.db, lot["id"], sha=bad, actor="claude1")

    def test_cli_acteur_lie_par_defaut(self):
        lot = work.add(self.db, title="lot")
        proc = self.mesh("work", "merged", str(lot["id"]), "--sha", SHA, "--note", "à la main",
                         env=self.env(AGENT_MAIL_NAME="claude1"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("lot #%d → merged (commit %s, déclaré par claude1)"
                      % (lot["id"], SHA[:12]), proc.stdout)
        self.assertEqual(self.merged_milestone(lot["id"])["actor"], "claude1")
        proc = self.mesh("work", "merged", str(lot["id"]), "--sha", SHA,
                         env=self.env(AGENT_MAIL_NAME="claude1"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("déjà merged", proc.stdout)
        proc = self.mesh("work", "merged", str(lot["id"]))          # --sha obligatoire
        self.assertEqual(proc.returncode, 2)


class CorrectionTest(_Base):
    """`ameesh work move <id> merged --correct "raison"` : un promoted à tort."""

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
        self.assertIn("correction tracée au journal, par orch", proc.stdout)
        item = work.get(self.db, self.lot["id"])
        self.assertEqual(item["state"], "merged")
        self.assertIsNone(item["closed_ts"])
        event = work.events(self.db, self.lot["id"])[0]
        self.assertEqual((event["state"], event["actor"]), ("merged", "orch"))
        self.assertEqual(event["note"], "correction : promoted → merged — promu par erreur : "
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
            ((), {}, "dites qui corrige"),                                 # humain anonyme
            ((), {"AGENT_MAIL_NAME": "dev1"}, "correction refusée : dev1"),  # agent ordinaire
            (("--actor", "human:proprio"), {"AGENT_MAIL_NAME": "dev1"},
             "cette session est celle de dev1"),                         # au nom d'un autre
            (("--actor", "dev1"), {}, "correction refusée : dev1"),
        ]
        for extra, env, attendu in cas:
            proc = self.correct(*extra, **env)
            self.assertEqual(proc.returncode, 1, (extra, env, proc.stdout))
            self.assertIn(attendu, proc.stderr, (extra, env))
        self.assertEqual(work.get(self.db, self.lot["id"])["state"], "promoted")
        # seule correction possible : promoted → merged ; et une raison
        autre = work.add(self.db, title="autre")
        with self.assertRaises(work.WorkError) as ctx:
            work.move(self.db, autre["id"], "merged", actor="orch", correct="raison")
        self.assertIn("correction refusée : intake → merged", str(ctx.exception))
        with self.assertRaises(work.WorkError):
            work.move(self.db, self.lot["id"], "merged", actor="orch", correct="  ")

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
                assignments, "is_orchestrator", side_effect=lambda cfg, db, name, roles=None:
                name == "dev1" and roles == work.CORRECTOR_ROLES):
            self.assertEqual(work.corrector(self.cfg, self.db, "dev1"), "dev1")
        with identite("dev1"), mock.patch.object(
                assignments, "is_orchestrator", side_effect=lambda cfg, db, name, roles=None:
                name == "dev1" and roles == work.CORRECTOR_ROLES):
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
        self.assertIn("acteur inconnu", proc.stderr)
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
        self.assertNotIn("avertissement", proc.stderr)
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
        return work.add(self.db, title=title, assignee="claude1", actor="orch")

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
        self.assertEqual(item["state"], "merged")
        jalon = self.merged_milestone(lot["id"])
        self.assertEqual(jalon["sha"], sha)
        self.assertIn("relevé des fusions, commit de fusion de develop qui porte "
                      "« ameesh-work: %d »" % lot["id"], jalon["note"])
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
        self.assertIn("cite #%d dans son titre (ameesh.lotRef hash)" % lot["id"],
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

    def test_cited_lots(self):
        cas = {
            "merge(desktop): DESKTOP-MAC-UNIVERSAL (#91) …": {91: "#id"},
            "Merge #93 COMPUTE-IDLE": {93: "#id"},
            "Merge pull request #45 from org/branche": {45: "#id"},
            "Merge #7.": {7: "#id"},
            "Merge PR#9, depot#9, ##9, #9a, &#93;": {},
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
        self.assertIn("ameesh.target = origin/develop introuvable", entry["detail"])

    def test_stale_lot_dit_l_action_puis_se_tait_apres_le_releve(self):
        lot = self.lot()
        for sql in ("UPDATE work_items SET created_at = now() - interval '8 hours',"
                    " updated_at = now() - interval '7 hours'",
                    "UPDATE work_item_events SET created_at = now() - interval '7 hours'",
                    "UPDATE work_item_milestones SET at = now() - interval '7 hours'"):
            self.db.execute(sql)
        stale = [a for a in exploitation.alerts(self.cfg, self.db) if a["type"] == "stale_lot"]
        self.assertEqual([a["lot"] for a in stale], [lot["id"]])
        self.assertIn("fusionné ? fermez-le : ameesh work merged %d --sha <commit de fusion> ; "
                      "sinon relancez claude1" % lot["id"], stale[0]["detail"])
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
        self.assertIn("(sans branche)", proc.stdout)
        self.assertIn("FERMÉ", proc.stdout)
        self.assertEqual(work.get(self.db, lot["id"])["state"], "merged")


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
        work.merged(self.db, self.relu["id"], sha=SHA, actor="claude2")
        self.assertEqual(self.lot_of("claude1")["source"], "session")
        # le lot de session est fusionné à son tour : jamais affiché
        work.merged(self.db, self.vieux["id"], sha=SHA, actor="claude1")
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
        autre = work.add(self.db, title="autre", assignee="claude1")
        work.merged(self.db, self.vieux["id"], sha=SHA, actor="claude1")
        rows = exploitation.annotate(self.cfg, self.db, [{"name": "claude1"}])
        self.assertEqual((rows[0]["lot"]["id"], rows[0]["lot"]["source"]),
                         (str(autre["id"]), "assigned"))


if __name__ == "__main__":
    unittest.main()
