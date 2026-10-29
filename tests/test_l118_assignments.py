# SPDX-License-Identifier: AGPL-3.0-only
"""L118 : le courrier qui confie du travail est lié aux lots ; un lot se ferme
quand sa branche est fusionnée, avec ou sans PR ; `idle_capacity` part aussi
à l'orchestrateur."""
from __future__ import annotations

import datetime
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from ameesh import (assignments, exploitation, notify, plan_git, registry, storage,
                    sous_utilisation, work)

from .support import PgTestCase

HEURE = 3600.0


def _git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


class _Base(PgTestCase):
    def agent(self, name, **kw):
        registry.upsert(self.db, name, harness="claude", host=kw.pop("host", self.cfg.host),
                        **kw)

    def diverge(self, name):
        """#858 : agent du canon, admis à sa dernière évaluation, dont le profil
        a divergé depuis (le prochain canon sync le réévaluera) : il n'est plus
        réclamable, donc plus réveillable — sans être arrêté."""
        self.db.execute(
            "UPDATE agent_registry SET canon_ref = %s, placement_ok = true, "
            "placement_profile = 'ancien' WHERE name = %s",
            ("agents/%s.md" % name, name))

    def unread(self, name):
        return self.db.query("SELECT body, work_item_id, kind FROM agent_mailbox"
                             " WHERE recipient = %s ORDER BY id", (name,))


class MailLinksLotsTest(_Base):
    """`mail send` : avertissement, `--lot`, `--new-lot`."""

    def send(self, *args, orchestrators="orch"):
        env = self.env(AMEESH_ALERT_ORCHESTRATORS=orchestrators)
        return self.cli("send", *args, env=env)

    def setUp(self):
        super().setUp()
        for name in ("orch", "dev1", "dev2"):
            self.agent(name)

    def test_orchestrateur_averti_si_le_destinataire_n_a_aucun_lot(self):
        proc = self.send("dev1", "Prends la relecture du module de facturation.", "--from",
                         "orch")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("dev1 has no open lot", proc.stderr)
        self.assertEqual(len(self.unread("dev1")), 1)      # le message part quand même
        # un autre expéditeur n'est pas averti ; un destinataire avec un lot non plus
        proc = self.send("dev1", "Une question sur ta revue.", "--from", "dev2")
        self.assertNotIn("has no open lot", proc.stderr)
        work.add(self.db, title="relecture", assignee="dev1")
        proc = self.send("dev1", "Autre chose à regarder.", "--from", "orch")
        self.assertNotIn("has no open lot", proc.stderr)

    def test_lot_rattache_assigne_et_branche_citee(self):
        lot = work.add(self.db, title="RT-9 : cercle de confiance")
        proc = self.send("dev1", "À toi : travaille sur agent/dev1-rt-9 puis gèle.", "--from",
                         "orch", "--lot", "RT-9")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("has no open lot", proc.stderr)
        self.assertIn("lot #%d assigned to dev1" % lot["id"], proc.stdout)
        item = work.get(self.db, lot["id"])
        self.assertEqual((item["assignee"], item["branch"]), ("dev1", "agent/dev1-rt-9"))
        self.assertEqual(self.unread("dev1")[0]["work_item_id"], str(lot["id"]))
        # par numéro aussi, et un lot déjà à lui ne bouge pas
        proc = self.send("dev1", "Rappel sur ce lot.", "--from", "orch", "--lot", str(lot["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "dev1")

    def test_branche_citee_d_un_autre_lot_n_est_pas_reprise(self):
        """#1646 : la branche du parent d'un lot empilé, citée par un courrier,
        ne devient pas la branche du lot — sa fusion le clorait sans son delta."""
        parent = work.add(self.db, title="parent empilé", assignee="dev1",
                          branch="agent/dev1-parent")
        enfant = work.add(self.db, title="suite empilée", assignee="dev2")
        proc = self.send("dev2", "Reprends sur agent/dev1-parent puis pousse.", "--from",
                         "orch", "--lot", str(enfant["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("branch agent/dev1-parent already belongs to lot #%d" % parent["id"],
                      proc.stderr)
        self.assertIsNone(work.get(self.db, enfant["id"])["branch"])
        self.assertEqual(work.get(self.db, parent["id"])["branch"], "agent/dev1-parent")
        # une branche que personne ne porte reste posée (inchangé)
        proc = self.send("dev2", "Ta branche : agent/dev2-enfant.", "--from", "orch",
                         "--lot", str(enfant["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.get(self.db, enfant["id"])["branch"], "agent/dev2-enfant")

    def test_branche_citee_d_un_autre_lot_sur_new_lot(self):
        """#1646 : même garde à la création par courrier."""
        parent = work.add(self.db, title="parent empilé", assignee="dev1",
                          branch="agent/dev1-parent")
        proc = self.send("dev2", "Nouveau lot, base agent/dev1-parent.", "--from", "orch",
                         "--new-lot", "suite du parent", "--estimate", "1h", "--app", "p",
                         "--new-project")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = work.list_items(self.db, assignee="dev2")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["branch"])
        self.assertIn("branch agent/dev1-parent already belongs to lot #%d" % parent["id"],
                      proc.stderr)

    def test_lot_d_un_autre_agent_jamais_repris(self):
        lot = work.add(self.db, title="implémentation", assignee="dev1")
        proc = self.send("dev2", "Relis le lot de dev1.", "--from", "orch", "--lot",
                         str(lot["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("belongs to dev1: not reassigned", proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "dev1")

    def test_lot_rattache_a_un_autre_agent_n_avertit_pas_sans_lot(self):
        # #1407 : le rattachement a bien eu lieu (work_item_id) même quand la
        # garde d'attribution ne confie pas le lot au destinataire : annoncer
        # « has no open lot » ferait croire à un dépôt sans lien. L'absence de
        # lot ouvert reste annoncée pour une simple étiquette (aucun lien).
        lot = work.add(self.db, title="implementation", assignee="dev1")
        proc = self.send("dev2", "Relis le lot de dev1.", "--from", "orch", "--lot",
                         str(lot["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("belongs to dev1: not reassigned", proc.stderr)
        self.assertNotIn("has no open lot", proc.stderr)
        self.assertEqual(self.unread("dev2")[0]["work_item_id"], str(lot["id"]))
        proc = self.send("dev2", "Sur un sujet libre.", "--from", "orch", "--lot",
                         "SUJET-LIBRE-1407")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("matches no open lot", proc.stderr)
        self.assertIn("has no open lot", proc.stderr)

    def test_historique_ne_reassigne_pas_un_lot_deja_assigne(self):
        # #859/#1066 : un agent qui a confié des lots (historique L94, comme
        # le bureau) n'est pas orchestrateur pour autant : son `--lot` ne
        # réattribue pas un lot DÉJÀ tenu par un agent — ici le sien, que
        # #1066 protège comme celui d'un autre.
        self.agent("dist")
        autre = work.add(self.db, title="à distribuer")
        work.assign(self.db, int(autre["id"]), "dev1", actor="dist")
        self.assertIn("dist", storage.of(self.db).operations.assigners(since_s=HEURE * 720))
        self.assertTrue(assignments.is_orchestrator(self.cfg, self.db, "dist"))
        self.assertFalse(assignments.is_orchestrator(self.cfg, self.db, "dist", history=False))
        lot = work.add(self.db, title="tenu par dist", assignee="dist")
        proc = self.send("dev2", "Pour information sur ce lot.", "--from", "dist",
                         "--lot", str(lot["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("is yours: linked without reassignment to dev2", proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "dist")
        # l'historique garde le droit de confier un lot SANS assigné (le
        # bureau distribue ainsi, canon §4) : rien ne change de ce côté.
        libre = work.add(self.db, title="libre")
        proc = self.send("dev2", "À toi.", "--from", "dist", "--lot", str(libre["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.get(self.db, libre["id"])["assignee"], "dev2")

    def test_lot_tenu_par_l_expediteur_n_est_pas_remis_par_l_etiquette(self):
        # #1066 (incident #1046) : un orchestrateur qui TIENT un lot et écrit
        # au pilote en citant `--lot` comme étiquette de fil ne lui remet pas
        # le lot — la remise est un geste explicite (`ameesh work assign`).
        lot = work.add(self.db, title="goldens de l'exécuteur médié", assignee="orch")
        proc = self.send("dev1", "Où en est ce lot ?", "--from", "orch", "--lot",
                         str(lot["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("is yours: linked without reassignment to dev1", proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "orch")
        # le message part quand même, rattaché au fil du lot
        self.assertEqual(self.unread("dev1")[0]["work_item_id"], str(lot["id"]))
        # la remise explicite, elle, aboutit
        work.assign(self.db, lot["id"], "dev1", actor="orch")
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "dev1")

    def test_new_lot_cree_et_assigne(self):
        proc = self.send("dev2", "Nouveau chantier, branche agent/dev2-ci.", "--from", "orch",
                         "--new-lot", "CI plus rapide", "--estimate", "2h", "--app", "p",
                         "--new-project")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = work.list_items(self.db, assignee="dev2")
        self.assertEqual([(r["title"], r["branch"]) for r in rows],
                         [("CI plus rapide", "agent/dev2-ci")])
        self.assertEqual(self.unread("dev2")[0]["work_item_id"], str(rows[0]["id"]))
        self.assertIn("created and assigned to dev2", proc.stdout)

    def test_lot_d_un_destinataire_non_reveillable_part_quand_meme(self):
        """#858 : profil divergé (admission en attente du prochain canon sync) —
        le message ne doit pas être perdu avec le réveil ; le lot reste une
        étiquette du fil et l'attribution est différée."""
        lot = work.add(self.db, title="RT-9 : cercle de confiance")
        self.diverge("dev1")
        proc = self.send("dev1", "À toi : reprends ce lot.", "--from", "orch",
                         "--lot", str(lot["id"]))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("lot #%d not assigned to dev1" % lot["id"], proc.stderr)
        self.assertIn("profile diverged", proc.stderr)
        self.assertIn("delivered to dev1", proc.stdout)
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])
        rows = self.unread("dev1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["work_item_id"], str(lot["id"]))

    def test_profil_divergue_depot_differe_n_avertit_pas_sans_lot(self):
        """#1614 (#1407) : un `--lot` résolu puis déposé en mode différé
        (profil divergé, #858) est un rattachement effectif — l'avertissement
        « has no open lot » ne doit pas apparaître (condition `item is None`
        de b85c5e5ef), seule la raison du différé est dite."""
        lot = work.add(self.db, title="RT-9 : cercle de confiance")
        self.diverge("dev1")
        proc = self.send("dev1", "À toi : reprends ce lot.", "--from", "orch",
                         "--lot", str(lot["id"]))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("has no open lot", proc.stderr)
        self.assertIn("lot #%d not assigned to dev1" % lot["id"], proc.stderr)
        self.assertEqual(self.unread("dev1")[0]["work_item_id"], str(lot["id"]))

    def test_new_lot_pour_un_destinataire_non_reveillable_cree_sans_assigne(self):
        """#858 : `--new-lot` vers un agent non réveillable crée le lot sans
        assigné au lieu de perdre le message et la demande."""
        self.diverge("dev2")
        proc = self.send("dev2", "Nouveau chantier, branche agent/dev2-ci.", "--from", "orch",
                         "--new-lot", "CI plus rapide", "--estimate", "2h", "--app", "p",
                         "--new-project")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("created for dev2 without assignee", proc.stdout)
        self.assertIn("not assigned to dev2", proc.stderr)
        rows = work.list_items(self.db)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["assignee"])
        self.assertIsNone(rows[0]["branch"])       # pas la branche d'un lot non attribué
        self.assertEqual(self.unread("dev2")[0]["work_item_id"], str(rows[0]["id"]))

    def test_erreur_du_lot_ou_course_non_avalee_comme_refus_destinataire(self):
        """#858/R1 (mesh-design 23:10) : `assign` lève aussi pour une raison du
        LOT ou de la course CAS (« already merged », « changed in the
        meantime ») ; ce n'est pas un refus du DESTINATAIRE — l'erreur remonte
        et rien n'est déposé, au lieu d'un faux « lot not assigned … » différé."""
        lot = work.add(self.db, title="RT-9 : cercle de confiance")
        for why in ("lot %d already merged: nothing to reassign" % lot["id"],
                    "lot %d changed in the meantime: retry" % lot["id"]):
            with mock.patch.dict(os.environ,
                                 {"AMEESH_ALERT_ORCHESTRATORS": "orch"}), \
                    mock.patch.object(assignments.work_mod, "assign",
                                      side_effect=work.WorkError(why)):
                with self.assertRaises(assignments.AssignmentError) as ctx:
                    assignments.link_message(self.cfg, self.db, sender="orch",
                                             recipient="dev1", lot=str(lot["id"]))
            self.assertIn(why, str(ctx.exception))
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])
        self.assertEqual(self.unread("dev1"), [])

    def test_lot_deja_livre_refuse_avant_depot(self):
        """#858/R1 : un lot fermé/fusionné n'est pas un destinataire non
        réveillable ; le rattachement refuse avant tout dépôt."""
        lot = work.add(self.db, title="RT-9 : cercle de confiance")
        work.close_merged(self.db, lot["id"], sha="a" * 40)
        proc = self.send("dev1", "Reprends ce lot.", "--from", "orch",
                         "--lot", str(lot["id"]))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("message not delivered", proc.stderr)
        self.assertIn("already delivered", proc.stderr)
        self.assertEqual(self.unread("dev1"), [])

    def test_garde_d_attribution_rien_n_est_depose(self):
        proc = self.send("inconnu", "Travail pour toi.", "--from", "orch", "--new-lot", "x",
                         "--estimate", "1h")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("message not delivered", proc.stderr)
        self.assertEqual(self.unread("inconnu"), [])
        self.assertEqual(work.list_items(self.db), [])

    def test_lot_d_un_agent_ordinaire_reste_une_etiquette(self):
        lot = work.add(self.db, title="libre")
        proc = self.send("dev1", "Je réponds sur ce lot.", "--from", "dev2", "--lot",
                         str(lot["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])
        # une étiquette libre reste acceptée (inchangé), l'orchestrateur est prévenu
        proc = self.send("dev1", "Sur le sujet libre.", "--from", "orch", "--lot",
                         "SUJET-LIBRE")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("matches no open lot", proc.stderr)
        self.assertEqual(self.unread("dev1")[-1]["work_item_id"], "SUJET-LIBRE")

    def test_assign_explicite_attribue(self):
        # #964: `--assign` is the explicit gesture that hands a FREE lot over
        # even from a sender the orchestrator detection does not recognise
        # (no distribution history); without it, its --lot stays a thread
        # label (invariant #859/#1066 kept).
        self.agent("dev3")
        self.assertFalse(assignments.is_orchestrator(self.cfg, self.db, "dev3"))
        lot = work.add(self.db, title="relecture à confier")
        proc = self.send("dev1", "Pour information.", "--from", "dev3", "--lot",
                         str(lot["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])
        proc = self.send("dev1", "À toi : relis puis gèle.", "--from", "dev3",
                         "--lot", str(lot["id"]), "--assign")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("lot #%d assigned to dev1" % lot["id"], proc.stdout)
        item = work.get(self.db, lot["id"])
        self.assertEqual(item["assignee"], "dev1")
        self.assertEqual(self.unread("dev1")[-1]["work_item_id"], str(lot["id"]))

    def test_assign_ne_prend_pas_le_lot_d_un_autre(self):
        # even with --assign, another agent's lot is not taken over:
        # handing over goes through `ameesh work assign` (module invariant)
        self.agent("dev3")
        lot = work.add(self.db, title="implémentation", assignee="dev3")
        proc = self.send("dev1", "Relis le lot de dev3.", "--from", "dev2",
                         "--lot", str(lot["id"]), "--assign")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("belongs to dev3: not reassigned", proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "dev3")

    def test_assign_exige_le_lot_et_refuse_l_accuse(self):
        proc = self.send("dev1", "Coucou.", "--from", "dev2", "--assign")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--assign requires --lot", proc.stderr)
        proc = self.send("dev1", "Coucou.", "--from", "dev2", "--lot", "X", "--assign",
                         "--ack")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("contradict", proc.stderr)
        self.assertEqual(self.unread("dev1"), [])

    def test_assign_garde_l_assignataire(self):
        # #1701 N3: an unknown name is refused by `undeliverable.check_send`
        # before `link_message` (nothing is deposited), not by the L37 guard;
        # the recipient's assignment guard is covered by the diverged-profile
        # witness below.
        lot = work.add(self.db, title="libre")
        proc = self.send("inconnu", "Travail pour toi.", "--from", "dev2",
                         "--lot", str(lot["id"]), "--assign")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("message not delivered", proc.stderr)
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])
        self.assertEqual(self.unread("inconnu"), [])

    def test_assign_reference_sans_lot_refusee(self):
        """#1701 N1: `--assign` names the lot to assign, so a reference that
        matches no open lot is refused before any deposit instead of becoming
        a plain thread label."""
        proc = self.send("dev1", "Coucou.", "--from", "dev2", "--lot", "INCONNU",
                         "--assign")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("message not delivered", proc.stderr)
        self.assertIn("matches no open lot", proc.stderr)
        self.assertEqual(self.unread("dev1"), [])

    def test_assign_differe_l_attribution_d_un_profil_diverge(self):
        """#858/#1701 N3: `--assign` does not bypass the recipient's
        assignment guard — a diverged profile leaves the lot free, the message
        goes out and the warning says why."""
        lot = work.add(self.db, title="RT-9 : cercle de confiance")
        self.diverge("dev1")
        proc = self.send("dev1", "À toi : reprends ce lot.", "--from", "dev2",
                         "--lot", str(lot["id"]), "--assign")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("lot #%d not assigned to dev1" % lot["id"], proc.stderr)
        self.assertIn("profile diverged", proc.stderr)
        self.assertIn("delivered to dev1", proc.stdout)
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])
        self.assertEqual(self.unread("dev1")[0]["work_item_id"], str(lot["id"]))

    def test_assign_ne_consulte_pas_la_detection_d_orchestrateur(self):
        """#1701 N2: `--assign` decides alone; the orchestrator detection (a
        canon read) is not consulted at all."""
        lot = work.add(self.db, title="libre")
        with mock.patch.object(assignments, "is_orchestrator",
                               side_effect=AssertionError("must not be consulted")):
            linked = assignments.link_message(self.cfg, self.db, sender="dev2",
                                              recipient="dev1", lot=str(lot["id"]),
                                              assign=True)
        self.assertTrue(linked["assigned"])
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "dev1")

    def test_reference_ambigue_refusee(self):
        work.add(self.db, title="DUP : un")
        work.add(self.db, title="DUP : deux")
        with self.assertRaises(assignments.AssignmentError):
            assignments.resolve_lot(self.db, "DUP")
        with mock.patch.dict(os.environ, {"AMEESH_ALERT_ORCHESTRATORS": "orch"}):
            self.assertTrue(assignments.is_orchestrator(self.cfg, self.db, "orch"))
            self.assertFalse(assignments.is_orchestrator(self.cfg, self.db, "dev1"))


class BranchMergeTest(_Base):
    """Un lot se ferme quand sa branche entre dans sa cible, avec ou sans PR."""

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "depot")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "develop")
        self.commit("socle")
        _git(self.repo, "config", plan_git.TARGET_GIT_KEY, "develop")
        self.agent("dev1", cwd=self.repo)

    def commit(self, name):
        with open(os.path.join(self.repo, name), "w") as fh:
            fh.write(name + "\n")
        _git(self.repo, "add", name)
        _git(self.repo, "commit", "-q", "-m", "ajoute %s" % name)
        return _git(self.repo, "rev-parse", "HEAD")

    def branch(self, name, *files):
        _git(self.repo, "checkout", "-q", "-b", name, "develop")
        for f in files:
            self.commit(f)
        _git(self.repo, "checkout", "-q", "develop")

    def lot(self, branch):
        # #540 : plusieurs lots non démarrés au même agent, forcés
        return work.add(self.db, title="lot", assignee="dev1", branch=branch,
                        force="test: several branches of one agent")

    def sync(self, **kw):
        return plan_git.sync_branches(self.db, host=self.cfg.host, **kw)

    def test_fusion_par_commit_de_fusion(self):
        self.branch("agent/dev1-a", "a1", "a2")
        lot = self.lot("agent/dev1-a")
        report = self.sync()
        self.assertEqual([r["result"] for r in report["results"]], ["open"])
        self.assertTrue(work.get(self.db, lot["id"])["branch_head"])
        self.commit("ailleurs")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "merge agent/dev1-a", "agent/dev1-a")
        self.assertEqual(self.sync(dry_run=True)["results"][0]["result"], "would-merge")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")
        report = self.sync()
        self.assertEqual((report["merged"], report["results"][0]["how"]), (1, "ancestor"))
        item = work.get(self.db, lot["id"])
        self.assertEqual(item["state"], "delivered")
        notes = [e["note"] for e in work.events(self.db, lot["id"])]
        self.assertTrue(any("branch agent/dev1-a" in n for n in notes), notes)
        self.assertEqual(self.sync()["results"], [])          # lot livré : plus examiné

    def test_2744_un_lot_blocked_n_est_pas_ferme_par_le_releve_des_branches(self):
        """#2744 : la garde vaut aussi pour le relevé automatique des branches."""
        self.branch("agent/dev1-bloque", "b1")
        lot = self.lot("agent/dev1-bloque")
        for state in ("build", "qa", "blocked"):
            work.move(self.db, lot["id"], state)
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "merge agent/dev1-bloque",
             "agent/dev1-bloque")
        report = self.sync()
        self.assertEqual(report["results"][0]["result"], "withheld")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "blocked")

    def test_branche_sans_travail_n_est_pas_une_fusion(self):
        self.branch("agent/dev1-vide")
        lot = self.lot("agent/dev1-vide")
        self.assertEqual(self.sync()["results"][0]["result"], "open")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")

    def test_branche_nee_d_un_paquet_deja_fusionne_n_est_pas_livree(self):
        """#2543 : une pointe entrée dans la cible par la fusion d'un AUTRE lot
        (un paquet) n'est pas une preuve de livraison. Tant que l'auteur n'a pas
        de commit propre, le lot reste ouvert ; après son commit puis sa fusion,
        le relevé le ferme."""
        # le paquet : son commit entre dans develop par un commit de fusion,
        # donc hors de la chaîne première-parente de la cible.
        self.branch("paquet/78", "paquet")
        paquet = _git(self.repo, "rev-parse", "paquet/78")
        self.commit("apres-paquet")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "Merge paquet/78", "paquet/78")
        # la branche du lot naît sur ce commit, sans travail propre
        _git(self.repo, "branch", "agent/dev1-paquet", paquet)
        lot = self.lot("agent/dev1-paquet")
        entry = self.sync()["results"][0]
        self.assertEqual(entry["result"], "open")
        self.assertIn("no work of its own", entry["detail"])
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")
        self.assertIsNone(work.get(self.db, lot["id"])["branch_head"])
        # le premier commit de l'auteur, vu en avance, puis sa fusion : livré
        _git(self.repo, "checkout", "-q", "agent/dev1-paquet")
        self.commit("propre")
        _git(self.repo, "checkout", "-q", "develop")
        self.assertEqual(self.sync()["results"][0]["result"], "open")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "merge agent/dev1-paquet",
             "agent/dev1-paquet")
        report = self.sync()
        self.assertEqual(report["merged"], 1)
        self.assertEqual(report["results"][0]["how"], "ancestor")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "delivered")

    def test_branche_associee_sur_sa_base_avant_son_premier_commit_ne_ferme_pas(self):
        """#4508 (#4482) : une branche associée au lot avant son premier
        commit, sur la base d'un AUTRE lot encore hors de la cible, n'a aucun
        commit propre. Le relevé l'a vue en avance (c'était la base) ; quand
        cette base entre ensuite dans la cible par un autre chemin, elle ne
        doit pas fermer le lot. Le premier commit de l'auteur, puis sa fusion,
        ferment normalement."""
        # un commit d'un autre lot, pas encore dans develop
        _git(self.repo, "checkout", "-q", "-b", "autre/lot", "develop")
        base = self.commit("base-autre-lot")
        _git(self.repo, "checkout", "-q", "develop")
        # la branche du lot naît sur cette base, sans commit propre, et le lot
        # l'associe avant le premier commit (comme #4482 à 08:24).
        _git(self.repo, "branch", "agent/dev1-base", base)
        lot = self.lot("agent/dev1-base")
        first = self.sync()["results"][0]
        self.assertEqual(first["result"], "open", first)
        self.assertIn("no work of its own", first["detail"], first)
        self.assertIsNone(work.get(self.db, lot["id"])["branch_head"])
        # la base entre dans develop par un commit de fusion qui ne cite pas
        # la branche du lot : c'est le paquet d'un autre lot, pas une preuve.
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "Merge autre/lot", "autre/lot")
        entry = self.sync()["results"][0]
        self.assertEqual(entry["result"], "open", entry)
        self.assertIn("no work of its own", entry["detail"])
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")
        # le premier commit de l'auteur, vu en avance, puis sa fusion : livré
        _git(self.repo, "checkout", "-q", "agent/dev1-base")
        propre = self.commit("propre")
        _git(self.repo, "checkout", "-q", "develop")
        self.assertEqual(self.sync()["results"][0]["result"], "open")
        self.assertEqual(work.get(self.db, lot["id"])["branch_head"], propre)
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "Merge agent/dev1-base",
             "agent/dev1-base")
        report = self.sync()
        self.assertEqual(report["merged"], 1)
        self.assertEqual(report["results"][0]["how"], "ancestor")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "merged")

    def test_gel_sur_le_commit_propre_ne_prouve_pas_la_base_vue(self):
        """#4508 (B dev1) : le gel du lot sur son commit propre F ne prouve
        pas la BASE B de la branche, qui en est un ancêtre. Le clone du
        balayage voit encore B (F pas poussé ou pas récupéré) : B n'est pas
        enregistrée comme tête vue, et quand elle entre par le paquet d'un
        autre lot, le lot reste ouvert."""
        # la base B : la pointe d'un autre lot, hors de develop
        _git(self.repo, "checkout", "-q", "-b", "autre/lot", "develop")
        base = self.commit("base-autre-lot")
        _git(self.repo, "checkout", "-q", "develop")
        # la branche du lot naît sur B, sans commit propre
        _git(self.repo, "branch", "agent/dev1-base-gel", base)
        lot = self.lot("agent/dev1-base-gel")
        first = self.sync()["results"][0]
        self.assertEqual(first["result"], "open", first)
        self.assertIn("no work of its own", first["detail"], first)
        self.assertIsNone(work.get(self.db, lot["id"])["branch_head"])
        # l'auteur a un commit propre F et gèle dessus ; le clone du balayage
        # voit encore la pointe de la branche à B
        _git(self.repo, "checkout", "-q", "agent/dev1-base-gel")
        propre = self.commit("propre")
        _git(self.repo, "checkout", "-q", "develop")
        _git(self.repo, "branch", "-f", "agent/dev1-base-gel", base)
        storage.of(self.db).work.add_milestone(
            lot["id"], "frozen", sha=propre, actor="dev1", verdict=None, note="")
        entry = self.sync()["results"][0]
        self.assertEqual(entry["result"], "open", entry)
        self.assertIsNone(work.get(self.db, lot["id"])["branch_head"])
        self.assertIn("no work of its own", entry["detail"], entry)
        # B entre dans develop par le paquet d'un autre lot : rien ne ferme
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "Merge autre/lot", "autre/lot")
        entry = self.sync()["results"][0]
        self.assertEqual(entry["result"], "open", entry)
        self.assertIn("no work of its own", entry["detail"], entry)
        self.assertEqual(work.get(self.db, lot["id"])["state"], "qa")

    def test_tete_sans_commit_propre_enregistree_avant_le_correctif_est_refusee(self):
        """#4508 : une tête déjà enregistrée (base d'un autre lot, avant le
        correctif) est refusée par la même lecture : le reflog de la branche
        locale dit qu'elle n'a pas bougé depuis sa création. Le lot reste
        ouvert même quand la base entre dans la cible par une fusion."""
        self.branch("autre/lot", "base-autre-lot")
        base = _git(self.repo, "rev-parse", "autre/lot")
        _git(self.repo, "branch", "agent/dev1-base-vue", base)
        lot = self.lot("agent/dev1-base-vue")
        # ce que l'ancien relevé avait enregistré : la base, vue en avance
        storage.of(self.db).work.set_branch_head(
            lot["id"], "agent/dev1-base-vue", base)
        # la base entre dans develop par une fusion, puis la branche de
        # l'autre lot est supprimée : seul le reflog reste
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "Merge autre/lot", "autre/lot")
        _git(self.repo, "branch", "-q", "-D", "autre/lot")
        entry = self.sync()["results"][0]
        self.assertEqual(entry["result"], "open", entry)
        self.assertIn("no work of its own", entry["detail"])
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")

    def test_reflog_expire_ne_fait_pas_d_un_vrai_commit_une_base(self):
        """#4508 (B1 cx2) : quand l'entrée de création a expiré, la plus
        ancienne du reflog est un commit ordinaire ; prendre ce SHA pour la
        création refuserait à tort un vrai commit. La preuve par la tête vue
        reste acquise."""
        t0 = int(time.time())
        _git(self.repo, "checkout", "-q", "-b", "agent/dev1-expire", "develop")
        time.sleep(2.1)                    # l'entrée de création date d'avant
        vrai = self.commit("vrai")
        _git(self.repo, "checkout", "-q", "develop")
        lot = self.lot("agent/dev1-expire")
        cutoff = datetime.datetime.fromtimestamp(
            t0 + 1, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _git(self.repo, "reflog", "expire", "--expire=" + cutoff,
             "refs/heads/agent/dev1-expire")
        # le relevé enregistre le vrai commit (pas de faux « no work »)
        self.assertEqual(self.sync()["results"][0]["result"], "open")
        self.assertEqual(work.get(self.db, lot["id"])["branch_head"], vrai)
        # sa fusion --no-ff, sans citation de la branche, le ferme
        _git(self.repo, "merge", "-q", "--no-ff", "-m",
             "Deliver reviewed packet", "agent/dev1-expire")
        report = self.sync()
        self.assertEqual(report["merged"], 1)
        self.assertEqual(report["results"][0]["how"], "ancestor")

    def test_tete_hors_premiere_parente_vue_contre_une_cible_perimee_n_est_pas_livree(self):
        """#3326 avec #2543 : une pointe entrée par une fusion (hors première
        parente) ne prouve rien non plus si elle était déjà dans la cible
        quand le relevé l'a vue."""
        # le paquet entre dans develop par une fusion ; sa pointe est hors de
        # la chaîne première-parente.
        self.branch("paquet/3326", "paquet")
        paquet = _git(self.repo, "rev-parse", "paquet/3326")
        self.commit("apres-paquet")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "Merge paquet/3326",
             "paquet/3326")
        # la branche du lot naît sur cette pointe, sans travail propre
        _git(self.repo, "branch", "agent/dev1-paquet-3326", paquet)
        lot = self.lot("agent/dev1-paquet-3326")
        # l'observation d'un relevé passé, contre une cible encore en retard
        time.sleep(1.1)                      # le reflog de develop date d'avant
        storage.of(self.db).work.set_branch_head(
            lot["id"], "agent/dev1-paquet-3326", paquet)
        self.commit("cible-rafraichie")
        entry = self.sync()["results"][0]
        self.assertEqual(entry["result"], "open", entry)
        self.assertIn("no work of its own", entry["detail"])
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")

    def test_tete_deja_dans_la_cible_quand_elle_a_ete_vue_n_est_pas_livree(self):
        """#3326 : un relevé qui a vu en avance, contre une cible périmée, une
        tête déjà livrée n'a vu aucun travail propre. La cible rafraîchie, la
        preuve « vu-ancestor » est refusée et le lot reste ouvert."""
        # la tête de la cible avant le relevé, datée pour un reflog déterministe
        with mock.patch.dict(os.environ, {
                "GIT_AUTHOR_DATE": "2026-10-18T10:00:00+0000",
                "GIT_COMMITTER_DATE": "2026-10-18T10:00:00+0000"}):
            self.commit("deja-livre")
        base = _git(self.repo, "rev-parse", "develop")
        _git(self.repo, "branch", "agent/dev1-deja", "develop")
        lot = self.lot("agent/dev1-deja")
        seen = datetime.datetime(2026, 10, 18, 10, 2,
                                 tzinfo=datetime.timezone.utc).timestamp()
        # ce que le balayage de 10:02 avait enregistré : la pointe était un
        # commit de la cible, pas un commit de la branche.
        self.db.execute("UPDATE work_items SET branch_head = %s,"
                        " branch_head_at = to_timestamp(%s) WHERE id = %s",
                        (base, seen, lot["id"]))
        # la cible avance et le clone la rafraîchit
        with mock.patch.dict(os.environ, {
                "GIT_AUTHOR_DATE": "2026-10-18T10:05:00+0000",
                "GIT_COMMITTER_DATE": "2026-10-18T10:05:00+0000"}):
            self.commit("cible-rafraichie")
        entry = self.sync()["results"][0]
        self.assertEqual(entry["result"], "open", entry)
        self.assertIn("no work of its own", entry["detail"])
        item = work.get(self.db, lot["id"])
        self.assertEqual(item["state"], "intake")
        self.assertEqual(item["branch_head"], base)   # l'observation reste, sans preuve

    def test_une_tete_deja_publiee_n_est_pas_vue_en_avance(self):
        """#3326 : la ref de la cible peut être en retard sur la forge (fetch
        best-effort). Une pointe que la forge porte déjà n'est pas un travail
        propre : elle n'est pas enregistrée, et la cible rafraîchie ne ferme
        pas le lot."""
        _bare, other = self.origin()
        # la forge avance, le clone de l'assigné garde son origin/develop en
        # retard : le fetch du relevé échoue.
        stale = _git(self.repo, "rev-parse", "origin/develop")
        tete = self.push_new_work(other, "develop", "livre")
        _git(self.repo, "fetch", "-q", "origin", "develop:refs/tmp/tete")
        # l'objet est là (pour la lecture fraîche), la ref du clone reste en
        # retard (fetch best-effort en échec).
        _git(self.repo, "update-ref", "refs/remotes/origin/develop", stale)
        self.assertEqual(tete, _git(self.repo, "rev-parse", "refs/tmp/tete"))
        self.assertNotEqual(tete, _git(self.repo, "rev-parse", "origin/develop"))
        _git(self.repo, "branch", "agent/dev1-deja-publiee", "refs/tmp/tete")
        lot = self.lot("agent/dev1-deja-publiee")
        with mock.patch.object(plan_git, "git_fetch", return_value="fetch refused"):
            first = self.sync()["results"][0]
        self.assertEqual(first["result"], "open", first)
        self.assertEqual(first["how"], "no-work", first)
        self.assertIn("read fresh", first["detail"])
        self.assertIsNone(work.get(self.db, lot["id"])["branch_head"])
        # le relevé suivant rafraîchit la cible : la pointe n'a jamais eu de
        # travail propre, rien ne prouve sa livraison.
        second = self.sync()["results"][0]
        self.assertEqual(second["result"], "open", second)
        self.assertIn("no work of its own", second["detail"])
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")

    def test_tete_publiee_absente_du_clone_n_est_pas_enregistree(self):
        """#3326 (relecture cx1) : la forge est à C, le clone à A, la branche
        naît sur B — présent localement, C absent. Un fetch refusé ne permet
        pas de prouver que B n'est pas dans la cible publiée : rien n'est
        enregistré, et la cible rafraîchie ne ferme pas le lot."""
        _bare, other = self.origin()
        _git(other, "checkout", "-q", "-B", "develop", "origin/develop")
        # B part sur une ref séparée, puis la forge avance à C sur develop.
        with open(os.path.join(other, "b"), "w") as fh:
            fh.write("b\n")
        _git(other, "add", "b")
        _git(other, "commit", "-qm", "B")
        b = _git(other, "rev-parse", "HEAD")
        _git(other, "push", "-q", "origin", "HEAD:refs/heads/bonly")
        with open(os.path.join(other, "c"), "w") as fh:
            fh.write("c\n")
        _git(other, "add", "c")
        _git(other, "commit", "-qm", "C")
        c = _git(other, "rev-parse", "HEAD")
        _git(other, "push", "-q", "origin", "HEAD:refs/heads/develop")
        # le clone récupère B seulement ; C reste absent de son dépôt.
        _git(self.repo, "fetch", "-q", "origin", "bonly:refs/tmp/b")
        self.assertEqual(b, _git(self.repo, "rev-parse", "refs/tmp/b"))
        self.assertNotEqual(b, _git(self.repo, "rev-parse", "origin/develop"))
        with self.assertRaises(AssertionError):
            _git(self.repo, "cat-file", "-e", c)
        _git(self.repo, "branch", "agent/dev1-publiee-absente", "refs/tmp/b")
        lot = self.lot("agent/dev1-publiee-absente")
        # la ref de cible du clone est en retard et le fetch du relevé échoue.
        with mock.patch.object(plan_git, "git_fetch", return_value="fetch refused"):
            first = self.sync()["results"][0]
        self.assertEqual(first["result"], "open", first)
        self.assertIn("not recorded", first["detail"])
        self.assertIsNone(work.get(self.db, lot["id"])["branch_head"])
        # la cible rafraîchie ne prouve aucun travail propre.
        second = self.sync()["results"][0]
        self.assertEqual(second["result"], "open", second)
        self.assertIn("no work of its own", second["detail"])
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")

    def test_avance_rapide_apres_un_releve(self):
        self.branch("agent/dev1-ff", "f1")
        lot = self.lot("agent/dev1-ff")
        self.sync()                                   # retient la pointe en avance
        _git(self.repo, "merge", "-q", "--ff-only", "agent/dev1-ff")
        report = self.sync()
        self.assertEqual(report["results"][0]["how"], "vu-ancestor")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "delivered")

    def origin(self):
        """#2654 : une forge (dépôt nu `origin`) et un second clone qui y pousse."""
        bare = os.path.join(self.tmp, "forge.git")
        _git(self.tmp, "init", "-q", "--bare", bare)
        _git(self.repo, "remote", "add", "origin", bare)
        _git(self.repo, "push", "-q", "origin", "develop")
        other = os.path.join(self.tmp, "autre")
        _git(self.tmp, "clone", "-q", bare, other)
        return bare, other

    def push_new_work(self, other, branch, name):
        _git(other, "fetch", "-q", "origin")
        _git(other, "checkout", "-q", "-B", branch, "origin/develop")
        with open(os.path.join(other, name), "w") as fh:
            fh.write(name + "\n")
        _git(other, "add", name)
        _git(other, "commit", "-q", "-m", "ajoute %s" % name)
        _git(other, "push", "-q", "-f", "origin", branch)
        return _git(other, "rev-parse", "HEAD")

    def test_branche_recreee_absente_du_clone_reste_ouverte(self):
        """#2654, témoin #2623 : la branche du lot, vue en avance puis entrée
        dans la cible, est recréée sous le même nom avec du travail neuf, poussé
        depuis un autre clone ; le clone relevé ne l'a pas. La tête vue,
        périmée, ne prouve plus rien : le lot reste ouvert."""
        _bare, other = self.origin()
        self.branch("agent/dev1-neuf", "n1")
        _git(self.repo, "push", "-q", "origin", "agent/dev1-neuf")
        lot = self.lot("agent/dev1-neuf")
        self.sync()                                   # retient la pointe en avance
        _git(self.repo, "merge", "-q", "--ff-only", "agent/dev1-neuf")
        _git(self.repo, "push", "-q", "origin", "develop")
        _git(self.repo, "branch", "-q", "-D", "agent/dev1-neuf")
        _git(self.repo, "branch", "-q", "-D", "-r", "origin/agent/dev1-neuf")
        self.push_new_work(other, "agent/dev1-neuf", "n2")
        # le fetch du relevé échoue (ou précède la poussée) : le clone l'ignore
        with mock.patch.object(plan_git, "git_fetch", return_value="fetch refused"):
            entry = self.sync()["results"][0]
        self.assertEqual(entry["result"], "open", entry)
        self.assertIn("absent from this clone", entry["detail"])
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")

    def test_branche_poussee_depuis_la_tete_vue_reste_ouverte(self):
        """#2654 : la pointe du clone est entrée dans la cible, mais la forge
        porte depuis un commit de plus, présent au clone : le lot reste ouvert
        et cette tête devient la tête vue."""
        self.origin()
        self.branch("agent/dev1-suite", "s1")
        lot = self.lot("agent/dev1-suite")
        self.sync()
        _git(self.repo, "merge", "-q", "--ff-only", "agent/dev1-suite")
        _git(self.repo, "push", "-q", "origin", "develop")
        _git(self.repo, "checkout", "-q", "--detach", "develop")
        suite = self.commit("s2")
        _git(self.repo, "checkout", "-q", "develop")
        _git(self.repo, "push", "-q", "origin", "%s:refs/heads/agent/dev1-suite" % suite)
        entry = self.sync()["results"][0]
        self.assertEqual(entry["result"], "open", entry)
        item = work.get(self.db, lot["id"])
        self.assertEqual((item["state"], item["branch_head"]), ("intake", suite))

    def test_branche_supprimee_de_la_forge_apres_fusion_ferme(self):
        """#2654 : la branche fusionnée puis supprimée de la forge (GitHub) :
        la tête vue reste la preuve, comme avant."""
        self.origin()
        self.branch("agent/dev1-fin", "f1")
        _git(self.repo, "push", "-q", "origin", "agent/dev1-fin")
        lot = self.lot("agent/dev1-fin")
        self.sync()
        _git(self.repo, "merge", "-q", "--ff-only", "agent/dev1-fin")
        _git(self.repo, "push", "-q", "origin", "develop", ":agent/dev1-fin")
        _git(self.repo, "branch", "-q", "-D", "agent/dev1-fin")
        report = self.sync()
        self.assertEqual(report["results"][0]["how"], "vu-ancestor")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "delivered")

    def test_forge_illisible_ne_conclut_pas(self):
        """#2654 : sans lecture de la forge, la tête vue ne ferme pas le lot."""
        self.branch("agent/dev1-muet", "m1")
        lot = self.lot("agent/dev1-muet")
        self.sync()
        _git(self.repo, "merge", "-q", "--ff-only", "agent/dev1-muet")
        _git(self.repo, "remote", "add", "origin", os.path.join(self.tmp, "absent.git"))
        entry = self.sync()["results"][0]
        self.assertEqual(entry["result"], "open", entry)
        self.assertIn("unreadable", entry["detail"])
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")

    def test_show_etiquette_la_tete_vue_au_balayage(self):
        """#1899 : la tête en avance affichée vient du dernier balayage, pas de
        la forge — deux triages faux le 16/10 l'ont lue comme la tête réelle."""
        self.branch("agent/dev1-cache", "c1")
        lot = self.lot("agent/dev1-cache")
        self.assertEqual(self.sync()["results"][0]["result"], "open")
        tete = work.get(self.db, lot["id"])["branch_head"]
        self.assertTrue(tete)
        proc = self.mesh("work", "show", str(lot["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("tête en avance vue au dernier balayage : %s" % tete[:12], proc.stdout)
        self.assertIn("vérifier à la forge", proc.stdout)
        self.assertNotIn("last commit seen ahead", proc.stdout)

    def test_squash_puis_branche_supprimee(self):
        self.branch("agent/dev1-sq", "s1", "s2")
        lot = self.lot("agent/dev1-sq")
        self.sync()
        _git(self.repo, "merge", "-q", "--squash", "agent/dev1-sq")
        _git(self.repo, "commit", "-q", "-m", "squash")
        _git(self.repo, "branch", "-q", "-D", "agent/dev1-sq")
        report = self.sync()
        self.assertEqual(report["results"][0]["how"], "vu-squash")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "delivered")

    def test_squash_branche_restee_sur_la_forge_ferme(self):
        """#2654 : la forge garde la branche après un squash ; sa tête, au
        contenu entré dans la cible, ne retient pas le lot."""
        self.origin()
        self.branch("agent/dev1-sqf", "q1", "q2")
        _git(self.repo, "push", "-q", "origin", "agent/dev1-sqf")
        lot = self.lot("agent/dev1-sqf")
        self.sync()
        _git(self.repo, "merge", "-q", "--squash", "agent/dev1-sqf")
        _git(self.repo, "commit", "-q", "-m", "squash")
        _git(self.repo, "push", "-q", "origin", "develop")
        _git(self.repo, "branch", "-q", "-D", "agent/dev1-sqf")
        _git(self.repo, "branch", "-q", "-D", "-r", "origin/agent/dev1-sqf")
        with mock.patch.object(plan_git, "git_fetch", return_value="fetch refused"):
            report = self.sync()
        self.assertEqual(report["results"][0]["how"], "vu-squash")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "delivered")

    def test_commit_de_fusion_qui_cite_la_branche(self):
        self.branch("agent/dev1-m", "m1")
        self.branch("agent/dev1-m-2", "m2")
        lot = self.lot("agent/dev1-m")
        lot2 = self.lot("agent/dev1-m-2")
        self.commit("pendant")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "merge(m): agent/dev1-m", "agent/dev1-m")
        _git(self.repo, "branch", "-q", "-D", "agent/dev1-m")
        report = {r["work_item"]: r for r in self.sync()["results"]}
        self.assertEqual(report[lot["id"]]["how"], "message")
        self.assertEqual(report[lot2["id"]]["result"], "open")   # nom entier seulement
        self.assertEqual(work.get(self.db, lot2["id"])["state"], "intake")

    def test_autre_hote_et_dossier_absent(self):
        self.agent("ailleurs", host="autre-hote", cwd=self.repo)
        self.agent("sansdossier", cwd=os.path.join(self.tmp, "absent"))
        self.branch("agent/x", "x1")
        work.add(self.db, title="l", assignee="ailleurs", branch="agent/x")
        work.add(self.db, title="l", assignee="sansdossier", branch="agent/x")
        self.assertEqual([r["result"] for r in self.sync()["results"]], ["no-repo"])

    def test_cli_branche_et_releve(self):
        self.branch("agent/dev1-cli", "c1")
        proc = self.mesh("work", "add", "--title", "par la CLI", "--assignee", "dev1",
                         "--branch", "agent/dev1-cli", "--target", "develop")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lot = work.list_items(self.db)[0]
        self.assertEqual((lot["branch"], lot["branch_target"]), ("agent/dev1-cli", "develop"))
        proc = self.mesh("work", "show", str(lot["id"]))
        self.assertIn("branch   : agent/dev1-cli → develop", proc.stdout)
        other = work.add(self.db, title="autre", assignee="dev1")
        proc = self.mesh("work", "assign", str(other["id"]), "dev1", "--branch", "agent/y")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.get(self.db, other["id"])["branch"], "agent/y")
        # #2543 : une pointe jamais vue en avance ne prouve rien (même forme
        # qu'une branche née sur un commit déjà fusionné) ; le relevé la voit
        # d'abord en avance, puis sa fusion la ferme.
        self.sync()
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "m", "agent/dev1-cli")
        proc = self.mesh("work", "sync-branches")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("CLOSED", proc.stdout)
        self.assertEqual(work.get(self.db, lot["id"])["state"], "delivered")

    def test_noms_refuses(self):
        for bad in ("a..b", "-x", "a b", "x.lock"):
            with self.assertRaises(work.WorkError):
                work.add(self.db, title="t", branch=bad)
        with self.assertRaises(work.WorkError):
            work.add(self.db, title="t", branch_target="develop")
        self.assertEqual(plan_git.cited_branch("agent/a puis agent/b"), None)
        self.assertEqual(plan_git.cited_branch("voir agent/a-b."), "agent/a-b")


class CloneDiscoveryTest(unittest.TestCase):
    def test_an_unreadable_clone_does_not_hide_other_local_clones(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "unreadable")
            good = os.path.join(tmp, "readable")
            os.makedirs(bad)
            os.makedirs(good)
            agents = [{"cwd": bad, "host": "h"}, {"cwd": good, "host": "h"}]

            def origin(repo, *args):
                if repo == bad:
                    raise plan_git.GitError("git remote: Permission denied")
                return "git@github.com:manaty/ameesh.git"

            with mock.patch.object(registry, "overview", return_value=agents), \
                    mock.patch.object(plan_git, "_out", side_effect=origin):
                self.assertEqual(plan_git._local_clones(None, "h"),
                                 [(good, "manaty/ameesh", agents[1])])


class CloneRefreshTest(_Base):
    """#2302 : le relevé rafraîchit le clone de l'assigné et lit le dépôt du
    projet, pas n'importe quel dépôt posé dans son dossier de travail."""

    def setUp(self):
        super().setUp()
        self.origin = os.path.join(self.tmp, "manaty", "ameesh.git")
        os.makedirs(os.path.dirname(self.origin))
        _git(self.tmp, "init", "-q", "--bare", "--initial-branch=main", self.origin)
        self.repo = os.path.join(self.tmp, "clone")
        _git(self.tmp, "clone", "-q", self.origin, self.repo)
        self.write(self.repo, "README.md", "base\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "base")
        _git(self.repo, "push", "-q", "-u", "origin", "main")

    def write(self, repo, name, text):
        path = os.path.join(repo, name)
        os.makedirs(os.path.dirname(path) or repo, exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)

    def lot_in_another_clone(self, branch):
        """La branche du lot naît dans un AUTRE clone, avec son commit, puis
        est poussée : le clone de l'assigné ne la voit pas sans fetch."""
        other = os.path.join(self.tmp, "other-" + branch.replace("/", "-"))
        _git(self.tmp, "clone", "-q", self.origin, other)
        _git(other, "checkout", "-q", "-b", branch, "origin/main")
        self.write(other, "lot.txt", branch + "\n")
        _git(other, "add", "-A")
        _git(other, "commit", "-qm", branch)
        _git(other, "push", "-q", "origin", branch)
        return other

    def merge_in_another_clone(self, other, branch):
        """La branche déjà vue en avance (#2543) est fusionnée dans `main`
        distante, que le clone de l'assigné ne voit pas non plus sans fetch."""
        _git(other, "checkout", "-q", "main")
        _git(other, "merge", "-q", "--no-ff", "-m", "merge %s" % branch, branch)
        _git(other, "push", "-q", "origin", "main")

    def test_a_stale_clone_is_fetched_before_the_probe(self):
        branch = "lot/2302-stale"
        stale = _git(self.repo, "rev-parse", "origin/main")
        merged = self.lot_in_another_clone(branch)
        self.agent("dev1", cwd=self.repo)
        lot = work.add(self.db, title="clone en retard", assignee="dev1",
                       branch=branch, branch_target="main")
        # #2543 : la pointe est d'abord vue en avance (le premier relevé
        # rafraîchit le clone), puis sa fusion est constatée.
        first = plan_git.sync_branches(self.db, host=self.cfg.host)["results"][0]
        self.assertEqual(first["result"], "open", first)
        self.assertIsNotNone(work.get(self.db, lot["id"])["branch_head"])
        self.merge_in_another_clone(merged, branch)
        self.assertNotEqual(stale, _git(merged, "rev-parse", "main"))
        report = plan_git.sync_branches(self.db, host=self.cfg.host)
        entry = report["results"][0]
        self.assertEqual(entry["result"], "merged", entry)
        self.assertEqual(entry["target"], "origin/main")     # ref publiée lue
        self.assertEqual(entry["how"], "ancestor")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "delivered")

    def test_the_project_repo_wins_over_the_assignee_wrong_repo(self):
        wrong_origin = os.path.join(self.tmp, "manaty", "nexlink.git")
        _git(self.tmp, "init", "-q", "--bare", "--initial-branch=main", wrong_origin)
        wrong = os.path.join(self.tmp, "nexlink-clone")
        _git(self.tmp, "clone", "-q", wrong_origin, wrong)
        self.write(wrong, "README.md", "nexlink\n")
        _git(wrong, "add", "-A")
        _git(wrong, "commit", "-qm", "base")
        _git(wrong, "push", "-q", "-u", "origin", "main")
        branch = "lot/2302-wrong-repo"
        other = self.lot_in_another_clone(branch)
        self.agent("dev1", cwd=wrong)
        self.agent("dev2", cwd=self.repo)
        lot = work.add(self.db, title="dépôt du projet", assignee="dev1",
                       branch=branch, branch_target="main",
                       issue_ref="manaty/ameesh#1692")
        # #2543 : même lecture en deux temps que ci-dessus.
        first = plan_git.sync_branches(self.db, host=self.cfg.host)["results"][0]
        self.assertEqual((first["result"], first["repo"]), ("open", self.repo), first)
        self.merge_in_another_clone(other, branch)
        report = plan_git.sync_branches(self.db, host=self.cfg.host)
        entry = report["results"][0]
        self.assertEqual(entry["result"], "merged", entry)
        self.assertEqual(entry["repo"], self.repo)
        self.assertIn("working directory", entry["repo_note"])
        self.assertEqual(work.get(self.db, lot["id"])["state"], "delivered")

    def test_a_wrong_repo_without_a_local_clone_is_reported(self):
        wrong_origin = os.path.join(self.tmp, "manaty", "nexlink.git")
        _git(self.tmp, "init", "-q", "--bare", "--initial-branch=main", wrong_origin)
        wrong = os.path.join(self.tmp, "nexlink-clone")
        _git(self.tmp, "clone", "-q", wrong_origin, wrong)
        self.write(wrong, "README.md", "nexlink\n")
        _git(wrong, "add", "-A")
        _git(wrong, "commit", "-qm", "base")
        _git(wrong, "push", "-q", "-u", "origin", "main")
        self.agent("dev1", cwd=wrong)
        lot = work.add(self.db, title="mauvais dépôt", assignee="dev1",
                       branch="lot/2302-absente", branch_target="main",
                       issue_ref="manaty/ameesh#1692")
        report = plan_git.sync_branches(self.db, host=self.cfg.host)
        entry = report["results"][0]
        self.assertEqual(entry["result"], "wrong-repo", entry)
        self.assertIn("manaty/ameesh", entry["detail"])
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")


class IdleCapacityToOrchestratorTest(_Base):
    """`idle_capacity` part aussi à l'orchestrateur, par courrier `event`."""

    def setUp(self):
        super().setUp()
        self.now = time.time()
        for name in ("orch", "dev1", "dev2"):
            self.agent(name)
            self.db.execute(
                "UPDATE agent_registry SET status = 'idle', lease_owner = %s,"
                " lease_expires_at = now() + interval '1 hour', team = 'equipe',"
                " responsible = 'human:proprio',"
                " status_since = now() - interval '2 hours',"
                " last_turn_at = now() - interval '2 hours' WHERE name = %s",
                ("runner-%s@h" % name, name))

    def test_briefs_et_envoi(self):
        lot = work.add(self.db, title="à prendre")
        alert = {"type": "idle_capacity", "agents": ["dev1", "dev2", "orch"],
                 "lots": [lot["id"]], "orchestrators": ["orch"], "threshold": 1800.0}
        listing = storage.of(self.db).operations.listing()
        briefs = sous_utilisation.orchestrator_briefs(alert, listing, {lot["id"]: "à prendre"})
        self.assertEqual(list(briefs), ["orch"])
        self.assertIn("dev1, dev2", briefs["orch"])
        self.assertNotIn("orch,", briefs["orch"])
        self.assertIn("#%d \"à prendre\"" % lot["id"], briefs["orch"])

        args = exploitation.build_parser().parse_args(["alerts"])
        seuils = exploitation.thresholds(args)
        seuils["orchestrators_declared"] = ("orch",)
        ncfg = notify.parse_config({"default": ["desktop"]})

        class Faux:
            def send(self, channel, message):
                pass

        notifier = notify.Notifier(self.cfg, ncfg, sender=Faux(), log=lambda t: None,
                                   clock=lambda: self.now)
        notifier.run_pass(self.db, thresholds=seuils)
        rows = self.unread("orch")
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["kind"], "event")
        self.assertIn("without an actionable lot", rows[0]["body"])
        notifier.run_pass(self.db, thresholds=seuils)       # même alerte : pas de doublon
        self.assertEqual(len(self.unread("orch")), 1)


if __name__ == "__main__":
    unittest.main()
